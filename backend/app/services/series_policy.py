"""书内投影的共享字段与写保护；写调用者必须先取得父项目锁。"""

from collections.abc import Collection
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Project, SeriesEntryUse

ProjectionKind = Literal["character", "world"]

CHARACTER_SHARED_BASELINE_FIELDS = frozenset({"profile", "appearance", "personality", "background", "gender"})
WORLD_SHARED_BASELINE_FIELDS = frozenset({"content"})
CHARACTER_SHARED_FIELDS = CHARACTER_SHARED_BASELINE_FIELDS | {"name", "aliases"}
WORLD_SHARED_FIELDS = WORLD_SHARED_BASELINE_FIELDS | {"title", "aliases", "category"}
AI_SHARED_FIELDS = frozenset({
    "ai_mode", "mention_enabled", "mention_case_sensitive", "mention_exclusions", "ai_hidden_builtin_fields",
})


def shared_baseline_fields(kind: str) -> frozenset[str]:
    if kind == "character":
        return CHARACTER_SHARED_BASELINE_FIELDS
    if kind == "world":
        return WORLD_SHARED_BASELINE_FIELDS
    return frozenset()


def managed_setting_error() -> ConflictError:
    return ConflictError(
        "该条目的共享基础由系列管理；请在系列库编辑或调整归属",
        extra={"code": "series_setting_managed"},
    )


async def used_local_ids(
    db: AsyncSession,
    project_id: int,
    kind: ProjectionKind,
    ids: Collection[int] | None = None,
) -> set[int]:
    if ids is not None and not ids:
        return set()
    column = SeriesEntryUse.character_id if kind == "character" else SeriesEntryUse.world_setting_id
    stmt = select(column).where(SeriesEntryUse.project_id == project_id, column.is_not(None))
    if ids is not None:
        stmt = stmt.where(column.in_(ids))
    with db.no_autoflush:
        return {value for value in await db.scalars(stmt) if value is not None}


async def managed_setting_error_for_actor(
    db: AsyncSession, project_id: int, kind: ProjectionKind, entry_id: int, actor_user_id: int | None = None,
) -> ConflictError:
    """人工作品协作者的错误不泄露其没有权限查看的系列身份。"""
    if actor_user_id is None:
        return managed_setting_error()
    from app.services import authorization_service
    from app.services.series_read import visible_series_ids

    column = SeriesEntryUse.character_id if kind == "character" else SeriesEntryUse.world_setting_id
    series_id = await db.scalar(select(SeriesEntryUse.series_id).where(
        SeriesEntryUse.project_id == project_id, column == entry_id,
    ))
    owner_id = await db.scalar(select(Project.user_id).where(Project.id == project_id))
    if (
        owner_id == actor_user_id
        or await authorization_service.allows(db, actor_user_id, "projects.read")
        or (series_id is not None and bool(await visible_series_ids(db, actor_user_id, {series_id})))
    ):
        return managed_setting_error()
    return ConflictError("该设定暂不能修改，请联系作品作者", extra={"code": "setting_edit_restricted"})


async def reject_bound_entry(
    db: AsyncSession, project_id: int, kind: ProjectionKind, entry_id: int, *, actor_user_id: int | None = None,
) -> None:
    if await used_local_ids(db, project_id, kind, {entry_id}):
        raise await managed_setting_error_for_actor(db, project_id, kind, entry_id, actor_user_id)
