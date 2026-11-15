"""PG 双实例与真实模型 HTTP 的单章任务原始绑定测试。"""

import asyncio
import json
from contextlib import suppress

import pytest
from sqlalchemy import func, select

from app.ai.providers import create_provider
from app.models import AIConfig, Chapter, Task
from app.services import ai_dispatch
from tests.helpers import create_ai_config, create_project, register, stream_task
from tests.test_ai_visibility_dispatch import _server, _sse
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_chapter_execution import (
    test_editor_identity_loss_never_writes_prompt_or_owner_model as _editor_cleanup_case,
)
from tests.test_collaboration_rewrite_identity import _http_clients, _world
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps as _postgres_fixture

postgres_chapter_execution_apps = _postgres_fixture


@pytest.mark.parametrize("change", ["token", "role", "owner_ai", "revoked", "replaced_task", "missing_identity"])
async def test_pg_editor_cleanup_does_not_persist_private_data(postgres_chapter_execution_apps, fake_llm, change):
    first_app, _second_app = postgres_chapter_execution_apps
    first_app.state.settings.allow_private_ai_endpoints = True
    await _editor_cleanup_case(first_app, fake_llm, change)


async def test_author_queued_binding_uses_original_model_on_postgresql(postgres_chapter_execution_apps):
    first_app, _second_app = postgres_chapter_execution_apps
    first_app.state.settings.allow_private_ai_endpoints = True
    first_app.state.task_engine.ctx.factory = create_provider
    try:
        async with (
            _server(payload=_sse("openai")) as (url, _received, headers, body, requests),
            _http_clients(first_app) as (owner, _unused),
        ):
            headers.set()
            body.set()
            await register(owner, "pg-chapter-model-owner")
            project = await create_project(owner)
            await create_ai_config(
                owner, name="初始模型", model="initial-model", base_url=f"{url}/v1/", is_default=True,
            )
            second = await create_ai_config(
                owner, name="后来模型", model="changed-model", base_url=f"{url}/v1/", is_default=False,
            )
            chapter = await owner.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})
            queued = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json={})
            assert queued.status_code == 202, queued.text
            changed = await owner.patch(f"/api/v1/ai-configs/{second['id']}", json={"is_default": True})
            assert changed.status_code == 200, changed.text
            await first_app.state.task_engine.start()
            events = await stream_task(owner, queued.json())
            assert events[-1][1]["task"]["status"] == "succeeded", events
            assert len(requests) == 1 and json.loads(requests[0])["model"] == "initial-model"
    finally:
        await first_app.state.task_engine.stop()


@pytest.mark.parametrize("model_type", ["text", "embedding"])
async def test_pg_reused_model_id_does_not_reauthorize_old_task(postgres_chapter_execution_apps, model_type):
    first_app, second_app = postgres_chapter_execution_apps
    first_app.state.settings.allow_private_ai_endpoints = True
    second_app.state.settings.allow_private_ai_endpoints = True
    second_app.state.task_engine.ctx.factory = create_provider
    try:
        async with (
            _server(payload=_sse("openai")) as (url, _received, headers, body, requests),
            _http_clients(first_app) as (owner, _unused),
        ):
            headers.set()
            body.set()
            await register(owner, "pg-chapter-model-aba")
            project = await create_project(owner)
            text_config = await create_ai_config(owner, name="文字模型", model="text-model", base_url=f"{url}/v1/")
            target = text_config if model_type == "text" else await create_ai_config(
                owner, name="向量模型", model="vector-model", model_type="embedding", base_url=f"{url}/v1/",
            )
            chapter = await owner.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节"})
            queued = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json={})
            assert queued.status_code == 202, queued.text
            async with second_app.state.sessionmaker() as db:
                old = await db.get(AIConfig, target["id"])
                fields = {name: getattr(old, name) for name in (
                    "user_id", "name", "provider", "base_url", "api_key_enc", "model", "model_type", "is_default",
                    "thinking_level", "context_window_k", "max_output_k",
                )}
                old_key = old.instance_key
                await db.delete(old)
                await db.commit()
            async with second_app.state.sessionmaker() as db:
                replacement = AIConfig(id=target["id"], **fields)
                db.add(replacement)
                await db.commit()
                assert replacement.instance_key != old_key
            await second_app.state.task_engine.start()
            # 任务由第二实例执行；TaskEngine 按单进程设计，第一实例的订阅收不到他实例的终态广播，
            # 因此以共享数据库的持久终态为准，而不是经第一实例订阅。
            async with asyncio.timeout(15):
                while True:
                    async with second_app.state.sessionmaker() as db:
                        row = await db.get(Task, queued.json()["id"])
                        if row.status not in ("queued", "running") and row.execution_key is None:
                            break
                    await asyncio.sleep(0.05)
            assert row.status == "failed" and row.error == "模型配置已失效，请重新选择", (row.status, row.error)
            assert not requests
    finally:
        await second_app.state.task_engine.stop()


async def test_author_password_change_waits_for_headers_and_blocks_save(postgres_chapter_execution_apps, monkeypatch):
    first_app, second_app = postgres_chapter_execution_apps
    first_app.state.settings.allow_private_ai_endpoints = True
    second_app.state.settings.allow_private_ai_endpoints = True
    second_app.state.task_engine.ctx.factory = create_provider
    original_authorize = ai_dispatch.authorize_send_actor
    pid = asyncio.get_running_loop().create_future()

    async def track_send(db, actor):
        await original_authorize(db, actor)
        if db in ai_dispatch._ACTIVE_SEND_LEASES and not pid.done():
            pid.set_result(await db.scalar(select(func.pg_backend_pid())))

    monkeypatch.setattr(ai_dispatch, "authorize_send_actor", track_send)
    try:
        async with (
            _server(payload=_sse("openai")) as (url, received, headers, body, requests),
            _http_clients(first_app) as (owner, _unused),
        ):
            await register(owner, "pg-chapter-token-owner")
            project = await create_project(owner)
            await create_ai_config(owner, base_url=f"{url}/v1/", model="initial-model")
            chapter = await owner.post(
                f"/api/v1/projects/{project['id']}/chapters", json={"title": "章节", "content": "原始正文"},
            )
            queued = await owner.post(f"/api/v1/chapters/{chapter.json()['id']}/generate", json={})
            assert queued.status_code == 202, queued.text
            task = queued.json()
            await second_app.state.task_engine.start()
            changed = None
            try:
                await asyncio.wait_for(received.wait(), 15)
                running = second_app.state.task_engine._running.get(task["id"])
                assert running is not None
                changed = asyncio.create_task(owner.post("/api/v1/auth/change-password", json={
                    "old_password": "secret123", "new_password": "new-secret123",
                }))
                await asyncio.wait_for(_wait_for_blocker(
                    first_app.state.sessionmaker, await asyncio.wait_for(pid, 10),
                ), 10)
                assert not changed.done()
                headers.set()
                assert (await asyncio.wait_for(changed, 15)).status_code == 204
                body.set()
                await asyncio.wait_for(asyncio.shield(running), 15)
                async with second_app.state.sessionmaker() as db:
                    current = await db.get(Chapter, chapter.json()["id"])
                    original = await db.get(Task, task["id"])
                    assert current.content == "原始正文" and original.status != "succeeded"
                assert len(requests) == 1
            finally:
                headers.set()
                body.set()
                if changed is not None and not changed.done():
                    changed.cancel()
                if changed is not None:
                    with suppress(asyncio.CancelledError):
                        await changed
    finally:
        await second_app.state.task_engine.stop()


async def test_editor_reinvite_after_send_cannot_save_old_result(postgres_chapter_execution_apps, monkeypatch):
    first_app, second_app = postgres_chapter_execution_apps
    first_app.state.settings.allow_private_ai_endpoints = True
    second_app.state.settings.allow_private_ai_endpoints = True
    second_app.state.task_engine.ctx.factory = create_provider
    original_authorize = ai_dispatch.authorize_send_actor
    pid = asyncio.get_running_loop().create_future()

    async def track_send(db, actor):
        await original_authorize(db, actor)
        if db in ai_dispatch._ACTIVE_SEND_LEASES and not pid.done():
            pid.set_result(await db.scalar(select(func.pg_backend_pid())))

    monkeypatch.setattr(ai_dispatch, "authorize_send_actor", track_send)
    try:
        async with (
            _server(payload=_sse("openai")) as (url, received, headers, body, requests),
            _http_clients(first_app) as (owner, _unused),
            _http_clients(second_app) as (_other, editor),
        ):
            project, chapter, editor_user = await _world(owner, editor, url)
            queued = await editor.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
            assert queued.status_code == 202, queued.text
            task = queued.json()
            await second_app.state.task_engine.start()
            revoke = None
            try:
                await asyncio.wait_for(received.wait(), 15)
                running = second_app.state.task_engine._running.get(task["id"])
                assert running is not None
                revoke = asyncio.create_task(owner.delete(
                    f"/api/v1/projects/{project['id']}/collaborators/{editor_user['id']}",
                    params={"expected_revision": 2},
                ))
                await asyncio.wait_for(_wait_for_blocker(
                    first_app.state.sessionmaker, await asyncio.wait_for(pid, 10),
                ), 10)
                assert not revoke.done()
                headers.set()
                assert (await asyncio.wait_for(revoke, 15)).status_code == 204
                invite = await owner.post(f"/api/v1/projects/{project['id']}/collaborators", json={
                    "username": "rewrite-editor", "role": "editor",
                })
                assert invite.status_code == 201, invite.text
                accepted = await editor.post(
                    f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
                    json={"expected_revision": invite.json()["revision"]},
                )
                assert accepted.status_code == 200, accepted.text
                body.set()
                await asyncio.wait_for(asyncio.shield(running), 15)
                async with second_app.state.sessionmaker() as db:
                    current = await db.get(Chapter, chapter["id"])
                    original = await db.get(Task, task["id"])
                    assert current.content == "旧正文示例" and original.status != "succeeded"
                assert len(requests) == 1
            finally:
                headers.set()
                body.set()
                if revoke is not None and not revoke.done():
                    revoke.cancel()
                if revoke is not None:
                    with suppress(asyncio.CancelledError):
                        await revoke
    finally:
        await second_app.state.task_engine.stop()
