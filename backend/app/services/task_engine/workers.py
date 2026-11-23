"""数据库执行者租约与接管。运行归属和业务执行键分别标识进程及任务轮次。"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4
from weakref import WeakKeyDictionary, WeakSet, WeakValueDictionary

from sqlalchemy import and_, delete, event, func, or_, select, update
from sqlalchemy.engine import Connection, Engine, RootTransaction
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.errors import ConflictError
from app.models import AgentChange, Project, Task, TaskExecutionSlot, TaskWorker, TaskWorkRecord
from app.services.ai_dispatch import is_sqlite_busy
from app.services.task_engine import sqlite_lease_pair, sqlite_worker_leases

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine


_local_leases: WeakValueDictionary[str, WorkerLease] = WeakValueDictionary()
_sqlite_leases: WeakKeyDictionary[Engine, WeakSet[WorkerLease]] = WeakKeyDictionary()
_transaction_workers: WeakKeyDictionary[RootTransaction, dict[str, WorkerLease]] = WeakKeyDictionary()
logger = logging.getLogger(__name__)


@event.listens_for(Engine, "rollback")
def _forget_transaction_workers(connection: Connection) -> None:
    transaction = connection.get_transaction()
    if transaction is not None:
        _transaction_workers.pop(transaction, None)


@event.listens_for(Engine, "commit")
def _verify_transaction_workers(connection: Connection) -> None:
    # 此事件在 ORM flush 后、驱动实际发出 COMMIT 前触发。登记绑定具体根事务，连接归还池后不会沿用。
    transaction = connection.get_transaction()
    guarded = _transaction_workers.pop(transaction, None) if transaction is not None else None
    if guarded and any(lease.worker_key != key or not lease.valid for key, lease in guarded.items()):
        # commit 事件抛错时 SQLAlchemy 将根事务标为 inactive。先回滚真实驱动，避免连接留有未决写入。
        try:
            connection.connection.rollback()
        except BaseException as exc:
            connection.invalidate(exc)
            raise
        raise ConflictError("任务执行身份已变化，请核对后重试", extra={"code": "task_execution_changed"})


def _pair_for_db(db: AsyncSession) -> sqlite_lease_pair.SQLiteLeasePair:
    for lease in _sqlite_leases.get(db.get_bind().engine, ()):
        if lease.sqlite_pair is not None:
            return lease.sqlite_pair
    raise sqlite_lease_pair.SQLiteLeasePairError("当前 SQLite 引擎没有已核验的租约文件")


async def _db_cutoff(db: AsyncSession, seconds: float = 30) -> datetime | str:
    dialect = db.get_bind().dialect.name
    # PostgreSQL 的 now() 固定在事务开始；业务事务此前可能已经等过锁。
    now = await db.scalar(select(func.clock_timestamp() if dialect == "postgresql" else func.now()))
    cutoff = now - timedelta(seconds=seconds)
    if dialect == "sqlite":
        # CURRENT_TIMESTAMP 用整秒文本；避免同一秒被带 .000000 的绑定值提前围栏。
        return cutoff.isoformat(sep=" ")
    return cutoff


def _pg_live_worker_condition(cutoff: datetime | str):
    return and_(TaskWorker.stopped_at.is_(None), TaskWorker.fenced_at.is_(None), TaskWorker.heartbeat_at >= cutoff)


def registered_worker_exists(worker_key: str | InstrumentedAttribute[str | None]):
    """主库登记仍开放。SQLite 的实际领取或写入另须核验权威租约。"""
    return select(TaskWorker.worker_key).where(
        TaskWorker.worker_key == worker_key, TaskWorker.stopped_at.is_(None), TaskWorker.fenced_at.is_(None),
    ).exists()


def confirmed_terminal(worker_key: str | InstrumentedAttribute[str | None]):
    """已提交到主库的不可逆终止决定，未登记或暂不可核验的 worker 不匹配。"""
    return select(TaskWorker.worker_key).where(
        TaskWorker.worker_key == worker_key, or_(TaskWorker.stopped_at.is_not(None), TaskWorker.fenced_at.is_not(None)),
    ).exists()


def _pg_orphan_running_condition(cutoff: datetime | str):
    live_owner = select(TaskWorker.worker_key).where(
        TaskWorker.worker_key == Task.worker_key, _pg_live_worker_condition(cutoff),
    ).exists()
    return and_(Task.status == "running", Task.recovered_at.is_(None), ~live_owner)


async def lock_live_worker(
    db: AsyncSession, worker_key: str, seconds: float = 30, *, read_only: bool = False,
) -> bool:
    """调用方先按业务锁序锁住目标 Task，再锁执行者；围栏与本次写提交有确定的先后。"""
    local = _local_leases.get(worker_key)
    if local is not None:
        if not local.valid or local.worker_key != worker_key:
            return False
        seconds = local.engine.lease_seconds
    if db.get_bind().dialect.name == "sqlite":
        conditions = (TaskWorker.worker_key == worker_key, TaskWorker.stopped_at.is_(None),
                      TaskWorker.fenced_at.is_(None))
        if read_only:
            # 已经处于实际的发送事务，其物理锁保持到响应头。
            registered = await db.scalar(select(TaskWorker.worker_key).where(*conditions)) is not None
        else:
            locked = await db.execute(update(TaskWorker).where(*conditions).values(worker_key=TaskWorker.worker_key)
                                      .execution_options(synchronize_session=False))
            registered = locked.rowcount == 1
        if not registered:
            return False
        try:
            alive = await sqlite_worker_leases.live(_pair_for_db(db), worker_key, seconds)
        except sqlite_lease_pair.SQLiteLeasePairError:
            return False
    else:
        # 普通心跳只更新非键列，应能与业务事务/慢速发送共存。围栏和停止显式取 FOR UPDATE。
        conditions = (TaskWorker.worker_key == worker_key, _pg_live_worker_condition(await _db_cutoff(db, seconds)))
        statement = select(TaskWorker.worker_key).where(*conditions).with_for_update(read=True, key_share=True)
        alive = await db.scalar(statement) is not None
    if alive and local is not None:
        physical = (await db.connection()).sync_connection.get_transaction()
        if physical is None or not local.valid or local.worker_key != worker_key:
            return False
        _transaction_workers.setdefault(physical, {})[worker_key] = local
    return alive


async def _release_confirmed_slots(db: AsyncSession, worker_key: str | None = None) -> None:
    terminal = select(TaskWorker.worker_key).where(
        or_(TaskWorker.stopped_at.is_not(None), TaskWorker.fenced_at.is_not(None)),
    )
    if worker_key is not None:
        terminal = terminal.where(TaskWorker.worker_key == worker_key)
    await db.execute(delete(TaskExecutionSlot).where(TaskExecutionSlot.worker_key.in_(terminal))
                     .execution_options(synchronize_session=False))
    # 只收回正常执行体的工作登记。请求侧清理、失败屏障及外部调用另有业务收据。
    await db.execute(delete(TaskWorkRecord).where(
        TaskWorkRecord.kind == "execution", TaskWorkRecord.state == "active", TaskWorkRecord.worker_key.in_(terminal),
    ).execution_options(synchronize_session=False))


async def _pg_fence_expired(db: AsyncSession, cutoff: datetime | str, worker_key: str | None = None) -> None:
    if db.get_bind().dialect.name != "postgresql":
        raise ValueError("仅 PostgreSQL 使用主库心跳判定失联")
    expired = (TaskWorker.stopped_at.is_(None), TaskWorker.fenced_at.is_(None), TaskWorker.heartbeat_at < cutoff)
    if worker_key is not None:
        expired += (TaskWorker.worker_key == worker_key,)
    # 某个长事务可能仍持执行者共享锁；本轮先处理其他失联登记，下个周期再查它。
    locked = select(TaskWorker.worker_key).where(*expired).correlate(None).with_for_update(skip_locked=True)
    fenced = await db.execute(update(TaskWorker).where(*expired, TaskWorker.worker_key.in_(locked))
                              .values(fenced_at=func.now()).execution_options(synchronize_session=False))
    if fenced.rowcount:
        await _release_confirmed_slots(db, worker_key)


def _confirmed_absent():
    terminal = select(TaskWorker.worker_key).where(
        TaskWorker.worker_key == Task.worker_key,
        or_(TaskWorker.stopped_at.is_not(None), TaskWorker.fenced_at.is_not(None)),
    ).exists()
    return or_(Task.worker_key.is_(None), terminal)


async def prepare_orphan_scan(db: AsyncSession, seconds: float, *, worker_key: str | None = None):
    """先提交 SQLite 侧库的不可逆决定；主库暂不可写时，下一轮仍能取回相同决定。"""
    if db.get_bind().dialect.name != "sqlite":
        cutoff = await _db_cutoff(db, seconds)
        await _pg_fence_expired(db, cutoff, worker_key)
        return _pg_orphan_running_condition(cutoff)
    pair = _pair_for_db(db)
    if worker_key is None:
        await sqlite_worker_leases.fence_expired(pair, seconds)
        active = list(await db.scalars(select(TaskWorker.worker_key).where(
            TaskWorker.stopped_at.is_(None), TaskWorker.fenced_at.is_(None),
        )))
    else:
        await sqlite_worker_leases.fence_expired(pair, seconds, worker_key)
        active = [worker_key]
    terminal = await sqlite_worker_leases.terminal_registrations(pair, active)
    for key, stopped, fenced in terminal:
        await db.execute(update(TaskWorker).where(
            TaskWorker.worker_key == key, TaskWorker.stopped_at.is_(None), TaskWorker.fenced_at.is_(None),
        ).values(stopped_at=datetime.fromisoformat(stopped) if stopped else None,
                 fenced_at=datetime.fromisoformat(fenced) if fenced else None)
            .execution_options(synchronize_session=False))
    if terminal:
        await _release_confirmed_slots(db, worker_key)
    return and_(Task.status == "running", Task.recovered_at.is_(None), _confirmed_absent())


async def adopt(db: AsyncSession, task_id: int, statistics_key: str | None, execution_key: str | None,
                old_worker_key: str | None, me: str) -> bool:
    # 扫描和认领是不同事务。只有已经围栏/停止/缺失的原执行者可以被替换。
    confirmed = (_confirmed_absent() if db.get_bind().dialect.name == "sqlite"
                 else ~registered_worker_exists(Task.worker_key))
    conditions = (Task.id == task_id, Task.statistics_key == statistics_key, Task.execution_key == execution_key,
                  Task.worker_key == old_worker_key, Task.status == "running", Task.recovered_at.is_(None),
                  confirmed)
    if await db.scalar(select(Task.id).where(*conditions).with_for_update(skip_locked=True)) is None:
        return False
    if not await lock_live_worker(db, me):
        return False
    changed = await db.execute(update(Task).where(*conditions).values(worker_key=me)
                               .execution_options(synchronize_session=False))
    return changed.rowcount == 1


class WorkerLease:
    def __init__(self, engine: TaskEngine, *, on_expire: Callable[[], None] | None = None):
        self.engine = engine
        self._on_expire = on_expire if on_expire is not None else engine._mark_lease_lost
        self.worker_key: str | None = None
        self.sqlite_pair: sqlite_lease_pair.SQLiteLeasePair | None = None
        self._last_ok: float | None = None
        self._deadline: asyncio.TimerHandle | None = None

    @property
    def valid(self) -> bool:
        return (self._last_ok is not None and time.monotonic() - self._last_ok
                < self.engine.lease_seconds - self.engine.lease_safety_seconds)

    def invalidate(self) -> None:
        self._last_ok = None
        if self._deadline is not None:
            self._deadline.cancel()
            self._deadline = None

    def _confirmed(self, started: float) -> None:
        self.invalidate()
        self._last_ok = started
        # 调度与心跳可能都卡在数据库。到达安全期限即通知执行体，不能等慢事务先返回。
        remaining = self.engine.lease_seconds - self.engine.lease_safety_seconds - (time.monotonic() - started)
        self._deadline = asyncio.get_running_loop().call_later(max(0.0, remaining), self._expire)

    def _expire(self) -> None:
        if self._last_ok is not None and self.valid:
            # 事件循环的定时器可能按单调时钟分辨率被略微提前触发。
            self._confirmed(self._last_ok)
        else:
            self._on_expire()

    async def register(self) -> str:
        if self.worker_key is not None:
            if self.valid:
                return self.worker_key
            await self.stop()
        key = str(uuid4())
        maker = self.engine.ctx.sessionmaker
        async with maker() as pairing:
            self.sqlite_pair = await sqlite_lease_pair.open_pair(pairing)
            self.worker_key = key
            if self.sqlite_pair is not None:
                _sqlite_leases.setdefault(pairing.get_bind().engine, WeakSet()).add(self)
        started = time.monotonic()
        pair = self.sqlite_pair
        if pair is not None:
            try:
                await sqlite_worker_leases.register(pair, key)
            except Exception:
                if not await sqlite_worker_leases.live(pair, key, self.engine.lease_seconds):
                    raise
        async with self.engine.ctx.sessionmaker() as db:
            cutoff = await _db_cutoff(db, timedelta(days=7).total_seconds())
            referenced = select(Task.id).where(
                Task.worker_key == TaskWorker.worker_key, Task.status == "running",
            ).exists()
            owns_slot = select(TaskExecutionSlot.execution_key).where(
                TaskExecutionSlot.worker_key == TaskWorker.worker_key,
            ).exists()
            owns_work = select(TaskWorkRecord.work_key).where(
                TaskWorkRecord.worker_key == TaskWorker.worker_key,
            ).exists()
            owns_deletion = select(Project.id).where(Project.deletion_worker_key == TaskWorker.worker_key).exists()
            owns_external = select(AgentChange.id).where(
                AgentChange.external_worker_key == TaskWorker.worker_key,
            ).exists()
            await db.execute(delete(TaskWorker).where(
                or_(TaskWorker.fenced_at < cutoff, TaskWorker.stopped_at < cutoff),
                ~referenced, ~owns_slot, ~owns_work, ~owns_deletion, ~owns_external,
            ).execution_options(synchronize_session=False))
            # 遗留占用仍能用其未裁剪的登记核对。下次启动再裁剪已清空的旧登记。
            await _release_confirmed_slots(db)
            if pair is not None:
                # 先取得主库写事务，再确认侧库有效；等待主库期间不占侧库的写槽。
                if not await sqlite_worker_leases.live(pair, key, self.engine.lease_seconds):
                    raise sqlite_lease_pair.SQLiteLeasePairError("SQLite 执行者登记已过期")
                retained = list(await db.scalars(select(TaskWorker.worker_key)))
                await sqlite_worker_leases.prune_terminal(pair, retained)
            db.add(TaskWorker(worker_key=key, started_at=func.now(), heartbeat_at=func.now(),
                              hostname=socket.gethostname()[:255], pid=os.getpid()))
            try:
                await db.commit()
            except Exception:
                # 数据库已提交但确认丢失：只认本次 UUID，不重放 INSERT。
                await db.rollback()
                async with maker() as proof:
                    if not await proof.scalar(select(registered_worker_exists(key))):
                        raise
        if pair is not None and not await sqlite_worker_leases.live(pair, key, self.engine.lease_seconds):
            raise sqlite_lease_pair.SQLiteLeasePairError("SQLite 执行者登记未获确认")
        self._confirmed(started)
        _local_leases[key] = self
        return key

    async def heartbeat(self) -> bool:
        key = self.worker_key
        if key is None or not self.valid:
            return False
        started = time.monotonic()
        if self.sqlite_pair is not None:
            try:
                renewed = await sqlite_worker_leases.heartbeat(self.sqlite_pair, key, self.engine.lease_seconds)
            except sqlite_lease_pair.SQLiteLeasePairError:
                logger.error("SQLite 租约存储无法核验，原执行者停止续约")
                return False
        else:
            async with self.engine.ctx.sessionmaker() as db:
                cutoff = await _db_cutoff(db, self.engine.lease_seconds)
                changed = await db.execute(update(TaskWorker).where(
                    TaskWorker.worker_key == key, _pg_live_worker_condition(cutoff),
                ).values(heartbeat_at=func.now()).execution_options(synchronize_session=False))
                await db.commit()
            renewed = changed.rowcount == 1
        if not renewed or key != self.worker_key or not self.valid:
            return False
        self._confirmed(started)
        return True

    async def stop(self) -> None:
        key = self.worker_key
        if key is None:
            return
        self.invalidate()
        pair = self.sqlite_pair
        confirmed = False
        try:
            if pair is not None:
                await sqlite_worker_leases.stop(pair, key)
                confirmed = True
            async with self.engine.ctx.sessionmaker() as db:
                if pair is None:
                    await db.scalar(select(TaskWorker.worker_key).where(TaskWorker.worker_key == key).with_for_update())
                await db.execute(update(TaskWorker).where(TaskWorker.worker_key == key, TaskWorker.stopped_at.is_(None))
                                 .values(stopped_at=func.now()))
                await _release_confirmed_slots(db, key)
                await db.commit()
                confirmed = True
        except OperationalError as exc:
            if not confirmed or not is_sqlite_busy(exc):
                raise
            # 独立租约已确认停止；主库有长事务时由下一次扫描补记，旧身份不能重获资格。
            logger.warning("SQLite 执行者已停止，主库登记等待同步")
        finally:
            if confirmed or pair is not None:
                self.worker_key = None
            if confirmed:
                _local_leases.pop(key, None)
            elif pair is not None:
                # 停止未获确认时，迟到的本地回调仍须被拒；不可提前忘记它已经在本进程失效。
                asyncio.get_running_loop().call_later(self.engine.lease_seconds + 1, _local_leases.pop, key, None)
            if pair is not None:
                pair.close()
                self.sqlite_pair = None
