"""HTTP 应用的执行者代次。停止后台调度不会使正在执行的请求失去归属。"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import ConflictError
from app.services.ai_dispatch import is_sqlite_busy
from app.services.task_engine import workers

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine

logger = logging.getLogger(__name__)
_CancelRound = tuple[int, str, datetime | None, str | None]


@dataclass
class _Request:
    worker_key: str | None = None
    done: asyncio.Future | None = None
    closed: bool = False


class RequestWorker:
    def __init__(self, engine: "TaskEngine"):
        self.engine = engine
        self.lease = workers.WorkerLease(engine, on_expire=self._expire)
        self._closing = asyncio.Event()
        self._heartbeat: asyncio.Task | None = None
        self._quiescing = False
        self._current: ContextVar[_Request | None] = ContextVar("request_worker_scope", default=None)
        self._requests: set[asyncio.Future] = set()
        self._cancel_rounds: set[_CancelRound] = set()
        self._recoveries: dict[_CancelRound, asyncio.Task] = {}
        self._inspiration_release: asyncio.Task | None = None
        self._lease_loss_handlers: set[Callable[[], None]] = set()

    @property
    def worker_key(self) -> str | None:
        return self.lease.worker_key

    @property
    def valid(self) -> bool:
        return not self._closing.is_set() and self.lease.valid

    def _expire(self) -> None:
        self.lease.invalidate()
        handlers = tuple(self._lease_loss_handlers)
        self._lease_loss_handlers.clear()
        for handler in handlers:
            handler()

    @contextmanager
    def on_lease_loss(self, handler: Callable[[], None]) -> Iterator[None]:
        """使本代次正在等待的外部 I/O 随安全期限结束。"""
        self._lease_loss_handlers.add(handler)
        try:
            yield
        finally:
            self._lease_loss_handlers.discard(handler)

    def recover_cancel(self, task_id: int, task_key: str | None, queued_at: datetime | None,
                       execution_key: str | None, *, retry: bool = False) -> asyncio.Task | None:
        """独立于 HTTP 断连，按请求指定的轮次启动或重试 C1 接管。"""
        if task_key is None:
            return None
        identity = (task_id, task_key, queued_at, execution_key)
        if identity in self._cancel_rounds:
            if retry:
                self._start_recovery(identity)
            return self._recoveries.get(identity)
        if self.engine._loop_task is not None and not self.engine._loop_task.done():
            return None
        self._cancel_rounds.add(identity)
        self._start_recovery(identity)
        return self._recoveries.get(identity)

    def _start_recovery(self, identity: _CancelRound) -> None:
        worker = self.worker_key
        if worker is None or self._quiescing or not self.valid or identity in self._recoveries:
            return
        self._recoveries[identity] = asyncio.create_task(self._recover_cancel(identity, worker))

    async def _recover_cancel(self, identity: _CancelRound, worker: str) -> tuple[str, Exception] | None:
        try:
            outcome = await self.engine.recover_requested_round(*identity, worker)
            if isinstance(outcome, Exception):
                logger.warning("请求执行者未能完成本轮任务收尾 task_key=%s", identity[1])
                return worker, outcome
            if outcome:
                self._cancel_rounds.discard(identity)
        except Exception:  # noqa: BLE001 认领提交回执或数据库暂时不可读，下次以同轮原身份重查。
            logger.warning("请求侧任务接管等待重新核对 task_key=%s", identity[1])
        finally:
            self._recoveries.pop(identity, None)
        return None

    @contextmanager
    def http_scope(self):
        # 只登记实际使用请求执行权的 HTTP。普通读取和订阅可持续很久。
        state = _Request()
        token = self._current.set(state)
        try:
            yield
        finally:
            state.closed = True
            if state.done is not None:
                self._requests.discard(state.done)
                state.done.set_result(None)
            self._current.reset(token)

    async def require(self, db: AsyncSession, expected_key: str | None = None, *, read_only: bool = False) -> str:
        """在调用方当前事务内验证原请求代次；不会在持业务锁期间新开登记事务。"""
        key = self.worker_key
        scope = self._current.get()
        if scope is not None and scope.closed:
            # 后台 drain 可在受理 HTTP 返回后继续。它用自己的持久清理身份。
            scope = None
        original = scope.worker_key if scope is not None else None
        if (not self.valid or key is None or (expected_key is not None and key != expected_key)
                or (original is not None and key != original)
                or (self._quiescing and original is None and expected_key is None)):
            raise ConflictError("请求执行身份已变化，请核对后重试", extra={"code": "task_execution_changed"})
        if scope is not None and scope.done is None:
            scope.worker_key = key
            scope.done = asyncio.get_running_loop().create_future()
            self._requests.add(scope.done)
        if not await workers.lock_live_worker(db, key, read_only=read_only):
            raise ConflictError("请求执行身份已变化，请核对后重试", extra={"code": "task_execution_changed"})
        return key

    async def start(self) -> None:
        if self._heartbeat is not None:
            return
        self._closing.clear()
        self._quiescing = False
        deadline = time.monotonic() + self.engine.ctx.settings.ai_request_timeout
        while True:
            try:
                await self.lease.register()
                break
            except (OperationalError, sqlite3.OperationalError) as exc:
                await self.lease.stop()
                remaining = deadline - time.monotonic()
                if not is_sqlite_busy(exc) or remaining <= 0:
                    raise
                await asyncio.sleep(min(1.0, remaining))
            except BaseException:
                await self.lease.stop()
                raise
        self._heartbeat = asyncio.create_task(self._maintain(), name="request-worker-heartbeat")

    async def _maintain(self) -> None:
        from app.services.inspiration_service import schedule_releases

        while not self._closing.is_set():
            try:
                await asyncio.wait_for(self._closing.wait(), timeout=self.engine.heartbeat_interval)
                return
            except TimeoutError:
                pass
            try:
                if self.lease.valid and await self.lease.heartbeat():
                    if not self._quiescing:
                        self._inspiration_release = schedule_releases(self, self._inspiration_release)
                        self.engine.admin_cancellations.schedule_confirmations()
                        for identity in self._cancel_rounds.copy():
                            self._start_recovery(identity)
                    continue
                # 旧请求每次发包/提交都须持原代次。新的请求只使用新登记的身份。
                self._expire()
                await self._drain()
                if not self._quiescing and not self._closing.is_set():
                    await self.lease.register()
            except Exception:  # noqa: BLE001  主库暂不可用时由下一轮重试，不能继续签发失效身份。
                logger.warning("请求执行者续约或登记未确认")

    async def _drain(self) -> None:
        current = asyncio.current_task()
        if self._inspiration_release is not None:
            self._inspiration_release.cancel()
            await asyncio.gather(self._inspiration_release, return_exceptions=True)
            self._inspiration_release = None
        if self._recoveries:
            await asyncio.gather(*(asyncio.shield(t) for t in self._recoveries.values()), return_exceptions=True)
        cleanup = {task for scope in self.engine._cleanup_work.values() for task in scope if task is not current}
        admin = {task for task in self.engine.admin_cancellations.work.values() if task is not current}
        pending = {*self._requests, *cleanup, *admin}
        release_task = self.engine.admin_cancellations.release_task
        if release_task is not None and release_task is not current:
            pending.add(release_task)
        if pending:
            await asyncio.gather(*(asyncio.shield(work) for work in pending), return_exceptions=True)

    async def stop(self) -> None:
        self._quiescing = True
        await self._drain()
        self._closing.set()
        if self._heartbeat is not None:
            await self._heartbeat
            self._heartbeat = None
        await self.lease.stop()


class RequestWorkerScope:
    """在原 ASGI 请求协程内绑定 worker；不改变认证与发送屏障的任务身份。"""

    def __init__(self, app: ASGIApp, *, worker: RequestWorker):
        self.app = app
        self.worker = worker

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            with self.worker.http_scope():
                await self.app(scope, receive, send)
        else:
            await self.app(scope, receive, send)
