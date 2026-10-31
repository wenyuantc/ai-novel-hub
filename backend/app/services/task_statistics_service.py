"""任务统计的事务原语；除初始化外，提交始终由原业务入口负责。"""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import exists, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.models import DashboardTracking, Task, TaskStatistic
from app.models.entities import FINISHED_TASK_STATUSES


def now_utc() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def initialize_tracking(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """首次运行记录起点和存量，重启不改写历史、不补造新建或结束事件。"""
    async with sessionmaker() as db:
        dialect = db.get_bind().dialect.name
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        await db.execute(
            insert(DashboardTracking)
            .values(id=1, started_at=now_utc(), timezone="Asia/Shanghai")
            .on_conflict_do_nothing(index_elements=["id"])
        )
        # PostgreSQL 用单例行串行初始化，SQLite 的 INSERT 已取得写事务。
        await db.scalar(select(DashboardTracking.id).where(DashboardTracking.id == 1).with_for_update())
        tasks = await db.scalars(select(Task).where(
            ~exists(select(TaskStatistic.key).where(TaskStatistic.key == Task.statistics_key))
        ))
        for task in tasks:
            await _ensure_record(db, task, origin="baseline")
        await db.commit()


async def _ensure_record(db: AsyncSession, task: Task, *, origin: str = "baseline") -> TaskStatistic:
    if task.statistics_key is None:
        task.statistics_key = str(uuid4())
    # flush 包含任务插入，让服务端 created_at 与默认UUID在同一事务内可用。
    await db.flush()
    record = await db.get(TaskStatistic, task.statistics_key)
    if record is None:
        stamp = now_utc()
        record = TaskStatistic(
            key=task.statistics_key,
            kind=task.kind,
            origin=origin,
            observed_at=stamp,
            # SQLite created_at 的服务端默认值只有秒精度，不能拿它代表上线后的新建事件时刻。
            created_event_at=stamp if origin == "new" else None,
            status=task.status,
        )
        db.add(record)
        await db.flush()
    return record


async def record_created(db: AsyncSession, task: Task) -> None:
    """与真实新任务同事务插入；重复调用不把baseline改记为新建。"""
    await _ensure_record(db, task, origin="new")


async def record_state_change(
    db: AsyncSession, task: Task, status: str, *, finished_at: datetime | None = None
) -> None:
    """仅在业务条件UPDATE确实命中后调用；已记录的终态不可重复覆盖。"""
    record = await _ensure_record(db, task)
    values: dict = {"status": status}
    if status in FINISHED_TASK_STATUSES:
        values["finished_event_at"] = _as_utc(finished_at) if finished_at is not None else now_utc()
    await db.execute(
        update(TaskStatistic)
        .where(TaskStatistic.key == record.key, TaskStatistic.finished_event_at.is_(None))
        .values(**values)
    )


async def mark_removed(db: AsyncSession, *conditions: ColumnElement[bool]) -> list[str]:
    """标记并返回被锁定的UUID，调用者按同一集合删除，避免并发新行混入。"""
    # 与状态迁移保持 Task→统计 的锁顺序，避免级联删除和引擎结束互相等待。
    tasks = list(await db.scalars(select(Task).where(*conditions).order_by(Task.id).with_for_update()))
    keys = [(await _ensure_record(db, task)).key for task in tasks]
    if keys:
        await db.execute(
            update(TaskStatistic)
            .where(
                TaskStatistic.key.in_(keys),
                TaskStatistic.removed_at.is_(None),
            )
            .values(removed_at=now_utc())
        )
    return keys
