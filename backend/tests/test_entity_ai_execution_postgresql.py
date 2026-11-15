"""隔离 PostgreSQL 与真实 SDK/HTTP：实体任务跨响应头的原身份校验。"""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.ai.providers import create_provider
from app.models import AIConfig, Character, Relationship, Task, User
from tests.helpers import Gate, create_ai_config, create_project, parse_sse, register, wait_until
from tests.test_agent_providers import openai_chunk
from tests.test_ai_visibility_dispatch import _server
from tests.test_entity_ai_execution_identity import _install_guarded_entity_stream

pytest_plugins = ["tests.test_collaboration_access_postgresql"]


def _entity_response() -> bytes:
    content = json.dumps({"character": {"name": "响应头之后不得返回"}}, ensure_ascii=False)
    chunk = openai_chunk({"content": content}, "stop")
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode()


@pytest.mark.parametrize("mutation", ["task_input", "token", "model"])
async def test_entity_response_after_identity_change_is_not_published(postgres_collaboration_apps, mutation):
    app, _ = postgres_collaboration_apps
    engine = app.state.task_engine
    engine.ctx.factory = create_provider
    engine.ctx.settings.allow_private_ai_endpoints = True
    app.state.provider_factory = create_provider
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client,
        _server(payload=_entity_response()) as (url, received, headers, body, requests),
    ):
        user = await register(client, f"entity-pg-{mutation}")
        config = await create_ai_config(client, base_url=f"{url}/v1/", api_key="test", model="original-model")
        project = await create_project(client)
        response = await client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={})
        assert response.status_code == 202, response.text
        task_id = response.json()["id"]
        await engine.start()
        try:
            await asyncio.wait_for(received.wait(), 8)
            hub = engine.hubs[task_id]
            headers.set()
            # 响应头释放后变更原身份；原连接仍等待响应体。
            async with app.state.sessionmaker() as db:
                if mutation == "task_input":
                    task = await db.get(Task, task_id)
                    payload = deepcopy(task.input)
                    payload["request"]["instruction"] = "替换原请求"
                    task.input = payload
                elif mutation == "token":
                    actor = await db.get(User, user["id"])
                    actor.token_version += 1
                else:
                    config_row = await db.get(AIConfig, config["id"])
                    config_row.model = "modified-model"
                await db.commit()
            body.set()
            await wait_until(lambda: task_id not in engine._running, within=15)
            assert len(requests) == 1
            assert "响应头之后不得返回" not in hub.text
            assert hub.result is None
        finally:
            headers.set()
            body.set()
            await engine.stop()


async def test_entity_real_send_holds_actor_barrier_through_response_headers(postgres_collaboration_apps):
    app, _ = postgres_collaboration_apps
    engine = app.state.task_engine
    engine.ctx.factory = create_provider
    engine.ctx.settings.allow_private_ai_endpoints = True
    app.state.provider_factory = create_provider
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client,
        _server(payload=_entity_response()) as (url, received, headers, body, requests),
    ):
        user = await register(client, "entity-pg-lock")
        await create_ai_config(client, base_url=f"{url}/v1/", api_key="test", model="original-model")
        project = await create_project(client)
        response = await client.post(f"/api/v1/projects/{project['id']}/characters/generate", json={})
        assert response.status_code == 202, response.text
        await engine.start()
        try:
            await asyncio.wait_for(received.wait(), 8)
            async with app.state.sessionmaker() as db:
                with pytest.raises(DBAPIError) as error:
                    await db.scalar(select(User.id).where(User.id == user["id"]).with_for_update(nowait=True))
                assert any(getattr(item, "sqlstate", None) == "55P03"
                           for item in (error.value.orig, getattr(error.value.orig, "__cause__", None)))
                await db.rollback()
            headers.set()
            body.set()
            await wait_until(lambda: response.json()["id"] not in engine._running, within=15)
            assert len(requests) == 1
        finally:
            headers.set()
            body.set()
            await engine.stop()


async def test_relation_source_is_locked_until_real_response_headers(postgres_collaboration_apps):
    app, _ = postgres_collaboration_apps
    engine = app.state.task_engine
    engine.ctx.factory = create_provider
    engine.ctx.settings.allow_private_ai_endpoints = True
    app.state.provider_factory = create_provider
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client,
        _server(payload=_entity_response()) as (url, received, headers, body, requests),
    ):
        await register(client, "entity-pg-relation")
        await create_ai_config(client, base_url=f"{url}/v1/", api_key="test", model="original-model")
        project = await create_project(client)
        base = f"/api/v1/projects/{project['id']}"
        chars = []
        for name in ("甲", "乙", "丙"):
            created = await client.post(f"{base}/characters", json={"name": name})
            assert created.status_code == 201, created.text
            chars.append(created.json()["id"])
        types = await client.get(f"{base}/relationship-types")
        relation = await client.post(f"{base}/relationships", json={
            "a_id": chars[0], "b_id": chars[1], "type_id": types.json()[0]["id"],
        })
        assert relation.status_code == 201, relation.text
        response = await client.post(f"{base}/relationships/generate", json={"character_ids": chars})
        assert response.status_code == 202, response.text
        await engine.start()
        try:
            await asyncio.wait_for(received.wait(), 8)
            async with app.state.sessionmaker() as db:
                with pytest.raises(DBAPIError) as error:
                    await db.scalar(select(Relationship.id).where(Relationship.id == relation.json()["id"])
                                    .with_for_update(nowait=True))
                assert any(getattr(item, "sqlstate", None) == "55P03"
                           for item in (error.value.orig, getattr(error.value.orig, "__cause__", None)))
                await db.rollback()
            headers.set()
            body.set()
            await wait_until(lambda: response.json()["id"] not in engine._running, within=15)
            assert len(requests) == 1
        finally:
            headers.set()
            body.set()
            await engine.stop()


async def test_guarded_entity_sse_holds_actor_and_source_during_asgi_send(postgres_collaboration_apps, fake_llm):
    """测试专用受控 SSE 路由；生产任务路由是否切换由中央引擎决定。"""
    app, _ = postgres_collaboration_apps
    engine = app.state.task_engine
    entered, release = asyncio.Event(), asyncio.Event()
    model_gate = Gate()

    async def wrapped(scope, receive, send):
        async def delayed(message):
            if b"event: snapshot" in message.get("body", b""):
                entered.set()
                await release.wait()
            await send(message)

        await app(scope, receive, delayed)

    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://testserver") as stream_client,
    ):
        user = await register(client, "entity-pg-sse")
        await create_ai_config(client)
        project = await create_project(client)
        base = f"/api/v1/projects/{project['id']}"
        chars = []
        for name in ("甲", "乙"):
            created = await client.post(f"{base}/characters", json={"name": name})
            assert created.status_code == 201, created.text
            chars.append(created.json()["id"])
        fake_llm.push(['{"relationships":', model_gate, '[]}'])
        response = await client.post(f"{base}/relationships/generate", json={"character_ids": chars})
        assert response.status_code == 202, response.text
        await engine.start()
        pending = None
        try:
            await model_gate.wait_reached()
            _install_guarded_entity_stream(app, user["id"], response.json())
            pending = asyncio.create_task(stream_client.get("/_test/entity-guarded-stream"))
            await asyncio.wait_for(entered.wait(), 10)
            for model, ident in ((User, user["id"]), (Character, chars[0])):
                async with app.state.sessionmaker() as db:
                    with pytest.raises(DBAPIError) as error:
                        await db.scalar(select(model.id).where(model.id == ident).with_for_update(nowait=True))
                    assert any(getattr(item, "sqlstate", None) == "55P03"
                               for item in (error.value.orig, getattr(error.value.orig, "__cause__", None)))
                    await db.rollback()
            release.set()
            model_gate.release()
            streamed = await asyncio.wait_for(pending, 15)
            assert parse_sse(streamed.text)[-1][0] == "done"
        finally:
            release.set()
            model_gate.release()
            if pending is not None and not pending.done():
                pending.cancel()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await engine.stop()
