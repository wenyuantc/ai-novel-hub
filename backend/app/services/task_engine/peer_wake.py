"""用有界的内部提醒缩短数据库轮询延迟。接收端独立读取已提交状态。"""

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from itertools import islice
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine

logger = logging.getLogger(__name__)

_MAX_KEYS = 64
_FANOUT = 8


class PeerWakes:
    def __init__(self, engine: "TaskEngine") -> None:
        self.engine = engine
        self.refresh_settings: Callable[[], None] | None = None
        self._members: dict[tuple[int, int], set[asyncio.Event]] = {}
        self._pending: set[tuple] = set()
        self._sender: asyncio.Task | None = None

    @contextmanager
    def watch_member(self, project_id: int, actor_id: int) -> Iterator[asyncio.Event]:
        key = (project_id, actor_id)
        changed = asyncio.Event()
        waiting = self._members.setdefault(key, set())
        waiting.add(changed)
        try:
            yield changed
        finally:
            waiting.discard(changed)
            if not waiting:
                self._members.pop(key, None)

    def member_changed(self, project_id: int, actor_id: int) -> None:
        for waiter in self._members.get((project_id, actor_id), ()):
            waiter.set()

    def schedule(self, worker_key: str | None = None) -> None:
        local_keys = (self.engine.lease.worker_key, self.engine.request_worker.worker_key)
        if worker_key is not None and worker_key in local_keys:
            return
        self._enqueue(("schedule", worker_key))

    def settings(self) -> None:
        self._enqueue(("settings",))

    def collaboration(self, project_id: int, actor_id: int) -> None:
        self._enqueue(("member", project_id, actor_id))

    def _enqueue(self, key: tuple) -> None:
        if not self.engine.peer.url or ("all",) in self._pending:
            return
        self._pending.add(key)
        if len(self._pending) > _MAX_KEYS:
            self._pending = {("all",)}
        if self._sender is None:
            self._sender = asyncio.create_task(self._drain(), name="peer-wake")

    async def _drain(self) -> None:
        try:
            while self._pending:
                pending, self._pending = self._pending, set()
                try:
                    await self._send(pending)
                except Exception as exc:
                    logger.warning("内部提醒暂不可用 error_type=%s", type(exc).__name__)
        finally:
            self._sender = None

    async def _send(self, pending: set[tuple]) -> None:
        peer = self.engine.peer
        members = [(hint[1], hint[2]) for hint in pending if hint[0] == "member"]
        full = ("all",) in pending
        schedule_all = full or ("schedule", None) in pending
        settings = full or ("settings",) in pending
        broadcast = {"schedule": schedule_all, "settings": settings, "all_members": full, "members": members}

        # 一份端点在 requests/scheduler 两个代次中都可登记，按地址合并提醒。
        deliveries = {}
        if schedule_all or settings or members:
            for dest in await peer.targets():
                deliveries[dest.url] = (dest, broadcast)
        if not schedule_all:
            for hint in pending:
                if hint[0] == "schedule" and (dest := await peer.target(hint[1])) is not None:
                    deliveries[dest.url] = (dest, {**broadcast, "schedule": True})

        async def deliver(dest, payload) -> None:
            try:
                await peer.wake(dest, payload)
            except Exception as exc:
                logger.debug("内部提醒投递失败 error_type=%s", type(exc).__name__)

        targets = iter(deliveries.values())
        while batch := list(islice(targets, _FANOUT)):
            await asyncio.gather(*(deliver(dest, payload) for dest, payload in batch))

    async def receive(self, payload: dict) -> None:
        if (not isinstance(payload, dict) or set(payload) != {"schedule", "settings", "members", "all_members"}
                or any(type(payload[key]) is not bool for key in ("schedule", "settings", "all_members"))):
            raise ValueError("内部提醒无效")
        members = payload["members"]
        if (type(members) is not list or len(members) > _MAX_KEYS
                or any(type(pair) is not list or len(pair) != 2
                       or any(type(value) is not int or value <= 0 for value in pair) for pair in members)):
            raise ValueError("内部提醒无效")
        if payload["settings"] and self.refresh_settings is not None:
            self.refresh_settings()
        if payload["schedule"]:
            self.engine.notify()
        if payload["all_members"]:
            for key in self._members:
                self.member_changed(*key)
            self.engine.recheck_all_collaboration()
        else:
            for project_id, actor_id in set(map(tuple, members)):
                self.engine.recheck_collaboration(project_id, actor_id)

    async def stop(self) -> None:
        self._pending.clear()
        if self._sender is not None:
            self._sender.cancel()
            await asyncio.gather(self._sender, return_exceptions=True)
            self._sender = None
