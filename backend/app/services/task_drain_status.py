"""管理端只读排空状态。"""

import sqlite3

from sqlalchemy import DateTime, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models import BackupRun, SystemSettings, Task, TaskExecutionSlot
from app.models.entities import OPEN_TASK_STATUSES
from app.schemas.system_settings import TaskDrainStatus, TaskDrainWorker
from app.services.project_lifecycle import as_utc
from app.services.system_settings_snapshot import effective_settings_from_row
from app.services.task_engine import workers
from app.services.task_engine.sqlite_lease_pair import SQLiteLeasePairError


class DrainStatusUnavailable(AppError):
    status_code = 503


async def read_status(db: AsyncSession, *, lease_seconds: float) -> TaskDrainStatus:
    # PostgreSQL READ COMMITTED 每条语句一个快照。计数和开关在同一语句读取，
    # 避免跨语句读到父任务已完成、子任务尚未入队且执行槽又已释放的组合。
    open_counts = select(*(
        func.count().filter(Task.status == status).label(status) for status in OPEN_TASK_STATUSES
    )).where(Task.status.in_(OPEN_TASK_STATUSES)).subquery()
    covered_slot = select(Task.id).where(
        Task.id == TaskExecutionSlot.task_id,
        Task.statistics_key == TaskExecutionSlot.statistics_key,
        Task.queued_at == TaskExecutionSlot.queued_at,
        Task.execution_key == TaskExecutionSlot.execution_key,
        Task.worker_key == TaskExecutionSlot.worker_key,
        Task.status.in_(OPEN_TASK_STATUSES),
    ).correlate(TaskExecutionSlot).exists()
    finalizing = select(func.count(TaskExecutionSlot.execution_key)).where(~covered_slot).scalar_subquery()
    backups = select(func.count(BackupRun.id)).where(BackupRun.active_slot == 1).scalar_subquery()
    sampled_at = (
        func.statement_timestamp(type_=DateTime(timezone=True)) if db.get_bind().dialect.name == "postgresql"
        else func.now()
    )
    statement = (
        select(SystemSettings, sampled_at, open_counts.c.queued, open_counts.c.running, open_counts.c.paused,
               finalizing, backups)
        .select_from(open_counts)
        .outerjoin(SystemSettings, SystemSettings.id == 1)
        .execution_options(populate_existing=True)
    )
    try:
        with db.no_autoflush:
            snapshot = await db.execute(statement)
            stored, sampled, queued, running, paused, finalizing_count, active_backups = snapshot.one()
            enabled = effective_settings_from_row(db, stored).task_admission_closed
            registrations = await workers.live_registrations(db, lease_seconds)
    except (SQLAlchemyError, sqlite3.Error, SQLiteLeasePairError):
        raise DrainStatusUnavailable("排空状态暂时无法确认，请稍后刷新重试") from None

    processes: dict[tuple[str, int, str | None], TaskDrainWorker] = {}
    for registration in registrations:
        key = (registration.hostname, registration.pid, registration.peer_url)
        started_at = as_utc(registration.started_at)
        heartbeat_at = as_utc(registration.heartbeat_at)
        if (previous := processes.get(key)) is not None:
            started_at = min(previous.started_at, started_at)
            heartbeat_at = max(previous.heartbeat_at, heartbeat_at)
        processes[key] = TaskDrainWorker(
            hostname=registration.hostname, pid=registration.pid, started_at=started_at, heartbeat_at=heartbeat_at,
        )
    return TaskDrainStatus(
        enabled=enabled, sampled_at=as_utc(sampled), queued=queued, running=running, paused=paused,
        finalizing=finalizing_count, active_backups=active_backups,
        workers=sorted(processes.values(), key=lambda worker: (worker.hostname, worker.pid, worker.started_at)),
    )
