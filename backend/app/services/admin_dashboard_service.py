"""实例级只读统计与每日规模采样，不读取任务内容或模型配置。"""

import asyncio
import logging
from contextlib import suppress
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import AppError
from app.models import (
    Chapter,
    DashboardDailySnapshot,
    DashboardTracking,
    Project,
    Task,
    TaskStatistic,
    User,
    WorkshopSubmission,
)
from app.schemas.admin_dashboard import (
    AdminDashboardHistory,
    AdminDashboardOverview,
    AdminDashboardTasks,
    DashboardComparison,
    DashboardDays,
    DashboardHistoryPoint,
    DashboardTaskDay,
    DashboardTotals,
)
from app.services.project_lifecycle import as_utc, now_utc

logger = logging.getLogger(__name__)
STATISTICS_TIMEZONE = ZoneInfo("Asia/Shanghai")
SAMPLE_INTERVAL = 60.0
RESULT_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")


class DashboardUnavailableError(AppError):
    status_code = 503


async def _tracking(db: AsyncSession) -> DashboardTracking:
    tracking = await db.get(DashboardTracking, 1)
    if tracking is None:
        raise DashboardUnavailableError("管理统计尚未初始化，请稍后重试")
    return tracking


def _count(column, *conditions):
    return select(func.count(column)).where(*conditions).scalar_subquery()


async def _totals(db: AsyncSession) -> DashboardTotals:
    # 独立标量子查询避免连接用户、作品和章节时产生乘积；包含回收站和删除中作品。
    row = (
        await db.execute(select(
            _count(User.id).label("users"),
            _count(Project.id).label("projects"),
            _count(Chapter.id).label("chapters"),
            select(func.coalesce(func.sum(Chapter.word_count), 0)).scalar_subquery().label("total_words"),
            _count(WorkshopSubmission.id, WorkshopSubmission.status == "pending").label("pending_submissions"),
        ))
    ).mappings().one()
    return DashboardTotals.model_validate(dict(row))


async def overview(db: AsyncSession) -> AdminDashboardOverview:
    as_of = now_utc()
    tracking = await _tracking(db)
    totals = await _totals(db)
    return AdminDashboardOverview(
        as_of=as_of, tracking_since=as_utc(tracking.started_at), **totals.model_dump()
    )


async def tasks(db: AsyncSession) -> AdminDashboardTasks:
    as_of = now_utc()
    tracking = await _tracking(db)
    since = as_utc(tracking.started_at)
    finished = (
        TaskStatistic.finished_event_at >= since,
        TaskStatistic.finished_event_at <= as_of,
    )
    fields = [
        _count(TaskStatistic.key, TaskStatistic.origin == "baseline").label("baseline_tasks"),
        _count(
            TaskStatistic.key,
            TaskStatistic.origin == "new",
            TaskStatistic.created_event_at >= since,
            TaskStatistic.created_event_at <= as_of,
        ).label("new_tasks"),
        *(_count(Task.id, Task.status == status).label(status) for status in ("queued", "running", "paused")),
        *(
            _count(TaskStatistic.key, TaskStatistic.status == status, *finished).label(status)
            for status in RESULT_STATUSES
        ),
        *(
            _count(
                TaskStatistic.key,
                TaskStatistic.status == status,
                *finished,
                TaskStatistic.finished_event_at > as_of - timedelta(hours=24),
            ).label(f"{status}_24h")
            for status in ("failed", "interrupted")
        ),
        _count(
            TaskStatistic.key,
            TaskStatistic.removed_at.is_not(None),
            TaskStatistic.finished_event_at.is_(None),
            TaskStatistic.status.not_in(RESULT_STATUSES),
        ).label("removed_unfinished"),
    ]
    row = (await db.execute(select(*fields))).mappings().one()
    return AdminDashboardTasks(as_of=as_of, tracking_since=since, **dict(row))


def _day_start(day: date) -> datetime:
    return as_utc(datetime.combine(day, time.min, tzinfo=STATISTICS_TIMEZONE))


def _day_buckets(column, days: list[date]):
    # 使用 UTC 区间 CASE，PostgreSQL / SQLite 不依赖各自的时区日期函数。
    return case(*(
        (and_(column >= _day_start(day), column < _day_start(day + timedelta(days=1))), day.isoformat())
        for day in days
    ))


async def _task_days(
    db: AsyncSession, days: list[date], since: datetime, as_of: datetime
) -> dict[date, dict[str, int]]:
    start = max(_day_start(days[0]), since)
    counts: dict[date, dict[str, int]] = {}
    created_day = _day_buckets(TaskStatistic.created_event_at, days)
    new_rows = await db.execute(
        select(created_day.label("day"), func.count(TaskStatistic.key))
        .where(
            TaskStatistic.origin == "new", TaskStatistic.created_event_at >= start,
            TaskStatistic.created_event_at <= as_of,
        )
        .group_by(created_day)
    )
    for day, count in new_rows:
        if day is not None:
            counts.setdefault(date.fromisoformat(day), {})["created"] = int(count)
    finished_day = _day_buckets(TaskStatistic.finished_event_at, days)
    result_rows = await db.execute(
        select(finished_day.label("day"), TaskStatistic.status, func.count(TaskStatistic.key))
        .where(
            TaskStatistic.status.in_(RESULT_STATUSES), TaskStatistic.finished_event_at >= start,
            TaskStatistic.finished_event_at <= as_of,
        )
        .group_by(finished_day, TaskStatistic.status)
    )
    for day, status, count in result_rows:
        if day is not None:
            counts.setdefault(date.fromisoformat(day), {})[status] = int(count)
    return counts


async def history(db: AsyncSession, days: DashboardDays = 30) -> AdminDashboardHistory:
    as_of = now_utc()
    tracking = await _tracking(db)
    since = as_utc(tracking.started_at)
    today = as_of.astimezone(STATISTICS_TIMEZONE).date()
    start = today - timedelta(days=days - 1)
    dates = [start + timedelta(days=offset) for offset in range(days)]
    snapshots = {
        row.day: row for row in await db.scalars(
            select(DashboardDailySnapshot).where(
                DashboardDailySnapshot.day >= start, DashboardDailySnapshot.day <= today,
                DashboardDailySnapshot.captured_at >= since, DashboardDailySnapshot.captured_at <= as_of,
            )
        )
    }
    task_counts = await _task_days(db, dates, since, as_of)
    tracking_day = since.astimezone(STATISTICS_TIMEZONE).date()
    points = []
    for day in dates:
        snapshot = snapshots.get(day) if day >= tracking_day else None
        observed = day >= tracking_day and (snapshot is not None or day in task_counts)
        captured_at = as_utc(snapshot.captured_at) if snapshot is not None else None
        incomplete_sample = captured_at is None or captured_at < (
            _day_start(day + timedelta(days=1)) - timedelta(seconds=SAMPLE_INTERVAL)
        )
        points.append(DashboardHistoryPoint(
            day=day,
            captured_at=captured_at,
            totals=DashboardTotals.model_validate({
                field: getattr(snapshot, field) for field in DashboardTotals.model_fields
            }) if snapshot is not None else None,
            tasks=DashboardTaskDay(**task_counts.get(day, {})) if observed else None,
            partial=day in (tracking_day, today) or incomplete_sample,
        ))
    first, last = points[0].totals, points[-1].totals
    comparable = first is not None and last is not None and len(points) > 1
    comparison = DashboardComparison(
        from_day=start,
        to_day=today,
        **{
            field: getattr(last, field) - getattr(first, field) if comparable else None
            for field in ("users", "projects", "chapters", "total_words")
        },
    )
    return AdminDashboardHistory(
        as_of=as_of, tracking_since=since, days=days, start_date=start, end_date=today,
        points=points, comparison=comparison,
    )


async def collect_snapshot(
    sessionmaker: async_sessionmaker[AsyncSession], *, captured_at: datetime | None = None
) -> None:
    """在独立事务里记录当日最后成功值；失败时由会话回滚，不改变原日记录。"""
    stamp = as_utc(captured_at) if captured_at is not None else now_utc()
    async with sessionmaker() as db:
        tracking = await _tracking(db)
        if stamp < as_utc(tracking.started_at):
            raise ValueError("采样时间早于统计起点")
        values = {
            "day": stamp.astimezone(STATISTICS_TIMEZONE).date(),
            "captured_at": stamp,
            **(await _totals(db)).model_dump(),
        }
        dialect = db.get_bind().dialect.name
        if dialect not in ("sqlite", "postgresql"):
            raise ValueError("当前数据库不支持管理统计采样")
        insert = sqlite_insert if dialect == "sqlite" else pg_insert
        statement = insert(DashboardDailySnapshot).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[DashboardDailySnapshot.day],
            set_={field: getattr(statement.excluded, field) for field in values if field != "day"},
            where=DashboardDailySnapshot.captured_at <= statement.excluded.captured_at,
        )
        await db.execute(statement)
        await db.commit()


class DashboardSampler:
    """单进程采样协程；页面未打开也采样，停止时撤销正在等待或写入的采样。"""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], *, interval: float = SAMPLE_INTERVAL):
        if interval <= 0:
            raise ValueError("采样间隔必须大于零")
        self.sessionmaker = sessionmaker
        self.interval = interval
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()

    async def start(self) -> None:
        async with self._lock:
            if self._task is not None:
                return
            self._stop.clear()
            await self._pass()
            self._task = asyncio.create_task(self._run(), name="admin-dashboard-sampler")

    async def _pass(self) -> None:
        try:
            await collect_snapshot(self.sessionmaker)
        except Exception:
            logger.exception("管理员统计采样失败，下个周期重试")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                async with asyncio.timeout(self.interval):
                    await self._stop.wait()
            except TimeoutError:
                await self._pass()

    async def stop(self) -> None:
        async with self._lock:
            self._stop.set()
            task, self._task = self._task, None
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
