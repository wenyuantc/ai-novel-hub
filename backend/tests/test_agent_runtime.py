import json

import pytest
from sqlalchemy import func, select

from app.ai.providers import AIProviderError
from app.models import AgentMessage, GenerationLog
from tests.helpers import create_ai_config, create_project, register, wait_task


def call(name, args, ident="c1"):
    return "tool_call", {"id": ident, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


async def setup_agent(client, *, auto=False):
    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    conv = (await client.post(base + "/conversations", json={})).json()
    url = base + f"/conversations/{conv['id']}"
    if auto:
        assert (await client.patch(url, json={"auto_execute": True})).status_code == 200
    return project, base, url


async def send(client, url, text="请帮我创作"):
    response = await client.post(url + "/messages", json={"content": text})
    assert response.status_code == 202, response.text
    return response.json()["task"]["id"]


async def test_read_then_answer_with_usage(client, fake_llm, app):
    _, _, url = await setup_agent(client)
    fake_llm.push_tools([("text", {"text": "先了解项目"}), call("get_project_overview", {})])
    fake_llm.push_tools([("text", {"text": "当前是科幻小说。"}), ("usage", {"input_tokens": 100, "output_tokens": 20})])
    task_id = await send(client, url)
    task = await wait_task(client, task_id)
    assert task["status"] == "succeeded", task
    detail = (await client.get(url)).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "tool", "assistant"]
    result = json.loads(detail["messages"][2]["content"])
    assert result["untrusted"] is True
    assert fake_llm.tool_calls[1]["messages"][-1]["tool_call_id"] == "c1"
    async with app.state.sessionmaker() as db:
        logs = list(
            await db.scalars(select(GenerationLog).where(GenerationLog.task_id == task_id).order_by(GenerationLog.id))
        )
        assert len(logs) == 2 and logs[-1].prompt_tokens == 100 and logs[-1].completion_tokens == 20


async def test_confirm_resume_reject_and_duplicate_confirmation(client, fake_llm):
    project, base, url = await setup_agent(client)
    fake_llm.push_tools([call("create_character", {"name": "林远", "personality": "冷静"})])
    fake_llm.push_tools([call("create_character", {"name": "林小远"}, "c2")])
    fake_llm.push_tools([("text", {"text": "已按确认结果处理。"})])
    task_id = await send(client, url)
    assert (await wait_task(client, task_id, statuses=("paused", "failed")))["status"] == "paused"
    change = (await client.get(url)).json()["changes"][0]
    response = await client.post(base + f"/changes/{change['id']}/confirm")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "executed", response.text
    assert (await client.post(base + f"/changes/{change['id']}/confirm")).status_code == 200
    await wait_task(client, task_id, statuses=("paused",))
    result = json.loads(fake_llm.tool_calls[1]["messages"][-1]["content"])["data"]
    assert result["id"] > 0
    response = await client.post(base + f"/turns/{task_id}/reject-all")
    assert response.status_code == 200, response.text
    assert (await wait_task(client, task_id))["status"] == "succeeded"
    assert "用户拒绝" in fake_llm.tool_calls[-1]["messages"][-1]["content"]
    characters = (await client.get(f"/api/v1/projects/{project['id']}/characters")).json()
    assert len(characters) == 1


@pytest.mark.parametrize("count,paused", [(1, False), (3, False), (4, True)])
async def test_auto_execution_threshold(client, fake_llm, count, paused):
    _, _, url = await setup_agent(client, auto=True)
    fake_llm.push_tools([call("create_character", {"name": f"人物{i}"}, f"c{i}") for i in range(count)])
    fake_llm.push_tools([("text", {"text": "完成"})])
    task = await wait_task(client, await send(client, url), statuses=("succeeded", "paused", "failed"), within=15)
    assert task["status"] == ("paused" if paused else "succeeded"), task
    changes = (await client.get(url)).json()["changes"]
    assert len(changes) == count
    assert all(c["status"] == ("proposed" if paused else "executed") for c in changes)


async def test_auto_second_batch_requires_confirmation(client, fake_llm):
    _, _, url = await setup_agent(client, auto=True)
    fake_llm.push_tools([call("create_character", {"name": "第一个"}, "c1")])
    fake_llm.push_tools([call("create_character", {"name": "第二个"}, "c2")])
    task = await wait_task(client, await send(client, url), statuses=("paused", "failed"))
    assert task["status"] == "paused", task
    assert [c["status"] for c in (await client.get(url)).json()["changes"]] == ["executed", "proposed"]


async def test_auto_only_first_batch_even_when_first_batch_needed_confirmation(client, fake_llm):
    _, base, url = await setup_agent(client, auto=True)
    fake_llm.push_tools([call("create_character", {"name": f"首批{i}"}, f"c{i}") for i in range(4)])
    fake_llm.push_tools([call("create_character", {"name": "后续小修改"}, "later")])
    task_id = await send(client, url)
    assert (await wait_task(client, task_id, statuses=("paused", "failed"), within=15))["status"] == "paused"
    assert (await client.post(base + f"/turns/{task_id}/confirm-all")).status_code == 200
    task = await wait_task(client, task_id, statuses=("paused", "succeeded", "failed"), within=15)
    assert task["status"] == "paused", task
    changes = (await client.get(url)).json()["changes"]
    assert [c["status"] for c in changes] == ["executed"] * 4 + ["proposed"]


@pytest.mark.parametrize("with_small", [False, True])
async def test_auto_batch_with_delete_waits_for_confirmation(client, fake_llm, with_small):
    project, _, url = await setup_agent(client, auto=True)
    char_url = f"/api/v1/projects/{project['id']}/characters"
    victim = (await client.post(char_url, json={"name": "待删除"})).json()
    batch = [call("create_character", {"name": "新角色"}, "small")] if with_small else []
    fake_llm.push_tools([*batch, call("delete_character", {"id": victim["id"]}, "delete")])
    task_id = await send(client, url, "请审核待删除角色")
    task = await wait_task(client, task_id, statuses=("paused", "succeeded", "failed"))
    assert task["status"] == "paused", task
    assert all(c["status"] == "proposed" for c in (await client.get(url)).json()["changes"])
    assert [c["name"] for c in (await client.get(char_url)).json()] == ["待删除"]


@pytest.mark.parametrize("bulk_tool", ["update_chapter_plans", "update_career"])
async def test_auto_bulk_sync_always_requires_confirmation(client, fake_llm, bulk_tool):
    project, _, url = await setup_agent(client, auto=True)
    project_url = f"/api/v1/projects/{project['id']}"
    if bulk_tool == "update_chapter_plans":
        outline = (await client.post(project_url + "/outlines", json={"title": "批量细纲"})).json()
        args = {"outline_id": outline["id"], "plans": [{"title": "第一节"}, {"title": "第二节"}]}
    else:
        career = (
            await client.post(project_url + "/careers", json={"name": "修士", "stages": [{"name": "一阶"}]})
        ).json()
        args = {"id": career["id"], "stages": [{"name": "替换原阶段"}]}
    fake_llm.push_tools([call(bulk_tool, args)])
    task_id = await send(client, url, "请调整修士或细纲")
    task = await wait_task(client, task_id, statuses=("paused", "succeeded", "failed"))
    assert task["status"] == "paused", task
    assert (await client.get(url)).json()["changes"][0]["status"] == "proposed"


async def test_tool_limit_returns_every_result_before_none(client, fake_llm):
    _, _, url = await setup_agent(client)
    fake_llm.push_tools([call("get_project_overview", {}, f"c{i}") for i in range(10)])
    fake_llm.push_tools([("text", {"text": "已达到本轮上限。"})])
    task = await wait_task(client, await send(client, url))
    assert task["status"] == "succeeded", task
    assert fake_llm.tool_calls[-1]["tool_choice"] == "none"
    results = [m for m in fake_llm.tool_calls[-1]["messages"] if m["role"] == "tool"]
    assert len(results) == 10
    assert sum("error" in json.loads(r["content"])["data"] for r in results) == 2


async def test_failed_resume_retry_does_not_repeat_write(client, fake_llm, app):
    _, base, url = await setup_agent(client)
    fake_llm.push_tools([call("create_character", {"name": "林远"})])
    fake_llm.push_tools(AIProviderError("连接中断"))
    task_id = await send(client, url)
    await wait_task(client, task_id, statuses=("paused",))
    assert (await client.post(base + f"/turns/{task_id}/confirm-all")).status_code == 200
    assert (await wait_task(client, task_id))["status"] == "failed"
    fake_llm.push_tools([("text", {"text": "已创建"})])
    response = await client.post(f"/api/v1/tasks/{task_id}/retry")
    assert response.status_code == 202, response.text
    retried = response.json()["id"]
    assert (await wait_task(client, retried))["status"] == "succeeded"
    detail = (await client.get(url)).json()
    assert len(detail["changes"]) == 1 and detail["changes"][0]["status"] == "executed"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(AgentMessage.id)).where(AgentMessage.role == "user")) == 1


async def test_fifty_proposal_guard_is_independent_of_tool_budget(client, fake_llm, app):
    from app.models import AgentChange, AgentConversation
    from tests.helpers import Gate

    project, _, url = await setup_agent(client)
    gate = Gate()
    fake_llm.push_tools([gate, call("create_character", {"name": "不能创建"}, "overflow")])
    task_id = await send(client, url)
    await gate.wait_reached()
    async with app.state.sessionmaker() as db:
        conv = await db.scalar(select(AgentConversation))
        for i in range(50):
            db.add(
                AgentChange(
                    conversation_id=conv.id,
                    project_id=project["id"],
                    turn_task_id=task_id,
                    tool_call_id=f"previous{i}",
                    tool_name="create_character",
                    kind="small",
                    arguments={"name": "previous"},
                    preview={},
                    status="rejected",
                    seq=i + 1,
                )
            )
        await db.commit()
    gate.release()
    task = await wait_task(client, task_id)
    assert task["status"] == "succeeded", task
    assert "单轮最多 50 项" in fake_llm.tool_calls[-1]["messages"][-1]["content"]
    assert len((await client.get(url)).json()["changes"]) == 50


async def test_cancel_and_retry_preserves_committed_tool_results(client, fake_llm, app):
    from tests.helpers import Gate

    _, _, url = await setup_agent(client, auto=True)
    gate = Gate()
    fake_llm.push_tools([call("create_character", {"name": "不会重复"})])
    fake_llm.push_tools([gate, ("text", {"text": "结束"})])
    task_id = await send(client, url)
    await gate.wait_reached()
    assert (await client.post(f"/api/v1/tasks/{task_id}/cancel")).status_code == 200
    assert (await wait_task(client, task_id))["status"] == "cancelled"
    fake_llm.push_tools([("text", {"text": "继续完成"})])
    retry = (await client.post(f"/api/v1/tasks/{task_id}/retry")).json()
    assert (await wait_task(client, retry["id"]))["status"] == "succeeded"
    assert len((await client.get(url)).json()["changes"]) == 1
    assert (await client.post(f"/api/v1/tasks/{task_id}/retry")).status_code == 409


async def test_new_turn_after_cancel_completes_abandoned_tool_protocol(client, fake_llm):
    _, _, url = await setup_agent(client)
    fake_llm.push_tools([call("create_character", {"name": "未执行角色"})])
    original = await send(client, url)
    await wait_task(client, original, statuses=("paused",))
    assert (await client.post(f"/api/v1/tasks/{original}/cancel")).status_code == 200
    fake_llm.push_tools([("text", {"text": "新的回复"})])
    newer = await send(client, url, "开始新问题")
    assert (await wait_task(client, newer))["status"] == "succeeded"
    history = fake_llm.tool_calls[-1]["messages"]
    previous = next(i for i, m in enumerate(history) if m.get("tool_calls"))
    assert history[previous + 1]["role"] == "tool"
    assert history[previous + 1]["tool_call_id"] == "c1"
    assert "未完成" in history[previous + 1]["content"]


async def test_retry_cancelled_before_start_keeps_original_proposal(client, fake_llm, app):
    _, base, url = await setup_agent(client)
    fake_llm.push_tools([call("create_character", {"name": "唯一提案"})])
    original = await send(client, url)
    await wait_task(client, original, statuses=("paused",))
    change = (await client.get(url)).json()["changes"][0]
    await app.state.task_engine.stop()
    assert (await client.post(f"/api/v1/tasks/{original}/cancel")).status_code == 200
    first = (await client.post(f"/api/v1/tasks/{original}/retry")).json()
    assert first["status"] == "queued"
    assert (await client.post(f"/api/v1/tasks/{first['id']}/cancel")).status_code == 200
    second = (await client.post(f"/api/v1/tasks/{first['id']}/retry")).json()
    await app.state.task_engine.start()
    await wait_task(client, second["id"], statuses=("paused",))
    changes = (await client.get(url)).json()["changes"]
    assert len(changes) == 1
    assert changes[0]["id"] == change["id"]
    assert changes[0]["turn_task_id"] == second["id"]
    assert (await client.post(base + f"/turns/{second['id']}/confirm-all")).status_code == 200
    assert (await wait_task(client, second["id"]))["status"] == "succeeded"


async def test_agent_logs_use_shared_retention_and_survive_config_deletion(client, fake_llm, app, monkeypatch):
    from app.services import generation_log_service
    from tests.helpers import Gate

    project, _, url = await setup_agent(client)
    monkeypatch.setattr(generation_log_service, "MAX_LOGS_PER_PROJECT", 2)
    for index in range(2):
        assert (await wait_task(client, await send(client, url, f"问题{index}")))["status"] == "succeeded"
    gate = Gate()
    fake_llm.push_tools([gate, ("text", {"text": "请求期间配置被删除也能完成"})])
    task = await send(client, url)
    await gate.wait_reached()
    configs = (await client.get("/api/v1/ai-configs")).json()
    assert (await client.delete(f"/api/v1/ai-configs/{configs[0]['id']}")).status_code == 204
    gate.release()
    assert (await wait_task(client, task))["status"] == "succeeded"
    async with app.state.sessionmaker() as db:
        logs = list(await db.scalars(select(GenerationLog).where(GenerationLog.project_id == project["id"])))
        assert len(logs) == 2
        assert all(log.ai_config_id is None for log in logs)


async def test_interrupted_by_restart_then_retry_continues_same_turn(client, fake_llm, app):
    from tests.helpers import Gate

    _, _, url = await setup_agent(client)
    gate = Gate()
    fake_llm.push_tools([call("get_project_overview", {})])
    fake_llm.push_tools([gate, ("text", {"text": "不会完成"})])
    task_id = await send(client, url)
    await gate.wait_reached()
    await app.state.task_engine.stop()
    await app.state.task_engine.start()
    task = await wait_task(client, task_id, statuses=("interrupted",))
    assert task["status"] == "interrupted", task
    fake_llm.push_tools([("text", {"text": "重启后完成"})])
    response = await client.post(f"/api/v1/tasks/{task_id}/retry")
    assert response.status_code == 202, response.text
    assert (await wait_task(client, response.json()["id"]))["status"] == "succeeded"
    messages = (await client.get(url)).json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[-1]["content"] == "重启后完成"
    assert {m["turn_task_id"] for m in messages} == {response.json()["id"]}


async def test_model_without_tool_support_fails_with_clear_hint(client, fake_llm):
    from app.ai.providers.base import TOOLS_UNSUPPORTED

    _, _, url = await setup_agent(client)
    fake_llm.push_tools(AIProviderError(TOOLS_UNSUPPORTED))
    task = await wait_task(client, await send(client, url))
    assert task["status"] == "failed" and task["error"] == TOOLS_UNSUPPORTED, task
