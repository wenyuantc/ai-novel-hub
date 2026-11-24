"""会话操作跨提交串行。等待与长 I/O 期间不占用数据库事务。"""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models import AgentConversation
from app.models.agent_operation import AgentConversationOperation as Operation
from app.services.agent import transactions
from app.services.agent.external_activity import drain
from app.services.ai_dispatch import is_sqlite_busy
from app.services.task_engine import workers
from app.services.task_engine.request_worker import RequestWorker

_CURRENT = "agent_conversation_operation"
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Claim:
    conversation_id: int
    conversation_key: str
    key: str
    worker: RequestWorker
    worker_key: str

    def matches(self):
        return operation_matches(self.conversation_id, self.key, self.worker_key)


def operation_matches(conversation_id: int, key: str, worker_key: str):
    return (
        Operation.conversation_id == conversation_id, Operation.operation_key == key,
        Operation.worker_key == worker_key,
    )


def _busy() -> ConflictError:
    return ConflictError("会话有进行中的操作，请稍候再试")


def _transient(exc: DBAPIError) -> bool:
    return transactions.is_serialization_error(exc) or isinstance(exc, OperationalError) and is_sqlite_busy(exc)


async def _take(db: AsyncSession, claim: _Claim, user_id: int, project_id: int) -> bool:
    # 短事务与中央任务入口共用会话行。PG RR 若已设置更早的快照，会话行写入会使后到者重试，
    # 从而不能在旧快照中漏掉新的跨提交占用。显式保留业务时间戳。
    conversation = AgentConversation
    available = ~select(Operation.conversation_id).where(
        Operation.conversation_id == conversation.id, ~workers.confirmed_terminal(Operation.worker_key),
    ).exists()
    row = await db.execute(update(conversation).where(
        conversation.id == claim.conversation_id, conversation.instance_key == claim.conversation_key,
        conversation.user_id == user_id,
        conversation.project_id == project_id, available,
    ).values(updated_at=conversation.updated_at))
    if row.rowcount != 1:
        return False
    await claim.worker.require(db, claim.worker_key)
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    values = {"operation_key": claim.key, "worker_key": claim.worker_key}
    result = await db.execute(insert(Operation).values(conversation_id=claim.conversation_id, **values)
                              .on_conflict_do_update(index_elements=[Operation.conversation_id], set_=values,
                                                     where=workers.confirmed_terminal(Operation.worker_key)))
    return result.rowcount == 1


async def _claim_or_wait(claim: _Claim, user_id: int, project_id: int) -> None:
    worker = claim.worker
    factory = worker.engine.ctx.sessionmaker
    while worker.valid and worker.worker_key == claim.worker_key:
        try:
            async with factory() as db:
                holder = (await db.execute(select(AgentConversation.id, Operation.worker_key).outerjoin(
                    Operation, Operation.conversation_id == AgentConversation.id,
                ).where(
                    AgentConversation.id == claim.conversation_id,
                    AgentConversation.instance_key == claim.conversation_key, AgentConversation.user_id == user_id,
                    AgentConversation.project_id == project_id,
                ))).one_or_none()
                if holder is None:
                    raise NotFoundError("会话不存在")
                holder_key = holder.worker_key
                dead = holder_key is not None and await db.scalar(select(workers.confirmed_terminal(holder_key)))
                if holder_key is not None and not dead:
                    live = await workers.lock_live_worker(
                        db, holder_key, worker.engine.lease_seconds, read_only=True,
                    )
            if holder_key is None or dead:
                async with factory() as db:
                    if await _take(db, claim, user_id, project_id):
                        await db.commit()
                        return
                    # 另一个领取者先提交时，空更新可能已改到会话行而最终 CAS 失败。
                    # 回滚这次尝试，不能让落败方提交新行版本并冲掉胜方的 RR 快照。
                    await db.rollback()
            elif not live:
                # 围栏独立提交，等待期间不持作品、会话或原业务事务。
                async with factory.begin() as db:
                    await workers.prepare_orphan_scan(db, worker.engine.lease_seconds, worker_key=holder_key)
        except DBAPIError as exc:
            if not _transient(exc):
                raise
        await asyncio.sleep(0.05)
    raise ConflictError("请求执行身份已变化，请核对后重试", extra={"code": "task_execution_changed"})


async def _release(claim: _Claim) -> None:
    delay = 0.05
    worker = claim.worker
    while True:
        try:
            async with worker.engine.ctx.sessionmaker.begin() as db:
                await db.execute(delete(Operation).where(*claim.matches()))
            return
        except DBAPIError:
            # 如无法确认提交，按同一个操作键重查/回收。永久失效的归属由下一执行者接手。
            if worker.worker_key != claim.worker_key or not worker.valid:
                return
            if delay == 0.05:
                logger.warning("智能体会话操作释放尚未确认")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 1.0)


@asynccontextmanager
async def hold(
    db: AsyncSession, worker: RequestWorker, conversation_id: int, user_id: int, project_id: int, *,
    conversation_key: str,
):
    if not conversation_key:
        raise NotFoundError("会话不存在")
    current = db.info.get(_CURRENT)
    if current is not None:
        if current.conversation_id != conversation_id or current.worker is not worker:
            raise _busy()
        if current.conversation_key != conversation_key:
            raise NotFoundError("会话不存在")
        yield
        return
    if db.in_transaction():
        raise RuntimeError("等待智能体会话操作前须结束原事务")
    worker_key = worker.worker_key
    if worker_key is None:
        raise ConflictError("请求执行身份已变化，请核对后重试", extra={"code": "task_execution_changed"})
    claim = _Claim(conversation_id, conversation_key, str(uuid4()), worker, worker_key)
    try:
        await _claim_or_wait(claim, user_id, project_id)
        db.info[_CURRENT] = claim
        yield
    finally:
        db.info.pop(_CURRENT, None)

        async def finish():
            try:
                if db.in_transaction():
                    await db.rollback()
            finally:
                await _release(claim)

        await drain(finish())


async def guard(db: AsyncSession, conversation_id: int, *, conversation_key: str | None = None) -> str | None:
    """业务锁内复核原操作。中央任务入口只使用一个业务事务，遇到跨提交占用时立即冲突。"""
    current = db.info.get(_CURRENT)
    conversation = AgentConversation
    if current is not None:
        if current.conversation_id != conversation_id:
            raise _busy()
        if conversation_key is not None and conversation_key != current.conversation_key:
            raise NotFoundError("会话不存在")
        conversation_key = current.conversation_key
    if not conversation_key:
        raise NotFoundError("会话不存在")
    criteria = (conversation.id == conversation_id, conversation.instance_key == conversation_key)
    if db.get_bind().dialect.name == "sqlite":
        guarded = await db.execute(update(conversation).where(*criteria).values(updated_at=conversation.updated_at))
        matched = guarded.rowcount == 1
    else:
        matched = await db.scalar(select(conversation.id).where(*criteria).with_for_update()) is not None
    if not matched:
        raise NotFoundError("会话不存在")
    if current is None:
        if await db.scalar(select(Operation.conversation_id).where(
            Operation.conversation_id == conversation_id, ~workers.confirmed_terminal(Operation.worker_key),
        )) is not None:
            raise _busy()
        return None
    if await db.scalar(select(Operation.conversation_id).where(*current.matches())) is None:
        raise _busy()
    await current.worker.require(db, current.worker_key)
    return current.key
