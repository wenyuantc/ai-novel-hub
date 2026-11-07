"""系列档案的唯一写入口；同步维护各书投影和相关读版本。"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Collection, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.models import (
    Character,
    Project,
    Series,
    SeriesBook,
    SeriesEntry,
    SeriesEntryUse,
    SettingTimeline,
    WorldSetting,
)
from app.schemas import series as s
from app.schemas.setting_ai_visibility import StoredAiSettings
from app.schemas.setting_field_value import EntityRef, SettingEntryType
from app.schemas.setting_fields import SettingEntry
from app.services import (
    ai_dispatch,
    planning_lifecycle,
    project_lifecycle,
    series_dependencies,
    series_policy,
    setting_catalog_service,
    setting_field_service,
    setting_progression_service,
    story_service,
)
from app.services import setting_ai_visibility as visibility

_DEFAULT_AI = {
    "ai_mode": "detected",
    "mention_enabled": True,
    "mention_case_sensitive": True,
    "mention_exclusions": [],
    "ai_hidden_builtin_fields": [],
}
_MAX_IMPACT_BOOKS = 20


class InvalidSeriesDataError(AppError):
    status_code = 422


def _stale(current: int) -> ConflictError:
    return ConflictError(
        "系列已被其他操作修改，请刷新后保留草稿重试",
        extra={"code": "stale_revision", "scope": "series", "current_revision": current},
    )


async def _owned(db: AsyncSession, actor: int, sid: int) -> Series:
    item = await db.scalar(
        select(Series).where(Series.id == sid, Series.owner_id == actor).execution_options(populate_existing=True)
    )
    if item is None:
        raise NotFoundError("系列不存在")
    return item


@asynccontextmanager
async def _edit(db: AsyncSession, actor: int, sid: int, expected: int):
    """先锁系列行，再按稳定顺序取各作品发送门禁及父行锁。"""
    try:
        with db.no_autoflush:
            result = await db.execute(
                update(Series).where(Series.id == sid, Series.owner_id == actor)
                .values(revision=Series.revision, updated_at=Series.updated_at)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise NotFoundError("系列不存在")
            series = await _owned(db, actor, sid)
        if series.revision != expected:
            raise _stale(series.revision)
        yield series
        await db.commit()
    except BaseException as exc:
        await db.rollback()
        if (
            isinstance(exc, OperationalError)
            and db.get_bind().dialect.name == "sqlite"
            and ai_dispatch.is_sqlite_busy(exc)
        ):
            raise ai_dispatch.VisibilityBusyError from None
        raise


def _book_out(project: Project) -> s.SeriesBookOut:
    return s.SeriesBookOut(id=project.id, title=project.title, recycled=project.deleted_at is not None)


def _series_out(item: Series, book_count: int, entry_count: int) -> s.SeriesOut:
    return s.SeriesOut(
        id=item.id, name=item.name, revision=item.revision, book_count=book_count, entry_count=entry_count,
    )


async def _summary(db: AsyncSession, item: Series) -> s.SeriesOut:
    book_count, entry_count = (
        await db.execute(select(
            select(func.count()).select_from(SeriesBook).where(SeriesBook.series_id == item.id).scalar_subquery(),
            select(func.count()).select_from(SeriesEntry).where(SeriesEntry.series_id == item.id).scalar_subquery(),
        ))
    ).one()
    return _series_out(item, book_count, entry_count)


async def list_series(db: AsyncSession, actor: int) -> list[s.SeriesOut]:
    book_count = (
        select(func.count()).select_from(SeriesBook).where(SeriesBook.series_id == Series.id).correlate(Series)
    )
    entry_count = (
        select(func.count()).select_from(SeriesEntry).where(SeriesEntry.series_id == Series.id).correlate(Series)
    )
    rows = await db.execute(
        select(Series, book_count.scalar_subquery(), entry_count.scalar_subquery())
        .where(Series.owner_id == actor).order_by(Series.updated_at.desc(), Series.id.desc())
    )
    return [_series_out(item, books, entries) for item, books, entries in rows]


async def detail(db: AsyncSession, actor: int, sid: int) -> s.SeriesDetail:
    item = await _owned(db, actor, sid)
    projects = await db.scalars(
        select(Project).join(SeriesBook, SeriesBook.project_id == Project.id)
        .where(SeriesBook.series_id == sid).order_by(Project.id)
    )
    books = [_book_out(book) for book in projects]
    entries = await db.scalar(select(func.count()).select_from(SeriesEntry).where(SeriesEntry.series_id == sid)) or 0
    # 彻删作品可级联移除成员而不经系列 CAS；成员数量与列表使用同一次读取。
    result = s.SeriesDetail(**_series_out(item, len(books), entries).model_dump(), books=books)
    await _verify_read_revision(db, item.id, result.revision)
    return result


async def _verify_read_revision(db: AsyncSession, sid: int, revision: int) -> None:
    current = await db.scalar(select(Series.revision).where(Series.id == sid))
    if current is None:
        raise NotFoundError("系列不存在")
    if current != revision:
        raise _stale(current)


async def create(db: AsyncSession, actor: int, body: s.SeriesCreate) -> s.SeriesOut:
    item = Series(owner_id=actor, name=body.name)
    db.add(item)
    await db.flush()
    result = _series_out(item, 0, 0)
    await db.commit()
    return result


async def rename(db: AsyncSession, actor: int, sid: int, body: s.SeriesRename) -> s.SeriesOut:
    async with _edit(db, actor, sid, body.expected_revision) as series:
        if series.name != body.name:
            series.name = body.name
            series.revision += 1
        return await _summary(db, series)


async def delete_series(db: AsyncSession, actor: int, sid: int, expected: int) -> None:
    async with _edit(db, actor, sid, expected) as series:
        summary = await _summary(db, series)
        if summary.book_count or summary.entry_count:
            raise ConflictError("只能删除没有成员和共享条目的空系列", extra={"code": "series_not_empty"})
        await db.delete(series)


async def _projects(db: AsyncSession, actor: int, ids: Collection[int]) -> dict[int, Project]:
    if not ids:
        return {}
    projects = {
        book.id: book for book in await db.scalars(
            select(Project).where(Project.user_id == actor, Project.id.in_(ids))
            .execution_options(populate_existing=True)
        )
    }
    if projects.keys() != set(ids):
        raise NotFoundError("作品不存在")
    return projects


async def _lock_projects(
    db: AsyncSession, actor: int, ids: Collection[int], *, active: Collection[int] = ()
) -> dict[int, Project]:
    await _projects(db, actor, ids)
    ordered = sorted(set(ids))
    for pid in ordered:
        await ai_dispatch.lock_visibility_write(db, pid, include_recycled=pid not in active)
    result = {}
    for pid in ordered:
        book = await project_lifecycle.lock_record(db, pid, active_only=pid in active)
        if book.user_id != actor:
            raise NotFoundError("作品不存在")
        if book.deletion_started_at is not None:
            raise ConflictError("作品正在删除，请稍后重试", extra={"code": "project_deleting"})
        result[pid] = book
    return result


async def _member(db: AsyncSession, sid: int, pid: int) -> SeriesBook:
    row = await db.scalar(
        select(SeriesBook).where(SeriesBook.project_id == pid, SeriesBook.series_id == sid)
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise NotFoundError("作品不是该系列的成员")
    return row


async def join(db: AsyncSession, actor: int, sid: int, body: s.SeriesBookMutation) -> s.SeriesOut:
    async with _edit(db, actor, sid, body.expected_revision) as series:
        pid = body.project_id
        await _lock_projects(db, actor, [pid], active=[pid])
        current = await db.get(SeriesBook, pid, populate_existing=True)
        if current is None:
            db.add(SeriesBook(project_id=pid, owner_id=actor, series_id=sid))
            series.revision += 1
        elif current.series_id != sid:
            raise ConflictError("该作品已加入其他系列，请先退出原系列", extra={"code": "series_book_already_joined"})
        return await _summary(db, series)


async def _entry(db: AsyncSession, sid: int, eid: int) -> SeriesEntry:
    item = await db.scalar(
        select(SeriesEntry).where(SeriesEntry.id == eid, SeriesEntry.series_id == sid)
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise NotFoundError("共享设定不存在")
    return item


async def _uses(
    db: AsyncSession, sid: int, *, entry_ids: Collection[int] | None = None, pid: int | None = None,
) -> list[SeriesEntryUse]:
    query = select(SeriesEntryUse).where(SeriesEntryUse.series_id == sid)
    if entry_ids is not None:
        if not entry_ids:
            return []
        query = query.where(SeriesEntryUse.series_entry_id.in_(entry_ids))
    if pid is not None:
        query = query.where(SeriesEntryUse.project_id == pid)
    return list(await db.scalars(
        query.order_by(SeriesEntryUse.project_id, SeriesEntryUse.series_entry_id)
        .execution_options(populate_existing=True)
    ))


def _ref(use: SeriesEntryUse) -> s.SeriesLocalRef:
    local = use.character_id if use.kind == "character" else use.world_setting_id
    assert local is not None
    return s.SeriesLocalRef(kind=cast(s.SeriesKind, use.kind), id=local)


def _entry_out(entry: SeriesEntry, uses: Sequence[SeriesEntryUse]) -> s.SeriesEntryOut:
    ai = s.SeriesAiOut.model_validate(entry.ai_settings)
    out_uses = [s.SeriesUseOut(project_id=use.project_id, ref=_ref(use)) for use in uses]
    if entry.kind == "character":
        return s.SeriesCharacterOut(
            id=entry.id, data=s.SeriesCharacterData.model_validate(entry.data), ai_settings=ai, uses=out_uses,
        )
    return s.SeriesWorldOut(
        id=entry.id, data=s.SeriesWorldData.model_validate(entry.data), ai_settings=ai, uses=out_uses,
    )


async def read_entry(db: AsyncSession, actor: int, sid: int, eid: int) -> s.SeriesEntryOut:
    series = await _owned(db, actor, sid)
    entry = await _entry(db, sid, eid)
    result = _entry_out(entry, await _uses(db, sid, entry_ids=[eid]))
    await _verify_read_revision(db, sid, series.revision)
    return result


async def list_entries(
    db: AsyncSession, actor: int, sid: int, *, kind: s.SeriesKind | None, q: str | None, page: int, page_size: int
) -> s.SeriesEntryPage:
    series = await _owned(db, actor, sid)
    filters = [SeriesEntry.series_id == sid]
    if kind is not None:
        filters.append(SeriesEntry.kind == kind)
    if q and q.strip():
        filters.append(func.lower(SeriesEntry.display_name).contains(q.strip().lower(), autoescape=True))
    total = await db.scalar(select(func.count()).select_from(SeriesEntry).where(*filters)) or 0
    entries = list(await db.scalars(
        select(SeriesEntry).where(*filters).order_by(SeriesEntry.display_name, SeriesEntry.id)
        .offset((page - 1) * page_size).limit(page_size)
    ))
    uses = defaultdict(list)
    for use in await _uses(db, sid, entry_ids=[entry.id for entry in entries]):
        uses[use.series_entry_id].append(use)
    items = [_entry_out(entry, uses[entry.id]) for entry in entries]
    await _verify_read_revision(db, sid, series.revision)
    return s.SeriesEntryPage(items=items, total=total, revision=series.revision)


def _name(kind: str, data: dict) -> str:
    return data["name" if kind == "character" else "title"]


def _normalize_data(kind: s.SeriesKind, values: dict) -> dict:
    model = s.SeriesCharacterData if kind == "character" else s.SeriesWorldData
    try:
        return model.model_validate(values).model_dump()
    except ValidationError as exc:
        raise InvalidSeriesDataError("请填写该类型所需的系列基础字段，不能传入本书专属字段") from exc


def _normalize_ai(kind: s.SeriesKind, values: dict) -> dict:
    try:
        return visibility.normalize_settings(kind, StoredAiSettings.model_validate(values)).model_dump()
    except ValidationError as exc:
        raise InvalidSeriesDataError("系列 AI 设置无效，请检查重复字段和选项") from exc


async def create_entry(db: AsyncSession, actor: int, sid: int, body: s.SeriesEntryCreate) -> s.SeriesEntryMutation:
    async with _edit(db, actor, sid, body.expected_revision) as series:
        data = _normalize_data(body.kind, body.data.model_dump(exclude_unset=True))
        ai_patch = body.ai_settings.model_dump(exclude_unset=True) if body.ai_settings else {}
        ai = _normalize_ai(body.kind, _DEFAULT_AI | ai_patch)
        row = SeriesEntry(series_id=sid, kind=body.kind, display_name=_name(body.kind, data), data=data, ai_settings=ai)
        db.add(row)
        series.revision += 1
        await db.flush()
        return s.SeriesEntryMutation(revision=series.revision, item=_entry_out(row, []))


async def _local(db: AsyncSession, pid: int, ref: s.SeriesLocalRef) -> Character | WorldSetting:
    model = Character if ref.kind == "character" else WorldSetting
    row = await db.scalar(select(model).where(model.project_id == pid, model.id == ref.id))
    if row is None:
        raise NotFoundError("本书设定不存在")
    return row


async def _baseline(db: AsyncSession, pid: int, ref: s.SeriesLocalRef) -> SettingTimeline:
    timeline = await setting_catalog_service.find_entry_timeline(db, pid, ref.kind, ref.id)
    if timeline is None or timeline.baseline_status != "confirmed":
        raise ConflictError(
            "请先在本书详情确认基础设定，不能从剧情末态推断共享档案",
            extra={"code": "series_baseline_unverified"},
        )
    return timeline


async def _bump_project(db: AsyncSession, pid: int, *, tighten: bool, planning: bool = False) -> None:
    await visibility.bump_visibility_revision(db, pid, tighten=tighten)
    if planning:
        await planning_lifecycle.bump_revision(db, pid)


async def _project_settings(
    db: AsyncSession, pid: int, ref: s.SeriesLocalRef, data: dict, ai_settings: dict,
) -> None:
    item = await _local(db, pid, ref)
    timeline = await _baseline(db, pid, ref)
    timeline_fields = series_policy.shared_baseline_fields(ref.kind)
    if isinstance(item, WorldSetting) and item.category != data["category"]:
        await setting_field_service.assert_entry_type_change_allowed(
            db, pid, "world", ref.id, cast(SettingEntryType, f"world.{data['category']}"),
        )
    for field, value in (data | ai_settings).items():
        if field not in timeline_fields and getattr(item, field) != value:
            setattr(item, field, value)
    changes = {field: data[field] for field in timeline_fields if timeline.baseline.get(field) != data[field]}
    if changes:
        await setting_progression_service._apply_series_baseline(db, timeline, changes)


async def patch_entry(
    db: AsyncSession, actor: int, sid: int, eid: int, body: s.SeriesEntryPatch,
) -> s.SeriesEntryMutation:
    async with _edit(db, actor, sid, body.expected_revision) as series:
        row = await _entry(db, sid, eid)
        kind = cast(s.SeriesKind, row.kind)
        data_patch = body.data.model_dump(exclude_unset=True) if body.data else {}
        ai_patch = body.ai_settings.model_dump(exclude_unset=True) if body.ai_settings else {}
        data = _normalize_data(kind, row.data | data_patch)
        ai = _normalize_ai(kind, row.ai_settings | ai_patch)
        uses = await _uses(db, sid, entry_ids=[eid])
        if data == row.data and ai == row.ai_settings:
            return s.SeriesEntryMutation(revision=series.revision, item=_entry_out(row, uses))
        await _lock_projects(db, actor, {use.project_id for use in uses})
        uses = await _uses(db, sid, entry_ids=[eid])
        new_name = _name(kind, data)
        directory_changed = (
            new_name != row.display_name or data["aliases"] != row.data["aliases"]
            or data.get("category") != row.data.get("category")
        )
        for use in uses:
            ref = _ref(use)
            current_entry = SettingEntry(
                reference=EntityRef(kind=kind, id=ref.id), name=row.display_name,
                category=row.data.get("category"),
                type=cast(SettingEntryType, kind if kind == "character" else f"world.{row.data['category']}"),
            )
            tightened = directory_changed or visibility._tightens(
                current_entry, StoredAiSettings.model_validate(row.ai_settings), StoredAiSettings.model_validate(ai),
                removed_links=False,
            )
            await _project_settings(db, use.project_id, ref, data, ai)
            await _bump_project(db, use.project_id, tighten=tightened, planning=directory_changed)
        row.data, row.ai_settings, row.display_name = data, ai, new_name
        series.revision += 1
        return s.SeriesEntryMutation(revision=series.revision, item=_entry_out(row, uses))


@dataclass(frozen=True)
class _Source:
    data: dict
    ai: dict
    stamp: str


async def _source(db: AsyncSession, book: Project, ref: s.SeriesLocalRef) -> _Source:
    item = await _local(db, book.id, ref)
    await series_policy.reject_bound_entry(db, book.id, ref.kind, ref.id)
    timeline = await _baseline(db, book.id, ref)
    shared = series_policy.CHARACTER_SHARED_FIELDS if ref.kind == "character" else series_policy.WORLD_SHARED_FIELDS
    baseline_fields = series_policy.shared_baseline_fields(ref.kind)
    data = _normalize_data(ref.kind, {
        field: timeline.baseline.get(field, "") if field in baseline_fields else getattr(item, field)
        for field in shared
    })
    ai = StoredAiSettings.model_validate(item).model_dump()
    raw_stamp = {
        "project": book.id,
        "kind": ref.kind,
        "id": ref.id,
        "timeline_id": timeline.id,
        "timeline_revision": timeline.revision,
        "data": data,
        "ai": ai,
        "lifecycle_generation": book.last_deleted_at.isoformat() if book.last_deleted_at is not None else None,
    }
    stamp = hashlib.sha256(json.dumps(raw_stamp, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return _Source(data=data, ai=ai, stamp=stamp)


def _binding(entry: SeriesEntry, pid: int, ref: s.SeriesLocalRef) -> SeriesEntryUse:
    return SeriesEntryUse(
        project_id=pid, series_id=entry.series_id, series_entry_id=entry.id, kind=ref.kind,
        character_id=ref.id if ref.kind == "character" else None,
        world_setting_id=ref.id if ref.kind == "world" else None,
    )


async def promote(db: AsyncSession, actor: int, sid: int, body: s.SeriesPromote) -> s.SeriesEntryMutation:
    async with _edit(db, actor, sid, body.expected_revision) as series:
        pid, ref = body.project_id, body.ref
        projects = await _lock_projects(db, actor, [pid], active=[pid])
        await _member(db, sid, pid)
        source = await _source(db, projects[pid], ref)
        if source.stamp != body.source_stamp:
            raise ConflictError("来源设定已被修改，请重新预览后再提升", extra={"code": "series_source_changed"})
        ai_patch = body.ai_settings.model_dump(exclude_unset=True) if body.ai_settings else {}
        if source.ai["ai_mode"] is None:
            if ai_patch.keys() != {"ai_mode", "mention_enabled"}:
                raise ConflictError(
                    "旧条目需明确选择系列 AI 可见档位和提及开关",
                    extra={"code": "series_ai_confirmation_required"},
                )
        elif ai_patch:
            raise BadRequestError("提升会保留现有 AI 设置，请在系列库中另行修改")
        data = source.data
        ai = _normalize_ai(ref.kind, source.ai | ai_patch | {
            "mention_enabled": ai_patch.get("mention_enabled", visibility.effective_mention_enabled(
                ref.kind, source.ai["ai_mode"], source.ai["mention_enabled"], category=data.get("category"),
            )),
        })
        row = SeriesEntry(series_id=sid, kind=ref.kind, display_name=_name(ref.kind, data), data=data, ai_settings=ai)
        db.add(row)
        await db.flush()
        use = _binding(row, pid, ref)
        db.add(use)
        await _project_settings(db, pid, ref, data, ai)
        await _bump_project(db, pid, tighten=source.ai["ai_mode"] is None, planning=True)
        series.revision += 1
        return s.SeriesEntryMutation(revision=series.revision, item=_entry_out(row, [use]), ref=ref)


async def _new_local(db: AsyncSession, pid: int, row: SeriesEntry) -> s.SeriesLocalRef:
    create_local = story_service.new_character if row.kind == "character" else story_service.new_world
    item = await create_local(db, pid, row.data | row.ai_settings)
    return s.SeriesLocalRef(kind=cast(s.SeriesKind, row.kind), id=item.id)


async def use_entry(
    db: AsyncSession, actor: int, sid: int, eid: int, body: s.SeriesBookMutation,
) -> s.SeriesEntryMutation:
    async with _edit(db, actor, sid, body.expected_revision) as series:
        pid = body.project_id
        row = await _entry(db, sid, eid)
        await _lock_projects(db, actor, [pid], active=[pid])
        await _member(db, sid, pid)
        uses = await _uses(db, sid, entry_ids=[eid])
        current = next((use for use in uses if use.project_id == pid), None)
        if current is not None:
            return s.SeriesEntryMutation(revision=series.revision, item=_entry_out(row, uses), ref=_ref(current))
        ref = await _new_local(db, pid, row)
        use = _binding(row, pid, ref)
        db.add(use)
        await _bump_project(db, pid, tighten=True, planning=True)
        series.revision += 1
        uses.append(use)
        uses.sort(key=lambda entry_use: entry_use.project_id)
        return s.SeriesEntryMutation(revision=series.revision, item=_entry_out(row, uses), ref=ref)


def _refs_by_book(uses: Sequence[SeriesEntryUse]) -> dict[int, list[EntityRef]]:
    refs = defaultdict(list)
    for use in uses:
        local = _ref(use)
        refs[use.project_id].append(EntityRef(kind=local.kind, id=local.id))
    return refs


async def _blockers(
    db: AsyncSession, projects: dict[int, Project], uses: Sequence[SeriesEntryUse],
) -> list[s.SeriesImpactBlocker]:
    found = []
    for pid, refs in _refs_by_book(uses).items():
        if reasons := await series_dependencies.blocking_reasons(db, pid, refs):
            book = _book_out(projects[pid])
            found.append(s.SeriesImpactBlocker(
                project_id=pid, title=book.title, recycled=book.recycled, reasons=reasons,
            ))
            if len(found) == _MAX_IMPACT_BOOKS:
                break
    return found


async def _remove_projections(
    db: AsyncSession, projects: dict[int, Project], uses: Sequence[SeriesEntryUse],
) -> None:
    blockers = await _blockers(db, projects, uses)
    if blockers:
        raise ConflictError(
            "无法调整归属：仍有本书资料或已保存引用使用该设定",
            extra={"code": "series_setting_in_use", "blockers": [item.model_dump() for item in blockers]},
        )
    for pid, refs in _refs_by_book(uses).items():
        await setting_field_service.cleanup_unreferenced_handles(db, pid, refs)
    for use in uses:
        await db.delete(use)
    await db.flush()
    for use in uses:
        await db.delete(await _local(db, use.project_id, _ref(use)))
    await db.flush()
    for pid in _refs_by_book(uses):
        await _bump_project(db, pid, tighten=True, planning=True)


async def leave(db: AsyncSession, actor: int, sid: int, pid: int, expected: int) -> s.SeriesOut:
    async with _edit(db, actor, sid, expected) as series:
        projects = await _lock_projects(db, actor, [pid])
        membership = await _member(db, sid, pid)
        await _remove_projections(db, projects, await _uses(db, sid, pid=pid))
        await db.delete(membership)
        series.revision += 1
        return await _summary(db, series)


async def unuse(db: AsyncSession, actor: int, sid: int, eid: int, pid: int, expected: int) -> s.SeriesEntryMutation:
    async with _edit(db, actor, sid, expected) as series:
        row = await _entry(db, sid, eid)
        projects = await _lock_projects(db, actor, [pid])
        await _member(db, sid, pid)
        uses = await _uses(db, sid, entry_ids=[eid])
        if selected := [use for use in uses if use.project_id == pid]:
            await _remove_projections(db, projects, selected)
            series.revision += 1
        return s.SeriesEntryMutation(
            revision=series.revision, item=_entry_out(row, [use for use in uses if use.project_id != pid]),
        )


async def _retire_entry(
    db: AsyncSession, actor: int, sid: int, eid: int, expected: int, *, move_to: int | None = None,
) -> s.SeriesEntryMutation:
    async with _edit(db, actor, sid, expected) as series:
        row = await _entry(db, sid, eid)
        uses = await _uses(db, sid, entry_ids=[eid])
        ids = {use.project_id for use in uses}
        target_ids = [] if move_to is None else [move_to]
        projects = await _lock_projects(db, actor, ids.union(target_ids), active=target_ids)
        uses = await _uses(db, sid, entry_ids=[eid])
        target_ref = None
        if move_to is not None:
            await _member(db, sid, move_to)
        await _remove_projections(db, projects, [use for use in uses if use.project_id != move_to])
        if move_to is not None:
            existing = next((use for use in uses if use.project_id == move_to), None)
            if existing is not None:
                target_ref = _ref(existing)
                await db.delete(existing)
                await db.flush()
            else:
                target_ref = await _new_local(db, move_to, row)
            await _bump_project(db, move_to, tighten=existing is None, planning=True)
        await db.delete(row)
        series.revision += 1
        return s.SeriesEntryMutation(revision=series.revision, ref=target_ref)


async def delete_entry(db: AsyncSession, actor: int, sid: int, eid: int, expected: int) -> s.SeriesEntryMutation:
    return await _retire_entry(db, actor, sid, eid, expected)


async def move_entry(
    db: AsyncSession, actor: int, sid: int, eid: int, body: s.SeriesBookMutation,
) -> s.SeriesEntryMutation:
    return await _retire_entry(db, actor, sid, eid, body.expected_revision, move_to=body.project_id)


async def impact(
    db: AsyncSession, actor: int, sid: int, *, action: s.SeriesImpactAction, pid: int | None,
    kind: s.SeriesKind | None, eid: int | None,
) -> s.SeriesImpactOut:
    series = await _owned(db, actor, sid)
    if action != "delete":
        if pid is None:
            raise BadRequestError("请指定作品")
        await _member(db, sid, pid)
    if action == "promote":
        if pid is None or eid is None or kind is None:
            raise BadRequestError("请指定要提升的本书设定")
        book = project_lifecycle.ensure_active((await _projects(db, actor, [pid]))[pid])
        source = await _source(db, book, s.SeriesLocalRef(kind=kind, id=eid))
        result = s.SeriesImpactOut(
            revision=series.revision, blockers=[], affected_books=[_book_out(book)], source_stamp=source.stamp,
            requires_ai_settings=source.ai["ai_mode"] is None,
        )
    else:
        if action == "leave":
            uses = await _uses(db, sid, pid=pid)
        else:
            if eid is None:
                raise BadRequestError("请指定系列设定")
            await _entry(db, sid, eid)
            uses = await _uses(db, sid, entry_ids=[eid], pid=pid if action == "unuse" else None)
        ids = {use.project_id for use in uses}
        if pid is not None and action in ("leave", "unuse", "move"):
            ids.add(pid)
        books = await _projects(db, actor, ids)
        if action == "move" and pid is not None:
            project_lifecycle.ensure_active(books[pid])
        considered = [use for use in uses if action != "move" or use.project_id != pid]
        result = s.SeriesImpactOut(
            revision=series.revision,
            blockers=await _blockers(db, books, considered),
            affected_books=[_book_out(books[book_id]) for book_id in sorted(ids)[:_MAX_IMPACT_BOOKS]],
        )
    await _verify_read_revision(db, sid, result.revision)
    return result
