"""给作品内的人工设定接口附加归属，不扩充作品的可读资料范围。"""

from collections.abc import Collection
from typing import Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.errors import ConflictError, NotFoundError, UnauthorizedError
from app.models import (
    AuthorizationState,
    Character,
    Project,
    ProjectCollaborator,
    Series,
    SeriesBook,
    SeriesCollaborator,
    SeriesEntryUse,
    User,
    WorldSetting,
)
from app.schemas.story import CharacterWithOrigin, SettingOrigin, WorldSettingWithOrigin
from app.services import authorization_service, project_access, story_service

SettingOut = CharacterWithOrigin | WorldSettingWithOrigin


async def visible_series_ids(db: AsyncSession, user_id: int, series_ids: set[int]) -> set[int]:
    """批量投影系列身份；单书权限不授予系列访问。"""
    if not series_ids:
        return set()
    return set(await db.scalars(
        select(Series.id)
        .outerjoin(SeriesCollaborator, and_(
            SeriesCollaborator.series_id == Series.id,
            SeriesCollaborator.user_id == user_id,
            SeriesCollaborator.status == "accepted",
        ))
        .where(Series.id.in_(series_ids), or_(Series.owner_id == user_id, SeriesCollaborator.id.is_not(None)))
    ))


async def _final_book_access(
    db: AsyncSession, user_id: int, project_id: int, *, guard: ColumnElement[bool] | None = None,
    subject_kind: Literal["character", "world"] | None = None, subject_ids: Collection[int] = (),
    require_admin: bool = False,
) -> tuple[set[int], bool, bool, set[int]]:
    """最终同一 SQL 快照校验单书和系列身份，避免在两次鉴权之间暴露旧授权资料。"""
    with db.no_autoflush:
        snapshot = await authorization_service.require_authenticated(db, user_id)
        statement = (
            select(
                Project.user_id.label("owner_id"), Project.deleted_at, Project.deletion_started_at,
                ProjectCollaborator.id.label("project_member_id"),
                ProjectCollaborator.role.label("project_member_role"), User.id.label("actor_id"),
                User.is_active, User.token_version, AuthorizationState.revision.label("authorization_revision"),
                Series.id.label("series_id"), Series.owner_id.label("series_owner_id"),
                SeriesCollaborator.id.label("series_member_id"),
            )
            .select_from(Project)
            .outerjoin(User, User.id == user_id)
            .outerjoin(AuthorizationState, AuthorizationState.id == 1)
            .outerjoin(ProjectCollaborator, and_(
                ProjectCollaborator.project_id == Project.id, ProjectCollaborator.user_id == user_id,
                ProjectCollaborator.status == "accepted",
            ))
            .outerjoin(SeriesBook, SeriesBook.project_id == Project.id)
            .outerjoin(Series, Series.id == SeriesBook.series_id)
            .outerjoin(SeriesCollaborator, and_(
                SeriesCollaborator.series_id == Series.id, SeriesCollaborator.user_id == user_id,
                SeriesCollaborator.status == "accepted", SeriesCollaborator.role == "editor",
            ))
            .where(Project.id == project_id, *([guard] if guard is not None else []))
        )
        if subject_kind is not None and subject_ids:
            model = Character if subject_kind == "character" else WorldSetting
            use_column = SeriesEntryUse.character_id if subject_kind == "character" else SeriesEntryUse.world_setting_id
            statement = (statement.add_columns(model.id.label("current_subject_id"),
                                               use_column.label("bound_subject_id"))
                         .outerjoin(model, and_(model.project_id == Project.id, model.id.in_(subject_ids)))
                         .outerjoin(SeriesEntryUse, and_(SeriesEntryUse.project_id == Project.id,
                                                         use_column == model.id)))
        rows = (await db.execute(statement)).all()
    if not rows:
        raise NotFoundError("项目不存在")
    row = rows[0]
    if row.actor_id is None or not row.is_active or row.token_version != snapshot.token_version:
        raise UnauthorizedError("未登录或登录已过期")
    if row.authorization_revision is None:
        raise authorization_service.AuthorizationStorageError("授权状态不可用，请联系管理员")
    if row.authorization_revision != snapshot.revision:
        raise ConflictError("权限状态已变化，请刷新后重试", extra={"code": "stale_series_access"})
    admin_read = row.owner_id != user_id and snapshot.allows("projects.read")
    if require_admin and not admin_read:
        raise NotFoundError("项目不存在")
    if row.owner_id != user_id and row.project_member_id is None and not admin_read:
        raise NotFoundError("项目不存在")
    if row.deleted_at is not None:
        raise NotFoundError("项目不存在")
    if row.deletion_started_at is not None:
        raise ConflictError("作品正在删除，请稍后重试", extra={"code": "project_deleting"})
    series_visible = (
        {row.series_id} if row.series_id is not None
        and (row.series_owner_id == user_id or row.series_member_id is not None) else set()
    )
    can_edit = (row.owner_id == user_id or row.project_member_role == "editor"
                or (admin_read and snapshot.allows("projects.edit")))
    local_ids = ({result.current_subject_id for result in rows
                  if result.current_subject_id is not None and result.bound_subject_id is None}
                 if subject_kind is not None and subject_ids else set())
    return series_visible, admin_read, can_edit, local_ids


async def read_book_settings(
    db: AsyncSession,
    kind: story_service.StoryKind,
    user_id: int,
    project_id: int,
    *,
    category: str | None = None,
    item_id: int | None = None,
) -> list[SettingOut]:
    with db.no_autoflush:
        _, initial_access, member = await project_access.resolve_access(db, user_id, project_id)
        grant_guard = None
        if initial_access.is_collaborator and member is not None:
            grant_guard = and_(ProjectCollaborator.invitation_id == member.invitation_id,
                               ProjectCollaborator.revision == member.revision)
        if kind is story_service.CHARACTERS:
            binding_column = SeriesEntryUse.character_id
            schema = CharacterWithOrigin
        elif kind is story_service.WORLD:
            binding_column = SeriesEntryUse.world_setting_id
            schema = WorldSettingWithOrigin
        else:
            raise ValueError("不支持该类型的书内设定投影")
        model = kind.model
        statement = (
            select(model, SeriesEntryUse.series_id, SeriesEntryUse.series_entry_id)
            .outerjoin(
                SeriesEntryUse, and_(SeriesEntryUse.project_id == model.project_id, binding_column == model.id),
            )
            .where(model.project_id == project_id)
            .order_by(*kind.order_by)
        )
        if category is not None and kind is story_service.WORLD:
            statement = statement.where(model.category == category)
        if item_id is not None:
            statement = statement.where(model.id == item_id)
        rows = (await db.execute(statement)).all()
        from app.services.setting_image_service import metadata_for_book

        thumbnail_kind = "character" if kind is story_service.CHARACTERS else "world"
        thumbnails = await metadata_for_book(db, project_id, thumbnail_kind, [item.id for item, _, _ in rows])
        series_ids = {series_id for _, series_id, _ in rows if series_id is not None}
        visible_series = await visible_series_ids(db, user_id, series_ids)
        current_series, admin_read, can_edit, local_ids = await _final_book_access(
            db, user_id, project_id, subject_kind=thumbnail_kind, subject_ids=[item.id for item, _, _ in rows],
            guard=grant_guard, require_admin=initial_access.role == "admin",
        )
        visible_series.intersection_update(current_series)
    # 原管理员人工投影仅显示“系列”而不包含系列编号；单书协作不获得这一来源。
    result: list[SettingOut] = []
    for item, series_id, entry_id in rows:
        out = schema.model_validate(item)
        out.thumbnail = thumbnails.get(item.id)
        out.thumbnail_editable = can_edit and item.id in local_ids
        if series_id is not None:
            if series_id in visible_series:
                out.origin = SettingOrigin(scope="series", series_id=series_id, series_entry_id=entry_id)
            elif admin_read:
                out.origin = SettingOrigin(scope="series")
        result.append(out)
    if item_id is not None and not result:
        raise NotFoundError(kind.not_found)
    return result
