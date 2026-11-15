"""任务逐帧租约经真实 PostgreSQL、ASGI send 与本机 TCP 验证。"""

import asyncio
import socket
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
import uvicorn
from sqlalchemy import func, select

from app.models import Task
from app.services.task_engine import stream_evidence, stream_lease
from app.services.task_engine.stream_lease import task_sse_response
from tests.helpers import create_ai_config, wait_until
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_collaboration_rewrite_identity_postgresql import _assert_no_idle_output_transaction
from tests.test_collaboration_task_postgresql import postgres_collaboration_apps as _postgres_fixture
from tests.test_collaboration_task_privacy import _setup, _tasks
from tests.test_task_sse_scope import _binding

postgres_task_sse_apps = _postgres_fixture


@asynccontextmanager
async def _tcp_server(app, entered, release):
    async def wrapped(scope, receive, send):
        async def delayed(message):
            if scope.get("path") == "/_test/task-frame" and b"event: snapshot" in message.get("body", b""):
                entered.set()
                await release.wait()
            await send(message)

        await app(scope, receive, delayed)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    server = uvicorn.Server(uvicorn.Config(wrapped, lifespan="off", access_log=False, log_level="warning"))
    runner = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await wait_until(lambda: server.started, within=10)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=10) as client:
            yield client
    finally:
        release.set()
        server.should_exit = True
        await asyncio.wait_for(runner, 10)
        sock.close()


async def _world(owner_app, guest_app, owner, guest):
    owner_user, editor, project, member = await _setup(guest_app, owner, guest)
    tasks = await _tasks(owner_app, owner_user["id"], editor["id"], project["id"],
                         member["invitation_id"], member["revision"])
    binding = await _binding(guest_app, editor["id"], *tasks["valid"])
    return editor, project, member, binding


def _add_stream(app, binding, backend_pid, next_frame, monkeypatch, *, before_lease=None, frame_timeout=5):
    async def verify(db, _task, _project, _event, _data, evidence):
        if not backend_pid.done():
            backend_pid.set_result(await db.scalar(select(func.pg_backend_pid())))
        # 只用于证明锁顺序；实际引擎的逐帧凭据需单独端到端验证。
        return evidence == "原帧证明"

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", verify)
    if before_lease is not None:
        original = stream_lease._task_frame_lease

        @asynccontextmanager
        async def delayed(*args):
            before_lease[0].set()
            await before_lease[1].wait()
            async with original(*args) as scope:
                yield scope

        monkeypatch.setattr(stream_lease, "_task_frame_lease", delayed)

    async def events():
        yield "snapshot", {"text": "撤权前的快照"}, "原帧证明"
        await next_frame.wait()
        yield "delta", {"text": "撤权后不得发送"}, "原帧证明"

    async def endpoint():
        return task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding,
                                 frame_timeout=frame_timeout)

    app.add_api_route("/_test/task-frame", endpoint, methods=["GET"])


async def test_send_holds_postgres_lease_until_revoke_then_blocks_next_frame(postgres_task_sse_apps, monkeypatch):
    owner_app, guest_app = postgres_task_sse_apps
    entered, release, next_frame = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pid = asyncio.get_running_loop().create_future()
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        editor, project, member, binding = await _world(owner_app, guest_app, owner, guest)
        _add_stream(guest_app, binding, pid, next_frame, monkeypatch)
        async with _tcp_server(guest_app, entered, release) as tcp:
            pending = asyncio.create_task(tcp.get("/_test/task-frame"))
            revoke = None
            try:
                await asyncio.wait_for(entered.wait(), 10)
                backend_pid = await asyncio.wait_for(pid, 5)
                revoke = asyncio.create_task(owner.delete(
                    f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}",
                    params={"expected_revision": member["revision"]},
                ))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, backend_pid), 10)
                assert not revoke.done()
                release.set()
                assert (await asyncio.wait_for(revoke, 10)).status_code == 204
                next_frame.set()
                response = await asyncio.wait_for(pending, 10)
                assert "撤权前的快照" in response.text
                assert "撤权后不得发送" not in response.text and "permission_denied" in response.text
                await _assert_no_idle_output_transaction(owner_app.state.sessionmaker, backend_pid)
            finally:
                release.set()
                next_frame.set()
                for item in (pending, revoke):
                    if item is not None and not item.done():
                        item.cancel()
                await asyncio.gather(*(item for item in (pending, revoke) if item is not None), return_exceptions=True)


@pytest.mark.parametrize("change", ["revoke", "reinvite", "reuse_id"])
async def test_old_postgres_snapshot_not_sent_after_identity_changes(postgres_task_sse_apps, monkeypatch, change):
    owner_app, guest_app = postgres_task_sse_apps
    before, allow, entered, release, next_frame = (asyncio.Event() for _ in range(5))
    pid = asyncio.get_running_loop().create_future()
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        editor, project, member, binding = await _world(owner_app, guest_app, owner, guest)
        _add_stream(guest_app, binding, pid, next_frame, monkeypatch, before_lease=(before, allow))
        async with _tcp_server(guest_app, entered, release) as tcp:
            pending = asyncio.create_task(tcp.get("/_test/task-frame"))
            try:
                await asyncio.wait_for(before.wait(), 10)
                if change == "reuse_id":
                    async with owner_app.state.sessionmaker() as db:
                        original = await db.get(Task, binding.task_id)
                        saved_input, saved_queue = original.input, original.queued_at
                        await db.delete(original)
                        await db.flush()
                        db.add(Task(id=binding.task_id, statistics_key=str(uuid4()), user_id=editor["id"],
                                    project_id=project["id"], kind="chapter.generate", status="running",
                                    input=saved_input, queued_at=saved_queue))
                        await db.commit()
                else:
                    base = f"/api/v1/projects/{project['id']}"
                    assert (await owner.delete(f"{base}/collaborators/{editor['id']}", params={
                        "expected_revision": member["revision"],
                    })).status_code == 204
                    if change == "reinvite":
                        invitation = (await owner.post(f"{base}/collaborators", json={
                            "username": "task-guest", "role": "editor",
                        })).json()
                        assert (await guest.post(
                            f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
                            json={"expected_revision": invitation["revision"]},
                        )).status_code == 200
                allow.set()
                response = await asyncio.wait_for(pending, 10)
                assert "permission_denied" in response.text and "撤权前的快照" not in response.text
                assert not entered.is_set()
            finally:
                allow.set()
                release.set()
                next_frame.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def test_postgres_slow_send_timeout_releases_all_locks(postgres_task_sse_apps, monkeypatch):
    owner_app, guest_app = postgres_task_sse_apps
    entered, release, next_frame = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pid = asyncio.get_running_loop().create_future()
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        editor, project, member, binding = await _world(owner_app, guest_app, owner, guest)
        _add_stream(guest_app, binding, pid, next_frame, monkeypatch, frame_timeout=1)
        async with _tcp_server(guest_app, entered, release) as tcp:
            pending = asyncio.create_task(tcp.get("/_test/task-frame"))
            revoke = None
            try:
                await asyncio.wait_for(entered.wait(), 10)
                backend_pid = await asyncio.wait_for(pid, 5)
                revoke = asyncio.create_task(owner.delete(
                    f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}",
                    params={"expected_revision": member["revision"]},
                ))
                assert (await asyncio.wait_for(revoke, 8)).status_code == 204
                await _assert_no_idle_output_transaction(owner_app.state.sessionmaker, backend_pid)
            finally:
                release.set()
                next_frame.set()
                for item in (pending, revoke):
                    if item is not None and not item.done():
                        item.cancel()
                await asyncio.gather(*(item for item in (pending, revoke) if item is not None), return_exceptions=True)


async def test_postgres_client_disconnect_releases_frame_locks(postgres_task_sse_apps, monkeypatch):
    owner_app, guest_app = postgres_task_sse_apps
    entered, release, next_frame = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pid = asyncio.get_running_loop().create_future()
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        editor, project, member, binding = await _world(owner_app, guest_app, owner, guest)
        _add_stream(guest_app, binding, pid, next_frame, monkeypatch)
        async with _tcp_server(guest_app, entered, release) as tcp:
            revoke = None
            try:
                async with tcp.stream("GET", "/_test/task-frame") as response:
                    assert response.status_code == 200
                    await asyncio.wait_for(entered.wait(), 10)
                    backend_pid = await asyncio.wait_for(pid, 5)
                    revoke = asyncio.create_task(owner.delete(
                        f"/api/v1/projects/{project['id']}/collaborators/{editor['id']}",
                        params={"expected_revision": member["revision"]},
                    ))
                    await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, backend_pid), 10)
                    await response.aclose()
                    release.set()
                assert (await asyncio.wait_for(revoke, 8)).status_code == 204
                await _assert_no_idle_output_transaction(owner_app.state.sessionmaker, backend_pid)
            finally:
                release.set()
                next_frame.set()
                if revoke is not None and not revoke.done():
                    revoke.cancel()
                if revoke is not None:
                    await asyncio.gather(revoke, return_exceptions=True)


async def test_postgres_model_config_update_waits_for_frame_send(postgres_task_sse_apps, monkeypatch):
    owner_app, guest_app = postgres_task_sse_apps
    entered, release, next_frame = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pid = asyncio.get_running_loop().create_future()
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        _editor, _project, _member, binding = await _world(owner_app, guest_app, owner, guest)
        config = await create_ai_config(owner)
        _add_stream(guest_app, binding, pid, next_frame, monkeypatch)
        async with _tcp_server(guest_app, entered, release) as tcp:
            pending = asyncio.create_task(tcp.get("/_test/task-frame"))
            update = None
            try:
                await asyncio.wait_for(entered.wait(), 10)
                backend_pid = await asyncio.wait_for(pid, 5)
                update = asyncio.create_task(owner.patch(
                    f"/api/v1/ai-configs/{config['id']}", json={"model": "changed"},
                ))
                await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, backend_pid), 10)
                assert not update.done()
                release.set()
                assert (await asyncio.wait_for(update, 10)).status_code == 200
                await _assert_no_idle_output_transaction(owner_app.state.sessionmaker, backend_pid)
            finally:
                release.set()
                next_frame.set()
                for item in (pending, update):
                    if item is not None and not item.done():
                        item.cancel()
                await asyncio.gather(*(item for item in (pending, update) if item is not None), return_exceptions=True)
