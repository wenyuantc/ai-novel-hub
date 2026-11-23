"""本人和作品删除发起的运行中取消意图，绑定原任务和入队轮次。"""

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models import Task, TaskExecutionSlot, TaskWorkRecord

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine


def _unproven_execution():
    # 作品上的其他错误不是这条任务的证据，必须命中同一 UUID、轮次和原执行。
    return select(TaskWorkRecord.work_key).where(
        TaskWorkRecord.kind == "abort_failure", TaskWorkRecord.state == "failed", TaskWorkRecord.evidence.is_(None),
        TaskWorkRecord.task_id == Task.id, TaskWorkRecord.task_key == Task.statistics_key,
        TaskWorkRecord.queued_at.is_not_distinct_from(Task.queued_at),
        TaskWorkRecord.project_id.is_not_distinct_from(Task.project_id),
        TaskWorkRecord.execution_key.is_not_distinct_from(Task.execution_key),
    ).correlate(Task).exists()


async def requested_result_unproven(db: AsyncSession, task: Task) -> bool:
    return await db.scalar(select(Task.id).where(
        Task.id == task.id, Task.statistics_key == task.statistics_key, Task.queued_at == task.queued_at,
        Task.execution_key == task.execution_key, Task.worker_key == task.worker_key, Task.status == "running",
        Task.cancel_requested_at.is_not(None), _unproven_execution(),
    )) is not None


async def record_request(db: AsyncSession, task: Task) -> bool:
    """调用者已锁定原授权或作品删除意图，和它共用事务。接管可更换 worker，保留原执行 UUID。"""
    if task.statistics_key is None:
        return False
    statement = update(Task).where(
        Task.id == task.id, Task.statistics_key == task.statistics_key, Task.queued_at == task.queued_at,
        Task.execution_key == task.execution_key, Task.status == "running", Task.recovered_at.is_(None),
        Task.user_id == task.user_id, Task.project_id == task.project_id, Task.kind == task.kind,
        Task.admin_cancel_operation_id.is_(None), ~_unproven_execution(),
    )
    if task.kind == "chapter.analyze" or task.kind.startswith("wizard."):
        # 收据尚未核对的遗留 running 不能作为活模型取消，删除排空也必须有同样的原执行证据。
        statement = statement.where(select(TaskExecutionSlot.execution_key).where(
            TaskExecutionSlot.task_id == Task.id, TaskExecutionSlot.statistics_key == Task.statistics_key,
            TaskExecutionSlot.queued_at == Task.queued_at, TaskExecutionSlot.execution_key == Task.execution_key,
            TaskExecutionSlot.worker_key == Task.worker_key,
        ).correlate(Task).exists())
    changed = await db.execute(statement.values(cancel_requested_at=func.coalesce(Task.cancel_requested_at, func.now()))
                               .execution_options(synchronize_session=False))
    return changed.rowcount == 1


def _uncertain(task: Task) -> ConflictError:
    if task.kind == "chapter.analyze":
        return ConflictError("分析执行结果尚未核对，不能取消")
    if task.kind.startswith("wizard."):
        return ConflictError("向导执行结果尚未核对，不能取消")
    return ConflictError("任务执行结果尚未核对，不能取消", extra={"code": "task_cleanup_unproven"})


async def _original(
    db: AsyncSession, task_id: int, key: str | None, queued_at: datetime | None, *, check_uncertain: bool = True,
) -> Task:
    found = (await db.execute(select(Task, _unproven_execution()).where(Task.id == task_id, Task.statistics_key == key)
                              .execution_options(populate_existing=True))).one_or_none()
    if found is None:
        raise NotFoundError("任务不存在")
    task, unproven = found
    if task.queued_at != queued_at:
        raise ConflictError("任务执行轮次已变化，请刷新后重试", extra={"code": "task_round_changed"})
    if check_uncertain and unproven and task.status == "running" and task.admin_cancel_operation_id is None:
        raise _uncertain(task)
    return task


async def wait_for_result(
    db: AsyncSession, engine: "TaskEngine", task_id: int, key: str | None, queued_at: datetime | None,
    execution_key: str | None, *, admin_operation: str | None, deadline: float, local_work: asyncio.Task | None = None,
) -> Task:
    """每次从新快照观察。无论等待的是本人或管理员，都不持业务锁或重新预算十秒。"""
    while True:
        failure = local_work.result() if local_work and local_work.done() and not local_work.cancelled() else None
        row = await _original(db, task_id, key, queued_at, check_uncertain=admin_operation is None)
        if admin_operation is not None:
            if row.admin_cancel_operation_id != admin_operation:
                return row
        else:
            if row.status != "running":
                return row
            if row.execution_key != execution_key:
                raise ConflictError("任务执行轮次已变化，请刷新后重试", extra={"code": "task_round_changed"})
            if engine.execution_is_uncertain(row):
                raise _uncertain(row)
            if row.admin_cancel_operation_id is not None:
                admin_operation = row.admin_cancel_operation_id
                engine.request_admin_cancel(key, admin_operation)
            elif failure is not None and row.worker_key == failure[0]:
                raise failure[1]
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return row
        await db.rollback()
        await asyncio.sleep(min(remaining, 0.05))


async def request_running(db: AsyncSession, engine: "TaskEngine", task: Task) -> Task:
    task_id, key, queued_at, execution = task.id, task.statistics_key, task.queued_at, task.execution_key
    worker = task.worker_key
    operation = None
    if engine.execution_is_uncertain(task):
        raise _uncertain(task)
    if not await record_request(db, task):
        row = await _original(db, task_id, key, queued_at)
        if row.status == "running" and row.admin_cancel_operation_id is None:
            if engine.execution_is_uncertain(row) or (row.execution_key == execution and (
                    row.kind == "chapter.analyze" or row.kind.startswith("wizard."))):
                raise _uncertain(row)
            raise ConflictError("任务执行轮次已变化，请刷新后重试", extra={"code": "task_round_changed"})
        if row.admin_cancel_operation_id is None:
            return row
        operation = row.admin_cancel_operation_id
        await db.rollback()
    else:
        try:
            await db.commit()
        except asyncio.CancelledError:
            # 客户端可能在数据库已提交、回执尚未抵达时断连；接管者自行核验证据。
            engine.request_worker.recover_cancel(task_id, key, queued_at, execution, retry=True)
            raise
        except Exception:
            engine.request_worker.recover_cancel(task_id, key, queued_at, execution, retry=True)
            await db.rollback()
            proof = await _original(db, task_id, key, queued_at)
            # 已终结时直接返回真实结果；若原 running 的意图并未提交，绝不能凭本地信号取消。
            if proof.status != "running":
                return proof
            if proof.execution_key != execution:
                raise
            operation = proof.admin_cancel_operation_id
            if operation is None and proof.cancel_requested_at is None:
                raise
            await db.rollback()
    deadline = asyncio.get_running_loop().time() + engine.cancel_wait_seconds
    engine.notify()
    local_work = None
    if operation is not None:
        engine.request_admin_cancel(key, operation)
    elif worker is not None and worker == engine.lease.worker_key and engine.lease.valid:
        await engine.cancel_running(task_id, statistics_key=key, expected_queued_at=queued_at,
                                    expected_execution_key=execution)
    else:
        local_work = engine.request_worker.recover_cancel(task_id, key, queued_at, execution, retry=True)
    return await wait_for_result(db, engine, task_id, key, queued_at, execution,
                                 admin_operation=operation, deadline=deadline, local_work=local_work)


async def scan(engine: "TaskEngine") -> None:
    """只通知本机持有的原执行体。信号同步送出，调度协程不等待逐项收尾。"""
    if not engine._running or engine.lease.worker_key is None or not engine.lease.valid:
        return
    async with engine.ctx.sessionmaker() as db:
        rows = (await db.execute(select(Task.id, Task.statistics_key, Task.queued_at, Task.execution_key)
            .where(Task.worker_key == engine.lease.worker_key, Task.status == "running",
                   Task.cancel_requested_at.is_not(None), Task.admin_cancel_operation_id.is_(None)))).all()
    for task_id, key, queued_at, execution in rows:
        engine.signal_running_cancel(task_id, statistics_key=key, expected_queued_at=queued_at,
                                     expected_execution_key=execution)
