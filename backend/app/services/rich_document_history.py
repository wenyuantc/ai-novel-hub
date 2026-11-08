"""结构历史的预检与恢复；不为缺失身份造记录。"""

from dataclasses import dataclass
from itertools import batched
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Scene, SceneDocumentMeta, SceneMarker, SceneSection
from app.schemas.rich_document import SceneDocument, TextFormatting, validate_chapter_document_boundaries
from app.schemas.scene import SceneSnapshot, SceneSnapshotV2, SnapshotScene
from app.services.rich_document_core import validate_storage_bounds
from app.services.rich_document_store import capture_documents, retire_marker


@dataclass
class DocumentRestorePlan:
    documents: dict[int, SceneDocument]
    requires_confirmation: bool
    blockers: list[dict]
    changes: list[dict]
    visibility_changed: bool
    visibility_tightened: bool


def _has_material(document: SceneDocument) -> bool:
    formatting = document.formatting
    return bool(document.sections or document.markers or formatting.boundaries or formatting.inline
                or formatting.blocks)


def _visible(
    scene: Scene | SnapshotScene, document: SceneDocument, *, sort: int, separator: str,
) -> dict[tuple[int, UUID], tuple]:
    return {(scene.id, item.key): (sort, separator, item.anchor, item.order, item.title, item.content)
            for item in document.sections if item.include_in_ai}


async def _cross_chapter_lineage_blockers(
    db: AsyncSession, targets: list[Scene], desired: dict[int, SceneDocument],
) -> list[dict]:
    """不让恢复新制造跨章双活；既有双活及退场场景资料保持原样。"""
    if not targets:
        return []
    target_by_id = {scene.id: scene for scene in targets}
    target_ids = list(target_by_id)
    blockers = []
    for model, origin, live_condition, kind, label in (
        (SceneSection, SceneSection.origin_section_id, SceneSection.active.is_(True), "sections", "区块"),
        (SceneMarker, SceneMarker.origin_marker_id, SceneMarker.status != "deleted", "markers", "标记"),
    ):
        wanted = {scene_id: {str(item.key) for item in getattr(document, kind)}
                  for scene_id, document in desired.items()}
        candidates = {}
        for batch in batched(target_ids, 200):
            for row in await db.scalars(select(model).where(model.scene_id.in_(batch))):
                if row.logical_key not in wanted[row.scene_id]:
                    continue
                was_live = row.active if model is SceneSection else row.status != "deleted"
                if not target_by_id[row.scene_id].active or not was_live:
                    candidates[row.id] = row.scene_id
        for batch in batched(list(candidates), 200):
            lineage = select(model.id.label("root"), model.id.label("id")).where(model.id.in_(batch)).cte(
                f"restore_{kind}_lineage", recursive=True,
            )
            adjacent = select(lineage.c.root, case(
                (model.id == lineage.c.id, origin), else_=model.id,
            ).label("id")).join(
                lineage, or_(model.id == lineage.c.id, origin == lineage.c.id),
            ).where(origin.is_not(None))
            lineage = lineage.union(adjacent)
            roots = await db.scalars(select(lineage.c.root).select_from(model).join(
                lineage, model.id == lineage.c.id,
            ).join(Scene, Scene.id == model.scene_id).where(
                live_condition, Scene.active.is_(True), Scene.chapter_id != targets[0].chapter_id,
            ).distinct())
            for root in roots:
                blockers.append({"kind": "rich_document_history", "id": root, "scene_id": candidates[root],
                                 "label": f"历史中的{label}已迁移到其他活动章节，请先处理该身份再恢复"})
    return blockers


async def plan_restore(
    db: AsyncSession, current: list[Scene], targets: list[Scene], snapshot: SceneSnapshot | SceneSnapshotV2,
) -> DocumentRestorePlan:
    all_scenes = {scene.id: scene for scene in (*current, *targets)}
    docs, _has_documents = await capture_documents(db, list(all_scenes.values()))
    current_ids = {scene.id for scene in current}
    items = {item.id: item for item in snapshot.scenes}
    desired = {
        item.id: SceneDocument.model_validate(
            {"content": item.content, **item.document} if isinstance(snapshot, SceneSnapshotV2)
            else {"content": item.content}
        ) for item in snapshot.scenes
    }
    try:
        validate_chapter_document_boundaries([
            (item.separator_before, desired[item.id]) for item in snapshot.scenes
        ])
    except ValueError as exc:
        raise ConflictError("场景历史的文档元数据落在章节 CRLF 中间，不能恢复", extra={
            "code": "rich_document_invalid",
        }) from exc
    changes, blockers = [], []
    confirm = False
    for scene_id in sorted(current_ids | set(items)):
        previous = docs[scene_id]
        target = desired.get(scene_id)
        if scene_id not in current_ids:
            if previous != target and _has_material(previous):
                blockers.append({"kind": "rich_document_history", "id": scene_id, "scene_id": scene_id,
                                 "label": "退场场景的当前资料不在活动场景基线，无法无损覆盖"})
            continue
        if target is None or previous != target:
            if _has_material(previous):
                confirm = True
            if _has_material(previous) or (target is not None and _has_material(target)):
                changes.append({"scene_id": scene_id, "will_retire": target is None,
                                "before_sections": len(previous.sections), "before_markers": len(previous.markers),
                                "after_sections": len(target.sections) if target else 0,
                                "after_markers": len(target.markers) if target else 0,
                                "sections_changed": previous.sections != (target.sections if target else []),
                                "markers_changed": previous.markers != (target.markers if target else []),
                                "format_changed": previous.formatting != (
                                    target.formatting if target else TextFormatting()),
                                "before_format_ranges": (len(previous.formatting.inline)
                                                         + len(previous.formatting.blocks)
                                                         + len(previous.formatting.boundaries)),
                                "after_format_ranges": (len(target.formatting.inline) + len(target.formatting.blocks)
                                                        + len(target.formatting.boundaries)) if target else 0})
    desired_sections = {scene_id: {item.key: item for item in document.sections}
                        for scene_id, document in desired.items()}
    for batch in batched(list(desired), 200):
        rows = await db.scalars(select(SceneSection).where(
            SceneSection.scene_id.in_(batch), SceneSection.active.is_(False),
        ))
        for row in rows:
            try:
                key = UUID(row.logical_key)
                target = desired_sections[row.scene_id].get(key)
                if target is None:
                    continue
                formatting = TextFormatting.model_validate(row.formatting)
                formatting.validate_for_text(row.content)
                stored_material = bool(row.content or formatting.boundaries or formatting.inline or formatting.blocks)
                if not stored_material or (
                    row.content == target.content and row.title == target.title and formatting == target.formatting
                ):
                    continue
            except (ValueError, TypeError, AttributeError, ValidationError):
                pass
            blockers.append({"kind": "rich_document_history", "id": row.id, "scene_id": row.scene_id,
                             "label": "退场区块仍有未归档资料，不能直接覆盖"})
    before: dict[tuple[int, UUID], tuple] = {}
    blockers.extend(await _cross_chapter_lineage_blockers(db, targets, desired))
    for scene in current:
        before.update(_visible(scene, docs[scene.id], sort=scene.sort, separator=scene.separator_before))
    after: dict[tuple[int, UUID], tuple] = {}
    for item in snapshot.scenes:
        after.update(_visible(item, desired[item.id], sort=item.sort, separator=item.separator_before))
    return DocumentRestorePlan(
        documents=desired, requires_confirmation=confirm, blockers=blockers, changes=changes,
        visibility_changed=before != after,
        visibility_tightened=any(after.get(key) != value for key, value in before.items()),
    )


async def apply_restored_documents(
    db: AsyncSession, scenes: list[Scene], documents: dict[int, SceneDocument],
) -> None:
    """同事务恢复已有身份；先预检全章，再释放顺序并写入，调用方负责 CAS 和历史。"""
    sections_by_scene: dict[int, dict[UUID, SceneSection]] = {}
    active_by_scene: dict[int, set[UUID]] = {}
    markers_by_scene: dict[int, dict[UUID, SceneMarker]] = {}
    meta_by_scene: dict[int, SceneDocumentMeta | None] = {}
    for scene in scenes:
        sections = list(await db.scalars(select(SceneSection).where(SceneSection.scene_id == scene.id)))
        markers = list(await db.scalars(select(SceneMarker).where(SceneMarker.scene_id == scene.id)))
        try:
            section_map = {UUID(row.logical_key): row for row in sections}
            marker_map = {UUID(row.logical_key): row for row in markers}
        except (ValueError, TypeError, AttributeError) as exc:
            raise ConflictError("结构历史的持久身份无效", extra={"code": "rich_document_invalid"}) from exc
        document = documents[scene.id]
        section_keys = {item.key for item in document.sections} | set(document.retired_section_keys)
        marker_keys = {item.key for item in document.markers}
        if (len(section_map) != len(sections) or len(marker_map) != len(markers)
                or not section_keys <= set(section_map) or not marker_keys <= set(marker_map)):
            raise ConflictError("结构历史身份已缺失或不属于本场景，无法恢复", extra={
                "code": "rich_document_identity_missing",
            })
        sections_by_scene[scene.id], markers_by_scene[scene.id] = section_map, marker_map
        active_by_scene[scene.id] = {key for key, row in section_map.items() if row.active}
        meta_by_scene[scene.id] = await db.get(SceneDocumentMeta, scene.id)
    for section_map in sections_by_scene.values():
        for row in section_map.values():
            row.active = False
    await db.flush()
    for scene in scenes:
        document = documents[scene.id]
        sections, markers = sections_by_scene[scene.id], markers_by_scene[scene.id]
        # 仅清空本次从活动态退场的区块；先前退场、未纳入基线的数据不能被历史恢复抹除。
        for key in active_by_scene[scene.id]:
            row = sections[key]
            row.content, row.formatting, row.include_in_ai = "", {}, False
        for item in document.sections:
            row = sections[item.key]
            row.anchor, row.sort, row.title, row.content = item.anchor, item.order, item.title, item.content
            row.formatting = item.formatting.model_dump(mode="json")
            row.include_in_ai, row.active = item.include_in_ai, True
        for row in markers.values():
            if row.status != "deleted":
                retire_marker(row)
        for item in document.markers:
            row = markers[item.key]
            row.section_id = sections[item.section_key].id if item.section_key is not None else None
            row.status, row.color = item.status, item.color
            row.start_offset, row.end_offset, row.last_start, row.excerpt, row.excerpt_truncated = (
                item.start, item.end, item.last_start, item.excerpt, item.excerpt_truncated,
            )
            row.draft_excerpt, row.draft_excerpt_truncated, row.draft_last_start = (
                item.draft_excerpt, item.draft_excerpt_truncated, item.draft_last_start,
            )
        formatting = document.formatting
        meta = meta_by_scene[scene.id]
        has_formatting = bool(formatting.boundaries or formatting.inline or formatting.blocks)
        if has_formatting:
            if meta is None:
                meta = SceneDocumentMeta(scene_id=scene.id)
                db.add(meta)
            meta.formatting = formatting.model_dump(mode="json")
        elif meta is not None:
            await db.delete(meta)
    await db.flush()
    for scene in scenes:
        sections, markers = sections_by_scene[scene.id], markers_by_scene[scene.id]
        section_fields = ("id", "logical_key", "origin_section_id", "anchor", "sort", "title", "content",
                          "formatting", "include_in_ai", "active")
        marker_fields = ("logical_key", "origin_marker_id", "section_id", "status", "color", "start_offset",
                         "end_offset", "last_start", "excerpt", "excerpt_truncated", "draft_excerpt",
                         "draft_excerpt_truncated", "draft_last_start")
        formatting = documents[scene.id].formatting
        has_formatting = bool(formatting.boundaries or formatting.inline or formatting.blocks)
        try:
            validate_storage_bounds(
                formatting.model_dump(mode="json") if has_formatting else {},
                [{field: getattr(row, field) for field in section_fields} for row in sections.values()],
                [{field: getattr(row, field) for field in marker_fields} for row in markers.values()],
            )
        except ValueError as exc:
            raise ConflictError("历史恢复后富文档会超过存储限制", extra={"code": "rich_document_limit"}) from exc
