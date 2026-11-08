"""作者已保存的全书标记目录；不索引区块或正文以外的派生内容。"""

import hashlib
import json
from collections import defaultdict

from pydantic import ValidationError
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError
from app.models import Chapter, Scene, SceneMarker, SceneSection
from app.schemas.rich_document import (
    DocumentMarker,
    MarkerTimelineItem,
    MarkerTimelinePage,
    marker_excerpt,
    require_range,
)
from app.services import scene_service
from app.services.project_service import get_manageable_project


async def _project_marker_stamp(db: AsyncSession, project_id: int) -> dict[int, tuple[int, str, int]]:
    rows = await db.execute(select(Chapter.id, Chapter.chapter_no, Chapter.title, Chapter.content_revision)
                            .where(Chapter.project_id == project_id))
    return {row.id: (row.chapter_no, row.title, row.content_revision) for row in rows}


async def list_markers(
    db: AsyncSession, user_id: int, project_id: int, *, offset: int = 0, limit: int = 50,
    expected_snapshot: str | None = None,
) -> MarkerTimelinePage:
    with db.no_autoflush:
        await get_manageable_project(db, user_id, project_id)
        planning_revision = await scene_service._planning_revision(db, project_id)
        chapter_stamp = await _project_marker_stamp(db, project_id)
        serialized = json.dumps([project_id, planning_revision, sorted(chapter_stamp.items())], separators=(",", ":"))
        snapshot = hashlib.sha256(serialized.encode("ascii")).hexdigest()
        if offset and expected_snapshot is None:
            raise BadRequestError("加载后续页时必须携带第一页的目录快照", extra={
                "code": "marker_timeline_snapshot_required",
            })
        if expected_snapshot is not None and expected_snapshot != snapshot:
            raise ConflictError("标记目录已变化，请从第一页重新加载", extra={"code": "marker_timeline_changed"})
        condition = (Chapter.project_id == project_id, SceneMarker.status != "deleted")
        base = (select(SceneMarker, Scene.title.label("scene_title"), Scene.active.label("scene_active"),
                       Chapter.id.label("chapter_id"),
                       Chapter.chapter_no, Chapter.title.label("chapter_title"), Chapter.content_revision,
                       SceneSection.logical_key.label("section_key"), SceneSection.title.label("section_title"),
                       SceneSection.scene_id.label("section_scene_id"), SceneSection.active.label("section_active"))
                .join(Scene, Scene.id == SceneMarker.scene_id).join(Chapter, Chapter.id == Scene.chapter_id)
                .outerjoin(SceneSection, SceneSection.id == SceneMarker.section_id).where(*condition))
        position = func.coalesce(SceneSection.anchor, SceneMarker.start_offset, SceneMarker.last_start, 0)
        # 同锚区块位于后续正文字符之前，不能按标记类型把正文提前。
        target_order = case((SceneMarker.section_id.is_(None), 1), else_=0)
        within_target = func.coalesce(SceneMarker.start_offset, SceneMarker.last_start, 0)
        rows = (await db.execute(base.order_by(
            Chapter.chapter_no, Chapter.id, Scene.sort, Scene.id, position, target_order,
            SceneSection.sort, within_target, SceneMarker.id,
        ).offset(offset).limit(limit))).mappings().all()
        total = int(await db.scalar(select(func.count()).select_from(SceneMarker)
                                    .join(Scene, Scene.id == SceneMarker.scene_id)
                                    .join(Chapter, Chapter.id == Scene.chapter_id).where(*condition)) or 0)
        prose_refs: dict[int, list[SceneMarker]] = defaultdict(list)
        section_refs: dict[int, list[SceneMarker]] = defaultdict(list)
        for row in rows:
            marker = row.SceneMarker
            if marker.status == "active":
                if marker.section_id is None:
                    prose_refs[marker.scene_id].append(marker)
                else:
                    section_refs[marker.section_id].append(marker)
        previews: dict[int, tuple[str, bool]] = {}

        async def read_previews(model, refs: dict[int, list[SceneMarker]]) -> None:
            if not refs:
                return
            # 每次只取一个源正文，保留最多每个标记一段有界预览；避免同页很多长场景全部驻留。
            result = await db.stream(select(model.id, model.content).where(model.id.in_(refs))
                                     .execution_options(yield_per=1))
            try:
                async for source_id, source in result:
                    for marker in refs[source_id]:
                        try:
                            if marker.start_offset is None or marker.end_offset is None:
                                raise ValueError("有效标记缺少正文或范围")
                            require_range(source, marker.start_offset, marker.end_offset)
                            previews[marker.id] = marker_excerpt(source, marker.start_offset, marker.end_offset)
                        except (TypeError, ValueError) as exc:
                            raise ConflictError("正文标记范围无效", extra={"code": "rich_document_invalid"}) from exc
            finally:
                await result.close()

        await read_previews(Scene, prose_refs)
        await read_previews(SceneSection, section_refs)
        items = []
        for row in rows:
            marker = row.SceneMarker
            if marker.section_id is not None and (
                row.section_scene_id != marker.scene_id or marker.status == "active" and not row.section_active
            ):
                raise ConflictError("正文标记的区块身份无效", extra={"code": "rich_document_invalid"})
            try:
                DocumentMarker.model_validate({
                    "key": marker.logical_key, "section_key": row.section_key, "status": marker.status,
                    "color": marker.color, "start": marker.start_offset, "end": marker.end_offset,
                    "last_start": marker.last_start, "excerpt": marker.excerpt,
                    "excerpt_truncated": marker.excerpt_truncated,
                    "draft_excerpt": marker.draft_excerpt, "draft_excerpt_truncated": marker.draft_excerpt_truncated,
                    "draft_last_start": marker.draft_last_start,
                })
            except ValidationError as exc:
                raise ConflictError("正文标记结构无效", extra={"code": "rich_document_invalid"}) from exc
            if marker.status == "active":
                preview = previews.get(marker.id)
                if preview is None:
                    raise ConflictError("正文标记缺少源文本", extra={"code": "rich_document_invalid"})
                excerpt, truncated = preview
            else:
                excerpt, truncated = marker.excerpt, marker.excerpt_truncated
            status = marker.status if row.scene_active else "stale"
            start = marker.start_offset if row.scene_active else None
            end = marker.end_offset if row.scene_active else None
            last_start = marker.last_start if row.scene_active or marker.status == "stale" else marker.start_offset
            items.append(MarkerTimelineItem(
                key=marker.logical_key, chapter_id=row.chapter_id, chapter_no=row.chapter_no,
                chapter_title=row.chapter_title, scene_id=marker.scene_id, scene_title=row.scene_title,
                scene_active=row.scene_active, section_key=row.section_key, section_title=row.section_title,
                status=status, color=marker.color, start=start, end=end, last_start=last_start, excerpt=excerpt,
                excerpt_truncated=truncated, draft_excerpt=marker.draft_excerpt,
                draft_excerpt_truncated=marker.draft_excerpt_truncated, draft_last_start=marker.draft_last_start,
                content_revision=row.content_revision,
            ))
        if (chapter_stamp != await _project_marker_stamp(db, project_id)
                or planning_revision != await scene_service._planning_revision(db, project_id)):
            raise ConflictError("标记目录读取期间已变化，请重新加载", extra={"code": "scene_context_changed"})
        return MarkerTimelinePage(items=items, total=total, offset=offset, limit=limit,
                                  planning_revision=planning_revision, snapshot=snapshot)
