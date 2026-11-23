"""管理员跨事务清理的工作登记。业务回退与回退收据必须一起提交。"""

from contextvars import ContextVar
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Task, TaskWorkRecord
from app.services import chapter_claim
from app.services.task_engine import cleanup_work, workers

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine

KIND = "admin_cleanup"
current: ContextVar[TaskWorkRecord | None] = ContextVar("admin_cleanup", default=None)


def _task_identity(task: Task, operation_id: str):
    return (
        Task.id == task.id, Task.statistics_key == task.statistics_key, Task.queued_at == task.queued_at,
        Task.execution_key == task.execution_key, Task.worker_key == task.worker_key, Task.status == task.status,
        Task.kind == task.kind, Task.project_id == task.project_id, Task.user_id == task.user_id,
        Task.admin_cancel_operation_id == operation_id,
    )


async def _lock_task(db: AsyncSession, task: Task, operation_id: str) -> bool:
    # 包括 SQLite：一次条件写同时建立真实写事务，并与领取、轮次变更和另一 drain 的认领串行。
    result = await db.execute(update(Task).where(*_task_identity(task, operation_id))
                              .values(admin_cancel_operation_id=operation_id, updated_at=Task.updated_at)
                              .execution_options(synchronize_session=False))
    return result.rowcount == 1


def _new_record(task: Task, operation_id: str, recovery: bool) -> TaskWorkRecord:
    return TaskWorkRecord(work_key=operation_id, kind=KIND, project_id=task.project_id, task_id=task.id,
                          task_key=task.statistics_key, queued_at=task.queued_at, execution_key=task.execution_key,
                          operation_id=operation_id, state="ready", evidence={"recovery": recovery})


async def reserve_adopted(db: AsyncSession, task_id: int, *, recovery: bool) -> None:
    """C1 已条件更新持有者并锁住 Task。在同一提交中固定首次接管的原因。"""
    task = await db.scalar(select(Task).where(Task.id == task_id, Task.admin_cancel_operation_id.is_not(None)))
    if task is not None and await db.get(TaskWorkRecord, task.admin_cancel_operation_id) is None:
        db.add(_new_record(task, task.admin_cancel_operation_id, recovery))


async def claim(
    engine: "TaskEngine", task: Task, operation_id: str, attempt: str, *, recovery: bool,
) -> TaskWorkRecord | None:
    async with engine.ctx.sessionmaker() as db:
        if not await _lock_task(db, task, operation_id):
            return None
        if await db.scalar(select(TaskWorkRecord.work_key).where(
            TaskWorkRecord.kind == "request_cleanup", TaskWorkRecord.task_key == task.statistics_key,
            TaskWorkRecord.task_id == task.id, TaskWorkRecord.queued_at == task.queued_at,
        ).limit(1)):
            return None
        me = await engine.request_worker.require(db)
        record = await db.get(TaskWorkRecord, operation_id, with_for_update=True)
        if record is None:
            record = _new_record(task, operation_id, recovery)
            db.add(record)
        else:
            if (record.kind != KIND or record.operation_id != operation_id
                    or record.task_id != task.id or record.task_key != task.statistics_key
                    or record.queued_at is None
                    or chapter_claim._utc(record.queued_at) != chapter_claim._utc(task.queued_at)
                    or record.project_id != task.project_id or record.execution_key != task.execution_key
                    or record.state not in ("active", "aborted", "ready", "ready_aborted")
                    or not isinstance(record.evidence, dict)
                    or type(record.evidence.get("recovery")) is not bool):
                return None
            if record.worker_key == me:
                if record.evidence.get("attempt") in engine.admin_cancellations.attempts:
                    return None
            elif record.worker_key is None:
                if record.state not in ("ready", "ready_aborted"):
                    return None
            elif not await db.scalar(select(workers.confirmed_terminal(record.worker_key))):
                return None
        record.worker_key = me
        record.state = "aborted" if record.state in ("aborted", "ready_aborted") else "active"
        record.evidence = {**record.evidence, "attempt": attempt}
        await engine.admin_cancellations.commit_claim(db, task.statistics_key, operation_id)
        return record


async def abort_once(engine: "TaskEngine", task: Task, record: TaskWorkRecord, handler) -> bool:
    if record.state == "aborted":
        return True
    async with engine.ctx.sessionmaker() as db:
        # handler 的独立会话仍有各自的提交/回滚，实际落在同一外层事务的保存点中。
        # 开始时不取业务写锁：纯内存的等待不会阻断受理、心跳、读取或删除意图。
        async with cleanup_work.atomic_handler_context(engine, db) as context:
            await handler.on_abort(context, task)
        if not await _lock_task(db, task, record.operation_id):
            return False
        actual = await db.execute(update(TaskWorkRecord).where(
            TaskWorkRecord.work_key == record.work_key, TaskWorkRecord.worker_key == record.worker_key,
            TaskWorkRecord.kind == KIND, TaskWorkRecord.state == "active",
            TaskWorkRecord.evidence["attempt"].as_string() == record.evidence["attempt"],
        ).values(state="aborted"))
        if actual.rowcount != 1:
            return False
        await engine.request_worker.require(db, expected_key=record.worker_key)
        await db.commit()
        return True


def _owned(record: TaskWorkRecord):
    return (
        TaskWorkRecord.work_key == record.work_key, TaskWorkRecord.kind == KIND,
        TaskWorkRecord.worker_key == record.worker_key,
        TaskWorkRecord.evidence["attempt"].as_string() == record.evidence["attempt"],
    )


async def check_owner(db: AsyncSession) -> None:
    record = current.get()
    if record is None:
        return
    if (await db.scalar(select(TaskWorkRecord.work_key).where(*_owned(record)).with_for_update()) is None
            or not await workers.lock_live_worker(db, record.worker_key)):
        raise ConflictError("管理员取消处理权已变化")


async def finish(db: AsyncSession, task: Task, operation_id: str) -> None:
    """仅随原轮次的终态或章节清理收据一起归还。"""
    await check_owner(db)
    await db.execute(delete(TaskWorkRecord).where(
        TaskWorkRecord.work_key == operation_id, TaskWorkRecord.kind == KIND,
        TaskWorkRecord.task_key == task.statistics_key, TaskWorkRecord.task_id == task.id,
        TaskWorkRecord.queued_at == task.queued_at,
    ))


def _pending():
    return select(Task.id).where(Task.id == TaskWorkRecord.task_id, Task.statistics_key == TaskWorkRecord.task_key,
                                 Task.queued_at == TaskWorkRecord.queued_at,
                                 Task.admin_cancel_operation_id == TaskWorkRecord.operation_id).exists()


async def relinquish(engine: "TaskEngine", record: TaskWorkRecord) -> None:
    """活处理者已退出。保留原处理模式/业务回执，允许下一扫描者继续。"""
    async with engine.ctx.sessionmaker() as db:
        await db.execute(delete(TaskWorkRecord).where(*_owned(record), ~_pending()))
        for old, new in (("active", "ready"), ("aborted", "ready_aborted")):
            await db.execute(update(TaskWorkRecord).where(*_owned(record), TaskWorkRecord.state == old)
                             .values(state=new, worker_key=None))
        await db.commit()


async def reap_finished(db: AsyncSession) -> None:
    conditions = (
        TaskWorkRecord.kind == KIND, ~_pending(),
        TaskWorkRecord.worker_key.is_(None) | workers.confirmed_terminal(TaskWorkRecord.worker_key),
    )
    if await db.scalar(select(TaskWorkRecord.work_key).where(*conditions).limit(1)) is not None:
        await db.execute(delete(TaskWorkRecord).where(*conditions))
        await db.commit()
