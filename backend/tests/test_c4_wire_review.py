"""C4 的真实连接边界，以及 WAL 下连续大帧的公开交付。"""

import asyncio
import os
import socket
from contextlib import AsyncExitStack

import httpx
import pytest

from app.services.task_engine import peer_protocol as protocol
from app.services.task_engine import peer_socket
from app.services.task_engine import peer_transport as transport
from tests.helpers import Gate, wait_until
from tests.test_c4_multiapp_streams import accounts, chapter, public_pair, start_generation, terminal, visible
from tests.test_c4_transport import listeners
from tests.test_collaboration_stream_agent import _ProdStream
from tests.test_collaboration_stream_running_e5c1 import _serve


def test_partial_owner_record_failure_preserves_only_foreign_replacement(tmp_path, monkeypatch):
    original_open = os.fdopen
    path = tmp_path / "endpoint.sock"
    marker = path.with_suffix(".owner")
    replace = False

    class PartialWrite:
        def __init__(self, fd, mode):
            self.file = original_open(fd, mode)

        def __enter__(self):
            return self

        def write(self, value):
            self.file.write(value[:8])
            self.file.flush()
            if replace:
                marker.unlink()
                marker.write_bytes(b"another generation")
            raise OSError("write interrupted")

        def __exit__(self, *_):
            self.file.close()

    monkeypatch.setattr(peer_socket.os, "fdopen", PartialWrite)
    with pytest.raises(OSError, match="write interrupted"):
        peer_socket.save_identity(path, (1, 2))
    assert not marker.exists()
    replace = True
    with pytest.raises(OSError, match="write interrupted"):
        peer_socket.save_identity(path, (1, 2))
    assert marker.read_bytes() == b"another generation"


async def test_shutdown_before_first_handler_step_closes_accepted_writer(app):
    engine = app.state.task_engine
    engine.ctx.settings.task_peer_bind_host = engine.ctx.settings.task_peer_advertise_host = "127.0.0.1"
    peer = engine.peer
    await peer.start()
    client_socket, accepted_socket = socket.socketpair()
    client_socket.setblocking(False)
    reader, writer = await asyncio.open_connection(sock=accepted_socket)
    try:
        # accept 同步持有资源后、处理协程尚未运行就进入 shutdown。
        peer._accept(reader, writer)
        await peer.stop()
        assert writer.is_closing() and not peer._active
        async with asyncio.timeout(1):
            assert await asyncio.get_running_loop().sock_recv(client_socket, 1) == b""
    finally:
        client_socket.close()
        writer.close()
        await writer.wait_closed()
        await peer.stop()


async def test_inbound_and_outbound_capacity_recovers_after_disconnect(app, second_app, monkeypatch):
    started, ended = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(transport, "MAX_CONNECTIONS", 1)

    async def hold(_payload, _channel):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            ended.set()

    async with listeners(app, second_app, tcp=True) as (source, destination):
        source.peer.on_subscribe = hold
        target = await destination.peer.target(source.lease.worker_key)
        async with destination.peer.subscribe(target, {}):
            await asyncio.wait_for(started.wait(), 2)
            with pytest.raises(protocol.PeerUnavailable):
                await destination.peer.wake(target, {})
            _, _, port = target.url.rpartition(":")
            reader, writer = await asyncio.open_connection("127.0.0.1", int(port))
            try:
                assert await asyncio.wait_for(reader.read(1), 2) == b""
            finally:
                writer.close()
                await writer.wait_closed()
        await asyncio.wait_for(ended.wait(), 2)
        await wait_until(lambda: not source.peer._active, within=2)
        assert not destination.peer._outbound
        async with destination.peer.subscribe(target, {}):
            assert destination.peer._outbound


async def test_unacknowledged_frame_expires_and_oversize_snapshot_is_never_published(app, second_app, monkeypatch):
    monkeypatch.setattr(protocol, "IO_SECONDS", 0.4)
    monkeypatch.setattr(protocol, "FRAME_BYTES", 2048)
    ended = asyncio.Event()
    confirmed = []

    async def serve(payload, channel):
        try:
            await channel.send({"event": "snapshot", "data": {"text": "A" * payload["length"]}, "proof": None})
            confirmed.append(await channel.confirmation())
        finally:
            ended.set()

    async with listeners(app, second_app, tcp=True) as (source, destination):
        source.peer.on_subscribe = serve
        target = await destination.peer.target(source.lease.worker_key)
        async with destination.peer.subscribe(target, {"length": 16}) as client:
            assert (await client.receive()).event == "snapshot"
            await asyncio.wait_for(ended.wait(), 2)
            await wait_until(lambda: not source.peer._active, within=2)
        ended.clear()
        async with destination.peer.subscribe(target, {"length": 2048}) as client:
            with pytest.raises(asyncio.IncompleteReadError):
                await asyncio.wait_for(client.receive(), 2)
            assert client.pending is None
        assert ended.is_set() and not confirmed


async def test_partial_record_activity_cannot_extend_deadline(monkeypatch):
    # 每次送达间隔均低于 IO_SECONDS；完整帧累计时间超过该截止。
    monkeypatch.setattr(protocol, "IO_SECONDS", 0.25)
    left, right = socket.socketpair()
    right.setblocking(False)
    reader, writer = await asyncio.open_connection(sock=left)
    chunks = 0

    async def send_part():
        nonlocal chunks
        loop = asyncio.get_running_loop()
        for chunk in (b"\x00", b"\x00", b"\x00\x20" + b"A" * 16, b"B" * 16):
            await loop.sock_sendall(right, chunk)
            chunks += 1
            await asyncio.sleep(0.07)
        # 若实现对头、签名、正文独立续期，最终会读到完整消息。
        await loop.sock_sendall(right, b"E" * 32)

    sender = asyncio.create_task(send_part())
    try:
        with pytest.raises(TimeoutError):
            await protocol.read_record(reader, b"local", 32, idle=2)
        assert chunks >= 2
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        right.close()
        writer.close()
        await writer.wait_closed()


async def test_two_large_wal_frames_reach_slow_public_reader_in_order(tmp_path, fake_llm, monkeypatch):
    first, second = "PART-A-" + "甲" * 65536, "PART-B-" + "乙" * 65536
    written = []
    send = protocol.PeerPublisher.send

    async def bounded_send(publisher, frame):
        if frame["event"] == "delta":
            sock = publisher.writer.get_extra_info("socket")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
            capacity = sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
            data = frame["data"]["text"]
            if data in (first, second):
                assert len(data.encode()) > capacity
                written.append(data[:7])
        await send(publisher, frame)

    monkeypatch.setattr(protocol.PeerPublisher, "send", bounded_send)
    async with public_pair(tmp_path, fake_llm, monkeypatch, wal=True) as pair, accounts(
        pair, collaborator=True,
    ) as (owner, editor, project, _, _), AsyncExitStack() as cleanup:
        before, finish = Gate(), Gate()
        entered, release = asyncio.Event(), asyncio.Event()
        fake_llm.push(["就绪", before, first, second, finish, "结束"])
        cleanup.callback(before.release)
        cleanup.callback(finish.release)
        task = await start_generation(editor, await chapter(owner, project, 1))
        await before.wait_reached(within=12)

        async def slow_public(scope, receive, send):
            async def slow(message):
                if b"PART-A-" in message.get("body", b"") and not entered.is_set():
                    entered.set()
                    await release.wait()
                await send(message)
            await pair.destination(scope, receive, slow)

        base = await cleanup.enter_async_context(_serve(slow_public))
        stream = _ProdStream(base, editor, task)
        cleanup.push_async_callback(stream.aclose)
        cleanup.callback(release.set)
        await stream.wait_for(visible("就绪"))
        before.release()
        await asyncio.wait_for(entered.wait(), 10)
        assert written == ["PART-A-"]
        assert "PART-A-" not in stream.text
        release.set()
        await stream.wait_for(visible("PART-B-"), within=10)
        finish.release()
        frames = await stream.finish()
        assert written == ["PART-A-", "PART-B-"]
        deltas = [data["text"] for event, data in frames if event == "delta"]
        assert len(deltas) >= 2
        assert deltas[0] == first and deltas[1] == second
        assert [event for event, _ in terminal(frames)] == ["done"]
        # 内部监听从未挂到真实公共端口。
        async with httpx.AsyncClient(base_url=pair.remote_url, trust_env=False) as public:
            assert (await public.get("/openapi.json")).status_code == 200
            assert (await public.post("/peer/subscribe", json={})).status_code == 404
