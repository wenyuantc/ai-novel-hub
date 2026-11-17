"""个人模板授权、快照持久化与一次事务的幂等建书。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING

import anyio
from pydantic import ValidationError
from sqlalchemy import LargeBinary, Text, cast, delete, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import defer

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import NovelTemplate, NovelTemplateCreationReceipt, NovelTemplateImage, Project, User, WritingStyle
from app.schemas.novel_template import (
    TemplateConfigSummary,
    TemplateContentItem,
    TemplateDetail,
    TemplateGroup,
    TemplateList,
    TemplatePreview,
    TemplateProjectCreate,
    TemplateSave,
    TemplateSnapshotReplace,
    TemplateSummary,
    TemplateUpdate,
)
from app.schemas.novel_template_snapshot import (
    MAX_IMAGE_BYTES,
    MAX_JSON_BYTES,
    NovelTemplateSnapshot,
    TemplateSelection,
)
from app.services import authorization_service as authz
from app.services import pen_name_service, project_lifecycle, project_service, quota_service
from app.services.setting_category_query_service import category_read_snapshot
from app.services.setting_image_codec import MAX_STANDARD_BYTES, StandardImage, validate_standard_image

if TYPE_CHECKING:
    from app.services.novel_template_snapshot import CaptureResult

_validation_limiter = anyio.CapacityLimiter(2)
_GROUPS: tuple[TemplateGroup, ...] = ("characters", "world_settings", "organizations", "careers", "outlines")
# JSON 编码器可能将一个字节的字符扩成六字节的 \u00XX 转义。
_MAX_JSON_STORAGE_BYTES = 6 * MAX_JSON_BYTES


def _actor(db: AsyncSession, user: User) -> authz.AuthenticatedActor:
    actor = authz.actor_snapshot(db, user)
    if not isinstance(actor, authz.AuthenticatedActor):
        raise TypeError("模板请求必须携带原登录身份")
    return actor


def _source_stamp(project: Project, capture: CaptureResult) -> str:
    # 来源标题会随模板保存，生命周期代次防止回收再恢复后复用删除前的预览。
    generation = project.last_deleted_at.isoformat() if project.last_deleted_at is not None else ""
    value = json.dumps(["novel-template-source-v1", project.id, generation, project.title, capture.source_stamp],
                       ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stale_source() -> ConflictError:
    return ConflictError("源作品已变化，请更新预览", extra={"code": "template_source_changed"})


def _stale_revision(row: NovelTemplate) -> ConflictError:
    return ConflictError("模板已变化，请刷新后重试", extra={"code": "stale_revision", "revision": row.revision})


def _summary(row) -> TemplateSummary:
    return TemplateSummary.model_validate(row)


async def _refresh_summary(db: AsyncSession, row: NovelTemplate) -> TemplateSummary:
    await db.refresh(row, attribute_names=[
        "id", "name", "description", "revision", "source_title", "counts", "created_at", "updated_at",
    ])
    return _summary(row)


async def _owned(db: AsyncSession, owner_id: int, template_id: int, *, lock: bool = False) -> NovelTemplate:
    # 列表和元数据写操作不需要加载正文；读取正文前另作数据库内体积预检。
    statement = select(NovelTemplate).options(defer(NovelTemplate.snapshot)).where(
        NovelTemplate.id == template_id, NovelTemplate.owner_id == owner_id,
    )
    if lock:
        statement = statement.with_for_update()
    with db.no_autoflush:
        row = await db.scalar(statement.execution_options(populate_existing=True))
    if row is None:
        raise NotFoundError("小说模板不存在")
    return row


async def _bounded_snapshot(db: AsyncSession, row: NovelTemplate) -> dict:
    text_value = cast(NovelTemplate.snapshot, Text)
    length = (func.octet_length(text_value) if db.get_bind().dialect.name == "postgresql"
              else func.length(cast(text_value, LargeBinary)))
    # SQLite 允许 JSON 列包含非法 UTF-8；按字节取出后由线程统一验证。
    stored_value = text_value if db.get_bind().dialect.name == "postgresql" else cast(text_value, LargeBinary)
    revision = row.revision
    # 读取语句自身也限制体积，避免预检到读取之间快照发生变化后加载无界数据。
    try:
        # 取有界文本而非 ORM JSON：既避免 SQLite 损坏内容在 ORM 隐式解码时抛出 500，
        # 也避免把可能很大的 JSON 解码放在事件循环里。
        raw = await db.scalar(select(stored_value).where(
            NovelTemplate.id == row.id, NovelTemplate.owner_id == row.owner_id,
            NovelTemplate.revision == revision, length <= _MAX_JSON_STORAGE_BYTES,
        ))
    except UnicodeDecodeError as exc:
        raise BadRequestError("小说模板快照无效", extra={"code": "invalid_novel_template"}) from exc
    if raw is None:
        current = await _owned(db, row.owner_id, row.id)
        if current.revision != revision:
            raise _stale_revision(current)
        size = await db.scalar(select(length).where(NovelTemplate.id == row.id))
        if size is not None and size > _MAX_JSON_STORAGE_BYTES:
            raise BadRequestError("模板内容过大", extra={"code": "template_too_large"})
        raise BadRequestError("小说模板快照无效", extra={"code": "invalid_novel_template"})
    try:
        value = await anyio.to_thread.run_sync(json.loads, raw, limiter=_validation_limiter)
    except (ValueError, TypeError, UnicodeDecodeError, RecursionError) as exc:
        raise BadRequestError("小说模板快照无效", extra={"code": "invalid_novel_template"}) from exc
    if not isinstance(value, dict):
        raise BadRequestError("小说模板快照无效", extra={"code": "invalid_novel_template"})
    return value


async def list_templates(
    db: AsyncSession, user: User, *, q: str, page: int, page_size: int,
) -> TemplateList:
    actor = _actor(db, user)
    await authz.require_authenticated(db, actor)
    condition = NovelTemplate.owner_id == actor.id
    if q:
        # 输入按字面匹配，不能将 %/_ 当作通配符。
        literal = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        condition &= NovelTemplate.name.ilike(f"%{literal}%", escape="\\")
    total = int(await db.scalar(select(func.count(NovelTemplate.id)).where(condition)) or 0)
    columns = (NovelTemplate.id, NovelTemplate.name, NovelTemplate.description, NovelTemplate.revision,
               NovelTemplate.source_title, NovelTemplate.counts, NovelTemplate.created_at, NovelTemplate.updated_at)
    rows = (await db.execute(select(*columns).where(condition).order_by(
        NovelTemplate.updated_at.desc(), NovelTemplate.id.desc(),
    ).offset((page - 1) * page_size).limit(page_size))).all()
    await authz.require_authenticated(db, actor)
    return TemplateList(items=[_summary(dict(row._mapping)) for row in rows], total=total,
                        page=page, page_size=page_size)


def _validate_json(snapshot: dict) -> NovelTemplateSnapshot:
    try:
        data = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(data) > MAX_JSON_BYTES:
            raise BadRequestError("模板内容过大", extra={"code": "template_too_large"})
        return NovelTemplateSnapshot.model_validate(snapshot)
    except BadRequestError:
        raise
    except (ValidationError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise BadRequestError("小说模板快照无效", extra={"code": "invalid_novel_template"}) from exc


async def template_detail(
    db: AsyncSession, user: User, template_id: int, *, group: TemplateGroup | None, page: int, page_size: int,
) -> TemplateDetail:
    actor = _actor(db, user)
    await authz.require_authenticated(db, actor)
    row = await _owned(db, actor.id, template_id)
    revision = row.revision
    payload = await _bounded_snapshot(db, row)
    snapshot = await anyio.to_thread.run_sync(_validate_json, payload, limiter=_validation_limiter)
    contents: dict[str, list[TemplateContentItem]] = {}
    for key in _GROUPS:
        entries = getattr(snapshot, key)
        selected = entries[(page - 1) * page_size:page * page_size] if group == key else entries[:50]
        if group is not None and group != key:
            selected = []
        contents[key] = [TemplateContentItem(
            ref=item.ref, name=getattr(item, "name", None) or getattr(item, "title", ""),
            has_image=getattr(item, "image_ref", None) is not None,
        ) for item in selected]
    await authz.require_authenticated(db, actor)
    fresh = await _owned(db, actor.id, template_id)
    if fresh.revision != revision:
        raise _stale_revision(fresh)
    config = snapshot.config
    config_summary = TemplateConfigSummary(
        genre=config.genre, premise=config.premise, default_style_name=config.style.name if config.style else None,
        field_count=len(snapshot.field_definitions), category_count=len(snapshot.categories),
    ) if config is not None else None
    return TemplateDetail(**_summary(fresh).model_dump(), include_config=config is not None,
                          config_summary=config_summary, contents=contents)


async def template_image(
    db: AsyncSession, user: User, template_id: int, ref: str, revision: int,
) -> tuple[bytes, str, str]:
    actor = _actor(db, user)
    await authz.require_authenticated(db, actor)
    row = (await db.execute(
        select(NovelTemplateImage.id, NovelTemplateImage.mime_type, NovelTemplateImage.sha256,
               NovelTemplateImage.width, NovelTemplateImage.height, NovelTemplateImage.byte_size,
               func.length(NovelTemplateImage.data).label("actual_size"))
        .join(NovelTemplate, NovelTemplate.id == NovelTemplateImage.template_id)
        .where(NovelTemplate.id == template_id, NovelTemplate.owner_id == actor.id,
               NovelTemplate.revision == revision, NovelTemplateImage.local_ref == ref)
    )).one_or_none()
    if row is None:
        raise NotFoundError("模板图片不存在或已变化")
    if row.actual_size != row.byte_size or not 0 < row.byte_size <= MAX_STANDARD_BYTES:
        raise BadRequestError("小说模板图片无效", extra={"code": "invalid_novel_template"})
    data = await db.scalar(select(NovelTemplateImage.data).where(
        NovelTemplateImage.id == row.id, func.length(NovelTemplateImage.data) == row.byte_size,
    ))
    if data is None:
        raise NotFoundError("模板图片不存在或已变化")

    def verify() -> None:
        if len(data) != row.byte_size or hashlib.sha256(data).hexdigest() != row.sha256:
            raise BadRequestError("小说模板图片无效", extra={"code": "invalid_novel_template"})
        try:
            image = validate_standard_image(data, row.mime_type)
        except BadRequestError as exc:
            raise BadRequestError("小说模板图片无效", extra={"code": "invalid_novel_template"}) from exc
        if image.width != row.width or image.height != row.height:
            raise BadRequestError("小说模板图片无效", extra={"code": "invalid_novel_template"})

    await anyio.to_thread.run_sync(verify, limiter=_validation_limiter)
    await authz.require_authenticated(db, actor)
    # 将模板版本、当前图片和原 token 放在同一最终 SQL 中核验。
    valid = await db.scalar(
        select(NovelTemplateImage.id).join(NovelTemplate, NovelTemplate.id == NovelTemplateImage.template_id)
        .join(User, User.id == NovelTemplate.owner_id)
        .where(NovelTemplate.id == template_id, NovelTemplate.owner_id == actor.id,
               NovelTemplate.revision == revision, NovelTemplateImage.id == row.id, NovelTemplateImage.local_ref == ref,
               NovelTemplateImage.sha256 == row.sha256, User.is_active.is_(True),
               User.token_version == actor.token_version)
    )
    if valid is None:
        raise NotFoundError("模板图片不存在或已变化")
    return data, row.mime_type, row.sha256


async def preview_source(
    factory: async_sessionmaker[AsyncSession], actor: authz.AuthenticatedActor,
    project_id: int, selection: TemplateSelection,
) -> TemplatePreview:
    from app.services import novel_template_snapshot

    async with category_read_snapshot(factory, actor, project_id) as read_db:
        source = await project_service.get_owned_project(read_db, actor.id, project_id)
        generation: datetime | None = source.last_deleted_at
        captured = await novel_template_snapshot.capture(read_db, project_id, selection)
        stamp = _source_stamp(source, captured)
    # 必须使用新事务重验，不在旧一致性快照中伪装成实时授权。
    async with factory() as check_db:
        await authz.require_authenticated(check_db, actor)
        current = await project_service.get_owned_project(check_db, actor.id, project_id)
        if current.last_deleted_at != generation:
            raise _stale_source()
    return TemplatePreview(
        source_stamp=stamp, missing_dependencies=captured.missing_dependencies,
        included_definitions=captured.included_definitions,
        included_relations=captured.included_relations, omitted_relations=captured.omitted_relations,
        counts=captured.counts, estimated_bytes=captured.estimated_bytes, json_bytes=captured.json_bytes,
        image_bytes=captured.image_bytes, warnings=captured.warnings,
    )


async def _source_locked(db: AsyncSession, actor: authz.AuthenticatedActor, project_id: int) -> Project:
    # 风格删除可能以 Style→Project 顺序执行 SET NULL；先锁风格，后锁项目并复验绑定，避免反序死锁。
    source = await project_service.get_owned_project(db, actor.id, project_id)
    style_id = source.default_style_id
    if style_id is not None:
        with db.no_autoflush:
            await db.scalar(select(WritingStyle.id).where(WritingStyle.id == style_id).with_for_update())
    source = await project_service.get_owned_project(db, actor.id, project_id, lock=True)
    if source.default_style_id != style_id:
        raise _stale_source()
    await authz.require_authenticated(db, actor)
    return source


async def _capture_for_save(
    db: AsyncSession, source: Project, selection: TemplateSelection, expected_stamp: str,
) -> CaptureResult:
    from app.services import novel_template_snapshot

    captured = await novel_template_snapshot.capture(db, source.id, selection)
    if _source_stamp(source, captured) != expected_stamp:
        raise _stale_source()
    if captured.missing_dependencies or captured.snapshot is None:
        raise BadRequestError("请先补选所需设定后重新预览", extra={
            "code": "template_dependencies_missing", "missing_dependencies": captured.missing_dependencies,
        })
    return captured


async def _write_images(db: AsyncSession, template_id: int, images: dict[str, StandardImage]) -> None:
    for ref, image in images.items():
        db.add(NovelTemplateImage(template_id=template_id, local_ref=ref, mime_type=image.mime_type, data=image.data,
                                  width=image.width, height=image.height, byte_size=len(image.data),
                                  sha256=image.sha256))
    await db.flush()


async def save_template(db: AsyncSession, user: User, body: TemplateSave) -> TemplateSummary:
    actor = _actor(db, user)
    try:
        await authz.require_authenticated(db, actor, write=True)
        source = await _source_locked(db, actor, body.source_project_id)
        captured = await _capture_for_save(db, source, body.selection, body.source_stamp)
        row = NovelTemplate(owner_id=actor.id, name=body.name, description=body.description,
                            snapshot=captured.snapshot.model_dump(mode="json"), counts=captured.counts,
                            source_title=source.title)
        db.add(row)
        await db.flush()
        await _write_images(db, row.id, captured.images)
        await db.commit()
        return await _refresh_summary(db, row)
    except BaseException:
        await db.rollback()
        raise


async def update_template(db: AsyncSession, user: User, template_id: int, body: TemplateUpdate) -> TemplateSummary:
    actor = _actor(db, user)
    try:
        await authz.require_authenticated(db, actor, write=True)
        row = await _owned(db, actor.id, template_id, lock=True)
        if row.revision != body.expected_revision:
            raise _stale_revision(row)
        if "name" in body.model_fields_set:
            row.name = body.name
        if "description" in body.model_fields_set:
            row.description = body.description
        row.revision += 1
        await db.commit()
        return await _refresh_summary(db, row)
    except BaseException:
        await db.rollback()
        raise


async def replace_snapshot(
    db: AsyncSession, user: User, template_id: int, body: TemplateSnapshotReplace,
) -> TemplateSummary:
    actor = _actor(db, user)
    try:
        await authz.require_authenticated(db, actor, write=True)
        # 与其他模板写共用 User 屏障；先锁风格和来源作品，再锁模板行。
        source = await _source_locked(db, actor, body.source_project_id)
        row = await _owned(db, actor.id, template_id, lock=True)
        if row.revision != body.expected_revision:
            raise _stale_revision(row)
        captured = await _capture_for_save(db, source, body.selection, body.source_stamp)
        await db.execute(delete(NovelTemplateImage).where(NovelTemplateImage.template_id == row.id))
        await _write_images(db, row.id, captured.images)
        row.snapshot = captured.snapshot.model_dump(mode="json")
        row.counts = captured.counts
        row.source_title = source.title
        row.revision += 1
        await db.commit()
        return await _refresh_summary(db, row)
    except BaseException:
        await db.rollback()
        raise


async def delete_template(db: AsyncSession, user: User, template_id: int, expected_revision: int) -> None:
    actor = _actor(db, user)
    try:
        await authz.require_authenticated(db, actor, write=True)
        row = await _owned(db, actor.id, template_id, lock=True)
        if row.revision != expected_revision:
            raise _stale_revision(row)
        await db.delete(row)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


def _request_digest(template_id: int, body: TemplateProjectCreate) -> str:
    request = {"version": 1, "template_id": template_id, "expected_revision": body.expected_revision,
               "title": body.title, "pen_name_id": body.pen_name_id,
               "overrides": body.overrides.model_dump(mode="json", exclude_unset=True)}
    raw = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _material(db: AsyncSession, row: NovelTemplate) -> tuple[NovelTemplateSnapshot, dict[str, StandardImage]]:
    from app.services.novel_template_snapshot import validate_snapshot

    metadata = (await db.execute(select(
        NovelTemplateImage.id, NovelTemplateImage.local_ref, NovelTemplateImage.mime_type, NovelTemplateImage.width,
        NovelTemplateImage.height, NovelTemplateImage.byte_size, NovelTemplateImage.sha256,
        func.length(NovelTemplateImage.data).label("actual_size"),
    ).where(NovelTemplateImage.template_id == row.id).limit(2001))).all()
    if len(metadata) > 2000 or sum(image.actual_size for image in metadata) > MAX_IMAGE_BYTES:
        raise BadRequestError("模板图片过大", extra={"code": "template_too_large"})
    if any(image.actual_size != image.byte_size or not 0 < image.byte_size <= MAX_STANDARD_BYTES for image in metadata):
        raise BadRequestError("小说模板图片无效", extra={"code": "invalid_novel_template"})

    def checked_batch(batch, blobs) -> dict[str, StandardImage]:
        checked = {}
        for meta in batch:
            data = blobs.get(meta.id)
            if data is None or len(data) != meta.byte_size or hashlib.sha256(data).hexdigest() != meta.sha256:
                raise BadRequestError("小说模板图片无效", extra={"code": "invalid_novel_template"})
            checked[meta.local_ref] = StandardImage(data, meta.width, meta.height, meta.mime_type)
        return checked

    images: dict[str, StandardImage] = {}
    for offset in range(0, len(metadata), 200):
        batch = metadata[offset:offset + 200]
        blobs = dict((await db.execute(select(NovelTemplateImage.id, NovelTemplateImage.data).where(
            NovelTemplateImage.template_id == row.id,
            tuple_(NovelTemplateImage.id, func.length(NovelTemplateImage.data)).in_(
                [(item.id, item.byte_size) for item in batch]
            ),
        ))).all())
        images.update(await anyio.to_thread.run_sync(checked_batch, batch, blobs, limiter=_validation_limiter))
    payload = await _bounded_snapshot(db, row)
    try:
        snapshot = await anyio.to_thread.run_sync(validate_snapshot, payload, images, limiter=_validation_limiter)
    except RecursionError as exc:
        raise BadRequestError("小说模板快照无效", extra={"code": "invalid_novel_template"}) from exc
    return snapshot, images


async def create_project_from_template(
    db: AsyncSession, user: User, template_id: int, body: TemplateProjectCreate,
):
    from app.services.novel_template_apply import apply_template

    actor = _actor(db, user)
    digest = _request_digest(template_id, body)
    try:
        # 与其他写入一致，按 gate→User 获取并持有原身份屏障直到事务结束。
        await authz.require_authenticated(db, actor, write=True)
        # 仅普通读取 receipt，系统 purge 的顺序是 Project→receipt。
        receipt = await db.scalar(select(NovelTemplateCreationReceipt).where(
            NovelTemplateCreationReceipt.owner_id == actor.id,
            NovelTemplateCreationReceipt.intent_key == body.intent_key,
        ))
        if receipt is not None:
            if receipt.request_version != 1 or receipt.request_digest != digest:
                raise ConflictError("该建书意图已用于其他请求", extra={"code": "creation_intent_conflict"})
            if receipt.result_project_id is None:
                raise ConflictError("此前创建的作品已不可用", extra={"code": "creation_result_unavailable"})
            try:
                project = await project_lifecycle.lock_record(db, receipt.result_project_id)
            except NotFoundError:
                raise ConflictError("此前创建的作品已不可用", extra={"code": "creation_result_unavailable"}) from None
            if project.user_id != actor.id or project.deleted_at is not None or project.deletion_started_at is not None:
                raise ConflictError("此前创建的作品已不可用", extra={"code": "creation_result_unavailable"})
            result = await project_service.project_out(db, project, actor_user_id=actor.id)
            await db.rollback()
            return result
        row = await _owned(db, actor.id, template_id, lock=True)
        if row.revision != body.expected_revision:
            raise _stale_revision(row)
        snapshot, images = await _material(db, row)
        await quota_service.check_project_creation(db, actor.id)
        project = Project(user_id=actor.id, title=body.title, genre="", premise="", style="", status="planning")
        db.add(project)
        await db.flush()
        await apply_template(db, project, snapshot, images)
        for key, value in body.overrides.model_dump(exclude_unset=True).items():
            setattr(project, key, value)
        if body.pen_name_id is not None:
            await pen_name_service.bind_without_commit(db, actor.id, project.id, body.pen_name_id)
        db.add(NovelTemplateCreationReceipt(owner_id=actor.id, intent_key=body.intent_key,
                                             request_version=1, request_digest=digest, result_project_id=project.id))
        await db.flush()
        await authz.require_authenticated(db, actor)
        await db.commit()
        await db.refresh(project)
        return await project_service.project_out(db, project, actor_user_id=actor.id)
    except BaseException:
        await db.rollback()
        raise
