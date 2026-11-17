"""设定缩略图的授权、版本及不可变资源操作。"""

from collections.abc import Collection
from dataclasses import dataclass
from typing import cast

from sqlalchemy import and_, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models import Career, Character, Organization, ProjectCollaborator, SeriesEntryUse, WorldSetting
from app.models.setting_images import SettingImageResource, SettingThumbnailBinding
from app.schemas.setting_field_value import EntityKind
from app.schemas.setting_image import SeriesThumbnailResult, ThumbnailMeta
from app.services import authorization_service, project_access, series_policy
from app.services.setting_image_codec import StandardImage, normalize_image_async

_MODELS = {"character": Character, "world": WorldSetting, "organization": Organization, "career": Career}
_COLUMNS = {
    "character": SettingThumbnailBinding.character_id,
    "world": SettingThumbnailBinding.world_setting_id,
    "organization": SettingThumbnailBinding.organization_id,
    "career": SettingThumbnailBinding.career_id,
}
_FIELD_NAMES = {"character": "character_id", "world": "world_setting_id", "organization": "organization_id",
                "career": "career_id"}


def _meta(row) -> ThumbnailMeta:
    return ThumbnailMeta(version=row.id, mime_type=row.mime_type, width=row.width, height=row.height)


async def _subject(db: AsyncSession, project_id: int, kind: EntityKind, entity_id: int):
    model = _MODELS[kind]
    row = await db.scalar(select(model).where(model.id == entity_id, model.project_id == project_id))
    if row is None:
        raise NotFoundError("设定条目不存在")
    return row


async def metadata_for_book(
    db: AsyncSession, project_id: int, kind: EntityKind, ids: Collection[int],
) -> dict[int, ThumbnailMeta]:
    """批量读取当前绑定的元数据，不读取图像 bytes。"""
    if not ids:
        return {}
    column = _COLUMNS[kind]
    resource = SettingImageResource
    binding = SettingThumbnailBinding
    rows = await db.execute(select(column.label("subject_id"), resource.id, resource.mime_type,
                                   resource.width, resource.height)
                            .join(resource, resource.id == binding.image_resource_id)
                            .where(binding.project_id == project_id, column.in_(ids)))
    result = {row.subject_id: _meta(row) for row in rows}
    if kind in ("character", "world"):
        use_column = SeriesEntryUse.character_id if kind == "character" else SeriesEntryUse.world_setting_id
        rows = await db.execute(select(use_column.label("subject_id"), resource.id, resource.mime_type,
                                       resource.width, resource.height)
                                .select_from(SeriesEntryUse)
                                .outerjoin(binding, and_(binding.series_id == SeriesEntryUse.series_id,
                                                         binding.series_entry_id == SeriesEntryUse.series_entry_id))
                                .outerjoin(resource, resource.id == binding.image_resource_id)
                                .where(SeriesEntryUse.project_id == project_id, use_column.in_(ids)))
        for row in rows:
            # 已采用的设定只显示系列当前图，即使存在异常的遗留本地绑定也不回退。
            result.pop(row.subject_id, None)
            if row.id is not None:
                result[row.subject_id] = _meta(row)
    return result


async def metadata_for_series(db: AsyncSession, sid: int, ids: Collection[int]) -> dict[int, ThumbnailMeta]:
    if not ids:
        return {}
    resource, binding = SettingImageResource, SettingThumbnailBinding
    rows = await db.execute(select(binding.series_entry_id, resource.id, resource.mime_type,
                                   resource.width, resource.height)
                            .join(resource, resource.id == binding.image_resource_id)
                            .where(binding.series_id == sid, binding.series_entry_id.in_(ids)))
    return {row.series_entry_id: _meta(row) for row in rows}


async def _current_book_resource(db: AsyncSession, project_id: int, kind: EntityKind, entity_id: int):
    binding = SettingThumbnailBinding
    if kind in ("character", "world"):
        use_col = SeriesEntryUse.character_id if kind == "character" else SeriesEntryUse.world_setting_id
        use = await db.scalar(select(SeriesEntryUse).where(SeriesEntryUse.project_id == project_id,
                                                            use_col == entity_id))
        if use is not None:
            return await db.scalar(select(SettingImageResource).join(
                binding, binding.image_resource_id == SettingImageResource.id,
            ).where(binding.series_id == use.series_id, binding.series_entry_id == use.series_entry_id))
    return await db.scalar(select(SettingImageResource).join(
        binding, binding.image_resource_id == SettingImageResource.id,
    ).where(binding.project_id == project_id, _COLUMNS[kind] == entity_id))


def _current_book_image_guard(project_id: int, kind: EntityKind, entity_id: int, image_id: str):
    """最终授权的同一快照中确认主体和资源仍为当前有效绑定。"""
    model = _MODELS[kind]
    subject = select(model.id).where(model.project_id == project_id, model.id == entity_id).exists()
    binding = SettingThumbnailBinding
    local = select(binding.id).where(
        binding.project_id == project_id, _COLUMNS[kind] == entity_id, binding.image_resource_id == image_id,
    ).exists()
    if kind not in ("character", "world"):
        return and_(subject, local)
    use_col = SeriesEntryUse.character_id if kind == "character" else SeriesEntryUse.world_setting_id
    adopted = select(SeriesEntryUse.project_id).where(
        SeriesEntryUse.project_id == project_id, use_col == entity_id,
    ).exists()
    canonical = select(binding.id).join(
        SeriesEntryUse, and_(binding.series_id == SeriesEntryUse.series_id,
                             binding.series_entry_id == SeriesEntryUse.series_entry_id),
    ).where(
        SeriesEntryUse.project_id == project_id, use_col == entity_id, binding.image_resource_id == image_id,
    ).exists()
    return and_(subject, or_(canonical, and_(not_(adopted), local)))


async def get_book_image(db: AsyncSession, actor: int, project_id: int, kind: EntityKind, entity_id: int):
    from app.services.series_read import _final_book_access

    _, access, member = await project_access.resolve_access(db, actor, project_id)
    grant = (member.invitation_id, member.revision) if access.is_collaborator and member else None
    await _subject(db, project_id, kind, entity_id)
    image = await _current_book_resource(db, project_id, kind, entity_id)
    guard = _current_book_image_guard(project_id, kind, entity_id, image.id) if image is not None else None
    if grant is not None:
        guard = and_(
            guard if guard is not None else True,
            ProjectCollaborator.invitation_id == grant[0], ProjectCollaborator.revision == grant[1],
        )
    await _final_book_access(db, actor, project_id, guard=guard, require_admin=access.role == "admin")
    if image is None:
        raise NotFoundError("缩略图不存在")
    return image


@dataclass(frozen=True)
class _BookAuthority:
    grant: tuple[str, int] | None
    admin: bool


async def _book_write_guard(db: AsyncSession, actor: int, project_id: int, kind: EntityKind, entity_id: int,
                            *, lock: bool, authority: _BookAuthority | None = None) -> _BookAuthority | None:
    with db.no_autoflush:
        if lock:
            await project_access.require_project(
                db, actor, project_id, "content.edit", for_update=True,
                grant_identity=authority.grant if authority else None,
            )
            if authority is not None and authority.admin:
                snapshot = await authorization_service.require_authenticated(db, actor)
                if not snapshot.allows("projects.read") or not snapshot.allows("projects.edit"):
                    raise ForbiddenError("原作品管理权限已变化", extra={"code": "permission_denied"})
        else:
            _, access, member = await project_access.resolve_access(db, actor, project_id)
            if not access.can_edit_content:
                # 复用原有错误语义；若权限恰在两次读取之间改变，拒绝本次旧请求。
                await project_access.require_project(db, actor, project_id, "content.edit")
                raise ConflictError("作品权限已变化，请刷新后重试", extra={"code": "stale_project_access"})
            grant = (member.invitation_id, member.revision) if access.is_collaborator and member else None
            authority = _BookAuthority(grant=grant, admin=access.role == "admin")
        await _subject(db, project_id, kind, entity_id)
        if kind in ("character", "world"):
            await series_policy.reject_bound_entry(db, project_id, cast(series_policy.ProjectionKind, kind), entity_id,
                                                   actor_user_id=actor)
        return authority


def _check_version(current: str | None, expected: str) -> None:
    if current != (expected or None):
        raise ConflictError("缩略图已变化，请刷新后重试", extra={"code": "thumbnail_changed"})


async def set_book_image_without_commit(db: AsyncSession, project_id: int, kind: EntityKind, entity_id: int,
                                        image: StandardImage | None) -> ThumbnailMeta | None:
    binding = await db.scalar(select(SettingThumbnailBinding).where(
        SettingThumbnailBinding.project_id == project_id, _COLUMNS[kind] == entity_id))
    if image is None:
        if binding is not None:
            await db.delete(binding)
        return None
    resource = SettingImageResource(project_id=project_id, data=image.data, mime_type=image.mime_type,
                                    width=image.width, height=image.height, byte_size=len(image.data),
                                    sha256=image.sha256)
    db.add(resource)
    await db.flush()
    if binding is None:
        binding = SettingThumbnailBinding(project_id=project_id, image_resource_id=resource.id,
                                          **{_FIELD_NAMES[kind]: entity_id})
        db.add(binding)
    else:
        binding.image_resource_id = resource.id
    await db.flush()
    return _meta(resource)


async def put_book_image(db: AsyncSession, actor: int, project_id: int, kind: EntityKind, entity_id: int,
                         data: bytes, mime_type: str | None, expected: str) -> ThumbnailMeta:
    authority = await _book_write_guard(db, actor, project_id, kind, entity_id, lock=False)
    image = await normalize_image_async(data, mime_type)
    await _book_write_guard(db, actor, project_id, kind, entity_id, lock=True, authority=authority)
    current = await _current_book_resource(db, project_id, kind, entity_id)
    _check_version(current.id if current else None, expected)
    meta = await set_book_image_without_commit(db, project_id, kind, entity_id, image)
    await db.commit()
    assert meta is not None
    return meta


async def delete_book_image(db: AsyncSession, actor: int, project_id: int, kind: EntityKind, entity_id: int,
                            expected: str) -> None:
    await _book_write_guard(db, actor, project_id, kind, entity_id, lock=True)
    current = await _current_book_resource(db, project_id, kind, entity_id)
    # 无图时重复删除幂等；有新图时旧版本绝不可删除。
    if current is not None:
        _check_version(current.id, expected)
        await set_book_image_without_commit(db, project_id, kind, entity_id, None)
    await db.commit()


async def _series_image(db: AsyncSession, sid: int, eid: int):
    return await db.scalar(select(SettingImageResource).join(
        SettingThumbnailBinding, SettingThumbnailBinding.image_resource_id == SettingImageResource.id,
    ).where(SettingThumbnailBinding.series_id == sid, SettingThumbnailBinding.series_entry_id == eid))


async def get_series_image(db: AsyncSession, actor: int, sid: int, eid: int):
    from app.services import series_service as series

    item = await series._readable(db, actor, sid)
    version = await series._current_authorization_version(db, item, actor)
    await series._entry(db, sid, eid)
    image = await _series_image(db, sid, eid)
    await series._verify_read_revision(db, actor, sid, item.revision, version)
    if image is None:
        raise NotFoundError("缩略图不存在")
    return image


async def set_series_image_without_commit(db: AsyncSession, sid: int, eid: int,
                                          image: StandardImage | None) -> ThumbnailMeta | None:
    binding = await db.scalar(select(SettingThumbnailBinding).where(
        SettingThumbnailBinding.series_id == sid, SettingThumbnailBinding.series_entry_id == eid))
    if image is None:
        if binding is not None:
            await db.delete(binding)
        return None
    resource = SettingImageResource(series_id=sid, data=image.data, mime_type=image.mime_type,
                                    width=image.width, height=image.height, byte_size=len(image.data),
                                    sha256=image.sha256)
    db.add(resource)
    await db.flush()
    if binding is None:
        db.add(SettingThumbnailBinding(series_id=sid, series_entry_id=eid, image_resource_id=resource.id))
    else:
        binding.image_resource_id = resource.id
    await db.flush()
    return _meta(resource)


async def put_series_image(db: AsyncSession, actor: int, sid: int, eid: int, data: bytes,
                           mime_type: str | None, expected: str, revision: int) -> SeriesThumbnailResult:
    from app.services import series_service as series

    readable = await series._readable(db, actor, sid)
    authorization_version = await series._current_authorization_version(db, readable, actor)
    await series._entry(db, sid, eid)
    image = await normalize_image_async(data, mime_type)
    async with series._edit(db, actor, sid, revision) as parent:
        if await series._current_authorization_version(db, parent, actor) != authorization_version:
            raise ConflictError("系列协作授权已变化，请刷新后重试", extra={"code": "stale_series_access"})
        await series._entry(db, sid, eid)
        uses = await series._uses(db, sid, entry_ids=[eid])
        await series._lock_projects(db, actor, parent, [use.project_id for use in uses], projection=True)
        current = await _series_image(db, sid, eid)
        _check_version(current.id if current else None, expected)
        meta = await set_series_image_without_commit(db, sid, eid, image)
        parent.revision += 1
        return SeriesThumbnailResult(thumbnail=meta, revision=parent.revision)


async def delete_series_image(db: AsyncSession, actor: int, sid: int, eid: int,
                              expected: str, revision: int) -> SeriesThumbnailResult:
    from app.services import series_service as series

    async with series._edit(db, actor, sid, revision) as parent:
        await series._entry(db, sid, eid)
        uses = await series._uses(db, sid, entry_ids=[eid])
        await series._lock_projects(db, actor, parent, [use.project_id for use in uses], projection=True)
        current = await _series_image(db, sid, eid)
        if current is not None:
            _check_version(current.id, expected)
            await set_series_image_without_commit(db, sid, eid, None)
            parent.revision += 1
        return SeriesThumbnailResult(thumbnail=None, revision=parent.revision)
