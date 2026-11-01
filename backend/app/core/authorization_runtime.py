"""单应用共享、可重入的短发布临界段；数据库锁仍负责持久授权串行。"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass
class RuntimeLease:
    runtime: "AuthorizationRuntime"
    owner: asyncio.Task
    released: bool = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.runtime.release(self.owner)


class AuthorizationRuntime:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0
        # Settings提交事实未确认时暂停新准入，既有执行体仍继续。
        self.settings_confirmed = True

    async def acquire(self) -> RuntimeLease:
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("授权协调器需要异步任务")
        if self._owner is not owner:
            await self._lock.acquire()
            self._owner = owner
        self._depth += 1
        return RuntimeLease(self, owner)

    def release(self, owner: asyncio.Task) -> None:
        if self._owner is not owner or self._depth < 1:
            raise RuntimeError("授权协调器租约不匹配")
        self._depth -= 1
        if not self._depth:
            self._owner = None
            self._lock.release()

    @asynccontextmanager
    async def hold(self):
        lease = await self.acquire()
        try:
            yield
        finally:
            lease.release()
