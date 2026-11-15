"""Agent 人工操作绑定用户已展示的提案身份、内容及轮次。"""

import asyncio
import json
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import AgentChange, AgentMessage, Task
from app.services import personal_projection
from app.services.agent import runner, service, transactions
from tests.helpers import Gate, create_project, parse_sse, register, wait_task, wait_until
from tests.test_agent_runtime import call, setup_agent


async def _setup(app, client, monkeypatch):
    owner = await register(client, "action-identity-owner")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    conversation = (await client.post(f"{base}/conversations", json={})).json()
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner["id"], project_id=project["id"], kind="agent.turn", status="paused",
                    input={"request": {"conversation_id": conversation["id"]}})
        db.add(task)
        await db.flush()
        items = [AgentChange(conversation_id=conversation["id"], project_id=project["id"], turn_task_id=task.id,
                             turn_task_key=task.statistics_key,
                             tool_call_id=f"call-{n}", tool_name="test", kind="small", status="proposed",
                             arguments={"n": n}, preview={"title": f"提案 {n}"}, seq=n) for n in (1, 2)]
        db.add_all(items)
        await db.commit()
        task_id, task_key = task.id, task.statistics_key

    async def skip_resume(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "maybe_resume", skip_resume)
    return base, conversation["id"], task_id, task_key


async def test_single_action_requires_original_key_and_visible_version(app, client, monkeypatch):
    base, conversation_id, _, _ = await _setup(app, client, monkeypatch)
    detail = await client.get(f"{base}/conversations/{conversation_id}")
    assert detail.status_code == 200, detail.text
    change = detail.json()["changes"][0]
    assert change["change_key"] and len(change["view_version"]) == 64
    path = f"{base}/changes/{change['id']}/reject"
    assert (await client.post(path)).status_code == 422
    body = {"change_key": change["change_key"], "view_version": change["view_version"]}
    assert (await client.post(path, json={**body, "change_key": str(uuid4())})).status_code == 409
    assert (await client.post(path, json={**body, "view_version": "f" * 64})).status_code == 409
    response = await client.post(path, json=body)
    assert response.status_code == 200 and response.json()["status"] == "rejected", response.text
    assert response.json()["view_version"] == change["view_version"]
    after_rejection = await client.post(f"{base}/changes/{change['id']}/confirm", json=body)
    assert after_rejection.status_code == 200 and after_rejection.json()["status"] == "rejected"


async def test_task_proposal_origin_result_is_hidden_in_http_and_event_projection(app, client, monkeypatch):
    base, conversation_id, task_id, _ = await _setup(app, client, monkeypatch)
    proof = {"task_key": "private-original-task", "mac": "private-original-mac"}
    async with app.state.sessionmaker() as db:
        change = await db.scalar(
            select(AgentChange).where(AgentChange.turn_task_id == task_id).order_by(AgentChange.id)
        )
        change.kind, change.tool_name, change.result = "task", "start_task", {"_agent_task_origin": proof}
        change_id = change.id
        await db.commit()
    detail = await client.get(f"{base}/conversations/{conversation_id}")
    assert detail.status_code == 200, detail.text
    public = next(row for row in detail.json()["changes"] if row["id"] == change_id)
    assert public["result"] is None and "private-original" not in detail.text
    event = transactions.public_change_payload({"kind": "task", "status": "proposed", "result": {
        "_agent_task_origin": proof,
    }})
    assert event["result"] is None
    async with app.state.sessionmaker() as db:
        assert (await db.get(AgentChange, change_id)).result == {"_agent_task_origin": proof}


async def test_changed_server_preview_requires_a_new_confirmation(app, client, monkeypatch):
    base, conversation_id, _, _ = await _setup(app, client, monkeypatch)
    change = (await client.get(f"{base}/conversations/{conversation_id}")).json()["changes"][0]
    body = {"change_key": change["change_key"], "view_version": change["view_version"]}
    async with app.state.sessionmaker() as db:
        stored = await db.get(AgentChange, change["id"])
        stored.preview = {"title": "内容发生变化"}
        await db.commit()
    response = await client.post(f"{base}/changes/{change['id']}/reject", json=body)
    assert response.status_code == 409, response.text
    async with app.state.sessionmaker() as db:
        assert (await db.get(AgentChange, change["id"])).status == "proposed"


async def test_turn_reject_requires_exact_displayed_set_and_task_uuid(app, client, monkeypatch):
    base, conversation_id, task_id, task_key = await _setup(app, client, monkeypatch)
    changes = (await client.get(f"{base}/conversations/{conversation_id}")).json()["changes"]
    selected = [{"id": item["id"], "change_key": item["change_key"], "view_version": item["view_version"]}
                for item in changes]
    path = f"{base}/turns/{task_id}/reject-all"
    body = {"task_key": task_key, "changes": selected}
    assert (await client.post(path)).status_code == 422
    assert (await client.post(path, json={**body, "task_key": str(uuid4())})).status_code == 409
    assert (await client.post(path, json={**body, "changes": selected[:1]})).status_code == 409
    response = await client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert [item["status"] for item in response.json()["changes"]] == ["rejected", "rejected"]


async def test_turn_undo_requires_the_exact_displayed_executed_set(app, client, monkeypatch):
    base, conversation_id, task_id, task_key = await _setup(app, client, monkeypatch)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        task.status = "succeeded"
        for row in await db.scalars(select(AgentChange).where(AgentChange.turn_task_id == task_id)):
            row.status, row.effects = "executed", []
        await db.commit()
    changes = (await client.get(f"{base}/conversations/{conversation_id}")).json()["changes"]
    selected = [{"id": item["id"], "change_key": item["change_key"], "view_version": item["view_version"]}
                for item in changes]
    path = f"{base}/turns/{task_id}/undo"
    body = {"task_key": task_key, "changes": selected}
    assert (await client.post(path, json={**body, "changes": selected[:1]})).status_code == 409
    response = await client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert [item["status"] for item in response.json()["changes"]] == ["undone", "undone"]


async def test_single_undo_is_idempotent_for_the_same_original_view(app, client, monkeypatch):
    base, conversation_id, task_id, _ = await _setup(app, client, monkeypatch)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        task.status = "succeeded"
        change = await db.scalar(select(AgentChange).order_by(AgentChange.id))
        change.status, change.effects = "executed", []
        await db.commit()
    change = (await client.get(f"{base}/conversations/{conversation_id}")).json()["changes"][0]
    body = {"change_key": change["change_key"], "view_version": change["view_version"]}
    path = f"{base}/changes/{change['id']}/undo"
    first = await client.post(path, json=body)
    second = await client.post(path, json=body)
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()["status"] == second.json()["status"] == "undone"


async def test_hidden_private_arguments_cannot_be_approved_by_hashing_them(app, client, monkeypatch):
    base, conversation_id, _, _ = await _setup(app, client, monkeypatch)
    async with app.state.sessionmaker() as db:
        change = await db.scalar(select(AgentChange).order_by(AgentChange.id))
        change.arguments = {"_shared_embedding": {"config_id": 73, "fingerprint": "私人指纹"}}
        await db.commit()
        change_id = change.id
    detail = await client.get(f"{base}/conversations/{conversation_id}")
    assert detail.status_code == 200, detail.text
    change = detail.json()["changes"][0]
    assert change["arguments"] == {} and "私人指纹" not in detail.text
    body = {
        "change_key": change["change_key"], "view_version": change["view_version"],
    }
    response = await client.post(f"{base}/changes/{change_id}/confirm", json=body)
    assert response.status_code == 409
    async with app.state.sessionmaker() as db:
        assert (await db.get(AgentChange, change_id)).status == "proposed"
    rejected = await client.post(f"{base}/changes/{change_id}/reject", json=body)
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"


async def test_agent_history_does_not_rebind_old_task_ids_to_new_uuids(app, client, monkeypatch):
    base, conversation_id, task_id, old_task_key = await _setup(app, client, monkeypatch)
    async with app.state.sessionmaker() as db:
        db.add(AgentMessage(conversation_id=conversation_id, role="user", content="旧消息",
                            turn_task_id=task_id, turn_task_key=old_task_key))
        task = await db.get(Task, task_id)
        task.statistics_key = str(uuid4())
        await db.commit()
    response = await client.get(f"{base}/conversations/{conversation_id}")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert old_task_key not in response.text
    assert all(item["turn_task_id"] is None and item["turn_task_key"] is None for item in detail["changes"])
    assert detail["messages"][0]["turn_task_id"] is None and detail["messages"][0]["turn_task_key"] is None


@pytest.mark.parametrize("tampered", [False, True])
async def test_agent_sse_change_key_and_version_are_current_and_proven(app, client, fake_llm, monkeypatch, tampered):
    _, _, conversation_url = await setup_agent(client, auto=True)
    gate = Gate()
    fake_llm.push_tools([gate, call("create_character", {"name": "流式角色"})])
    fake_llm.push_tools([("text", {"text": "完成"})])
    original_out = runner._out

    def output(change):
        value = original_out(change)
        if tampered:
            value["change_key"] = str(uuid4())
        return value

    monkeypatch.setattr(runner, "_out", output)
    accepted = await client.post(f"{conversation_url}/messages", json={"content": "请创建角色"})
    assert accepted.status_code == 202, accepted.text
    task = accepted.json()["task"]
    await gate.wait_reached()
    pending = asyncio.create_task(client.get(f"/api/v1/tasks/{task['id']}/stream", params={
        "task_key": task["task_key"],
    }))
    await wait_until(lambda: (hub := app.state.task_engine.hubs.get(task["id"])) is not None
                     and hub.subscriber_count > 0)
    gate.release()
    response = await asyncio.wait_for(pending, 15)
    events = [payload["change"] for event, payload in parse_sse(response.text) if event == "executed"]
    if tampered:
        assert events == []
    else:
        assert len(events) == 1 and events[0]["change_key"] and len(events[0]["view_version"]) == 64
        detail = (await client.get(conversation_url)).json()
        assert events[0]["change_key"] == detail["changes"][0]["change_key"]
        assert events[0]["view_version"] == detail["changes"][0]["view_version"]


async def test_duplicate_concurrent_confirmation_applies_only_once(app, client, fake_llm):
    project, base, conversation_url = await setup_agent(client)
    fake_llm.push_tools([call("create_character", {"name": "幂等角色"})])
    fake_llm.push_tools([("text", {"text": "完成"})])
    accepted = await client.post(f"{conversation_url}/messages", json={"content": "创建角色"})
    assert accepted.status_code == 202, accepted.text
    task = accepted.json()["task"]
    assert (await wait_task(client, task, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(conversation_url)).json()["changes"][0]
    body = {"change_key": change["change_key"], "view_version": change["view_version"]}
    responses = await asyncio.gather(*(
        client.post(f"{base}/changes/{change['id']}/confirm", json=body) for _ in range(2)
    ))
    assert all(response.status_code == 200 for response in responses), [response.text for response in responses]
    assert all(response.json()["status"] == "executed" for response in responses)
    characters = (await client.get(f"/api/v1/projects/{project['id']}/characters")).json()
    assert [character["name"] for character in characters] == ["幂等角色"]


async def test_internal_analysis_wizard_and_batch_receipts_are_recursively_filtered(app, client):
    owner = await register(client, "internal-receipt-owner")
    project = await create_project(client)
    sensitive = {
        "_analysis_pending": {"claim": "private-analysis"}, "_analysis_completion": {"key": "private-receipt"},
        "_memory_ref_ids": [123, 456], "_memory_analysis_id": 789, "_analysis_index_receipt": "private-index",
        "_analysis_proposal_source": {"grant": "private-analysis-source"},
        "_agent_task_origin": {"key": "private-agent-source"},
        "_batch_attempt": {"key": "private-batch"}, "_batch_completion": {"key": "private-completion"},
        "_batch_outcome_unknown": {"key": "private-unknown-batch"},
        "_wizard_resume": {"source": "private-source"}, "continue_from_task_key": "private-task-key",
        "_wizard_pending": {"key": "private-wizard-pending"},
        "_wizard_completion": {"key": "private-wizard-completion"},
        "rich_analysis": {"run": "private-analysis-run", "hash": "private-hash"},
        "_external_execution": {"execution_key": "private-execution-key", "queued_at": "private-round"},
        "_external_snapshot_mac": "private-snapshot-mac",
        "_agent_actor_token_version": "private-token-version",
        "_task_actor_token_version": "private-task-version",
        "_wizard_actor_token_version": "private-wizard-version",
        "_retry_source_key": "private-retry-source",
        "api_key_enc": "private-encrypted-key", "apiKeyEnc": "private-encrypted-alias",
        "config_instance_key": "private-config-uuid", "_config_instance_key": "private-config-proof",
        "model_config_instance_key": "private-log-config-uuid",
        "_taskActorTokenVersion": "private-version-alias",
        "_admin_chapter_cancel": {"claim": "private-admin"}, "_admin_chapter_receipt": {"key": "private-admin-2"},
    }
    payload = {"events": [deepcopy(sensitive)], "text": json.dumps({"inner": sensitive}),
               "double": json.dumps(json.dumps({"inner": sensitive})),
               "partial": '{"_task\\u005factor-token-version":"private-partial", "next":',
               "partial_double": json.dumps(json.dumps({"_external_execution": "private-truncated"}))[:-2],
               "diff": [{"field": "tasks._external.snapshot.mac", "before": "private-path", "after": None},
                        {"field": "chapters.style", "before": "普通正文", "after": "新正文"}],
               "ordinary": "正文中提到 task_key"}
    nested = json.dumps({"_external_execution": "private-deep-truncation"})
    for _ in range(9):
        nested = json.dumps(nested)
    payload["deep_truncated"] = nested[:-2]
    original = deepcopy(payload)
    async with app.state.sessionmaker() as db:
        projected = await personal_projection.project_payload(db, owner["id"], project["id"], payload)
    assert projected["events"] == [{}] and json.loads(projected["text"]) == {"inner": {}}
    assert json.loads(json.loads(projected["double"])) == {"inner": {}}
    assert "private-partial" not in projected["partial"]
    assert "private-truncated" not in projected["partial_double"]
    assert "private-deep-truncation" not in projected["deep_truncated"]
    assert projected["diff"] == [{"field": "chapters.style", "before": "普通正文", "after": "新正文"}]
    assert projected["ordinary"] == payload["ordinary"]
    assert payload == original
