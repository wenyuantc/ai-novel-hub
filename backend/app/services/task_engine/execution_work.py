"""作品执行体登记在领取事务中创建，在原协程退出后才消耗。"""

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TaskWorkRecord
from app.services.task_engine.execution_identity import TaskExecutionIdentity


def register(db: AsyncSession, execution: TaskExecutionIdentity) -> None:
    db.add(TaskWorkRecord(
        work_key=execution.execution_key, kind="execution", state="active", worker_key=execution.worker_key,
        project_id=execution.project_id, task_id=execution.task_id, task_key=execution.task_key,
        queued_at=execution.queued_at, execution_key=execution.execution_key,
    ))


async def confirm_exit(db: AsyncSession, execution: TaskExecutionIdentity) -> None:
    await db.execute(delete(TaskWorkRecord).where(
        TaskWorkRecord.work_key == execution.execution_key, TaskWorkRecord.kind == "execution",
        TaskWorkRecord.state == "active", TaskWorkRecord.worker_key == execution.worker_key,
        TaskWorkRecord.task_id == execution.task_id, TaskWorkRecord.task_key == execution.task_key,
        TaskWorkRecord.queued_at == execution.queued_at, TaskWorkRecord.project_id == execution.project_id,
        TaskWorkRecord.execution_key == execution.execution_key,
    ))
    await db.commit()
