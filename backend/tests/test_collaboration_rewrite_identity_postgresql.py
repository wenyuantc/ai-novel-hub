"""同步改写在 PostgreSQL 的真实 HTTP 发送与撤权竞争。"""

import asyncio
import socket
from contextlib import asynccontextmanager, suppress

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select, text

from app.ai.providers import create_provider
from app.models import GenerationLog
from app.services import ai_dispatch, rewrite_service
from tests.helpers import parse_sse, wait_until
from tests.test_ai_visibility_dispatch import _server, _sse
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_rewrite_identity import _http_clients, _world
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps as _postgres_fixture

postgres_rewrite_apps = _postgres_fixture


async def _assert_no_idle_output_transaction(maker, backend_pid):
    async with maker() as probe:
        state = await probe.scalar(text("SELECT state FROM pg_stat_activity WHERE pid = :pid"), {"pid": backend_pid})
        assert state not in ("idle in transaction", "idle in transaction (aborted)")


@asynccontextmanager
async def _delayed_editor(app, entered, release):
    """在真正的 ASGI send 前暂停第一帧；传输本身仍由本机 TCP 客户端完成。"""
    app.state.provider_factory = create_provider

    async def wrapped(scope, receive, send):
        async def delayed_send(message):
            if message["type"] == "http.response.body" and b"event: delta" in message.get("body", b""):
                entered.set()
                await release.wait()
            await send(message)

        await app(scope, receive, delayed_send)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(wrapped, lifespan="off", access_log=False, log_level="warning"))
    runner = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await wait_until(lambda: server.started, within=10)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15) as client:
            yield client
    finally:
        release.set()
        server.should_exit = True
        await asyncio.wait_for(runner, 10)


async def test_rewrite_real_send_lease_and_revoke_after_headers(postgres_rewrite_apps, monkeypatch):
    first_app, second_app = postgres_rewrite_apps
    first_app.state.settings.allow_private_ai_endpoints = True
    second_app.state.settings.allow_private_ai_endpoints = True
    pid = asyncio.get_running_loop().create_future()
    original_authorize = ai_dispatch.authorize_send_actor

    async def track_send(db, actor):
        await original_authorize(db, actor)
        if db in ai_dispatch._ACTIVE_SEND_LEASES and not pid.done():
            pid.set_result(await db.scalar(select(func.pg_backend_pid())))

    monkeypatch.setattr(ai_dispatch, "authorize_send_actor", track_send)
    async with (
        _server(payload=_sse("openai")) as (url, received, headers, body, requests),
        _http_clients(first_app) as (owner, _unused),
        _http_clients(second_app) as (_other, editor),
    ):
        project, chapter, editor_user = await _world(owner, editor, url)
        pending = asyncio.create_task(editor.post(
            f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
        ))
        revoke = None
        try:
            await asyncio.wait_for(received.wait(), 15)
            backend_pid = await asyncio.wait_for(pid, 15)
            revoke = asyncio.create_task(owner.delete(
                f"/api/v1/projects/{project['id']}/collaborators/{editor_user['id']}",
                params={"expected_revision": 2},
            ))
            await asyncio.wait_for(_wait_for_blocker(first_app.state.sessionmaker, backend_pid), 15)
            assert not revoke.done()
            headers.set()
            revoked = await asyncio.wait_for(revoke, 15)
            assert revoked.status_code == 204, revoked.text
            assert not pending.done()
            body.set()
            response = await asyncio.wait_for(pending, 15)
            events = parse_sse(response.text)
            assert response.status_code == 200 and events[-1][0] == "error", response.text
            assert not [event for event, _ in events if event in {"delta", "done"}]
            assert len(requests) == 1
        finally:
            headers.set()
            body.set()
            for task in (pending, revoke):
                if task is not None and not task.done():
                    task.cancel()
            for task in (pending, revoke):
                if task is not None:
                    with suppress(asyncio.CancelledError):
                        await task


async def test_revoke_before_send_yields_zero_model_requests(postgres_rewrite_apps, monkeypatch):
    first_app, second_app = postgres_rewrite_apps
    first_app.state.settings.allow_private_ai_endpoints = True
    second_app.state.settings.allow_private_ai_endpoints = True
    reached, release = asyncio.Event(), asyncio.Event()
    original = rewrite_service.run_rewrite_job

    async def before_send(job, sessionmaker):
        reached.set()
        await release.wait()
        async for event in original(job, sessionmaker):
            yield event

    monkeypatch.setattr(rewrite_service, "run_rewrite_job", before_send)
    async with (
        _server(payload=_sse("openai")) as (url, _received, headers, body, requests),
        _http_clients(first_app) as (owner, _unused),
        _http_clients(second_app) as (_other, editor),
    ):
        headers.set()
        body.set()
        project, chapter, editor_user = await _world(owner, editor, url)
        pending = asyncio.create_task(editor.post(
            f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
        ))
        try:
            await asyncio.wait_for(reached.wait(), 15)
            revoked = await owner.delete(
                f"/api/v1/projects/{project['id']}/collaborators/{editor_user['id']}",
                params={"expected_revision": 2},
            )
            assert revoked.status_code == 204, revoked.text
            release.set()
            response = await asyncio.wait_for(pending, 15)
            events = parse_sse(response.text)
            assert response.status_code == 200 and events[-1][0] == "error", response.text
            assert not [event for event, _ in events if event in {"delta", "done"}]
            assert not requests
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending


async def test_reinvite_after_preview_cannot_authorize_old_apply(postgres_rewrite_apps, monkeypatch):
    first_app, second_app = postgres_rewrite_apps
    first_app.state.settings.allow_private_ai_endpoints = True
    second_app.state.settings.allow_private_ai_endpoints = True
    reached, release = asyncio.Event(), asyncio.Event()
    original = rewrite_service._ai_project_chapter

    async def before_lock(*args, **kwargs):
        if kwargs.get("visibility_write"):
            reached.set()
            await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(rewrite_service, "_ai_project_chapter", before_lock)
    async with (
        _server(payload=_sse("openai")) as (url, _received, headers, body, _requests),
        _http_clients(first_app) as (owner, _unused),
        _http_clients(second_app) as (_other, editor),
    ):
        headers.set()
        body.set()
        project, chapter, editor_user = await _world(owner, editor, url)
        preview = await editor.post(
            f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
        )
        assert preview.status_code == 200 and parse_sse(preview.text)[-1][0] == "done", preview.text
        log_id = parse_sse(preview.text)[-1][1]["log_id"]
        base = f"/api/v1/projects/{project['id']}"
        pending = asyncio.create_task(editor.post(f"{base}/chapters/{chapter['id']}/rewrite/apply", json={
            "start": 0, "end": 2, "original": "旧正", "replacement": "非法覆盖", "log_id": log_id,
        }))
        try:
            await asyncio.wait_for(reached.wait(), 15)
            revoked = await owner.delete(f"{base}/collaborators/{editor_user['id']}", params={"expected_revision": 2})
            assert revoked.status_code == 204, revoked.text
            invited = await owner.post(f"{base}/collaborators", json={"username": "rewrite-editor", "role": "editor"})
            assert invited.status_code == 201, invited.text
            accepted = await editor.post(
                f"/api/v1/collaboration/invitations/project/{invited.json()['invitation_id']}/accept",
                json={"expected_revision": invited.json()["revision"]},
            )
            assert accepted.status_code == 200, accepted.text
            release.set()
            result = await asyncio.wait_for(pending, 15)
            assert result.status_code in (403, 409), result.text
            assert (await owner.get(f"{base}/chapters/{chapter['id']}")).json()["content"] == "旧正文示例"
            async with first_app.state.sessionmaker() as db:
                assert (await db.get(GenerationLog, log_id)).actor_user_id == editor_user["id"]
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending


@pytest.mark.parametrize("slow_client", [False, True])
async def test_output_frame_holds_auth_until_send_or_timeout(postgres_rewrite_apps, monkeypatch, slow_client):
    first_app, second_app = postgres_rewrite_apps
    first_app.state.settings.allow_private_ai_endpoints = True
    second_app.state.settings.allow_private_ai_endpoints = True
    monkeypatch.setattr(rewrite_service, "OUTPUT_LEASE_SECONDS", 5.0 if slow_client else 15.0)
    entered, release = asyncio.Event(), asyncio.Event()
    pid = asyncio.get_running_loop().create_future()
    original = rewrite_service._stream_scope

    async def track_output(db, job):
        await original(db, job)
        if not pid.done():
            pid.set_result(await db.scalar(select(func.pg_backend_pid())))

    monkeypatch.setattr(rewrite_service, "_stream_scope", track_output)
    async with (
        _server(payload=_sse("openai")) as (url, _received, headers, body, requests),
        _http_clients(first_app) as (owner, _unused),
        _delayed_editor(second_app, entered, release) as editor,
    ):
        headers.set()
        body.set()
        project, chapter, editor_user = await _world(owner, editor, url)
        pending = asyncio.create_task(editor.post(
            f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
        ))
        revoke = None
        try:
            await asyncio.wait_for(entered.wait(), 15)
            backend_pid = await asyncio.wait_for(pid, 15)
            revoke = asyncio.create_task(owner.delete(
                f"/api/v1/projects/{project['id']}/collaborators/{editor_user['id']}",
                params={"expected_revision": 2},
            ))
            await asyncio.wait_for(_wait_for_blocker(first_app.state.sessionmaker, backend_pid), 4)
            assert not revoke.done()
            if not slow_client:
                release.set()
            revoked = await asyncio.wait_for(revoke, 10)
            assert revoked.status_code == 204, revoked.text
            if slow_client:
                # 发送被取消；客户端可以观察到提前断流，但锁必须先释放。
                with suppress(httpx.HTTPError):
                    await asyncio.wait_for(pending, 10)
                await _assert_no_idle_output_transaction(first_app.state.sessionmaker, backend_pid)
            else:
                response = await asyncio.wait_for(pending, 10)
                events = parse_sse(response.text)
                assert any(event == "delta" for event, _ in events), response.text
            assert len(requests) == 1
        finally:
            release.set()
            for task in (pending, revoke):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (pending, revoke) if task is not None), return_exceptions=True)


async def test_disconnected_output_closes_transaction_before_revocation(postgres_rewrite_apps, monkeypatch):
    first_app, second_app = postgres_rewrite_apps
    first_app.state.settings.allow_private_ai_endpoints = True
    second_app.state.settings.allow_private_ai_endpoints = True
    monkeypatch.setattr(rewrite_service, "OUTPUT_LEASE_SECONDS", 15.0)
    entered, release = asyncio.Event(), asyncio.Event()
    pid = asyncio.get_running_loop().create_future()
    original = rewrite_service._stream_scope

    async def track_output(db, job):
        await original(db, job)
        if not pid.done():
            pid.set_result(await db.scalar(select(func.pg_backend_pid())))

    monkeypatch.setattr(rewrite_service, "_stream_scope", track_output)
    async with (
        _server(payload=_sse("openai")) as (url, _received, headers, body, requests),
        _http_clients(first_app) as (owner, _unused),
        _delayed_editor(second_app, entered, release) as editor,
    ):
        headers.set()
        body.set()
        project, chapter, editor_user = await _world(owner, editor, url)
        pending = asyncio.create_task(editor.post(
            f"/api/v1/chapters/{chapter['id']}/rewrite", json={"op": "rewrite", "start": 0, "end": 2},
        ))
        try:
            await asyncio.wait_for(entered.wait(), 15)
            backend_pid = await asyncio.wait_for(pid, 15)
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            revoke = await asyncio.wait_for(owner.delete(
                f"/api/v1/projects/{project['id']}/collaborators/{editor_user['id']}",
                params={"expected_revision": 2},
            ), 8)
            assert revoke.status_code == 204, revoke.text
            await _assert_no_idle_output_transaction(first_app.state.sessionmaker, backend_pid)
            assert len(requests) == 1
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
