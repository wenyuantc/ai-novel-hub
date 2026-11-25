"""任务首次成功／失败的同事务站内通知，检查成功按运行身份单独投递。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConsistencyRun, Project, Task
from app.services.collaboration_task_access import task_grant_identity, task_visible_to_actor
from app.services.notification_service import deliver


async def notify_terminal(db: AsyncSession, task: Task, status: str) -> None:
    grant = task_grant_identity(task.input)
    grant_id = grant[0] if grant else None
    if task.kind != "consistency.check" or status != "succeeded":
        await deliver(
            db, recipient_id=task.user_id, kind="task", event_key=f"task:{task.statistics_key}",
            variant=status, project_id=task.project_id, grant_id=grant_id,
            task_id=task.id, task_key=task.statistics_key,
        )
        return

    run_id = task.result.get("run_id") if isinstance(task.result, dict) else None
    if type(run_id) is not int:
        return
    # 运行概要与任务终态分两次提交。只有同一作品、发起人和任务的成功运行才可提醒作者。
    owner_id = await db.scalar(
        select(Project.user_id)
        .select_from(ConsistencyRun).join(Project, Project.id == ConsistencyRun.project_id)
        .join(Task, Task.id == ConsistencyRun.task_id)
        .where(ConsistencyRun.id == run_id, ConsistencyRun.project_id == task.project_id,
               ConsistencyRun.actor_id == task.user_id, ConsistencyRun.status == "succeeded",
               Task.id == task.id, Task.statistics_key == task.statistics_key,
               task_visible_to_actor(db, task.user_id, active_grant_required=True))
    )
    if owner_id is None:
        return
    for recipient_id in sorted({task.user_id, owner_id}):
        await deliver(
            db, recipient_id=recipient_id, kind="consistency", event_key=f"consistency:{task.statistics_key}",
            project_id=task.project_id, run_id=run_id,
            grant_id=grant_id if recipient_id == task.user_id else None,
        )
