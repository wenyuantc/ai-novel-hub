"""领域中立的有界条目目录和显式时间线初始化。"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import cast

from sqlalchemy import false, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import Career, Character, Organization, SettingReferenceHandle, SettingTimeline, WorldSetting
from app.schemas.setting_fields import EntityRef, EntryKind, EntryType, SettingEntry, SettingEntryPage
from app.services.project_service import get_manageable_project

ENTRY_MODELS = {"character": Character, "world": WorldSetting, "organization": Organization, "career": Career}
TARGET_COLUMNS = {
    "character": "character_id",
    "world": "world_setting_id",
    "organization": "organization_id",
    "career": "career_id",
}
CANDIDATE_KINDS = ("character", "world", "organization", "career")
EntryKey = tuple[str, int]


@dataclass(frozen=True, order=True)
class CandidateCursor:
    kind_rank: int
    sort: int
    id: int


@dataclass(frozen=True)
class SettingCandidate:
    entry: SettingEntry
    setting_tags: tuple[str, ...]
    cursor: CandidateCursor


def _entry_union(project_id: int, *, candidates: bool = False):
    """公开目录和内部批次共用身份/类型投影，排序及附加列各自明确。"""
    queries = []
    for kind, model in ENTRY_MODELS.items():
        name = model.title if kind == "world" else model.name
        category = WorldSetting.category if kind == "world" else literal(None)
        entry_type = literal("world.") + WorldSetting.category if kind == "world" else literal(kind)
        columns = [
            literal(kind).label("kind"),
            model.id.label("id"),
            name.label("name"),
            category.label("category"),
            entry_type.label("type"),
        ]
        if candidates:
            columns.extend(
                [
                    literal(CANDIDATE_KINDS.index(kind)).label("kind_rank"),
                    model.sort.label("sort"),
                    model.setting_tags.label("setting_tags"),
                ]
            )
        queries.append(select(*columns).where(model.project_id == project_id))
    return union_all(*queries).subquery()


def _entry_filters(entries, types: Sequence[EntryType] | None, q: str | None):
    predicates = []
    if types is not None:
        predicates.append(entries.c.type.in_(types))
    if q and q.strip():
        escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        predicates.append(entries.c.name.ilike(f"%{escaped}%", escape="\\"))
    return predicates


async def candidate_batch(
    db: AsyncSession,
    project_id: int,
    *,
    types: Sequence[EntryType] | None = None,
    q: str | None = None,
    after: CandidateCursor | None = None,
    limit: int = 200,
) -> list[SettingCandidate]:
    """已授权快照内的内部游标批次，不放宽公开目录的100条分页合同。"""
    if not 1 <= limit <= 200:
        raise ValueError("候选批量大小必须在1至200之间")
    with db.no_autoflush:
        entries = _entry_union(project_id, candidates=True)
        predicates = _entry_filters(entries, types, q)
        if after is not None:
            predicates.append(
                or_(
                    entries.c.kind_rank > after.kind_rank,
                    (entries.c.kind_rank == after.kind_rank) & (entries.c.sort > after.sort),
                    (entries.c.kind_rank == after.kind_rank)
                    & (entries.c.sort == after.sort)
                    & (entries.c.id > after.id),
                )
            )
        rows = await db.execute(
            select(entries)
            .where(*predicates)
            .order_by(
                entries.c.kind_rank,
                entries.c.sort,
                entries.c.id,
            )
            .limit(limit)
        )
        return [
            SettingCandidate(
                entry=SettingEntry(
                    reference=EntityRef(kind=row.kind, id=row.id), type=row.type, name=row.name, category=row.category
                ),
                setting_tags=tuple(row.setting_tags),
                cursor=CandidateCursor(row.kind_rank, row.sort, row.id),
            )
            for row in rows
        ]


async def get_entry(db: AsyncSession, project_id: int, kind: EntryKind, entry_id: int) -> SettingEntry:
    """调用方校验项目可见性后，只读解析同项目实体身份。"""
    model = ENTRY_MODELS[kind]
    name = model.title if kind == "world" else model.name
    columns = [model.id, name.label("name")]
    if kind == "world":
        columns.append(WorldSetting.category)
    with db.no_autoflush:
        row = (
            await db.execute(select(*columns).where(model.project_id == project_id, model.id == entry_id))
        ).one_or_none()
    if row is None:
        raise NotFoundError("设定条目不存在")
    category = row.category if kind == "world" else None
    return SettingEntry(
        reference=EntityRef(kind=kind, id=row.id),
        name=row.name,
        category=category,
        type=cast(EntryType, f"world.{category}" if kind == "world" else kind),
    )


async def list_entries(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    *,
    types: Sequence[EntryType] | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
    allowed_ids: dict[str, set[int]] | None = None,
) -> SettingEntryPage:
    with db.no_autoflush:
        await get_manageable_project(db, user_id, project_id)
        entries = _entry_union(project_id)
        predicates = _entry_filters(entries, types, q)
        if allowed_ids is not None:
            predicates.append(or_(false(), *(
                (entries.c.kind == kind) & entries.c.id.in_(ids)
                for kind, ids in allowed_ids.items() if ids
            )))
        total = await db.scalar(select(func.count()).select_from(entries).where(*predicates))
        rows = await db.execute(
            select(entries)
            .where(*predicates)
            .order_by(
                entries.c.kind,
                func.lower(entries.c.name),
                entries.c.id,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return SettingEntryPage(
            items=[
                SettingEntry(
                    reference=EntityRef(kind=row.kind, id=row.id),
                    type=row.type,
                    name=row.name,
                    category=row.category,
                )
                for row in rows
            ],
            total=total or 0,
        )


async def get_entries(
    db: AsyncSession,
    project_id: int,
    references: Sequence[EntityRef],
) -> dict[tuple[str, int], SettingEntry]:
    """批量身份/名称投影；每种实体一次查询，不递归读取档案。"""
    result = {}
    with db.no_autoflush:
        for kind, model in ENTRY_MODELS.items():
            ids = {ref.id for ref in references if ref.kind == kind}
            if not ids:
                continue
            name = model.title if kind == "world" else model.name
            category = WorldSetting.category if kind == "world" else literal(None)
            rows = await db.execute(
                select(model.id, name.label("name"), category.label("category")).where(
                    model.project_id == project_id,
                    model.id.in_(ids),
                )
            )
            for row in rows:
                result[kind, row.id] = SettingEntry(
                    reference=EntityRef(kind=kind, id=row.id),
                    name=row.name,
                    category=row.category,
                    type=f"world.{row.category}" if kind == "world" else kind,
                )
    if set(result) != {(ref.kind, ref.id) for ref in references}:
        raise NotFoundError("设定条目不存在")
    return result


def reference_handle_key(handle) -> EntryKey:
    return handle.kind, getattr(handle, TARGET_COLUMNS[handle.kind])


async def find_reference_handles(
    db: AsyncSession,
    project_id: int,
    keys: Collection[EntryKey],
) -> dict[EntryKey, SettingReferenceHandle]:
    predicates = []
    for kind, column in TARGET_COLUMNS.items():
        ids = {entry_id for entry_kind, entry_id in keys if entry_kind == kind}
        if ids:
            predicates.append((SettingReferenceHandle.kind == kind) & getattr(SettingReferenceHandle, column).in_(ids))
    if not predicates:
        return {}
    with db.no_autoflush:
        rows = await db.scalars(select(SettingReferenceHandle).where(
            SettingReferenceHandle.project_id == project_id, or_(*predicates),
        ))
        return {reference_handle_key(row): row for row in rows}


async def ensure_reference_handles(
    db: AsyncSession,
    project_id: int,
    entries: Collection[SettingEntry],
    *,
    existing: dict[EntryKey, SettingReferenceHandle] | None = None,
) -> dict[EntryKey, SettingReferenceHandle]:
    """调用者持父项目锁；只允许已解析的同项目条目；不提交或回收句柄。"""
    keys = {(entry.reference.kind, entry.reference.id) for entry in entries}
    handles = await find_reference_handles(db, project_id, keys) if existing is None else dict(existing)
    missing = keys - handles.keys()
    for kind, entry_id in sorted(missing):
        handles[kind, entry_id] = SettingReferenceHandle(
            project_id=project_id, kind=kind, **{TARGET_COLUMNS[kind]: entry_id},
        )
        db.add(handles[kind, entry_id])
    if missing:
        await db.flush()
    return handles


async def find_entry_timeline(
    db: AsyncSession,
    project_id: int,
    kind: EntryKind,
    entry_id: int,
) -> SettingTimeline | None:
    with db.no_autoflush:
        return await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project_id,
                SettingTimeline.subject_kind == ("career_entry" if kind == "career" else kind),
                getattr(SettingTimeline, TARGET_COLUMNS[kind]) == entry_id,
            )
        )


async def ensure_entry_timeline(
    db: AsyncSession,
    project_id: int,
    kind: EntryKind,
    entry_id: int,
) -> SettingTimeline:
    """仅供持有项目写锁的组合事务使用；初始化后只 flush。"""
    from app.services import setting_progression_service as progressions

    await get_entry(db, project_id, kind, entry_id)
    existing = await find_entry_timeline(db, project_id, kind, entry_id)
    if existing is not None:
        return existing
    if kind == "character":
        return await progressions.create_character_timeline(db, await db.get(Character, entry_id))
    if kind == "world":
        return await progressions.create_world_timeline(db, await db.get(WorldSetting, entry_id))
    timeline = SettingTimeline(
        project_id=project_id,
        subject_kind="career_entry" if kind == "career" else kind,
        **{TARGET_COLUMNS[kind]: entry_id},
        baseline={},
        baseline_status="confirmed",
        legacy_compat_allowed=False,
    )
    db.add(timeline)
    await db.flush()
    return timeline
