"""应用 lifespan 持有的进程间监听。地址只用于发现，身份由活租约和双向认证决定。"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import socket
import stat
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

from sqlalchemy import or_, select, update

from app.models import TaskWorker
from app.services.task_engine import peer_socket, workers
from app.services.task_engine.peer_protocol import (
    CONTROL_BYTES,
    IDLE_SECONDS,
    PeerFrame,
    PeerPublisher,
    PeerSubscriber,
    PeerUnavailable,
    bounded_record,
    canonical,
    derive,
    digest,
    is_authenticated_frame,
    read_record,
    write_record,
)

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine

__all__ = ["PeerFrame", "PeerPublisher", "PeerTarget", "PeerTransport", "PeerUnavailable", "is_authenticated_frame"]

logger = logging.getLogger(__name__)
# 64 条连接、每次最多一个待确认帧。独立限制握手/订阅，繁忙时其余读者走持久降级。
MAX_CONNECTIONS = 64
HANDSHAKE_SECONDS = 2.0
CLOCK_SKEW_SECONDS = 30
CLOSE_SECONDS = 1.0
_MAGIC = b"AINOVEL-PEER/1\n"


# 登记点只在真实对端握手之后。仅以自选密钥构造底层编解码器不产生应用级证据。
_subscriptions = WeakKeyDictionary()


def _active_subscription(client, frame, ctx):
    registration = _subscriptions.get(client)
    return registration is not None and (ctx is None or registration[0] is ctx) and registration[1:] == (
        frame.source_key, frame.target_key, frame.subscription_id, digest(frame.request),
    )


class PeerTarget(NamedTuple):
    worker_key: str
    url: str


def _uuid(value):
    return type(value) is str and len(value) == 36 and str(UUID(value)) == value


async def _close(writer):
    writer.close()
    try:
        async with asyncio.timeout(CLOSE_SECONDS):
            await writer.wait_closed()
    except (TimeoutError, OSError):
        writer.transport.abort()


async def _serve_subscription(engine, payload, channel):
    from app.services.task_engine.peer_stream import serve_subscription

    await serve_subscription(engine, payload, channel)


class PeerTransport:
    def __init__(self, engine: TaskEngine):
        self.engine = engine
        config = engine.ctx.settings
        self._key = derive(config.jwt_secret.encode(), b"ainovel/task-peer/request/v1")
        self._directory: Path | None = None
        self._server = None
        self._pruner = None
        self._socket = None
        self._active = {}
        self._lifecycle = asyncio.Lock()
        self._handshakes_done = asyncio.Event()
        self._handshakes_done.set()
        self._outbound = set()
        self._opening = 0
        self._epoch = 0
        self.url: str | None = None
        self.on_wake = None
        self.on_subscribe = lambda payload, channel: _serve_subscription(engine, payload, channel)
        self._networks = ()

    def _local(self, key):
        return self.url is not None and any(
            lease.valid and lease.worker_key == key for lease in (self.engine.request_worker, self.engine.lease)
        )

    def _source(self):
        for lease in (self.engine.request_worker, self.engine.lease):
            if lease.valid and lease.worker_key is not None:
                return lease.worker_key
        raise PeerUnavailable()

    async def _targets(self, key=None):
        if self.url is None:
            return []
        query = select(TaskWorker.worker_key, TaskWorker.peer_url).where(
            TaskWorker.peer_url.is_not(None), TaskWorker.stopped_at.is_(None), TaskWorker.fenced_at.is_(None),
        )
        if key is not None:
            query = query.where(TaskWorker.worker_key == key)
        async with self.engine.ctx.sessionmaker() as db:
            result = []
            for worker_key, url in await db.execute(query):
                if url != self.url and await workers.lock_live_worker(
                    db, worker_key, self.engine.lease_seconds, read_only=True,
                ):
                    result.append(PeerTarget(worker_key, url))
            return result

    async def target(self, worker_key) -> PeerTarget | None:
        if not worker_key:
            return None
        rows = await self._targets(worker_key)
        return rows[0] if rows else None

    async def targets(self, *, all_generations=False):
        rows = await self._targets()
        return rows if all_generations else list({row.url: row for row in rows}.values())

    def _prepare_directory(self, config):
        if not hasattr(os, "getuid") or not hasattr(asyncio, "start_unix_server"):
            raise PeerUnavailable()
        tag = derive(config.jwt_secret.encode(), b"ainovel/task-peer/directory/v1").hex()[:12]
        default = Path("/tmp").resolve() / f"an-peer-{os.getuid()}-{tag}"
        self._directory = Path(config.task_peer_socket_dir or default).absolute()
        self._directory.mkdir(mode=0o700, exist_ok=True)
        self._directory_ok()

    def _directory_ok(self):
        if self._directory is None or not hasattr(os, "getuid"):
            raise PeerUnavailable()
        info = self._directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.getuid()):
            raise PeerUnavailable()

    def _owned_socket(self, path):
        self._directory_ok()
        if path.parent != self._directory or path.name != path.stem + ".sock" or not _uuid(path.stem):
            raise PeerUnavailable()
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PeerUnavailable()
        return info.st_dev, info.st_ino

    def _claim_socket(self, path):
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise PeerUnavailable()
        self._socket = path, (info.st_dev, info.st_ino)
        os.chmod(path, 0o600)
        if self._socket[1] != self._owned_socket(path):
            raise PeerUnavailable()
        peer_socket.save_identity(*self._socket)

    async def _addresses(self, host, port):
        results = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(item[4][0] for item in results))
        if not addresses or any(not any(ipaddress.ip_address(addr) in net for net in self._networks)
                                for addr in addresses):
            raise PeerUnavailable()
        return addresses

    async def start(self):
        async with self._lifecycle:
            await self._start()

    async def _start(self):
        if self._server is not None:
            return
        config = self.engine.ctx.settings
        if not config.task_peer_enabled:
            return
        try:
            if config.task_peer_bind_host or config.task_peer_advertise_host:
                host = config.task_peer_advertise_host
                if not host or not config.task_peer_bind_host or any(c in host for c in "/@?#%[]"):
                    raise PeerUnavailable()
                self._networks = tuple(ipaddress.ip_network(n.strip()) for n in config.task_peer_networks.split(","))
                async with asyncio.timeout(HANDSHAKE_SECONDS):
                    await self._addresses(host, 0)
                    bindings = await self._addresses(config.task_peer_bind_host, config.task_peer_port)
                    self._server = await asyncio.start_server(
                        self._accept, bindings[0], config.task_peer_port, backlog=MAX_CONNECTIONS,
                    )
                port = self._server.sockets[0].getsockname()[1]
                self.url = f"tcp://{'[' + host + ']' if ':' in host else host}:{port}"
            else:
                self._prepare_directory(config)
                path = self._directory / f"{uuid4()}.sock"
                self._server = await asyncio.start_unix_server(self._accept, path, backlog=MAX_CONNECTIONS)
                self._claim_socket(path)
                self.url = f"unix://{path}"
                self._pruner = asyncio.create_task(self._prune_retired(), name="task-peer-socket-cleanup")
        except (OSError, ValueError, PeerUnavailable, TimeoutError):
            await self._stop()
            logger.warning("内部任务监听不可用，将使用数据库跟随")
        except BaseException:
            await self._stop()
            raise

    async def _prune_retired(self):
        # 等 C1 确认死亡后再碰文件。同一监听可能还有另一份活的调度/HTTP 登记。
        while True:
            try:
                await self._prune_once()
            except Exception:
                logger.warning("内部任务套接字的归属暂时无法核实")
            await asyncio.sleep(max(self.engine.lease_seconds, 30))

    async def _prune_once(self):
        open_addresses = select(TaskWorker.peer_url).where(
            TaskWorker.peer_url.is_not(None), TaskWorker.stopped_at.is_(None), TaskWorker.fenced_at.is_(None),
        )
        async with self.engine.ctx.sessionmaker() as db:
            urls = list(await db.scalars(select(TaskWorker.peer_url).distinct().where(
                TaskWorker.peer_url.startswith("unix://"), TaskWorker.peer_url.not_in(open_addresses),
                or_(TaskWorker.stopped_at.is_not(None), TaskWorker.fenced_at.is_not(None)),
            )))
        for url in urls:
            if url == self.url:
                continue
            path = Path(url.removeprefix("unix://"))
            with suppress(OSError, ValueError, PeerUnavailable):
                identity = self._owned_socket(path)
                if not peer_socket.matches(path, identity):
                    continue
                # 租约可能全停而 lifespan 仍服务；不能清理仍接受连接的监听。
                try:
                    async with asyncio.timeout(CLOSE_SECONDS):
                        _, writer = await asyncio.open_unix_connection(path)
                except ConnectionRefusedError:
                    peer_socket.remove_if_owned(path, identity, current_identity=self._owned_socket)
                else:
                    await _close(writer)

    async def stop(self):
        async with self._lifecycle:
            await self._stop()

    async def _stop(self):
        address, self.url = self.url, None
        owners = [lease.worker_key for lease in (self.engine.request_worker, self.engine.lease) if lease.worker_key]
        self._epoch += 1
        if self._pruner is not None:
            self._pruner.cancel()
            await asyncio.gather(self._pruner, return_exceptions=True)
            self._pruner = None
        if self._server is not None:
            self._server.close()
        writers = set(self._outbound).union(self._active.values())
        for writer in writers:
            writer.close()
        for handler in tuple(self._active):
            handler.cancel()
        if self._active:
            await asyncio.gather(*tuple(self._active), return_exceptions=True)
        # 某个出站连接可能正在 DNS/open 阶段，尚未登记 writer。
        # 让它在原握手截止时间内观察关闭代次并归还连接；不会取消公开 HTTP 请求。
        await self._handshakes_done.wait()
        writers.update(self._outbound)
        if writers:
            await asyncio.gather(*(_close(writer) for writer in writers), return_exceptions=True)
        if self._server is not None:
            with suppress(TimeoutError):
                async with asyncio.timeout(CLOSE_SECONDS):
                    await self._server.wait_closed()
            self._server = None
        wakes = getattr(self.engine, "peer_wakes", None)
        if wakes is not None:
            await wakes.stop()
        if self._socket is not None:
            path, identity = self._socket
            self._socket = None
            with suppress(OSError, ValueError, PeerUnavailable):
                peer_socket.release_current(path, identity)
        if address is not None and owners:
            # 清除该监听的地址不影响任何执行/请求租约，也不标记任务故障。
            try:
                async with asyncio.timeout(HANDSHAKE_SECONDS), self.engine.ctx.sessionmaker.begin() as db:
                    await db.execute(update(TaskWorker).where(
                        TaskWorker.peer_url == address, TaskWorker.worker_key.in_(owners),
                    ).values(peer_url=None))
            except Exception:
                logger.warning("内部任务地址尚未撤下，对端将按连接故障降级")

    def _accept(self, reader, writer):
        if self.url is None or len(self._active) >= MAX_CONNECTIONS:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer), name="task-peer-connection")
        self._active[task] = writer
        task.add_done_callback(self._active.pop)

    async def _handle(self, reader, writer):
        try:
            async with asyncio.timeout(HANDSHAKE_SECONDS):
                # 每个新连接的不可预测挑战仅可消费一次；重启或同进程重放旧签名均不成立。
                challenge = secrets.token_bytes(32)
                writer.write(_MAGIC + challenge)
                await writer.drain()
                key = derive(self._key, challenge)
                request = await read_record(reader, key, CONTROL_BYTES)
                if (set(request) != {"source", "target", "at", "nonce", "op", "payload"}
                        or request["op"] not in ("wake", "subscribe") or type(request["payload"]) is not dict
                        or type(request["at"]) is not int or abs(time.time() - request["at"]) > CLOCK_SKEW_SECONDS
                        or not all(_uuid(request[k]) for k in ("source", "target", "nonce"))
                        or not self._local(request["target"]) or await self.target(request["source"]) is None):
                    raise PeerUnavailable()
                session_key = derive(key, b"session\0" + hashlib.sha256(canonical(request)).digest())
                if request["op"] == "wake":
                    if self.on_wake is None:
                        raise PeerUnavailable()
                    await self.on_wake(request["payload"])
                await write_record(writer, derive(session_key, b"ready"), {"ok": True}, CONTROL_BYTES)
            if request["op"] == "subscribe":
                epoch = self._epoch
                await self._publish(reader, writer, session_key, request,
                                    lambda: epoch == self._epoch and self._local(request["target"]))
        except asyncio.CancelledError:
            raise
        except Exception:  # 内部解析、授权、数据库错误一律结束同一连接，不公开原文和任务存在性。
            pass
        finally:
            await _close(writer)

    async def _publish(self, reader, writer, key, request, available):
        controls = asyncio.Queue(maxsize=1)
        publisher = PeerPublisher(writer, controls, key, request, available)

        async def watch_client():
            control_key = derive(key, b"control")
            while True:
                msg = await read_record(reader, control_key, CONTROL_BYTES, idle=IDLE_SECONDS * 2)
                if not publisher._outstanding or controls.full():
                    raise PeerUnavailable()
                controls.put_nowait(msg)

        async def serve():
            await self.on_subscribe(request["payload"], publisher)
            await publisher.finish()

        watcher = asyncio.create_task(watch_client())
        handler = asyncio.create_task(serve())
        try:
            done, _ = await asyncio.wait((watcher, handler), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            watcher.cancel()
            handler.cancel()
            await asyncio.gather(watcher, handler, return_exceptions=True)

    async def _open(self, destination):
        parts = urlsplit(destination)
        if parts.query or parts.fragment or parts.username or parts.password:
            raise PeerUnavailable()
        if parts.scheme == "unix" and not parts.netloc:
            path = Path(parts.path)
            self._owned_socket(path)
            return await asyncio.open_unix_connection(path)
        if parts.scheme != "tcp" or not parts.hostname or not parts.port or parts.path:
            raise PeerUnavailable()
        addresses = await self._addresses(parts.hostname, parts.port)
        # 连接刚校验过的数字地址，避免在实际连接时再次解析域名。
        for addr in addresses:
            try:
                return await asyncio.open_connection(addr, parts.port)
            except OSError:
                pass
        raise PeerUnavailable()

    @asynccontextmanager
    async def _session(self, op, target, payload):
        if self.url is None or len(self._outbound) + self._opening >= MAX_CONNECTIONS:
            raise PeerUnavailable()
        self._opening += 1
        self._handshakes_done.clear()
        writer = None
        started = self._epoch
        try:
            try:
                async with asyncio.timeout(HANDSHAKE_SECONDS):
                    if target != await self.target(target.worker_key):
                        raise PeerUnavailable()
                    source = self._source()
                    reader, writer = await self._open(target.url)
                    self._outbound.add(writer)
                    if started != self._epoch or not self._local(source):
                        raise PeerUnavailable()
                    hello = await reader.readexactly(len(_MAGIC) + 32)
                    if not hello.startswith(_MAGIC):
                        raise PeerUnavailable()
                    key = derive(self._key, hello[len(_MAGIC):])
                    request = {"source": source, "target": target.worker_key, "at": int(time.time()),
                               "nonce": str(uuid4()), "op": op,
                               "payload": json.loads(bounded_record(payload, CONTROL_BYTES))}
                    await write_record(writer, key, request, CONTROL_BYTES)
                    session_key = derive(key, b"session\0" + hashlib.sha256(canonical(request)).digest())
                    ready = await read_record(reader, derive(session_key, b"ready"), CONTROL_BYTES)
                    if ready != {"ok": True} or started != self._epoch or not self._local(source):
                        raise PeerUnavailable()
            except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError):
                raise PeerUnavailable() from None
            finally:
                self._opening -= 1
                if self._opening == 0:
                    self._handshakes_done.set()
            yield reader, writer, session_key, request, lambda: (
                not writer.is_closing() and started == self._epoch and self._local(source)
            )
        finally:
            if writer is not None:
                self._outbound.discard(writer)
                await _close(writer)

    async def wake(self, target: PeerTarget, payload: dict):
        async with self._session("wake", target, payload):
            pass

    @asynccontextmanager
    async def subscribe(self, target: PeerTarget, payload: dict):
        async with self._session("subscribe", target, payload) as channel:
            sub = PeerSubscriber(*channel)
            request = channel[3]
            _subscriptions[sub] = (
                self.engine.ctx, request["target"], request["source"], request["nonce"], digest(request["payload"]),
            )
            try:
                yield sub
            finally:
                _subscriptions.pop(sub, None)
                sub.invalidate()
