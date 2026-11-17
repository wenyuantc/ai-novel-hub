"""任务帧的授权租约覆盖实际 ASGI send，不序列化内部来源。"""

import asyncio
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn

from app.models import Task, User
from app.services import task_service
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine import stream_evidence, stream_lease
from app.services.task_engine.stream_lease import (
    TaskStreamLeaseError,
    _capture_stream_binding,
    require_task_stream_physical_lease,
    task_sse_response,
)
from tests.helpers import register, wait_until
from tests.stream_helpers import mount_stream_response


async def _binding(app, client):
    user = await register(client, "frame-owner")
    actor = AuthenticatedActor(user["id"], 0)
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user["id"], project_id=None, kind="book.import", status="succeeded")
        db.add(task)
        await db.commit()
        task_id, key = task.id, task.statistics_key
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = actor
        task = await task_service.get_owned_task(db, actor.id, task_id, expected_task_key=key)
        return await _capture_stream_binding(db, task, actor)


async def test_task_frames_guard_every_payload_but_not_heartbeat(app, client, monkeypatch):
    binding = await _binding(app, client)
    entered, sent = [], []
    secret = object()

    async def verify(db, _task, _project, event, data, evidence):
        await require_task_stream_physical_lease(db, binding.actor.id, None, task_id=binding.task_id,
                                                 task_key=binding.task_key)
        entered.append((event, data, evidence, asyncio.current_task()))
        return True

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", verify)

    async def events():
        for event in ("snapshot", "delta", "progress", "reset", "proposal", "paused", "done", "error"):
            yield event, {"value": event}, secret
        yield "", {}, secret

    async def send(message):
        sent.append((message, asyncio.current_task()))

    await task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding).stream_response(send)
    assert [item[0] for item in entered] == [
        "snapshot", "delta", "progress", "reset", "proposal", "paused", "done", "error",
    ]
    assert all(item[2] is secret for item in entered)
    assert all(task is entered[0][3] for _, task in sent)
    assert b": ping\n\n" in [item[0].get("body", b"") for item in sent]
    assert all(repr(secret).encode() not in item[0].get("body", b"") for item in sent)


async def test_task_frame_denial_emits_only_fixed_error_and_closes(app, client, monkeypatch):
    binding = await _binding(app, client)
    closed, sent = asyncio.Event(), []

    async def denied(*_args):
        raise RuntimeError("private SQL and token")

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", denied)

    async def events():
        try:
            yield "snapshot", {"text": "私人正文"}, object()
        finally:
            closed.set()

    async def send(message):
        sent.append(message)

    await task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding).stream_response(send)
    body = b"".join(item.get("body", b"") for item in sent)
    assert closed.is_set() and b"permission_denied" in body
    assert "私人正文".encode() not in body and b"private SQL" not in body


async def test_only_fixed_empty_task_error_may_bypass_lease(app, client, monkeypatch):
    binding = await _binding(app, client)
    entered, sent = [], []

    async def verify(*_args):
        entered.append(True)
        return True

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", verify)

    async def events():
        yield "error", {"task": None, "detail": "任务不存在"}, None
        yield "delta", {"text": "不得继续发送"}, object()

    async def send(message):
        sent.append(message)

    await task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding).stream_response(send)
    body = b"".join(item.get("body", b"") for item in sent)
    assert "任务不存在".encode() in body and "不得继续发送".encode() not in body
    assert not entered


async def test_task_frame_timeout_covers_wait_for_lease(app, client, monkeypatch):
    binding = await _binding(app, client)
    released, closed, never = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sent = []
    original = stream_lease._task_frame_lease

    @asynccontextmanager
    async def blocked(*args):
        try:
            await never.wait()
            async with original(*args) as scope:
                yield scope
        finally:
            released.set()

    monkeypatch.setattr(stream_lease, "_task_frame_lease", blocked)

    async def events():
        try:
            yield "snapshot", {"text": "不得输出"}, object()
        finally:
            closed.set()

    async def send(message):
        sent.append(message)

    await task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding,
                            frame_timeout=0.05).stream_response(send)
    assert released.is_set() and closed.is_set()
    assert all("不得输出".encode() not in item.get("body", b"") for item in sent)


async def test_terminal_receipt_wait_releases_locks_before_revalidating_same_frame(app, client, monkeypatch):
    binding = await _binding(app, client)
    waiting, release = asyncio.Event(), asyncio.Event()
    calls, sent = [], []
    evidence = object()

    class Pending(Exception):
        async def wait(self):
            waiting.set()
            await release.wait()

    async def verify(_db, _task, _project, event, data, proof):
        calls.append((event, data.copy(), proof))
        if len(calls) == 1:
            raise Pending()
        return True

    monkeypatch.setattr(stream_evidence, "TerminalEvidencePending", Pending, raising=False)
    monkeypatch.setattr(stream_evidence, "verify_stream_frame", verify)

    async def events():
        yield "delta", {"text": "本帧正文"}, evidence

    async def send(message):
        sent.append(message)

    response = task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding)
    running = asyncio.create_task(response.stream_response(send))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        # 等待收据时不能继续持有 User 行锁或 SQLite 写事务。
        async def update_user():
            async with app.state.sessionmaker() as db:
                user = await db.get(User, binding.actor.id)
                user.username = "frame-owner-renamed"
                await db.commit()

        await asyncio.wait_for(update_user(), 2)
        release.set()
        await asyncio.wait_for(running, 5)
        body = b"".join(item.get("body", b"") for item in sent)
        assert body.count("本帧正文".encode()) == 1
        assert len(calls) == 2 and calls[0] == calls[1] and calls[0][2] is evidence
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_terminal_receipt_wait_times_out_without_sending_or_holding_locks(app, client, monkeypatch):
    binding = await _binding(app, client)
    waiting, never, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sent = []

    class Pending(Exception):
        async def wait(self):
            waiting.set()
            await never.wait()

    async def verify(*_args):
        raise Pending()

    monkeypatch.setattr(stream_evidence, "TerminalEvidencePending", Pending, raising=False)
    monkeypatch.setattr(stream_evidence, "verify_stream_frame", verify)

    async def events():
        try:
            yield "delta", {"text": "不得输出"}, object()
        finally:
            closed.set()

    async def send(message):
        sent.append(message)

    response = task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding, frame_timeout=0.2)
    await asyncio.wait_for(response.stream_response(send), 5)
    assert waiting.is_set() and closed.is_set()
    assert all("不得输出".encode() not in item.get("body", b"") for item in sent)
    async with app.state.sessionmaker() as db:
        user = await db.get(User, binding.actor.id)
        user.username = "frame-owner-after-timeout"
        await db.commit()


@pytest.mark.parametrize("termination", ["timeout", "cancel", "disconnect"])
async def test_task_frame_slow_or_disconnected_send_releases_lease(app, client, monkeypatch, termination):
    binding = await _binding(app, client)
    entered, closed, never = asyncio.Event(), asyncio.Event(), asyncio.Event()
    sent, saved = [], []

    async def verify(db, *_args):
        saved.append(db)
        return True

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", verify)

    async def events():
        try:
            yield "snapshot", {"text": "private"}, object()
        finally:
            closed.set()

    async def send(message):
        sent.append(message)
        if b"snapshot" in message.get("body", b""):
            entered.set()
            if termination == "disconnect":
                raise OSError("sensitive client details")
            await never.wait()

    response = task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding,
                                 frame_timeout=0.05 if termination == "timeout" else 2)
    running = asyncio.create_task(response.stream_response(send))
    await asyncio.wait_for(entered.wait(), 5)
    if termination == "cancel":
        running.cancel()
    result = await asyncio.gather(running, return_exceptions=True)
    assert closed.is_set()
    with pytest.raises(TaskStreamLeaseError):
        await require_task_stream_physical_lease(saved[0], binding.actor.id, None, task_id=binding.task_id,
                                                 task_key=binding.task_key)
    assert len([item for item in sent if b"snapshot" in item.get("body", b"")]) == 1
    assert all(b"permission_denied" not in item.get("body", b"") for item in sent)
    if termination == "disconnect":
        assert isinstance(result[0], OSError) and "sensitive" not in str(result[0])


async def test_task_frame_lease_covers_real_asgi_send_and_tcp_client(app, client, monkeypatch):
    binding = await _binding(app, client)
    entered, release, verified = asyncio.Event(), asyncio.Event(), []

    async def verify(db, *_args):
        await require_task_stream_physical_lease(db, binding.actor.id, None, task_id=binding.task_id,
                                                 task_key=binding.task_key)
        verified.append(db)
        return True

    monkeypatch.setattr(stream_evidence, "verify_stream_frame", verify)

    async def events():
        yield "snapshot", {"text": "安全内容"}, object()

    async def endpoint():
        return task_sse_response(events(), sessionmaker=app.state.sessionmaker, binding=binding)

    mount_stream_response(app, "/_test/frame-tcp", endpoint)

    async def wrapped(scope, receive, send):
        async def delayed(message):
            if b"event: snapshot" in message.get("body", b""):
                entered.set()
                await release.wait()
            await send(message)

        await app(scope, receive, delayed)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    server = uvicorn.Server(uvicorn.Config(wrapped, lifespan="off", access_log=False, log_level="warning"))
    runner = asyncio.create_task(server.serve(sockets=[sock]))
    pending = None
    try:
        await wait_until(lambda: server.started, within=10)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}") as tcp:
            pending = asyncio.create_task(tcp.get("/_test/frame-tcp"))
            await asyncio.wait_for(entered.wait(), 5)
            assert verified
            release.set()
            response = await asyncio.wait_for(pending, 5)
            assert response.status_code == 200 and "安全内容" in response.text
            with pytest.raises(TaskStreamLeaseError):
                await require_task_stream_physical_lease(verified[0], binding.actor.id, None, task_id=binding.task_id,
                                                         task_key=binding.task_key)
    finally:
        release.set()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(runner, 10)
