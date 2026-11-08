"""显式处理失效／退场标记，保留按场景绑定的历史身份。"""

from hmac import compare_digest
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models import Chapter, Scene, SceneMarker, SceneSection
from app.schemas.rich_document import MarkerDeleteOut, MarkerRelink, MarkerRelinkOut, require_range
from app.services import rich_document_service, scene_service, setting_ai_visibility, version_service
from app.services.audit_writer import AuditOperation
from app.services.rich_document_store import retire_marker


async def _source_chapter_id(db: AsyncSession, project_id: int, scene_id: int) -> int:
    value = await db.scalar(select(Scene.chapter_id).join(Chapter, Scene.chapter_id == Chapter.id).where(
        Scene.id == scene_id, Chapter.project_id == project_id,
    ))
    if value is None:
        raise NotFoundError("场景不存在")
    return value


async def _source_marker(db: AsyncSession, scene: Scene, key: UUID, *, allow_active: bool = False) -> SceneMarker:
    marker = await db.scalar(select(SceneMarker).where(
        SceneMarker.scene_id == scene.id, SceneMarker.logical_key == str(key), SceneMarker.status != "deleted",
    ))
    if marker is None:
        raise NotFoundError("标记不存在")
    if not allow_active and scene.active and marker.status != "stale":
        raise ConflictError("活动标记请在文档中编辑", extra={"code": "marker_not_stale"})
    return marker


async def relink(
    db: AsyncSession, user_id: int, project_id: int, scene_id: int, key: UUID, body: MarkerRelink,
) -> MarkerRelinkOut:
    async with AuditOperation(db, user_id, "chapter.update", "chapter", project_id=project_id) as audit:
        await setting_ai_visibility.lock_author_visibility_write(db, user_id, project_id)
        source_id = await _source_chapter_id(db, project_id, scene_id)
        chapters = {chapter_id: await scene_service._write_chapter(db, user_id, project_id, chapter_id)
                    for chapter_id in sorted({source_id, body.target_chapter_id})}
        source, target = chapters[source_id], chapters[body.target_chapter_id]
        audit.target(source)
        await scene_service.check_revision(db, source, body.expected_source_revision)
        await scene_service.check_revision(db, target, body.expected_target_revision)
        source_scene = await scene_service.require_scene(db, source, scene_id, active=False)
        target_scene = await scene_service.require_scene(db, target, body.target_scene_id)
        marker = await _source_marker(db, source_scene, key)
        section = None
        text = target_scene.content
        if body.target_section_key is not None:
            section = await db.scalar(select(SceneSection).where(
                SceneSection.scene_id == target_scene.id, SceneSection.logical_key == str(body.target_section_key),
                SceneSection.active.is_(True),
            ))
            if section is None:
                raise NotFoundError("目标区块不存在")
            text = section.content
        try:
            require_range(text, body.start, body.end)
        except ValueError as exc:
            raise scene_service.SceneParameterError(str(exc)) from exc
        if not compare_digest(scene_service.content_fingerprint(text[body.start:body.end]), body.selected_hash):
            raise ConflictError("选中文本已变化，请重新选择", extra={"code": "marker_selection_changed"})
        if source_scene.id != target_scene.id:
            duplicate = await db.scalar(select(SceneMarker.id).where(
                SceneMarker.scene_id == target_scene.id, SceneMarker.logical_key == str(key),
            ))
            if duplicate is not None:
                raise ConflictError("目标场景已有同一标记身份，请选择其他场景", extra={
                    "code": "marker_identity_conflict",
                })
        for chapter in chapters.values():
            await version_service.ensure_baseline(db, chapter, force=True)
        if source_scene.id != target_scene.id:
            # 历史引用的是 (scene_id, logical_key)：保留旧行作墓碑，不移动其主键。
            retire_marker(marker)
            marker = SceneMarker(scene_id=target_scene.id, logical_key=str(key), color=marker.color, status="active",
                                 origin_marker_id=marker.id)
            db.add(marker)
        marker.section_id = section.id if section else None
        marker.status, marker.start_offset, marker.end_offset = "active", body.start, body.end
        marker.last_start, marker.excerpt, marker.excerpt_truncated = None, "", False
        marker.draft_excerpt, marker.draft_excerpt_truncated, marker.draft_last_start = None, False, None
        await db.flush()
        await rich_document_service.check_stored_bounds(db, target_scene.id)
        for chapter in chapters.values():
            await scene_service.refresh_projection(db, chapter, expected_revision=chapter.content_revision)
            audit.version(await version_service.record(db, chapter, "manual", note="重新关联标记"), project_id)
        audit.summary.changed_fields = ["content"]
        await audit.commit(flush_fn=version_service.flush_versioned)
        return MarkerRelinkOut(source_chapter_id=source.id, source_revision=source.content_revision,
                               target_chapter_id=target.id, target_revision=target.content_revision,
                               target_scene_id=target_scene.id, key=key)


async def delete_stale(
    db: AsyncSession, user_id: int, project_id: int, scene_id: int, key: UUID, expected_revision: int,
) -> MarkerDeleteOut:
    async with AuditOperation(db, user_id, "chapter.update", "chapter", project_id=project_id) as audit:
        await setting_ai_visibility.lock_author_visibility_write(db, user_id, project_id)
        chapter_id = await _source_chapter_id(db, project_id, scene_id)
        chapter = await scene_service._write_chapter(db, user_id, project_id, chapter_id)
        audit.target(chapter)
        await scene_service.check_revision(db, chapter, expected_revision)
        scene = await scene_service.require_scene(db, chapter, scene_id, active=False)
        marker = await _source_marker(db, scene, key, allow_active=True)
        await version_service.ensure_baseline(db, chapter, force=True)
        retire_marker(marker)
        await db.flush()
        await scene_service.refresh_projection(db, chapter, expected_revision=expected_revision)
        audit.version(await version_service.record(db, chapter, "manual", note="删除标记"), project_id)
        audit.summary.changed_fields = ["content"]
        await audit.commit(flush_fn=version_service.flush_versioned)
        return MarkerDeleteOut(chapter_id=chapter.id, content_revision=chapter.content_revision)
