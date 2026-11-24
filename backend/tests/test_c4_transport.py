"""真实私有监听、租约换代与单帧确认。"""

import asyncio
import os
import socket
import struct
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import TaskWorker
from app.services.task_engine import peer_protocol as protocol
from app.services.task_engine import peer_transport as transport


def test_record_budget_counts_utf8_escapes_keys_and_punctuation(monkeypatch):
    monkeypatch.setattr(protocol, "CHUNK_BYTES", 3)
    packet = {"文档": ['引号"、换行\n、反斜线\\与非 BMP 字符🙂', {}, [True, None], {12: "段落"}]}
    exact = protocol.canonical(packet)
    assert protocol.bounded_record(packet, len(exact)) == exact
    with pytest.raises(protocol.PeerUnavailable):
        protocol.bounded_record(packet, len(exact) - 1)


async def test_oversized_publication_is_refused_before_whole_frame_encoding(monkeypatch):
    request = {"source": "subscriber", "target": "publisher", "nonce": "subscription"}
    publisher = protocol.PeerPublisher(None, None, b"local-test", request, lambda: True)
    monkeypatch.setattr(protocol, "FRAME_BYTES", 1024)
    canonical = protocol.canonical

    def reject_full_encoding(value):
        assert not isinstance(value, (dict, list, tuple)), "超限数据不应进入整包编码或摘要"
        return canonical(value)

    monkeypatch.setattr(protocol, "canonical", reject_full_encoding)
    for oversized in ["x" * 1024, "\x00" * 210, ["多字节" * 40] * 10]:
        with pytest.raises(protocol.PeerUnavailable):
            await publisher.send({"event": "snapshot", "data": {"text": oversized}, "proof": None})
        assert not publisher._outstanding


@asynccontextmanager
async def listeners(first, second, *, tcp=False):
    engines = [first.state.task_engine, second.state.task_engine]
    try:
        for engine in engines:
            await engine.stop()
            await engine.request_worker.stop()
            if tcp:
                settings = engine.ctx.settings
                settings.task_peer_bind_host = settings.task_peer_advertise_host = "127.0.0.1"
            await engine.peer.start()
            assert engine.peer.url
            await engine.request_worker.start()
        # HTTP-only 第二端也应能转发。
        await engines[0].start()
        yield engines
    finally:
        for engine in engines:
            await engine.peer.stop()


@pytest.mark.parametrize("tcp", [False, True], ids=["uds", "tcp"])
async def test_signed_frames_and_lock_free_ack_recheck(app, second_app, tcp):
    payload = {"task": 21, "round": "example"}
    text = "任务原文" * 65536
    sent = []

    async def serve(request, channel):
        assert request == payload
        for i in range(2):
            frame = {"event": "snapshot", "data": {"text": text}, "proof": {"generation": i}}
            await channel.send(frame)
            sent.append(i)
            if await channel.confirmation() == "recheck":
                await channel.send({**frame, "proof": {"generation": i, "after": "commit"}})
                assert await channel.confirmation() == "ack"

    async with listeners(app, second_app, tcp=tcp) as (a, b):
        a.peer.on_subscribe = serve
        target = await b.peer.target(a.lease.worker_key)
        assert target and not await a.peer.target(a.request_worker.worker_key)
        assert len(await b.peer.targets()) == 1 and len(await b.peer.targets(all_generations=True)) == 2
        async with b.peer.subscribe(target, payload) as client:
            first = await client.receive()
            assert first.event == "snapshot" and first.data == {"text": text}
            assert protocol.is_authenticated_frame(first, ctx=b.ctx)
            assert not protocol.is_authenticated_frame(first, ctx=a.ctx)
            assert first.source_key == a.lease.worker_key and first.target_key == b.request_worker.worker_key
            assert sent == [0]
            await client.recheck()
            assert not protocol.is_authenticated_frame(first)
            confirmed = await client.receive()
            assert confirmed.sequence == first.sequence and confirmed.data == first.data
            assert confirmed.proof["after"] == "commit"
            await client.ack()
            confirmed.request['round'] = 'previous-frame-tamper'
            second = await client.receive()
            assert second.request == payload
            assert second.data["text"] == text and second.sequence == first.sequence + 1
            second.data["text"] = ""
            assert not protocol.is_authenticated_frame(second)
            second.data["text"] = text
            await client.ack()
            assert await client.receive() is None
        assert all(not protocol.is_authenticated_frame(item) for item in (first, confirmed, second))


async def test_idle_disconnect_and_worker_rotation(app, second_app):
    started, exited = asyncio.Event(), asyncio.Event()
    notices = []

    async def hold(request, channel):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            exited.set()

    async def notice(payload):
        notices.append(payload)

    async with listeners(app, second_app) as (a, b):
        a.peer.on_subscribe, a.peer.on_wake = hold, notice
        path = Path(a.peer.url.removeprefix("unix://"))
        assert (os.stat(path).st_mode & 0o777) == 0o600
        assert (os.stat(path.parent).st_mode & 0o777) == 0o700
        original = await b.peer.target(a.lease.worker_key)
        async with b.peer.subscribe(original, {}):
            await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(exited.wait(), 2)
        await a.stop()
        assert await b.peer.target(original.worker_key) is None
        with pytest.raises(protocol.PeerUnavailable):
            await b.peer.wake(original, {"kind": "stale"})
        request = await b.peer.target(a.request_worker.worker_key)
        await b.peer.wake(request, {"kind": "settings"})
        assert notices == [{"kind": "settings"}]
        await a.start()
        updated = await b.peer.target(a.lease.worker_key)
        assert updated.worker_key != original.worker_key and updated.url == original.url
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(TaskWorker.peer_url).where(TaskWorker.worker_key == updated.worker_key)) == (
                a.peer.url
            )
    assert not (await asyncio.to_thread(path.exists))


async def test_unauthenticated_and_replayed_requests_do_not_reach_handler(app, second_app):
    called = []

    async def handle(payload):
        called.append(payload)

    async with listeners(app, second_app) as (a, b):
        a.peer.on_wake = handle
        name = Path(a.peer.url.removeprefix("unix://"))
        data = {"source": b.request_worker.worker_key, "target": a.lease.worker_key, "at": int(time.time()),
                "nonce": str(uuid4()), "op": "wake", "payload": {"project_id": 1}}

        async def exchange(mode, previous=None):
            reader, writer = await asyncio.open_unix_connection(name)
            try:
                hello = await reader.readexactly(len(transport._MAGIC) + 32)
                key = protocol.derive(a.peer._key, hello[-32:])
                request = dict(data)
                if mode == "expired":
                    request["at"] -= 600
                raw = protocol.canonical(request)
                mac = protocol.derive(key if mode != "wrong" else b"not-a-deployment-key", raw)
                packet = struct.pack("!I", len(raw)) + mac + raw
                writer.write(previous if mode == "replay" else packet)
                await writer.drain()
                response = await asyncio.wait_for(reader.read(), 3)
                return packet, response
            finally:
                writer.close()
                await writer.wait_closed()

        assert (await exchange("wrong"))[1] == b""
        assert (await exchange("expired"))[1] == b""
        valid, ready = await exchange("valid")
        assert ready and len(called) == 1
        assert (await exchange("replay", valid))[1] == b"" and len(called) == 1
        assert all("peer" not in path for path in app.openapi()["paths"])


async def test_incomplete_or_replaced_socket_cleanup(app, tmp_path):
    engine = app.state.task_engine
    peer = engine.peer
    engine.ctx.settings.task_peer_socket_dir = str(tmp_path / ("x" * 120))
    await peer.start()
    assert peer.url is None
    engine.ctx.settings.task_peer_socket_dir = None
    await peer.start()
    path = Path(peer.url.removeprefix("unix://"))
    os.unlink(path)
    replacement = socket.socket(socket.AF_UNIX)
    try:
        replacement.bind(os.fspath(path))
        os.chmod(path, 0o600)
        await peer.stop()
        assert (await asyncio.to_thread(path.exists))
    finally:
        replacement.close()
        if (await asyncio.to_thread(path.exists)):
            os.unlink(path)


async def test_retired_socket_requires_owner_record_and_no_live_generation(app, second_app):
    from datetime import UTC, datetime

    from app.services.task_engine import peer_socket

    async with listeners(app, second_app) as (a, _b):
        peer = a.peer
        path = peer._directory / f"{uuid4()}.sock"
        raw = socket.socket(socket.AF_UNIX)
        raw.bind(str(path))
        raw.close()
        os.chmod(path, 0o600)
        peer_socket.save_identity(path, peer._owned_socket(path))
        url = f"unix://{path}"
        async with app.state.sessionmaker.begin() as db:
            retired = TaskWorker(worker_key=str(uuid4()), peer_url=url, hostname="old", pid=1,
                                 stopped_at=datetime.now(UTC))
            active = TaskWorker(worker_key=str(uuid4()), peer_url=url, hostname="old", pid=1)
            db.add_all([retired, active])
            active_id = active.worker_key
        await peer._prune_once()
        assert await asyncio.to_thread(path.exists)
        async with app.state.sessionmaker.begin() as db:
            live = await db.get(TaskWorker, active_id)
            live.fenced_at = datetime.now(UTC)
        await peer._prune_once()
        assert not await asyncio.to_thread(path.exists)
        assert not await asyncio.to_thread(path.with_suffix(".owner").exists)


async def test_unavailable_uds_platform_does_not_prevent_http_or_tcp(app, monkeypatch):
    engine = app.state.task_engine
    monkeypatch.delattr(transport.os, "getuid")
    peer = transport.PeerTransport(engine)
    await peer.start()
    assert peer.url is None
    engine.ctx.settings.task_peer_bind_host = engine.ctx.settings.task_peer_advertise_host = "127.0.0.1"
    try:
        await peer.start()
        assert peer.url.startswith("tcp://127.0.0.1:")
    finally:
        await peer.stop()


async def test_marker_write_failure_removes_bound_socket(app, monkeypatch):
    from app.services.task_engine import peer_socket

    observed = []

    def fail(path, _identity):
        observed.append(path)
        raise OSError("no space")

    monkeypatch.setattr(peer_socket, "save_identity", fail)
    peer = app.state.task_engine.peer
    await peer.start()
    assert len(observed) == 1 and peer.url is None
    assert not await asyncio.to_thread(observed[0].exists)


async def test_private_codec_alone_cannot_issue_a_received_frame():
    a, b = socket.socketpair()
    _, writer = await asyncio.open_connection(sock=a)
    reader, other = await asyncio.open_connection(sock=b)
    key = b"local-test-key"
    request = {"source": "own", "target": "another", "nonce": "random", "payload": {"binding": {}}}
    try:
        pub = protocol.PeerPublisher(writer, asyncio.Queue(), key, request, lambda: True)
        sub = protocol.PeerSubscriber(reader, other, key, request, lambda: True)
        await pub.send({"event": "delta", "data": {"text": "local-only"}, "proof": {}})
        frame = await sub.receive()
        assert not protocol.is_authenticated_frame(frame)
        with pytest.raises(protocol.PeerUnavailable):
            await sub.ack()
    finally:
        writer.close()
        other.close()
        await asyncio.gather(writer.wait_closed(), other.wait_closed())


async def test_restart_allocates_new_address_and_old_target_is_not_an_alias(app, second_app):
    async with listeners(app, second_app) as (a, b):
        old = await b.peer.target(a.lease.worker_key)
        await a.peer.stop()
        await a.stop()
        await a.request_worker.stop()
        await a.peer.start()
        await a.request_worker.start()
        await a.start()
        current = await b.peer.target(a.lease.worker_key)
        assert old.url != current.url and old.worker_key != current.worker_key
        assert await b.peer.target(old.worker_key) is None
        with pytest.raises(protocol.PeerUnavailable):
            await b.peer.wake(old, {})
