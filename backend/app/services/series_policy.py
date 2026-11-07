"""书内投影的共享字段与写保护；写调用者必须先取得父项目锁。"""

from collections.abc import Collection
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import SeriesEntryUse

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


async def reject_bound_entry(db: AsyncSession, project_id: int, kind: ProjectionKind, entry_id: int) -> None:
    if await used_local_ids(db, project_id, kind, {entry_id}):
        raise managed_setting_error()
