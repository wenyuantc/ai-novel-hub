"""作品生命周期的显式查询条件与父行事务屏障，不改变拥有权规则。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.util import identity_key

from app.core.errors import ConflictError, NotFoundError
from app.models import Project

RETENTION_DAYS = 30
LIFECYCLE_FIELDS = frozenset(
    {
        "deleted_at",
        "last_deleted_at",
        "deleted_by_user_id",
        "deleted_by_username",
        "deletion_started_at",
        "deletion_token",
    }
)


def now_utc() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def expires_at(deleted_at: datetime) -> datetime:
    return as_utc(deleted_at) + timedelta(days=RETENTION_DAYS)


def active_conditions():
    return Project.deleted_at.is_(None), Project.deletion_started_at.is_(None)


def ensure_active(project: Project | None) -> Project:
    if project is None or project.deleted_at is not None:
        raise NotFoundError("项目不存在")
    if project.deletion_started_at is not None:
        raise ConflictError("作品正在删除，请稍后重试", extra={"code": "project_deleting"})
    return project


async def lock_record(db: AsyncSession, project_id: int, *, active_only: bool = False) -> Project:
    """no-op UPDATE 获取父行写锁，直到调用者提交；不改变作品更新时间。

    显式 no_autoflush 防止调用者待写的子数据先于屏障落库。SQLite 同样获得物理事务，
    agent 的 SAVEPOINT 不会意外成为外层事务。这里只管生命周期，调用方仍须先核实权限。
    """
    statement = update(Project).where(Project.id == project_id)
    if active_only:
        statement = statement.where(*active_conditions())
    with db.no_autoflush:
        result = await db.execute(
            statement.values(id=Project.id, updated_at=Project.updated_at).execution_options(synchronize_session=False)
        )
        project = await load_record(db, project_id)
    if result.rowcount != 1:
        if active_only:
            ensure_active(project)
        raise NotFoundError("项目不存在")
    return ensure_active(project) if active_only else project


async def lock_active(db: AsyncSession, project_id: int) -> Project:
    return await lock_record(db, project_id, active_only=True)


async def load_record(db: AsyncSession, project_id: int) -> Project | None:
    """重读数据库生命周期但保留调用方待写的普通字段。

    agent 事务/任务校验会多次经过授权 helper。直接 populate_existing 会静默丢弃
    未 flush 的 title、wizard_progress 等值，直接使用 identity map 又会漏读软删除。
    生命周期字段只能由协调器在拿锁后设置，不从旧缓存恢复。
    """
    cached = db.identity_map.get(identity_key(Project, project_id))
    pending = {}
    if cached is not None:
        state = inspect(cached)
        pending = {
            column.key: deepcopy(getattr(cached, column.key))
            for column in state.mapper.columns
            if column.key not in LIFECYCLE_FIELDS | {"id", "user_id"} and state.attrs[column.key].history.has_changes()
        }
    with db.no_autoflush:
        project = await db.scalar(
            select(Project).where(Project.id == project_id).execution_options(populate_existing=True)
        )
    if project is not None:
        for field, value in pending.items():
            setattr(project, field, value)
    return project
