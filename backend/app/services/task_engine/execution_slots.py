"""正常领取从提交到原执行体收尾完成所持有的全站名额。"""

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task, TaskExecutionSlot
from app.services import task_service
from app.services.task_engine.execution_identity import TaskExecutionIdentity


def dispatchable_tasks():
    """预选和领取门内的队首检查必须使用相同的队列条件。"""
    return (
        Task.status == "queued", Task.execution_key.is_(None), Task.admin_cancel_operation_id.is_(None),
        task_service.visible_task_project_exists(),
    )


async def can_claim(db: AsyncSession, task: Task, global_limit: int, user_limit: int | None) -> bool:
    """调用方持有 authz.gate，直至领取和登记名额一起提交。"""
    conflict = TaskExecutionSlot.task_id == task.id
    if task.serial_key is not None:
        conflict |= TaskExecutionSlot.serial_key == task.serial_key
    total, owned, conflicts = (await db.execute(select(
        func.count(), func.count().filter(TaskExecutionSlot.user_id == task.user_id), func.count().filter(conflict),
    ).select_from(TaskExecutionSlot))).one()
    if total >= global_limit or (user_limit is not None and owned >= user_limit) or conflicts:
        return False
    if task.serial_key is None:
        return True
    earlier = select(Task.id).where(*dispatchable_tasks(), Task.id < task.id, Task.serial_key == task.serial_key)
    return not await db.scalar(select(earlier.exists()))


def reserve(db: AsyncSession, execution: TaskExecutionIdentity) -> None:
    db.add(TaskExecutionSlot(
        execution_key=execution.execution_key, worker_key=execution.worker_key, task_id=execution.task_id,
        statistics_key=execution.task_key, queued_at=execution.queued_at, user_id=execution.actor_id,
        serial_key=execution.serial_key,
    ))


def same_slot(execution: TaskExecutionIdentity):
    return (
        TaskExecutionSlot.execution_key == execution.execution_key,
        TaskExecutionSlot.worker_key == execution.worker_key, TaskExecutionSlot.task_id == execution.task_id,
        TaskExecutionSlot.statistics_key == execution.task_key, TaskExecutionSlot.queued_at == execution.queued_at,
    )


async def release(db: AsyncSession, execution: TaskExecutionIdentity) -> None:
    await db.execute(delete(TaskExecutionSlot).where(*same_slot(execution)))
    await db.commit()
