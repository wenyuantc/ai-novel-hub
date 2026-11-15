"""系列档案的唯一写入口；同步维护各书投影和相关读版本。"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Collection, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import cast

from pydantic import ValidationError
from sqlalchemy import and_, func, inspect, or_, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.orm.util import identity_key

from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError, UnauthorizedError
from app.models import (
    AuthorizationState,
    Character,
    Project,
    ProjectCollaborator,
    Series,
    SeriesBook,
    SeriesCollaborator,
    SeriesEntry,
    SeriesEntryUse,
    SettingTimeline,
    User,
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
from app.services import authorization_service as authz
from app.services import setting_ai_visibility as visibility
from app.services.series_authorization import authorization_version

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


def _pending_values(db: AsyncSession, model, identity: int | tuple[int, ...], immutable: frozenset[str]) -> dict:
    """刷新安全身份前保留合法待写列；危险待写列必须由调用方先处理。"""
    cached = db.identity_map.get(identity_key(model, identity))
    if cached is None:
        return {}
    state = inspect(cached)
    if any(state.attrs[key].history.has_changes() for key in immutable):
        raise ConflictError("系列资料的身份或版本尚有未保存变更，请先处理", extra={
            "code": "series_pending_identity",
        })
    return {
        column.key: deepcopy(getattr(cached, column.key))
        for column in state.mapper.columns
        if column.key not in immutable and state.attrs[column.key].history.has_changes()
    }


def _restore_pending(item, pending: dict) -> None:
    for field, value in pending.items():
        setattr(item, field, value)


async def _owned(db: AsyncSession, actor: int, sid: int) -> Series:
    pending = _pending_values(db, Series, sid, frozenset({"id", "owner_id", "revision"}))
    item = await db.scalar(
        select(Series).where(Series.id == sid, Series.owner_id == actor).execution_options(populate_existing=True)
    )
    if item is None:
        raise NotFoundError("系列不存在")
    _restore_pending(item, pending)
    return item


async def _readable(db: AsyncSession, actor: int, sid: int) -> Series:
    """系列权限独立于单书权限；管理员身份不会自动得到系列权限。"""
    with db.no_autoflush:
        await authz.require_authenticated(db, actor)
        pending = _pending_values(db, Series, sid, frozenset({"id", "owner_id", "revision"}))
        item = await db.scalar(
            select(Series)
            .outerjoin(SeriesCollaborator, and_(
                SeriesCollaborator.series_id == Series.id, SeriesCollaborator.user_id == actor,
                SeriesCollaborator.status == "accepted", SeriesCollaborator.role == "editor",
            ))
            .where(Series.id == sid, or_(Series.owner_id == actor, SeriesCollaborator.id.is_not(None)))
            .execution_options(populate_existing=True)
        )
    if item is None:
        raise NotFoundError("系列不存在")
    _restore_pending(item, pending)
    if item.owner_id != actor and "name" in pending:
        raise ConflictError("系列名称变更只能由作者执行", extra={"code": "series_owner_required"})
    return item


async def _visible_project_ids(db: AsyncSession, actor: int, series: Series, ids: Collection[int]) -> set[int]:
    """批量判断展示用途；不能据此推导共享写入或归属调整权限。"""
    if series.owner_id == actor:
        return set(ids)
    if not ids:
        return set()
    with db.no_autoflush:
        snapshot = await authz.require_authenticated(db, actor)
        stmt = select(Project.id).where(Project.id.in_(ids), *project_lifecycle.active_conditions())
        if not snapshot.allows("projects.read"):
            member = select(ProjectCollaborator.id).where(
                ProjectCollaborator.project_id == Project.id, ProjectCollaborator.user_id == actor,
                ProjectCollaborator.status == "accepted",
            ).exists()
            stmt = stmt.where(or_(Project.user_id == actor, member))
        return set(await db.scalars(stmt))


@asynccontextmanager
async def _edit(db: AsyncSession, actor: int, sid: int, expected: int, *, owner_only: bool = False):
    """先锁系列行，再按稳定顺序取各作品发送门禁及父行锁。"""
    try:
        with db.no_autoflush:
            sqlite = db.get_bind().dialect.name == "sqlite"
            timeout = int(await db.scalar(text("PRAGMA busy_timeout")) or 0) if sqlite else None
            if sqlite:
                await db.execute(text(f"PRAGMA busy_timeout = {int(ai_dispatch._LOCK_WAIT_SECONDS * 1000)}"))
            try:
                owner_id = await db.scalar(select(Series.owner_id).where(Series.id == sid))
                if owner_id is None:
                    raise NotFoundError("系列不存在")
                await authz.require_authenticated(db, actor, write=True, targets=(owner_id,))
                result = await db.execute(
                    update(Series).where(Series.id == sid)
                    .values(revision=Series.revision, updated_at=Series.updated_at)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise NotFoundError("系列不存在")
                series = await (_owned(db, actor, sid) if owner_only else _readable(db, actor, sid))
                if series.owner_id != owner_id:
                    raise ConflictError("系列归属已变化，请刷新后重试", extra={"code": "series_owner_changed"})
            finally:
                if timeout is not None:
                    await db.execute(text(f"PRAGMA busy_timeout = {timeout}"))
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


def _series_out(item: Series, book_count: int, entry_count: int, actor: int, version: str | None) -> s.SeriesOut:
    is_owner = item.owner_id == actor
    if is_owner != (version is None):
        raise ValueError("系列协作身份不可用")
    return s.SeriesOut(
        id=item.id, name=item.name, revision=item.revision, book_count=book_count, entry_count=entry_count,
        is_owner=is_owner, can_manage_collaborators=is_owner, authorization_version=version,
    )


async def _current_authorization_version(db: AsyncSession, item: Series, actor: int) -> str | None:
    if item.owner_id == actor:
        return None
    with db.no_autoflush:
        row = (await db.execute(select(SeriesCollaborator.invitation_id, SeriesCollaborator.revision).where(
            SeriesCollaborator.series_id == item.id, SeriesCollaborator.user_id == actor,
            SeriesCollaborator.status == "accepted", SeriesCollaborator.role == "editor",
        ))).one_or_none()
    if row is None:
        raise NotFoundError("系列不存在")
    return authorization_version(item.id, actor, row.invitation_id, row.revision)


async def _summary(db: AsyncSession, item: Series, actor: int) -> s.SeriesOut:
    book_count, entry_count = (
        await db.execute(select(
            select(func.count()).select_from(SeriesBook).where(SeriesBook.series_id == item.id).scalar_subquery(),
            select(func.count()).select_from(SeriesEntry).where(SeriesEntry.series_id == item.id).scalar_subquery(),
        ))
    ).one()
    return _series_out(item, book_count, entry_count, actor, await _current_authorization_version(db, item, actor))


async def list_series(db: AsyncSession, actor: int) -> list[s.SeriesOut]:
    book_count = (
        select(func.count()).select_from(SeriesBook).where(SeriesBook.series_id == Series.id).correlate(Series)
    )
    entry_count = (
        select(func.count()).select_from(SeriesEntry).where(SeriesEntry.series_id == Series.id).correlate(Series)
    )
    with db.no_autoflush:
        snapshot = await authz.require_authenticated(db, actor)
        accepted = select(SeriesCollaborator.id).where(
            SeriesCollaborator.series_id == Series.id, SeriesCollaborator.user_id == actor,
            SeriesCollaborator.status == "accepted", SeriesCollaborator.role == "editor",
        ).correlate(Series).exists()
        rows = (await db.execute(
            select(Series.id, Series.owner_id, Series.name, Series.revision,
                   SeriesCollaborator.invitation_id, SeriesCollaborator.revision,
                   book_count.scalar_subquery(), entry_count.scalar_subquery(), User.is_active, User.token_version)
            .select_from(User)
            .outerjoin(Series, or_(Series.owner_id == actor, accepted))
            .outerjoin(SeriesCollaborator, and_(
                SeriesCollaborator.series_id == Series.id, SeriesCollaborator.user_id == actor,
                SeriesCollaborator.status == "accepted", SeriesCollaborator.role == "editor",
            ))
            .where(User.id == actor)
            .order_by(Series.updated_at.desc(), Series.id.desc())
        )).all()
        if not rows or not rows[0].is_active or rows[0].token_version != snapshot.token_version:
            raise UnauthorizedError("未登录或登录已过期")
        result = []
        for sid, owner_id, name, revision, invitation_id, member_revision, books, entries, _, _ in rows:
            if sid is None:
                continue
            pending = _pending_values(db, Series, sid, frozenset({"id", "owner_id", "revision"}))
            if owner_id != actor and "name" in pending:
                raise ConflictError("系列名称变更只能由作者执行", extra={"code": "series_owner_required"})
            result.append(s.SeriesOut(
                id=sid, name=name, revision=revision, book_count=books, entry_count=entries,
                is_owner=owner_id == actor, can_manage_collaborators=owner_id == actor,
                authorization_version=None if owner_id == actor else authorization_version(
                    sid, actor, invitation_id, member_revision,
                ),
            ))
        return result


async def detail(db: AsyncSession, actor: int, sid: int) -> s.SeriesDetail:
    with db.no_autoflush:
        item = await _readable(db, actor, sid)
        version = await _current_authorization_version(db, item, actor)
        projects = list(await db.scalars(
            select(Project).join(SeriesBook, SeriesBook.project_id == Project.id)
            .where(SeriesBook.series_id == sid).order_by(Project.id)
        ))
        visible = await _visible_project_ids(db, actor, item, [book.id for book in projects])
        books = [_book_out(book) for book in projects if book.id in visible]
        entries = await db.scalar(
            select(func.count()).select_from(SeriesEntry).where(SeriesEntry.series_id == sid)
        ) or 0
        # 彻删作品可级联移除成员而不经系列 CAS；成员数量与列表使用同一次读取。
        result = s.SeriesDetail(**_series_out(item, len(projects), entries, actor, version).model_dump(), books=books)
        visible = await _verify_read_revision(db, actor, item.id, result.revision, version, visible)
        result.books = [book for book in result.books if book.id in visible]
        return result


async def _verify_read_revision(
    db: AsyncSession, actor: int, sid: int, revision: int, version: str | None,
    project_ids: Collection[int] = (), *, require_edit: bool = False, active: Collection[int] = (),
) -> set[int]:
    """在同一最终 SQL 快照中复核系列、登录身份和涉及书籍的真实权限。"""
    with db.no_autoflush:
        snapshot = await authz.require_authenticated(db, actor)
        series_member = aliased(SeriesCollaborator)
        book_member = aliased(ProjectCollaborator)
        rows = (await db.execute(
            select(
                Series.owner_id.label("series_owner"), Series.revision.label("series_revision"),
                series_member.invitation_id, series_member.revision.label("member_revision"),
                User.id.label("actor_id"), User.is_active, User.token_version,
                AuthorizationState.revision.label("authorization_revision"),
                Project.id.label("book_id"), Project.user_id.label("book_owner"),
                Project.deleted_at, Project.deletion_started_at, book_member.role.label("book_role"),
            )
            .select_from(Series)
            .outerjoin(User, User.id == actor)
            .outerjoin(AuthorizationState, AuthorizationState.id == 1)
            .outerjoin(series_member, and_(
                series_member.series_id == Series.id, series_member.user_id == actor,
                series_member.status == "accepted", series_member.role == "editor",
            ))
            .outerjoin(SeriesBook, SeriesBook.series_id == Series.id)
            .outerjoin(Project, Project.id == SeriesBook.project_id)
            .outerjoin(book_member, and_(
                book_member.project_id == Project.id, book_member.user_id == actor,
                book_member.status == "accepted",
            ))
            .where(Series.id == sid)
        )).all()
    if not rows:
        raise NotFoundError("系列不存在")
    current = rows[0]
    if (current.actor_id is None or not current.is_active or current.token_version != snapshot.token_version):
        raise UnauthorizedError("未登录或登录已过期")
    if current.authorization_revision is None:
        raise authz.AuthorizationStorageError("授权状态不可用，请联系管理员")
    if current.authorization_revision != snapshot.revision:
        raise ConflictError("系列协作授权已变化，请刷新后重试", extra={"code": "stale_series_access"})
    if current.series_owner == actor:
        current_version = None
    elif current.invitation_id is not None:
        current_version = authorization_version(sid, actor, current.invitation_id, current.member_revision)
    else:
        raise NotFoundError("系列不存在")
    if current.series_revision != revision:
        raise _stale(current.series_revision)
    if current_version != version:
        raise ConflictError("系列协作授权已变化，请刷新后重试", extra={"code": "stale_series_access"})

    requested, active_ids = set(project_ids), set(active)
    visible, editable = set(), set()
    for row in rows:
        if row.book_id not in requested:
            continue
        is_active = row.deleted_at is None and row.deletion_started_at is None
        if current.series_owner == actor:
            visible.add(row.book_id)
            if is_active or row.book_id not in active_ids:
                editable.add(row.book_id)
        elif is_active:
            if row.book_owner == actor or row.book_role is not None or snapshot.allows("projects.read"):
                visible.add(row.book_id)
            if row.book_owner == actor or row.book_role == "editor":
                editable.add(row.book_id)
    if require_edit and editable != requested:
        if current.series_owner == actor:
            raise ConflictError("作品状态已变化，请刷新后重试", extra={"code": "series_project_changed"})
        raise _book_edit_required()
    return visible


async def create(db: AsyncSession, actor: int, body: s.SeriesCreate) -> s.SeriesOut:
    await authz.require_authenticated(db, actor, write=True)
    item = Series(owner_id=actor, name=body.name)
    db.add(item)
    await db.flush()
    result = _series_out(item, 0, 0, actor, None)
    await db.commit()
    return result


async def rename(db: AsyncSession, actor: int, sid: int, body: s.SeriesRename) -> s.SeriesOut:
    async with _edit(db, actor, sid, body.expected_revision, owner_only=True) as series:
        if series.name != body.name:
            series.name = body.name
            series.revision += 1
        return await _summary(db, series, actor)


async def delete_series(db: AsyncSession, actor: int, sid: int, expected: int) -> None:
    async with _edit(db, actor, sid, expected, owner_only=True) as series:
        summary = await _summary(db, series, actor)
        if summary.book_count or summary.entry_count:
            raise ConflictError("只能删除没有成员和共享条目的空系列", extra={"code": "series_not_empty"})
        await db.delete(series)


async def _projects(db: AsyncSession, actor: int, ids: Collection[int]) -> dict[int, Project]:
    if not ids:
        return {}
    with db.no_autoflush:
        # 同一事务的 Project 可能已有未提交的普通字段；刷新生命周期/归属不能丢弃它们。
        pending = {}
        for book in db.identity_map.values():
            if isinstance(book, Project):
                state = inspect(book)
                book_id = state.identity[0] if state.identity else None
                if book_id not in ids:
                    continue
                if any(state.attrs[key].history.has_changes() for key in ("id", "user_id")):
                    raise ConflictError("作品身份正在变化，请刷新后重试", extra={"code": "series_project_changed"})
                pending[book_id] = {
                    column.key: deepcopy(getattr(book, column.key))
                    for column in state.mapper.columns
                    if column.key not in project_lifecycle.LIFECYCLE_FIELDS | {"id", "user_id"}
                    and state.attrs[column.key].history.has_changes()
                }
        projects = {
            book.id: book for book in await db.scalars(
                select(Project).where(Project.user_id == actor, Project.id.in_(ids))
                .execution_options(populate_existing=True)
            )
        }
        for book_id, values in pending.items():
            if book_id in projects:
                for field, value in values.items():
                    setattr(projects[book_id], field, value)
    if projects.keys() != set(ids):
        raise NotFoundError("作品不存在")
    return projects


async def _lock_projects(
    db: AsyncSession, actor: int, series: Series, ids: Collection[int], *,
    active: Collection[int] = (), projection: bool = False,
) -> dict[int, Project]:
    """投影模式只供共享基础传播；归属变更必须逐书检查作者或编辑者。"""
    if actor != series.owner_id and not projection:
        # 预检不可代替锁后的再次验证；它避免无权请求锁住隐藏作品的发送门禁。
        await _editable_projects(db, actor, series, ids)
    else:
        try:
            await _projects(db, series.owner_id, ids)
        except NotFoundError:
            if actor != series.owner_id and projection:
                raise _projection_conflict() from None
            raise
    ordered = sorted(set(ids))
    for pid in ordered:
        try:
            await ai_dispatch.lock_visibility_write(db, pid, include_recycled=pid not in active)
        except NotFoundError:
            if actor != series.owner_id:
                raise (_projection_conflict() if projection else _book_edit_required()) from None
            raise
    result = {}
    for pid in ordered:
        try:
            book = await project_lifecycle.lock_record(db, pid, active_only=pid in active)
        except (NotFoundError, ConflictError):
            if actor != series.owner_id:
                raise (_projection_conflict() if projection else _book_edit_required()) from None
            raise
        if book.user_id != series.owner_id:
            if actor != series.owner_id and projection:
                raise _projection_conflict()
            raise NotFoundError("作品不存在")
        if book.deletion_started_at is not None:
            if actor != series.owner_id:
                raise _projection_conflict() if projection else _book_edit_required()
            raise ConflictError("作品正在删除，请稍后重试", extra={"code": "project_deleting"})
        result[pid] = book
    if not projection:
        await _verify_project_edit(db, actor, series, result)
    return result


def _book_edit_required() -> ConflictError:
    return ConflictError("无法调整归属，请联系系列作者", extra={"code": "series_book_edit_required"})


def _projection_conflict() -> ConflictError:
    return ConflictError("共享设定暂不能更新，请联系系列作者", extra={"code": "series_projection_conflict"})


async def _verify_project_edit(db: AsyncSession, actor: int, series: Series, projects: dict[int, Project]) -> None:
    if actor == series.owner_id:
        return
    if any(book.deleted_at is not None or book.deletion_started_at is not None for book in projects.values()):
        raise _book_edit_required()
    if not projects:
        return
    with db.no_autoflush:
        allowed = set(await db.scalars(select(ProjectCollaborator.project_id).where(
            ProjectCollaborator.project_id.in_(projects), ProjectCollaborator.user_id == actor,
            ProjectCollaborator.status == "accepted", ProjectCollaborator.role == "editor",
        )))
    if allowed != set(projects):
        raise _book_edit_required()


async def _editable_projects(
    db: AsyncSession, actor: int, series: Series, ids: Collection[int],
) -> dict[int, Project]:
    try:
        projects = await _projects(db, series.owner_id, ids)
    except NotFoundError:
        if actor != series.owner_id:
            raise _book_edit_required() from None
        raise
    await _verify_project_edit(db, actor, series, projects)
    return projects


async def _member(db: AsyncSession, sid: int, pid: int) -> SeriesBook:
    with db.no_autoflush:
        pending = _pending_values(db, SeriesBook, pid, frozenset({"project_id", "series_id", "owner_id"}))
        row = await db.scalar(
            select(SeriesBook).where(SeriesBook.project_id == pid, SeriesBook.series_id == sid)
            .execution_options(populate_existing=True)
        )
    if row is None:
        raise NotFoundError("作品不是该系列的成员")
    _restore_pending(row, pending)
    return row


async def join(db: AsyncSession, actor: int, sid: int, body: s.SeriesBookMutation) -> s.SeriesOut:
    async with _edit(db, actor, sid, body.expected_revision, owner_only=True) as series:
        pid = body.project_id
        await _lock_projects(db, actor, series, [pid], active=[pid])
        current = await db.get(SeriesBook, pid, populate_existing=True)
        if current is None:
            db.add(SeriesBook(project_id=pid, owner_id=actor, series_id=sid))
            series.revision += 1
        elif current.series_id != sid:
            raise ConflictError("该作品已加入其他系列，请先退出原系列", extra={"code": "series_book_already_joined"})
        return await _summary(db, series, actor)


async def _entry(db: AsyncSession, sid: int, eid: int) -> SeriesEntry:
    with db.no_autoflush:
        pending = _pending_values(db, SeriesEntry, eid, frozenset({"id", "series_id", "kind"}))
        item = await db.scalar(
            select(SeriesEntry).where(SeriesEntry.id == eid, SeriesEntry.series_id == sid)
            .execution_options(populate_existing=True)
        )
    if item is None:
        raise NotFoundError("共享设定不存在")
    _restore_pending(item, pending)
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
    with db.no_autoflush:
        pending = {}
        for item in db.identity_map.values():
            if not isinstance(item, SeriesEntryUse):
                continue
            state = inspect(item)
            if state.identity is None:
                continue
            project_id, entry_id = state.identity
            if (pid is not None and project_id != pid) or (entry_ids is not None and entry_id not in entry_ids):
                continue
            history = state.attrs.series_id.history
            old_series = history.deleted[0] if history.has_changes() and history.deleted else item.series_id
            # 过期对象的原系列未知时也必须先拒绝，不能刷新后静默覆盖绑定。
            if old_series != sid and not (history.has_changes() and not history.deleted):
                continue
            pending[(project_id, entry_id)] = _pending_values(
                db, SeriesEntryUse, (project_id, entry_id),
                frozenset({"project_id", "series_entry_id", "series_id", "kind", "character_id", "world_setting_id"}),
            )
        uses = list(await db.scalars(
            query.order_by(SeriesEntryUse.project_id, SeriesEntryUse.series_entry_id)
            .execution_options(populate_existing=True)
        ))
        for item in uses:
            _restore_pending(item, pending.get((item.project_id, item.series_entry_id), {}))
        return uses


def _ref(use: SeriesEntryUse) -> s.SeriesLocalRef:
    local = use.character_id if use.kind == "character" else use.world_setting_id
    assert local is not None
    return s.SeriesLocalRef(kind=cast(s.SeriesKind, use.kind), id=local)


def _entry_out(
    entry: SeriesEntry, uses: Sequence[SeriesEntryUse], visible: Collection[int] | None = None,
) -> s.SeriesEntryOut:
    ai = s.SeriesAiOut.model_validate(entry.ai_settings)
    out_uses = [s.SeriesUseOut(project_id=use.project_id, ref=_ref(use)) for use in uses
                if visible is None or use.project_id in visible]
    if entry.kind == "character":
        return s.SeriesCharacterOut(
            id=entry.id, data=s.SeriesCharacterData.model_validate(entry.data), ai_settings=ai,
            has_uses=bool(uses), uses=out_uses,
        )
    return s.SeriesWorldOut(
        id=entry.id, data=s.SeriesWorldData.model_validate(entry.data), ai_settings=ai,
        has_uses=bool(uses), uses=out_uses,
    )


async def _entry_out_for_actor(
    db: AsyncSession, actor: int, series: Series, entry: SeriesEntry, uses: Sequence[SeriesEntryUse],
) -> s.SeriesEntryOut:
    visible = await _visible_project_ids(db, actor, series, [use.project_id for use in uses])
    return _entry_out(entry, uses, visible)


async def read_entry(db: AsyncSession, actor: int, sid: int, eid: int) -> s.SeriesEntryOut:
    with db.no_autoflush:
        series = await _readable(db, actor, sid)
        version = await _current_authorization_version(db, series, actor)
        entry = await _entry(db, sid, eid)
        uses = await _uses(db, sid, entry_ids=[eid])
        visible = await _visible_project_ids(db, actor, series, [use.project_id for use in uses])
        visible = await _verify_read_revision(db, actor, sid, series.revision, version, visible)
        return _entry_out(entry, uses, visible)


async def list_entries(
    db: AsyncSession, actor: int, sid: int, *, kind: s.SeriesKind | None, q: str | None, page: int, page_size: int
) -> s.SeriesEntryPage:
    with db.no_autoflush:
        series = await _readable(db, actor, sid)
        version = await _current_authorization_version(db, series, actor)
        filters = [SeriesEntry.series_id == sid]
        if kind is not None:
            filters.append(SeriesEntry.kind == kind)
        if q and q.strip():
            filters.append(func.lower(SeriesEntry.display_name).contains(q.strip().lower(), autoescape=True))
        total = await db.scalar(select(func.count()).select_from(SeriesEntry).where(*filters)) or 0
        rows = (await db.execute(
            select(SeriesEntry, SeriesEntry.id).where(*filters).order_by(SeriesEntry.display_name, SeriesEntry.id)
            .offset((page - 1) * page_size).limit(page_size)
        )).all()
        entries = []
        for entry, entry_id in rows:
            _pending_values(db, SeriesEntry, entry_id, frozenset({"id", "series_id", "kind"}))
            entries.append(entry)
        uses = defaultdict(list)
        for use in await _uses(db, sid, entry_ids=[entry.id for entry in entries]):
            uses[use.series_entry_id].append(use)
        all_ids = [use.project_id for group in uses.values() for use in group]
        visible = await _visible_project_ids(db, actor, series, all_ids)
        visible = await _verify_read_revision(db, actor, sid, series.revision, version, visible)
        items = [_entry_out(entry, uses[entry.id], visible) for entry in entries]
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
            return s.SeriesEntryMutation(revision=series.revision,
                                         item=await _entry_out_for_actor(db, actor, series, row, uses))
        await _lock_projects(db, actor, series, {use.project_id for use in uses}, projection=True)
        uses = await _uses(db, sid, entry_ids=[eid])
        visible = await _visible_project_ids(db, actor, series, [use.project_id for use in uses])
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
            try:
                await _project_settings(db, use.project_id, ref, data, ai)
                await _bump_project(db, use.project_id, tighten=tightened, planning=directory_changed)
            except AppError:
                if actor != series.owner_id and use.project_id not in visible:
                    raise _projection_conflict() from None
                raise
        row.data, row.ai_settings, row.display_name = data, ai, new_name
        series.revision += 1
        return s.SeriesEntryMutation(revision=series.revision, item=_entry_out(row, uses, visible))


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
        projects = await _lock_projects(db, actor, series, [pid], active=[pid])
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
        return s.SeriesEntryMutation(revision=series.revision,
                                     item=await _entry_out_for_actor(db, actor, series, row, [use]), ref=ref)


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
        await _lock_projects(db, actor, series, [pid], active=[pid])
        await _member(db, sid, pid)
        uses = await _uses(db, sid, entry_ids=[eid])
        current = next((use for use in uses if use.project_id == pid), None)
        if current is not None:
            return s.SeriesEntryMutation(revision=series.revision,
                                         item=await _entry_out_for_actor(db, actor, series, row, uses),
                                         ref=_ref(current))
        ref = await _new_local(db, pid, row)
        use = _binding(row, pid, ref)
        db.add(use)
        await _bump_project(db, pid, tighten=True, planning=True)
        series.revision += 1
        uses.append(use)
        uses.sort(key=lambda entry_use: entry_use.project_id)
        return s.SeriesEntryMutation(revision=series.revision,
                                     item=await _entry_out_for_actor(db, actor, series, row, uses), ref=ref)


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
    async with _edit(db, actor, sid, expected, owner_only=True) as series:
        projects = await _lock_projects(db, actor, series, [pid])
        membership = await _member(db, sid, pid)
        await _remove_projections(db, projects, await _uses(db, sid, pid=pid))
        await db.delete(membership)
        series.revision += 1
        return await _summary(db, series, actor)


async def unuse(db: AsyncSession, actor: int, sid: int, eid: int, pid: int, expected: int) -> s.SeriesEntryMutation:
    async with _edit(db, actor, sid, expected) as series:
        row = await _entry(db, sid, eid)
        projects = await _lock_projects(db, actor, series, [pid])
        await _member(db, sid, pid)
        uses = await _uses(db, sid, entry_ids=[eid])
        if selected := [use for use in uses if use.project_id == pid]:
            await _remove_projections(db, projects, selected)
            series.revision += 1
        return s.SeriesEntryMutation(
            revision=series.revision,
            item=await _entry_out_for_actor(db, actor, series, row, [use for use in uses if use.project_id != pid]),
        )


async def _retire_entry(
    db: AsyncSession, actor: int, sid: int, eid: int, expected: int, *, move_to: int | None = None,
) -> s.SeriesEntryMutation:
    async with _edit(db, actor, sid, expected) as series:
        row = await _entry(db, sid, eid)
        uses = await _uses(db, sid, entry_ids=[eid])
        if move_to is None and actor != series.owner_id:
            if uses:
                raise ConflictError("已采用的共享设定请由系列作者处理", extra={"code": "series_entry_bound"})
            await db.delete(row)
            series.revision += 1
            return s.SeriesEntryMutation(revision=series.revision)
        ids = {use.project_id for use in uses}
        target_ids = [] if move_to is None else [move_to]
        projects = await _lock_projects(db, actor, series, ids.union(target_ids), active=target_ids)
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
    # 预览是只读入口；包括权限失败分支都不能把调用方暂存对象隐式写入。
    with db.no_autoflush:
        return await _impact(db, actor, sid, action=action, pid=pid, kind=kind, eid=eid)


async def _impact(
    db: AsyncSession, actor: int, sid: int, *, action: s.SeriesImpactAction, pid: int | None,
    kind: s.SeriesKind | None, eid: int | None,
) -> s.SeriesImpactOut:
    series = await _readable(db, actor, sid)
    version = await _current_authorization_version(db, series, actor)
    if action == "leave" and series.owner_id != actor:
        raise NotFoundError("系列不存在")
    if action != "delete":
        if pid is None:
            raise BadRequestError("请指定作品")
        if series.owner_id != actor:
            # 先检查用户对输入作品的权限，避免以不同错误探测隐藏书的系列归属。
            await _editable_projects(db, actor, series, [pid])
        await _member(db, sid, pid)
    active_ids: set[int] = set()
    if action == "promote":
        if pid is None or eid is None or kind is None:
            raise BadRequestError("请指定要提升的本书设定")
        book = project_lifecycle.ensure_active((await _editable_projects(db, actor, series, [pid]))[pid])
        ids = {pid}
        active_ids.add(pid)
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
        if action == "delete" and series.owner_id != actor and uses:
            raise ConflictError("已采用的共享设定请由系列作者处理", extra={"code": "series_entry_bound"})
        ids = {use.project_id for use in uses}
        if pid is not None and action in ("leave", "unuse", "move"):
            ids.add(pid)
        books = await _editable_projects(db, actor, series, ids)
        if action == "move" and pid is not None:
            project_lifecycle.ensure_active(books[pid])
            active_ids.add(pid)
        considered = [use for use in uses if action != "move" or use.project_id != pid]
        result = s.SeriesImpactOut(
            revision=series.revision,
            blockers=await _blockers(db, books, considered),
            affected_books=[_book_out(books[book_id]) for book_id in sorted(ids)[:_MAX_IMPACT_BOOKS]],
        )
    await _verify_read_revision(db, actor, sid, result.revision, version, ids, require_edit=True, active=active_ids)
    return result
