"""实体草稿的真实任务模型绑定、执行身份与跨作品关系边界。"""

import asyncio
import json
from contextlib import aclosing
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.ai.providers import AIProviderError, create_provider
from app.api.v1 import tasks as task_routes
from app.models import AIConfig, KbDocument, Relationship, RelationshipType, RetrievalChunk, Task, User
from app.services import ai_config_service, collaboration_ai_guard, project_ai_config, retrieval_service
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine import admission as task_admission
from app.services.task_engine import stream_lease
from app.services.task_engine.registry import get_handler
from tests.helpers import (
    Gate,
    create_ai_config,
    create_project,
    fake_vector,
    parse_sse,
    register,
    run_task,
    task_params,
    wait_task,
    wait_until,
)
from tests.stream_helpers import mount_stream_response
from tests.test_agent_providers import openai_chunk
from tests.test_ai_visibility_dispatch import _server
from tests.test_setting_ai_context import _settings


def _install_guarded_entity_stream(app, user_id: int, task: dict) -> None:
    """仅测试路由：生产 /tasks/.../stream 尚未切换受控 ASGI 响应。"""
    actor = AuthenticatedActor(user_id, 0)
    engine = app.state.task_engine

    async def endpoint():
        async with app.state.sessionmaker() as db:
            db.info["authenticated_actor"] = actor
            current = await db.get(Task, task["id"])
            binding = await stream_lease._capture_stream_binding(db, current, actor)
        events = engine.subscribe(
            task["id"], statistics_key=task["task_key"], actor_id=user_id, token_version=0,
            active_grant_required=True, initial_project_id=binding.project_id,
            initial_queued_at=binding.queued_at, initial_grant=binding.task_grant, guarded=True,
        )
        return stream_lease.task_sse_response(events, sessionmaker=app.state.sessionmaker, binding=binding)

    mount_stream_response(app, "/_test/entity-guarded-stream", endpoint)


@pytest.mark.parametrize("mutation", ["switch_default", "edit_selected"])
async def test_entity_task_preserves_accepted_model_binding(app, client, fake_llm, mutation):
    await register(client, f"entity-model-{mutation}")
    first = await create_ai_config(client, model="original-model")
    project = await create_project(client)
    await app.state.task_engine.stop()
    response = await client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={})
    assert response.status_code == 202, response.text
    if mutation == "switch_default":
        await create_ai_config(client, name="新默认", model="different-model", is_default=True)
    else:
        changed = await client.patch(f"/api/v1/ai-configs/{first['id']}", json={"model": "modified-model"})
        assert changed.status_code == 200, changed.text
    fake_llm.push('{"character":{"name":"新角色"}}')
    await app.state.task_engine.start()
    task = await wait_task(client, response.json())
    if mutation == "switch_default":
        assert task["status"] == "succeeded", task
        assert [call["model"] for call in fake_llm.calls] == ["original-model"]
    else:
        assert task["status"] == "failed" and fake_llm.calls == []


async def test_entity_rejects_reused_config_id_with_same_fields_and_new_uuid(app, client, fake_llm):
    await register(client, "entity-config-reuse")
    config = await create_ai_config(client, model="original-model")
    project = await create_project(client)
    await app.state.task_engine.stop()
    response = await client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={})
    assert response.status_code == 202, response.text
    async with app.state.sessionmaker() as db:
        original = await db.get(AIConfig, config["id"])
        old_key = original.instance_key
        values = {column.name: deepcopy(getattr(original, column.name)) for column in AIConfig.__table__.columns
                  if column.name not in ("id", "instance_key")}
        await db.delete(original)
        await db.flush()
        replacement = AIConfig(id=config["id"], **values)
        db.add(replacement)
        await db.commit()
        assert replacement.instance_key != old_key
    fake_llm.push('{"character":{"name":"不得生成"}}')
    await app.state.task_engine.start()
    task = await wait_task(client, response.json(), within=15)
    assert task["status"] == "failed" and not fake_llm.calls


@pytest.mark.parametrize("kind", ["character", "organization", "career", "relations_generate", "relations_parse"])
async def test_entity_model_dns_preflight_does_not_hold_authentication_write_lock(app, client, monkeypatch, kind):
    user = await register(client, f"entity-dns-{kind}")
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chars = []
    for name in ("甲", "乙"):
        created = await client.post(f"{base}/characters", json={"name": name, "relations_text": "与乙是朋友"})
        assert created.status_code == 201, created.text
        chars.append(created.json()["id"])
    await app.state.task_engine.stop()
    gate = Gate()

    async def blocked_dns(_url):
        gate.reached.set()
        await gate.released.wait()

    monkeypatch.setattr(ai_config_service, "ensure_endpoint_allowed", blocked_dns)
    suffix, body = {
        "character": ("/characters/generate", {}),
        "organization": ("/organizations/generate", {}),
        "career": ("/careers/generate", {}),
        "relations_generate": ("/relationships/generate", {"character_ids": chars}),
        "relations_parse": (f"/characters/{chars[0]}/parse-relations", {}),
    }[kind]
    creating = asyncio.create_task(client.post(base + suffix, json=body))
    probe = None
    try:
        await gate.wait_reached()

        async def write_probe():
            async with app.state.sessionmaker() as db:
                await db.execute(update(User).where(User.id == user["id"]).values(
                    username=User.username, updated_at=User.updated_at,
                ))
                await db.commit()

        probe = asyncio.create_task(write_probe())
        await asyncio.wait_for(asyncio.shield(probe), 1)
    finally:
        gate.release()
        await asyncio.gather(creating, *([probe] if probe is not None else []), return_exceptions=True)


@pytest.mark.parametrize("mutation", ["default", "model"])
async def test_entity_admission_rechecks_original_model_after_unlocked_preflight(
    app, client, fake_llm, monkeypatch, mutation,
):
    await register(client, f"entity-admission-{mutation}")
    config = await create_ai_config(client, model="model-a")
    project = await create_project(client)
    gate = Gate()
    original = task_admission.prepare_task_admission

    async def gated_prepare(*args, **kwargs):
        proof = await original(*args, **kwargs)
        gate.reached.set()
        await gate.released.wait()
        return proof

    monkeypatch.setattr(task_admission, "prepare_task_admission", gated_prepare)
    creating = asyncio.create_task(client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={}))
    try:
        await gate.wait_reached()
        if mutation == "default":
            await create_ai_config(client, name="replacement", model="model-b", is_default=True)
        else:
            changed = await client.patch(f"/api/v1/ai-configs/{config['id']}", json={"model": "model-b"})
            assert changed.status_code == 200, changed.text
        gate.release()
        response = await creating
        assert response.status_code in (400, 409), response.text
        assert not fake_llm.calls
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(Task.id).where(Task.project_id == project["id"])) is None
    finally:
        gate.release()
        if not creating.done():
            creating.cancel()
        await asyncio.gather(creating, return_exceptions=True)


async def test_entity_retry_does_not_adopt_replaced_original_request(app, client, fake_llm, monkeypatch):
    await register(client, "entity-retry-source")
    await create_ai_config(client)
    project = await create_project(client)
    fake_llm.push(AIProviderError("temporary failure"))
    created = await client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={})
    assert created.status_code == 202, created.text
    old = await wait_task(client, created.json(), within=15)
    assert old["status"] == "failed"
    gate = Gate()
    original = task_routes.prepare_task_admission

    async def gated_prepare(*args, **kwargs):
        proof = await original(*args, **kwargs)
        gate.reached.set()
        await gate.released.wait()
        return proof

    monkeypatch.setattr(task_routes, "prepare_task_admission", gated_prepare)
    retrying = asyncio.create_task(client.post(f"/api/v1/tasks/{old['id']}/retry", params=task_params(old)))
    try:
        await gate.wait_reached()
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, old["id"])
            payload = deepcopy(row.input)
            payload["request"]["instruction"] = "后来替换的请求"
            row.input = payload
            await db.commit()
        gate.release()
        response = await retrying
        assert response.status_code == 409, response.text
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(Task.id).where(Task.retry_of_id == old["id"])) is None
        assert len(fake_llm.calls) == 1
    finally:
        gate.release()
        if not retrying.done():
            retrying.cancel()
        await asyncio.gather(retrying, return_exceptions=True)


async def _seed_foreign_relationship(app, client, *, owner_mismatch):
    await register(client, f"entity-foreign-{owner_mismatch}")
    await create_ai_config(client)
    project, other = await create_project(client), await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chars = []
    for name in ("甲", "乙", "丙"):
        response = await client.post(f"{base}/characters", json={"name": name, "relations_text": "与乙是朋友"})
        assert response.status_code == 201, response.text
        chars.append(response.json()["id"])
    async with app.state.sessionmaker() as db:
        foreign_type = RelationshipType(project_id=other["id"], category="social", role_a="他书秘密称呼", role_b="友")
        db.add(foreign_type)
        await db.flush()
        relation = Relationship(project_id=other["id"] if owner_mismatch else project["id"],
                                a_id=chars[0], b_id=chars[1], pair_low=chars[0], pair_high=chars[1],
                                type_id=foreign_type.id, description="他书关系秘密")
        db.add(relation)
        await db.commit()
        return base, chars, relation.id


@pytest.mark.parametrize("owner_mismatch", [False, True])
async def test_relations_generate_refuses_foreign_type_or_relationship(app, client, fake_llm, owner_mismatch):
    base, chars, _ = await _seed_foreign_relationship(app, client, owner_mismatch=owner_mismatch)
    response = await client.post(f"{base}/relationships/generate", json={"character_ids": chars})
    assert response.status_code == 409, response.text
    assert "他书秘密" not in response.text and not fake_llm.calls


async def test_relations_parse_refuses_foreign_relationship_result(app, client, fake_llm):
    base, chars, foreign_relation_id = await _seed_foreign_relationship(app, client, owner_mismatch=True)
    fake_llm.push(json.dumps({"relationships": [{"target": "乙", "self_role": "友", "target_role": "友"}]}))
    response = await client.post(f"{base}/characters/{chars[0]}/parse-relations", json={})
    assert response.status_code == 202, response.text
    task = await wait_task(client, response.json())
    assert task["status"] == "failed", task
    assert task.get("result") is None and "他书关系秘密" not in json.dumps(task, ensure_ascii=False)
    assert str(foreign_relation_id) not in json.dumps(task.get("result"), ensure_ascii=False)


async def test_relation_parse_ignores_unrelated_foreign_pair(app, client, fake_llm):
    await register(client, "entity-unrelated-pair")
    await create_ai_config(client)
    project, other = await create_project(client), await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chars = []
    for name in ("甲", "乙", "丙"):
        response = await client.post(f"{base}/characters", json={"name": name, "relations_text": "与乙丙是朋友"})
        assert response.status_code == 201, response.text
        chars.append(response.json()["id"])
    async with app.state.sessionmaker() as db:
        foreign_type = RelationshipType(project_id=other["id"], category="social", role_a="私密称呼", role_b="友")
        db.add(foreign_type)
        await db.flush()
        db.add(Relationship(project_id=other["id"], a_id=chars[1], b_id=chars[2], pair_low=chars[1],
                            pair_high=chars[2], type_id=foreign_type.id, description="他书秘密"))
        await db.commit()
    fake_llm.push(json.dumps({"relationships": [
        {"target": target, "self_role": "友", "target_role": "友"} for target in ("乙", "丙")
    ]}))
    response = await client.post(f"{base}/characters/{chars[0]}/parse-relations", json={})
    assert response.status_code == 202, response.text
    task = await wait_task(client, response.json(), within=15)
    assert task["status"] == "succeeded", task
    assert "他书秘密" not in json.dumps(task, ensure_ascii=False)


async def test_relation_result_is_rechecked_in_the_publication_transaction(app, client, fake_llm, monkeypatch):
    await register(client, "entity-result-race")
    await create_ai_config(client)
    project, other = await create_project(client), await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    first = await client.post(f"{base}/characters", json={"name": "甲", "relations_text": "乙是朋友"})
    second = await client.post(f"{base}/characters", json={"name": "乙"})
    assert first.status_code == second.status_code == 201
    type_id = (await client.get(f"{base}/relationship-types")).json()[0]["id"]
    relation = await client.post(f"{base}/relationships", json={
        "a_id": first.json()["id"], "b_id": second.json()["id"], "type_id": type_id,
    })
    assert relation.status_code == 201, relation.text
    fake_llm.push(json.dumps({"relationships": [{"target": "乙", "self_role": "朋友", "target_role": "朋友"}]}))
    gate = Gate()
    handler = get_handler("relations.parse")
    original_run = handler.run

    async def gated_run(job, ctx):
        async with aclosing(original_run(job, ctx)) as events:
            async for event, data in events:
                if event == "done":
                    gate.reached.set()
                    await gate.released.wait()
                yield event, data

    monkeypatch.setattr(handler, "run", gated_run)
    response = await client.post(f"{base}/characters/{first.json()['id']}/parse-relations", json={})
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    engine = app.state.task_engine
    try:
        await gate.wait_reached()
        hub = engine.hubs[task_id]
        async with app.state.sessionmaker() as db:
            row = await db.get(Relationship, relation.json()["id"])
            row.project_id = other["id"]
            await db.commit()
        gate.release()
        await wait_until(lambda: task_id not in engine._running, within=15)
        assert hub.result is None
    finally:
        gate.release()


async def test_relation_created_after_send_is_marked_as_conflict(app, client, fake_llm):
    await register(client, "entity-relation-after-send")
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chars = []
    for name in ("甲", "乙"):
        created = await client.post(f"{base}/characters", json={"name": name})
        assert created.status_code == 201, created.text
        chars.append(created.json()["id"])
    gate = Gate()
    fake_llm.push([gate, json.dumps({"relationships": [{
        "a_id": chars[0], "b_id": chars[1], "role_a": "朋友", "role_b": "朋友",
        "category": "social", "intimacy": 0, "status": "normal", "description": "",
    }]})])
    response = await client.post(f"{base}/relationships/generate", json={"character_ids": chars})
    assert response.status_code == 202, response.text
    try:
        await gate.wait_reached()
        types = await client.get(f"{base}/relationship-types")
        type_id = types.json()[0]["id"]
        relation = await client.post(f"{base}/relationships", json={
            "a_id": chars[0], "b_id": chars[1], "type_id": type_id,
        })
        assert relation.status_code == 201, relation.text
        gate.release()
        task, events = await run_task(client, response)
        assert task["status"] == "succeeded", task
        suggestion = events[-1][1]["result"]["suggestions"][0]
        assert not suggestion["resolved"] and suggestion["existing_relationship_id"] == relation.json()["id"]
        async with app.state.sessionmaker() as db:
            saved = await db.get(Task, task["id"])
            assert saved.result["suggestions"][0] == suggestion
    finally:
        gate.release()


async def test_relation_added_before_final_commit_recomputes_published_result(app, client, fake_llm, monkeypatch):
    await register(client, "entity-finalize-recompute")
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chars = []
    for name in ("甲", "乙"):
        created = await client.post(f"{base}/characters", json={"name": name})
        assert created.status_code == 201, created.text
        chars.append(created.json()["id"])
    fake_llm.push(json.dumps({"relationships": [{
        "a_id": chars[0], "b_id": chars[1], "role_a": "朋友", "role_b": "朋友",
        "category": "social", "intimacy": 0, "status": "normal", "description": "",
    }]}))
    engine = app.state.task_engine
    original = engine.complete_state
    gate = Gate()

    async def gated_complete(task, **kwargs):
        if task is not None and task.kind == "relations.generate" and kwargs.get("status") == "succeeded":
            gate.reached.set()
            await gate.released.wait()
        return await original(task, **kwargs)

    monkeypatch.setattr(engine, "complete_state", gated_complete)
    response = await client.post(f"{base}/relationships/generate", json={"character_ids": chars})
    assert response.status_code == 202, response.text
    try:
        await gate.wait_reached()
        types = await client.get(f"{base}/relationship-types")
        relation = await client.post(f"{base}/relationships", json={
            "a_id": chars[0], "b_id": chars[1], "type_id": types.json()[0]["id"],
        })
        assert relation.status_code == 201, relation.text
        gate.release()
        task, events = await run_task(client, response)
        assert task["status"] == "succeeded", task
        suggestion = events[-1][1]["result"]["suggestions"][0]
        assert not suggestion["resolved"] and suggestion["existing_relationship_id"] == relation.json()["id"]
        async with app.state.sessionmaker() as db:
            saved = await db.get(Task, task["id"])
            assert saved.result["suggestions"][0] == suggestion
    finally:
        gate.release()


@pytest.mark.parametrize("mutation", ["update", "delete"])
async def test_existing_relation_change_after_send_is_recomputed(app, client, fake_llm, mutation):
    await register(client, f"entity-relation-{mutation}")
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chars = []
    for name in ("甲", "乙", "丙"):
        created = await client.post(f"{base}/characters", json={"name": name})
        assert created.status_code == 201, created.text
        chars.append(created.json()["id"])
    types = await client.get(f"{base}/relationship-types")
    relation = await client.post(f"{base}/relationships", json={
        "a_id": chars[0], "b_id": chars[1], "type_id": types.json()[0]["id"], "description": "原关系",
    })
    assert relation.status_code == 201, relation.text
    gate = Gate()
    fake_llm.push([gate, json.dumps({"relationships": [{
        "a_id": chars[0], "b_id": chars[1], "role_a": "朋友", "role_b": "朋友",
        "category": "social", "intimacy": 0, "status": "normal", "description": "",
    }]})])
    response = await client.post(f"{base}/relationships/generate", json={"character_ids": chars})
    assert response.status_code == 202, response.text
    try:
        await gate.wait_reached()
        path = f"{base}/relationships/{relation.json()['id']}"
        changed = await client.patch(path, json={"description": "更新的关系"}) if mutation == "update" \
            else await client.delete(path)
        assert changed.status_code == (200 if mutation == "update" else 204), changed.text
        gate.release()
        task, events = await run_task(client, response)
        assert task["status"] == "succeeded", task
        suggestion = events[-1][1]["result"]["suggestions"][0]
        assert suggestion["resolved"] is (mutation == "delete")
        assert suggestion["existing_relationship_id"] == (relation.json()["id"] if mutation == "update" else None)
    finally:
        gate.release()


@pytest.mark.parametrize("owner_mismatch", [False, True])
async def test_foreign_relation_added_after_send_stops_later_output(app, client, fake_llm, owner_mismatch):
    await register(client, f"entity-late-foreign-{owner_mismatch}")
    await create_ai_config(client)
    project, other = await create_project(client), await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chars = []
    for name in ("甲", "乙"):
        created = await client.post(f"{base}/characters", json={"name": name})
        assert created.status_code == 201, created.text
        chars.append(created.json()["id"])
    gate = Gate()
    fake_llm.push([gate, '{"relationships": [], "secret": "不得返回"}'])
    response = await client.post(f"{base}/relationships/generate", json={"character_ids": chars})
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    engine = app.state.task_engine
    try:
        await gate.wait_reached()
        hub = engine.hubs[task_id]
        async with app.state.sessionmaker() as db:
            foreign_type = RelationshipType(project_id=other["id"], category="social", role_a="秘密", role_b="友")
            db.add(foreign_type)
            await db.flush()
            db.add(Relationship(project_id=other["id"] if owner_mismatch else project["id"],
                                a_id=chars[0], b_id=chars[1], pair_low=chars[0], pair_high=chars[1],
                                type_id=foreign_type.id))
            await db.commit()
        gate.release()
        await wait_until(lambda: task_id not in engine._running, within=15)
        assert len(fake_llm.calls) == 1 and "不得返回" not in hub.text and hub.result is None
    finally:
        gate.release()


async def test_entity_stream_guard_cannot_be_used_without_physical_lease(app, client, fake_llm, monkeypatch):
    await register(client, "entity-no-frame-lease")
    await create_ai_config(client)
    project = await create_project(client)
    gate = Gate()
    checks = []
    original = collaboration_ai_guard.SendGuard.prepare_provider

    async def capture(self, sessionmaker, factory, *, request_timeout):
        checks.append(self)
        return await original(self, sessionmaker, factory, request_timeout=request_timeout)

    monkeypatch.setattr(collaboration_ai_guard.SendGuard, "prepare_provider", capture)
    fake_llm.push([gate, '{"character":{"name":"例子"}}'])
    response = await client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={})
    assert response.status_code == 202, response.text
    try:
        await gate.wait_reached()
        assert len(checks) == 1
        async with app.state.sessionmaker() as db:
            with pytest.raises(VisibilityPolicyError):
                await checks[0].require_task_read(db)
        gate.release()
        assert (await wait_task(client, response.json(), within=15))["status"] == "succeeded"
    finally:
        gate.release()


async def test_guarded_entity_stream_sends_running_and_terminal_frames(app, client, fake_llm, monkeypatch):
    user = await register(client, "entity-guarded-sse")
    await create_ai_config(client)
    project = await create_project(client)
    model_gate, observed = Gate(), asyncio.Event()
    fake_llm.push(['{"character":{"name":"', model_gate, '示例"}}'])
    response = await client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={})
    assert response.status_code == 202, response.text
    await model_gate.wait_reached()
    _install_guarded_entity_stream(app, user["id"], response.json())
    original = stream_lease._TaskFrameGuardedResponse._send_frame

    async def observe(self, send, event, data, evidence):
        outcome = await original(self, send, event, data, evidence)
        if event == "snapshot" and outcome == "sent":
            observed.set()
        return outcome

    monkeypatch.setattr(stream_lease._TaskFrameGuardedResponse, "_send_frame", observe)
    pending = asyncio.create_task(client.get("/_test/entity-guarded-stream"))
    try:
        await asyncio.wait_for(observed.wait(), 10)
        model_gate.release()
        streamed = await asyncio.wait_for(pending, 15)
        events = parse_sse(streamed.text)
        assert any(event == "snapshot" for event, _ in events)
        assert events[-1][0] == "done" and events[-1][1]["result"]["draft"]["name"] == "示例", events
    finally:
        model_gate.release()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("mutation", ["model", "source"])
async def test_guarded_entity_terminal_rechecks_original_sources_before_send(
    app, client, fake_llm, monkeypatch, mutation,
):
    user = await register(client, f"entity-terminal-{mutation}")
    config = await create_ai_config(client)
    project, other = await create_project(client), await create_project(client)
    character = await client.post(f"/api/v1/projects/{project['id']}/characters", json={"name": "甲"})
    assert character.status_code == 201, character.text
    await _settings(client, f"/api/v1/projects/{project['id']}", "character", character.json()["id"], ai_mode="always")
    model_gate = Gate()
    fake_llm.push(['{"character":{"name":"', model_gate, '示例"}}'])
    response = await client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={})
    assert response.status_code == 202, response.text
    await model_gate.wait_reached()
    _install_guarded_entity_stream(app, user["id"], response.json())
    snapshot_sent, delayed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = stream_lease._TaskFrameGuardedResponse._send_frame

    async def gate_frame(self, send, event, data, evidence):
        if event == "done":
            delayed.set()
            await release.wait()
        outcome = await original(self, send, event, data, evidence)
        if event == "snapshot" and outcome == "sent":
            snapshot_sent.set()
        return outcome

    monkeypatch.setattr(stream_lease._TaskFrameGuardedResponse, "_send_frame", gate_frame)
    pending = asyncio.create_task(client.get("/_test/entity-guarded-stream"))
    try:
        await asyncio.wait_for(snapshot_sent.wait(), 10)
        model_gate.release()
        waiter = asyncio.create_task(delayed.wait())
        done, _ = await asyncio.wait((waiter, pending), timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        assert delayed.is_set(), pending.result().text if pending in done else "未收到终态帧"
        async with app.state.sessionmaker() as db:
            if mutation == "model":
                selected = await db.get(AIConfig, config["id"])
                selected.model = "changed-model"
            else:
                from app.models import Character

                source = await db.get(Character, character.json()["id"])
                source.project_id = other["id"]
            await db.commit()
        release.set()
        streamed = await asyncio.wait_for(pending, 15)
        assert "event: done" not in streamed.text and "permission_denied" in streamed.text
    finally:
        release.set()
        model_gate.release()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("mutation", ["token", "model", "source"])
async def test_guarded_entity_stream_drops_frame_changed_before_actual_send(
    app, client, fake_llm, monkeypatch, mutation,
):
    user = await register(client, f"entity-sse-{mutation}")
    config = await create_ai_config(client)
    project, other = await create_project(client), await create_project(client)
    character = await client.post(f"/api/v1/projects/{project['id']}/characters", json={
        "name": "甲",
    })
    assert character.status_code == 201, character.text
    await _settings(client, f"/api/v1/projects/{project['id']}", "character", character.json()["id"], ai_mode="always")
    model_gate, end_gate = Gate(), Gate()
    fake_llm.push(['{"character":{"name":"', model_gate, "X" * 256 + "不得发送", end_gate, '"}}'])
    response = await client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={})
    assert response.status_code == 202, response.text
    await model_gate.wait_reached()
    _install_guarded_entity_stream(app, user["id"], response.json())
    snapshot_sent, delayed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = stream_lease._TaskFrameGuardedResponse._send_frame

    async def gate_frame(self, send, event, data, evidence):
        if event == "delta" and "不得发送" in data.get("text", ""):
            delayed.set()
            await release.wait()
        outcome = await original(self, send, event, data, evidence)
        if event == "snapshot" and outcome == "sent":
            snapshot_sent.set()
        return outcome

    monkeypatch.setattr(stream_lease._TaskFrameGuardedResponse, "_send_frame", gate_frame)
    pending = asyncio.create_task(client.get("/_test/entity-guarded-stream"))
    try:
        await asyncio.wait_for(snapshot_sent.wait(), 10)
        model_gate.release()
        await asyncio.wait_for(delayed.wait(), 10)
        async with app.state.sessionmaker() as db:
            if mutation == "token":
                actor = await db.get(User, user["id"])
                actor.token_version += 1
            elif mutation == "model":
                selected = await db.get(AIConfig, config["id"])
                selected.model = "changed-model"
            else:
                from app.models import Character

                source = await db.get(Character, character.json()["id"])
                source.project_id = other["id"]
            await db.commit()
        release.set()
        streamed = await asyncio.wait_for(pending, 15)
        assert "不得发送" not in streamed.text and "permission_denied" in streamed.text
    finally:
        release.set()
        model_gate.release()
        end_gate.release()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_entity_editor_admission_boundaries_never_touch_owner_vectors(app, client, make_client, fake_llm):
    """E4-g3b 起 accepted editor 可发起实体 AI（成功路径见 test_collaboration_entity_entry_e4g3b）。本用例保留受理边界：
    editor 无本人模型且未获作者授权 → 400，降为 viewer → 403；均零任务、零模型/向量调用，作者向量配置不被触碰。"""
    owner = await register(client, "entity-owner")
    await create_ai_config(client)
    embedding = await create_ai_config(client, name="作者向量", model_type="embedding", model="vectors-owner")
    editor = make_client()
    actor = await register(editor, "entity-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chars = []
    for name in ("甲", "乙"):
        created = await client.post(f"{base}/characters", json={"name": name, "relations_text": "旧关系"})
        assert created.status_code == 201, created.text
        chars.append(created.json()["id"])
    invite = await client.post(f"{base}/collaborators", json={"username": "entity-editor", "role": "editor"})
    assert invite.status_code == 201, invite.text
    accepted = await editor.post(
        f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
        json={"expected_revision": 1},
    )
    assert accepted.status_code == 200, accepted.text
    paths = (
        ("/characters/generate", {}),
        ("/organizations/generate", {}),
        ("/careers/generate", {}),
        ("/relationships/generate", {"character_ids": chars}),
        (f"/characters/{chars[0]}/parse-relations", {}),
    )
    for path, body in paths:
        denied = await editor.post(base + path, json=body)
        assert denied.status_code == 400, denied.text
    changed = await client.patch(f"{base}/collaborators/{actor['id']}", json={
        "expected_revision": 2, "role": "viewer", "allow_owner_ai": True,
    })
    assert changed.status_code == 200, changed.text
    for path, body in paths:
        denied = await editor.post(base + path, json=body)
        assert denied.status_code == 403, denied.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Task.id).where(Task.user_id == actor["id"])) is None
        owner_embedding = await db.get(AIConfig, embedding["id"])
        assert owner_embedding.user_id == owner["id"] and owner_embedding.model == "vectors-owner"
    assert not fake_llm.calls and not fake_llm.embed_calls


@pytest.mark.parametrize("mutation", ["character", "relation", "type"])
async def test_relation_prompt_sources_are_checked_before_send(app, client, fake_llm, monkeypatch, mutation):
    await register(client, f"entity-source-{mutation}")
    await create_ai_config(client)
    project, other = await create_project(client), await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chars = []
    for name in ("甲", "乙", "丙"):
        response = await client.post(f"{base}/characters", json={"name": name})
        assert response.status_code == 201, response.text
        chars.append(response.json()["id"])
    created_type = await client.post(f"{base}/relationship-types", json={"role_a": "长辈", "role_b": "晚辈"})
    assert created_type.status_code == 201, created_type.text
    created_relation = await client.post(f"{base}/relationships", json={
        "a_id": chars[0], "b_id": chars[1], "type_id": created_type.json()["id"], "description": "本书秘密",
    })
    assert created_relation.status_code == 201, created_relation.text
    gate = Gate()
    original = collaboration_ai_guard.SendGuard.prepare_provider

    async def gated_prepare(self, sessionmaker, factory, *, request_timeout):
        if self._task is not None and self._task.kind == "relations.generate":
            gate.reached.set()
            await gate.released.wait()
        return await original(self, sessionmaker, factory, request_timeout=request_timeout)

    monkeypatch.setattr(collaboration_ai_guard.SendGuard, "prepare_provider", gated_prepare)
    fake_llm.push('{"relationships": []}')
    response = await client.post(f"{base}/relationships/generate", json={"character_ids": chars})
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    engine = app.state.task_engine
    try:
        await gate.wait_reached()
        hub = engine.hubs[task_id]
        async with app.state.sessionmaker() as db:
            if mutation == "character":
                from app.models import Character

                row = await db.get(Character, chars[0])
            elif mutation == "relation":
                row = await db.get(Relationship, created_relation.json()["id"])
            else:
                row = await db.get(RelationshipType, created_type.json()["id"])
            row.project_id = other["id"]
            await db.commit()
        gate.release()
        await wait_until(lambda: task_id not in engine._running, within=15)
        assert not fake_llm.calls and hub.result is None
    finally:
        gate.release()


async def test_entity_knowledge_hit_must_still_belong_to_project_at_send(app, client, fake_llm, monkeypatch):
    await register(client, "entity-source-document")
    await create_ai_config(client)
    project, other = await create_project(client), await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    upload = await client.post(f"{base}/kb/documents", files={
        "file": ("天剑宗.md", "# 天剑宗\n\n弟子遵守师承誓约。".encode(), "text/markdown"),
    })
    assert upload.status_code == 202, upload.text
    assert (await wait_task(client, upload.json()["task"]))["status"] == "succeeded"
    assert (await client.patch(f"{base}/kb/settings", json={"min_score": 0})).status_code == 200
    gate = Gate()
    captured = []
    original = collaboration_ai_guard.SendGuard.prepare_provider

    async def gated_prepare(self, sessionmaker, factory, *, request_timeout):
        if self._task is not None and self._task.kind == "character.generate":
            captured.extend(self._entity_sources.groups)
            gate.reached.set()
            await gate.released.wait()
        return await original(self, sessionmaker, factory, request_timeout=request_timeout)

    monkeypatch.setattr(collaboration_ai_guard.SendGuard, "prepare_provider", gated_prepare)
    fake_llm.push('{"character":{"name":"不应生成"}}')
    response = await client.post(f"{base}/characters/generate", json={"instruction": "天剑宗师承"})
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    engine = app.state.task_engine
    try:
        await gate.wait_reached()
        assert any(group.kind == "document" and group.rows for group in captured)
        hub = engine.hubs[task_id]
        async with app.state.sessionmaker() as db:
            document = await db.get(KbDocument, upload.json()["document"]["id"])
            document.project_id = other["id"]
            await db.commit()
        gate.release()
        await wait_until(lambda: task_id not in engine._running, within=15)
        assert not fake_llm.calls and hub.result is None
    finally:
        gate.release()


async def test_entity_does_not_send_knowledge_after_auto_inject_is_disabled(app, client, fake_llm, monkeypatch):
    await register(client, "entity-kb-setting")
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    upload = await client.post(f"{base}/kb/documents", files={
        "file": ("天剑宗.md", "# 天剑宗\n\n弟子遵守师承誓约。".encode(), "text/markdown"),
    })
    assert upload.status_code == 202, upload.text
    assert (await wait_task(client, upload.json()["task"]))["status"] == "succeeded"
    assert (await client.patch(f"{base}/kb/settings", json={"min_score": 0})).status_code == 200
    gate = Gate()
    original = collaboration_ai_guard.SendGuard.prepare_provider

    async def gated_prepare(self, sessionmaker, factory, *, request_timeout):
        if self._task is not None and self._task.kind == "character.generate":
            gate.reached.set()
            await gate.released.wait()
        return await original(self, sessionmaker, factory, request_timeout=request_timeout)

    monkeypatch.setattr(collaboration_ai_guard.SendGuard, "prepare_provider", gated_prepare)
    fake_llm.push('{"character":{"name":"不应生成"}}')
    response = await client.post(f"{base}/characters/generate", json={"instruction": "天剑宗师承"})
    assert response.status_code == 202, response.text
    engine = app.state.task_engine
    try:
        await gate.wait_reached()
        assert (await client.patch(f"{base}/kb/settings", json={"auto_inject": False})).status_code == 200
        gate.release()
        await wait_until(lambda: response.json()["id"] not in engine._running, within=15)
        assert not fake_llm.calls
    finally:
        gate.release()


@pytest.mark.parametrize("mutation", ["task_input", "execution", "token", "model"])
async def test_entity_stream_drops_later_output_when_original_identity_changes(app, client, fake_llm, mutation):
    user = await register(client, f"entity-stream-{mutation}")
    config = await create_ai_config(client)
    project = await create_project(client)
    gate = Gate()
    fake_llm.push([gate, '{"character":{"name":"不得返回的模型正文"}}'])
    response = await client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={})
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    await gate.wait_reached()
    engine = app.state.task_engine
    hub = engine.hubs[task_id]
    async with app.state.sessionmaker() as db:
        if mutation in {"task_input", "execution"}:
            task = await db.get(Task, task_id)
            if mutation == "task_input":
                payload = deepcopy(task.input)
                payload["request"]["instruction"] = "替换了原请求"
                task.input = payload
            else:
                task.execution_key = str(uuid4())
        elif mutation == "token":
            actor = await db.get(User, user["id"])
            actor.token_version += 1
        else:
            cfg = await db.get(AIConfig, config["id"])
            cfg.model = "改变后的模型"
        await db.commit()
    gate.release()
    await wait_until(lambda: task_id not in engine._running, within=15)
    assert "不得返回的模型正文" not in hub.text
    assert hub.result is None


async def test_real_http_entity_stream_stops_after_token_change(app, client):
    engine = app.state.task_engine
    engine.ctx.factory = create_provider
    engine.ctx.settings.allow_private_ai_endpoints = True
    app.state.provider_factory = create_provider
    payload = json.dumps(openai_chunk({"content": '{"character":{"name":"不得返回"}}'}, "stop"))
    async with _server(payload=f"data: {payload}\n\ndata: [DONE]\n\n".encode()) as (
        url, received, headers, body, requests,
    ):
        user = await register(client, "entity-real-http")
        await create_ai_config(client, base_url=f"{url}/v1/", api_key="test", model="original-model")
        project = await create_project(client)
        response = await client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={})
        assert response.status_code == 202, response.text
        task_id = response.json()["id"]
        try:
            await asyncio.wait_for(received.wait(), 8)
            hub = engine.hubs[task_id]
            headers.set()
            async with app.state.sessionmaker() as db:
                actor = await db.get(User, user["id"])
                actor.token_version += 1
                await db.commit()
            body.set()
            await wait_until(lambda: task_id not in engine._running, within=15)
            assert len(requests) == 1
            assert "不得返回" not in hub.text
            assert hub.result is None
        finally:
            headers.set()
            body.set()


async def test_entity_source_change_reports_safe_retry_message(app, client, fake_llm):
    await register(client, "entity-source-retry-message")
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    character = await client.post(f"{base}/characters", json={"name": "甲", "profile": "原设定"})
    assert character.status_code == 201, character.text
    await _settings(client, base, "character", character.json()["id"], ai_mode="always")
    gate = Gate()
    fake_llm.push([gate, '{"character":{"name":"不得输出"}}'])
    response = await client.post(f"{base}/characters/generate", json={})
    assert response.status_code == 202, response.text
    try:
        await gate.wait_reached()
        updated = await client.patch(f"{base}/characters/{character.json()['id']}", json={"profile": "新设定"})
        assert updated.status_code == 200, updated.text
        gate.release()
        task = await wait_task(client, response.json(), within=15)
        assert task["status"] == "failed" and "设定来源已变化" in task["error"] and "重新发起" in task["error"]
        output = await client.get(f"/api/v1/tasks/{task['id']}/output", params=task_params(task))
        assert "不得输出" not in output.text and "新设定" not in task["error"]
    finally:
        gate.release()


@pytest.mark.parametrize("mutation", ["default", "original", "legacy"])
async def test_entity_retry_keeps_original_model_or_refuses_unprovable_binding(app, client, fake_llm, mutation):
    await register(client, f"entity-retry-{mutation}")
    config = await create_ai_config(client, model="original-model")
    project = await create_project(client)
    fake_llm.push(AIProviderError("temporary failure"))
    initial = await client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={})
    assert initial.status_code == 202, initial.text
    task = await wait_task(client, initial.json())
    assert task["status"] == "failed"
    if mutation == "default":
        await create_ai_config(client, name="replacement", model="replacement-model", is_default=True)
    elif mutation == "original":
        changed = await client.patch(f"/api/v1/ai-configs/{config['id']}", json={"model": "modified-model"})
        assert changed.status_code == 200, changed.text
    else:
        async with app.state.sessionmaker() as db:
            saved = await db.get(Task, task["id"])
            payload = deepcopy(saved.input)
            payload.pop("_project_model")
            saved.input = payload
            await db.commit()
    fake_llm.push('{"character":{"name":"重试结果"}}')
    response = await client.post(f"/api/v1/tasks/{task['id']}/retry", params=task_params(task))
    if mutation == "default":
        assert response.status_code == 202, response.text
        retried = await wait_task(client, response.json())
        assert retried["status"] == "succeeded", retried
        assert [call["model"] for call in fake_llm.calls] == ["original-model", "original-model"]
    else:
        assert response.status_code in (400, 409), response.text
        assert len(fake_llm.calls) == 1


@pytest.mark.parametrize("configured", [False, True])
async def test_entity_freezes_server_embedding_selection_even_without_documents(app, client, fake_llm, configured):
    await register(client, f"entity-shared-{configured}")
    await create_ai_config(client)
    embedding = None
    if configured:
        embedding = await create_ai_config(client, name="author embedding", model_type="embedding", model="vectors-a")
    project = await create_project(client)
    await app.state.task_engine.stop()
    response = await client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={
        "_shared_embedding": {"mode": "vector", "model": {"config_id": 999}},
        "_project_model": {"config_id": 999},
    })
    assert response.status_code == 202, response.text
    assert "_shared_embedding" not in response.text and "_project_model" not in response.text
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, response.json()["id"])
        selection = task.input["_shared_embedding"]
        assert selection["mode"] == ("vector" if configured else "keyword")
        if configured:
            assert selection["model"]["config_id"] == embedding["id"]
        else:
            assert selection["model"] is None
        assert task.input["_project_model"]["config_id"] != 999


@pytest.mark.parametrize("mutation", ["new_default", "edit_original"])
async def test_entity_retry_preserves_original_shared_embedding(app, client, fake_llm, mutation):
    await register(client, f"entity-retry-{mutation}")
    await create_ai_config(client)
    if mutation == "edit_original":
        config = await create_ai_config(client, name="embedding", model_type="embedding", model="vectors-a")
    project = await create_project(client)
    fake_llm.push(AIProviderError("temporary failure"))
    response = await client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={})
    assert response.status_code == 202, response.text
    task = await wait_task(client, response.json())
    assert task["status"] == "failed"
    if mutation == "new_default":
        await create_ai_config(client, name="embedding", model_type="embedding", model="vectors-b")
    else:
        changed = await client.patch(f"/api/v1/ai-configs/{config['id']}", json={"model": "vectors-b"})
        assert changed.status_code == 200, changed.text
    fake_llm.push('{"character":{"name":"重试结果"}}')
    retry = await client.post(f"/api/v1/tasks/{task['id']}/retry", params=task_params(task))
    if mutation == "new_default":
        assert retry.status_code == 202, retry.text
        async with app.state.sessionmaker() as db:
            retried = await db.get(Task, retry.json()["id"])
            assert retried.input["_shared_embedding"]["mode"] == "keyword"
        assert (await wait_task(client, retry.json()))["status"] == "succeeded"
    else:
        assert retry.status_code in (400, 409), retry.text
        assert len(fake_llm.calls) == 1


@pytest.mark.parametrize("mutation", ["token", "text_model", "embedding_model", "task_input"])
async def test_entity_embedding_send_rechecks_original_execution(app, client, fake_llm, monkeypatch, mutation):
    user = await register(client, f"entity-embedding-{mutation}")
    text_config = await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    upload = await client.post(f"{base}/kb/documents", files={
        "file": ("资料.md", "# 天剑宗\n\n天剑宗弟子遵守师承誓约。".encode(), "text/markdown"),
    })
    assert upload.status_code == 202, upload.text
    assert (await wait_task(client, upload.json()["task"]))["status"] == "succeeded"
    embedding = await create_ai_config(client, name="embedding", model_type="embedding", model="vectors-a")
    await app.state.task_engine.stop()
    response = await client.post(f"{base}/characters/generate", json={"instruction": "天剑宗师承"})
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        selection = project_ai_config.deserialize_shared_embedding(
            task.input["_shared_embedding"], actor_id=user["id"], project_id=project["id"],
            project_owner_id=user["id"], task_grant=project_ai_config.TASK_GRANT_ABSENT,
        )
        config = await retrieval_service.load_bound_shared_embedding_config(
            db, selection, cipher=app.state.cipher, allow_private=False,
        )
        row = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.project_id == project["id"], RetrievalChunk.source == "kb",
        ))
        vector = retrieval_service.normalize_vector(fake_vector("天剑宗师承"))
        row.embedding = retrieval_service.encode_vector(vector)
        # E6-d（裁决 1）：作品向量空间为作者旧键 provider:model:dim（原 shared:v2 键已无读取方）。
        row.embedding_key = retrieval_service.make_embedding_key(f"{config.provider}:{config.model}", len(vector))
        row.version += 1
        await db.commit()
    retrieval_service.invalidate(project["id"])
    gate = Gate()
    original_embed = retrieval_service.Embedder.embed

    async def gated_embed(self, texts):
        gate.reached.set()
        await gate.released.wait()
        return await original_embed(self, texts)

    monkeypatch.setattr(retrieval_service.Embedder, "embed", gated_embed)
    fake_llm.push('{"character":{"name":"不应生成"}}')
    engine = app.state.task_engine
    await engine.start()
    try:
        await gate.wait_reached()
        hub = engine.hubs[task_id]
        async with app.state.sessionmaker() as db:
            if mutation == "token":
                actor = await db.get(User, user["id"])
                actor.token_version += 1
            elif mutation in {"text_model", "embedding_model"}:
                cfg = await db.get(AIConfig, text_config["id"] if mutation == "text_model" else embedding["id"])
                cfg.model = "modified-model"
            else:
                task = await db.get(Task, task_id)
                payload = deepcopy(task.input)
                payload["request"]["instruction"] = "替换原请求"
                task.input = payload
            await db.commit()
        gate.release()
        await wait_until(lambda: task_id not in engine._running, within=15)
        assert not fake_llm.embed_calls and not fake_llm.calls
        assert hub.result is None
    finally:
        gate.release()
