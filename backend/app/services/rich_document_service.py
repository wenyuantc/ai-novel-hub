"""作者场景文档的读取与单事务保存。"""

from uuid import UUID

from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Scene, SceneDocumentMeta, SceneMarker, SceneSection
from app.schemas.rich_document import SceneDocument, SceneDocumentOut, SceneDocumentSave, marker_excerpt
from app.services import scene_service, setting_ai_visibility, version_service
from app.services.audit_writer import AuditOperation
from app.services.chapter_service import get_manageable_chapter
from app.services.rich_document_core import validate_storage_bounds
from app.services.rich_document_store import capture_documents, retire_marker


def _intent_conflict() -> ConflictError:
    return ConflictError("区块或标记的增删意图与完整文档不一致，请保留草稿并重新核对", extra={
        "code": "document_intent_mismatch",
    })


def _mapping_conflict() -> ConflictError:
    return ConflictError("正文变更缺少可靠的区块或标记映射，请保留草稿并重新核对", extra={
        "code": "document_mapping_required",
    })


async def get_document(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, scene_id: int,
) -> SceneDocumentOut:
    with db.no_autoflush:
        chapter = await get_manageable_chapter(db, user_id, project_id, chapter_id)
        content_revision = chapter.content_revision
        planning_revision = await scene_service._planning_revision(db, project_id)
        scene = await scene_service.require_scene(db, chapter, scene_id)
        documents, _has_documents = await capture_documents(db, [scene])
        await scene_service.check_revision(db, chapter, content_revision)
        if planning_revision != await scene_service._planning_revision(db, project_id):
            raise ConflictError("文档读取期间规划已变化，请重新加载", extra={"code": "scene_context_changed"})
        return SceneDocumentOut(content_revision=content_revision, planning_revision=planning_revision,
                                document=documents[scene_id])


def _keyed(rows: list[SceneSection] | list[SceneMarker]) -> dict[UUID, SceneSection] | dict[UUID, SceneMarker]:
    try:
        result = {UUID(row.logical_key): row for row in rows}
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConflictError("富文档持久身份无效", extra={"code": "rich_document_invalid"}) from exc
    if len(result) != len(rows):
        raise ConflictError("富文档持久身份重复", extra={"code": "rich_document_invalid"})
    return result


def _check_intents(
    body: SceneDocumentSave, current: SceneDocument,
    section_rows: list[SceneSection], marker_rows: list[SceneMarker],
    *, converted_section_key: UUID | None = None,
) -> None:
    sections = _keyed(section_rows)
    markers = _keyed(marker_rows)
    active_sections = {key for key, row in sections.items() if row.active}
    retired_sections = set(sections) - active_sections
    live_markers = {key for key, row in markers.items() if row.status != "deleted"}
    next_sections = {item.key for item in body.document.sections}
    next_markers = {item.key for item in body.document.markers}
    for item in body.document.sections:
        if item.key in active_sections and (
            not {"key", "anchor", "order", "title", "content", "formatting", "include_in_ai"}
            <= item.model_fields_set
            or not {"version", "boundaries", "inline", "blocks"} <= item.formatting.model_fields_set
        ):
            raise _intent_conflict()
    for item in body.document.markers:
        if item.key in live_markers and not {
            "key", "section_key", "status", "start", "end", "color", "excerpt", "excerpt_truncated", "last_start",
        } <= item.model_fields_set:
            raise _intent_conflict()
    added_sections, deleted_sections = set(body.add_section_keys), set(body.delete_section_keys)
    restored_sections = set(body.restore_section_keys)
    added_markers, deleted_markers = set(body.add_marker_keys), set(body.delete_marker_keys)
    restored_markers = set(body.restore_marker_keys)
    if converted_section_key is not None and converted_section_key not in deleted_sections:
        raise _intent_conflict()
    if (added_sections | restored_sections != next_sections - active_sections
            or deleted_sections != active_sections - next_sections or added_sections & restored_sections
            or added_sections & set(sections) or not restored_sections <= retired_sections
            or set(body.document.retired_section_keys) != (retired_sections | deleted_sections) - restored_sections
            or added_markers | restored_markers != next_markers - live_markers
            or deleted_markers != live_markers - next_markers or added_markers & restored_markers
            or added_markers & set(markers)
            or any(key not in markers or markers[key].status != "deleted" for key in restored_markers)):
        raise _intent_conflict()
    mapped_sections, mapped_markers = set(body.mapped_section_keys), set(body.mapped_marker_keys)
    if not mapped_sections <= active_sections & next_sections or not mapped_markers <= live_markers & next_markers:
        raise _intent_conflict()
    old_sections = {section.key: section for section in current.sections}
    new_sections = {section.key: section for section in body.document.sections}
    old_markers = {marker.key: marker for marker in current.markers}
    new_markers = {marker.key: marker for marker in body.document.markers}
    if current.content != body.document.content and not active_sections & next_sections <= mapped_sections:
        raise _mapping_conflict()
    for key in live_markers & next_markers:
        old, new = old_markers[key], new_markers[key]
        if old.section_key != new.section_key:
            converted = old_sections.get(converted_section_key) if converted_section_key is not None else None
            if (converted is None or old.section_key != converted_section_key or new.section_key is not None
                    or old.status != "active" or new.status != "active" or old.start is None or old.end is None
                    or new.start != converted.anchor + old.start or new.end != converted.anchor + old.end
                    or old.color != new.color or old.excerpt != new.excerpt or old.last_start != new.last_start
                    or old.excerpt_truncated != new.excerpt_truncated or old.draft_excerpt != new.draft_excerpt
                    or old.draft_excerpt_truncated != new.draft_excerpt_truncated
                    or old.draft_last_start != new.draft_last_start):
                # 只有显式转换可迁移标记；普通保存不能通过猜测改变所属正文。
                raise _intent_conflict()
            continue
        if old.status == "stale":
            if new.status == "stale" and (
                old.excerpt != new.excerpt or old.last_start != new.last_start
                or old.excerpt_truncated != new.excerpt_truncated or old.draft_excerpt != new.draft_excerpt
                or old.draft_excerpt_truncated != new.draft_excerpt_truncated
                or old.draft_last_start != new.draft_last_start
            ):
                raise _intent_conflict()
            if new.status == "active" and key not in mapped_markers:
                raise _mapping_conflict()
            continue
        old_text = current.content if old.section_key is None else old_sections[old.section_key].content
        if new.status == "stale":
            assert old.start is not None and old.end is not None
            excerpt, truncated = marker_excerpt(old_text, old.start, old.end)
            if new.excerpt != excerpt or new.excerpt_truncated != truncated or new.last_start != old.start:
                raise _intent_conflict()
            if new.draft_excerpt is not None and key not in mapped_markers:
                raise _mapping_conflict()
            continue
        if old.section_key is None:
            new_text = body.document.content
        else:
            new_text = new_sections[old.section_key].content
        if old_text != new_text and key not in mapped_markers:
            raise _mapping_conflict()


def _enabled_payload(document: SceneDocument) -> dict[UUID, tuple[int, int, str, str]]:
    return {item.key: (item.anchor, item.order, item.title, item.content)
            for item in document.sections if item.include_in_ai}


async def _write_document_rows(
    db: AsyncSession, project_id: int, scene: Scene, body: SceneDocumentSave, current: SceneDocument,
    section_rows: list[SceneSection], marker_rows: list[SceneMarker],
) -> None:
    """不提交；调用方必须先按可见性写锁 → 项目／章锁顺序取得屏障并检查意图。"""
    scene_id = scene.id
    before_enabled, after_enabled = _enabled_payload(current), _enabled_payload(body.document)
    if before_enabled != after_enabled:
        tightened = any(after_enabled.get(key) != value for key, value in before_enabled.items())
        await setting_ai_visibility.bump_visibility_revision(db, project_id, tighten=tightened)

    old_sections = _keyed(section_rows)
    for row in section_rows:
        row.active = False
    await db.flush()  # 先释放同锚点唯一顺序，避免交换顺序时碰撞。
    section_by_key: dict[UUID, SceneSection] = {}
    for item in body.document.sections:
        row = old_sections.get(item.key)
        if row is None:
            row = SceneSection(scene_id=scene_id, logical_key=str(item.key), anchor=item.anchor, sort=item.order,
                               title=item.title, content=item.content)
            db.add(row)
        row.anchor, row.sort, row.title, row.content = item.anchor, item.order, item.title, item.content
        row.formatting = item.formatting.model_dump(mode="json")
        row.include_in_ai, row.active = item.include_in_ai, True
        section_by_key[item.key] = row
    for key in body.delete_section_keys:
        row = old_sections[key]
        row.content, row.formatting, row.include_in_ai = "", {}, False
    await db.flush()
    section_by_key.update({key: row for key, row in old_sections.items() if key not in section_by_key})

    old_markers = _keyed(marker_rows)
    for key in body.delete_marker_keys:
        row = old_markers[key]
        retire_marker(row)
    for item in body.document.markers:
        row = old_markers.get(item.key)
        if row is None:
            row = SceneMarker(scene_id=scene_id, logical_key=str(item.key), status=item.status, color=item.color)
            db.add(row)
        row.section_id = section_by_key[item.section_key].id if item.section_key is not None else None
        row.status, row.color = item.status, item.color
        row.start_offset, row.end_offset, row.last_start, row.excerpt, row.excerpt_truncated = (
            item.start, item.end, item.last_start, item.excerpt, item.excerpt_truncated,
        )
        row.draft_excerpt, row.draft_excerpt_truncated, row.draft_last_start = (
            item.draft_excerpt, item.draft_excerpt_truncated, item.draft_last_start,
        )
    formatting = body.document.formatting
    meta = await db.get(SceneDocumentMeta, scene_id)
    has_formatting = bool(formatting.boundaries or formatting.inline or formatting.blocks)
    if has_formatting:
        if meta is None:
            meta = SceneDocumentMeta(scene_id=scene_id)
            db.add(meta)
        meta.formatting = formatting.model_dump(mode="json")
    elif meta is not None:
        await db.delete(meta)
    scene.content = body.document.content
    await db.flush()
    await check_stored_bounds(db, scene_id)


async def check_stored_bounds(db: AsyncSession, scene_id: int) -> None:
    """验证持久行总量，包括历史身份墓碑，供独立的结构操作复用。"""
    meta = await db.get(SceneDocumentMeta, scene_id)
    stored_sections = list(await db.scalars(select(SceneSection).where(SceneSection.scene_id == scene_id)))
    stored_markers = list(await db.scalars(select(SceneMarker).where(SceneMarker.scene_id == scene_id)))
    section_fields = ("id", "logical_key", "origin_section_id", "anchor", "sort", "title", "content", "formatting",
                      "include_in_ai", "active")
    marker_fields = ("logical_key", "origin_marker_id", "section_id", "status", "color", "start_offset", "end_offset",
                     "last_start", "excerpt", "excerpt_truncated", "draft_excerpt", "draft_excerpt_truncated",
                     "draft_last_start")
    try:
        validate_storage_bounds(
            meta.formatting if meta is not None else {},
            [{field: getattr(row, field) for field in section_fields} for row in stored_sections],
            [{field: getattr(row, field) for field in marker_fields} for row in stored_markers],
        )
    except ValueError as exc:
        raise scene_service.SceneParameterError(str(exc)) from exc


async def write_mapped_document(
    db: AsyncSession, project_id: int, scene: Scene, current: SceneDocument, mapped: SceneDocument,
    expected_content_revision: int,
) -> None:
    """保存由可信服务明确计算的映射，仍校验完整身份与增删意图；不提交或推进章 revision。"""
    body = SceneDocumentSave.model_validate({
        "expected_content_revision": expected_content_revision, "document": mapped.model_dump(mode="json"),
        "add_section_keys": [], "delete_section_keys": [], "add_marker_keys": [], "delete_marker_keys": [],
        "mapped_section_keys": [section.key for section in current.sections],
        "mapped_marker_keys": [marker.key for marker in current.markers],
    })
    section_rows = list(await db.scalars(select(SceneSection).where(SceneSection.scene_id == scene.id)))
    marker_rows = list(await db.scalars(select(SceneMarker).where(SceneMarker.scene_id == scene.id)))
    _check_intents(body, current, section_rows, marker_rows)
    await _write_document_rows(db, project_id, scene, body, current, section_rows, marker_rows)


async def _check_restored_marker_lineage(db: AsyncSession, rows: list[SceneMarker], keys: list[UUID]) -> None:
    """只拒绝与墓碑有明确来源血缘的存活标记；无关同 UUID 不构成冲突。"""
    wanted = set(keys)
    ids = [row.id for row in rows if UUID(row.logical_key) in wanted]
    # 每次分批建立无环可终止的祖先/后代闭包；UNION 去重也防御异常循环。
    for start in range(0, len(ids), 200):
        lineage = select(SceneMarker.id.label("id")).where(SceneMarker.id.in_(ids[start:start + 200])).cte(
            "restored_marker_lineage", recursive=True,
        )
        adjacent = select(case(
            (SceneMarker.id == lineage.c.id, SceneMarker.origin_marker_id), else_=SceneMarker.id,
        ).label("id")).join(
            lineage, or_(SceneMarker.id == lineage.c.id, SceneMarker.origin_marker_id == lineage.c.id),
        ).where(SceneMarker.origin_marker_id.is_not(None))
        lineage = lineage.union(adjacent)
        live = await db.scalar(select(SceneMarker.id).join(lineage, SceneMarker.id == lineage.c.id).where(
            SceneMarker.status != "deleted",
        ).limit(1))
        if live is not None:
            raise ConflictError("标记已有仍存活的迁移身份，不能重复恢复", extra={"code": "marker_identity_conflict"})


async def _check_restored_section_lineage(db: AsyncSession, rows: list[SceneSection], keys: list[UUID]) -> None:
    """仅拒绝与墓碑存在明确来源血缘的活动区块，不把无关同 UUID 当成同一身份。"""
    wanted = set(keys)
    ids = [row.id for row in rows if UUID(row.logical_key) in wanted]
    for start in range(0, len(ids), 200):
        lineage = select(SceneSection.id.label("id")).where(SceneSection.id.in_(ids[start:start + 200])).cte(
            "restored_section_lineage", recursive=True,
        )
        adjacent = select(case(
            (SceneSection.id == lineage.c.id, SceneSection.origin_section_id), else_=SceneSection.id,
        ).label("id")).join(
            lineage, or_(SceneSection.id == lineage.c.id, SceneSection.origin_section_id == lineage.c.id),
        ).where(SceneSection.origin_section_id.is_not(None))
        lineage = lineage.union(adjacent)
        live = await db.scalar(select(SceneSection.id).join(lineage, SceneSection.id == lineage.c.id).where(
            SceneSection.active.is_(True),
        ).limit(1))
        if live is not None:
            raise ConflictError("区块已有仍存活的迁移身份，不能重复恢复", extra={"code": "section_identity_conflict"})


async def save_document(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, scene_id: int, body: SceneDocumentSave,
    *, converted_section_key: UUID | None = None,
) -> SceneDocumentOut:
    async with AuditOperation(db, user_id, "chapter.update", "chapter", chapter_id, project_id=project_id) as audit:
        # 正文中的已启用区块也是隐私策略：固定先拿权限写屏障、发送写门禁，再锁项目和章节。
        await setting_ai_visibility.lock_author_visibility_write(db, user_id, project_id)
        chapter = await scene_service._write_chapter(db, user_id, project_id, chapter_id)
        audit.target(chapter)
        audit.track(chapter, ["content"])
        await scene_service.check_revision(db, chapter, body.expected_content_revision)
        scene = await scene_service.require_scene(db, chapter, scene_id)
        current = (await capture_documents(db, [scene]))[0][scene_id]
        section_rows = list(await db.scalars(select(SceneSection).where(SceneSection.scene_id == scene_id)))
        marker_rows = list(await db.scalars(select(SceneMarker).where(SceneMarker.scene_id == scene_id)))
        _check_intents(body, current, section_rows, marker_rows, converted_section_key=converted_section_key)
        if body.restore_section_keys:
            await _check_restored_section_lineage(db, section_rows, body.restore_section_keys)
        if body.restore_marker_keys:
            await _check_restored_marker_lineage(db, marker_rows, body.restore_marker_keys)
        if current == body.document:
            return SceneDocumentOut(content_revision=chapter.content_revision,
                                    planning_revision=await scene_service._planning_revision(db, project_id),
                                    document=current)
        await version_service.ensure_baseline(db, chapter, force=True)
        await _write_document_rows(db, project_id, scene, body, current, section_rows, marker_rows)
        await scene_service.refresh_projection(db, chapter, expected_revision=body.expected_content_revision)
        version = await version_service.record_manual(db, chapter)
        audit.version(version, project_id)
        audit.summary.changed_fields = ["content"]
        await audit.commit(flush_fn=version_service.flush_versioned)
        output = (await capture_documents(db, [scene]))[0][scene_id]
        return SceneDocumentOut(content_revision=chapter.content_revision,
                                planning_revision=await scene_service._planning_revision(db, project_id),
                                document=output)
