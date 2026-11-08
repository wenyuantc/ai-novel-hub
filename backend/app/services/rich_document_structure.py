"""可证明无损的场景拆合；不推测跨分界结构和未知标记归属。"""

from copy import deepcopy

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Scene, SceneDocumentMeta, SceneMarker, SceneSection
from app.schemas.rich_document import SceneDocument, TextFormatting
from app.services.rich_document_service import check_stored_bounds
from app.services.rich_document_store import capture_documents, retire_marker


def _unmapped() -> ConflictError:
    return ConflictError("场景结构无法可靠映射，请先处理跨界资料", extra={"code": "rich_document_unmapped"})


def _document(data: dict) -> SceneDocument:
    try:
        return SceneDocument.model_validate(data)
    except ValidationError as exc:
        raise _unmapped() from exc


def _side(start: int, end: int, split: int) -> bool:
    if end < split:
        return False
    if start > split:
        return True
    raise _unmapped()


def split_document(original: SceneDocument, offset: int) -> tuple[SceneDocument, SceneDocument]:
    """不碰分界的格式、区块和活动标记按码点移动；来源失效的标记不猜归属。"""
    source = original.model_dump(mode="json")
    left, right = deepcopy(source), deepcopy(source)
    left["content"], right["content"] = original.content[:offset], original.content[offset:]
    left["sections"], right["sections"], right["retired_section_keys"] = [], [], []
    moved_sections: set[str] = set()
    for section in source["sections"]:
        if section["anchor"] == offset:
            raise _unmapped()
        if section["anchor"] > offset:
            moved_sections.add(section["key"])
            right["sections"].append({**section, "anchor": section["anchor"] - offset})
            left["retired_section_keys"].append(section["key"])
        else:
            left["sections"].append(section)
    left["markers"], right["markers"] = [], []
    left_fmt = {"version": 1, "boundaries": [], "inline": [], "blocks": []}
    right_fmt = {"version": 1, "boundaries": [], "inline": [], "blocks": []}
    for boundary in source["formatting"]["boundaries"]:
        if boundary == offset:
            raise _unmapped()
        (left_fmt if boundary < offset else right_fmt)["boundaries"].append(
            boundary if boundary < offset else boundary - offset,
        )
    for kind in ("inline", "blocks"):
        for item in source["formatting"][kind]:
            right_side = _side(item["start"], item["end"], offset)
            if right_side:
                item = {**item, "start": item["start"] - offset, "end": item["end"] - offset}
            (right_fmt if right_side else left_fmt)[kind].append(item)
    for marker in source["markers"]:
        if marker["status"] == "stale":
            raise _unmapped()
        if marker["section_key"] is not None:
            (right if marker["section_key"] in moved_sections else left)["markers"].append(marker)
            continue
        right_side = _side(marker["start"], marker["end"], offset)
        if right_side:
            marker = {**marker, "start": marker["start"] - offset, "end": marker["end"] - offset}
        (right if right_side else left)["markers"].append(marker)
    left["formatting"], right["formatting"] = left_fmt, right_fmt
    return _document(left), _document(right)


async def _set_format(db: AsyncSession, scene_id: int, formatting: TextFormatting) -> None:
    meta = await db.get(SceneDocumentMeta, scene_id)
    if formatting.boundaries or formatting.inline or formatting.blocks:
        if meta is None:
            meta = SceneDocumentMeta(scene_id=scene_id)
            db.add(meta)
        meta.formatting = formatting.model_dump(mode="json")
    elif meta is not None:
        await db.delete(meta)


async def _marker_rows(db: AsyncSession, scene_id: int) -> dict[str, SceneMarker]:
    rows = await db.scalars(select(SceneMarker).where(SceneMarker.scene_id == scene_id))
    return {row.logical_key: row for row in rows}


async def _section_rows(db: AsyncSession, scene_id: int) -> dict[str, SceneSection]:
    rows = await db.scalars(select(SceneSection).where(SceneSection.scene_id == scene_id))
    return {row.logical_key: row for row in rows}


async def apply_split(
    db: AsyncSession, left: Scene, right: Scene, left_doc: SceneDocument, right_doc: SceneDocument,
) -> None:
    await _set_format(db, left.id, left_doc.formatting)
    await _set_format(db, right.id, right_doc.formatting)
    old_sections = await _section_rows(db, left.id)
    new_sections: dict[str, SceneSection] = {}
    for item in right_doc.sections:
        source = old_sections[str(item.key)]
        source.active = False
        target = SceneSection(scene_id=right.id, logical_key=source.logical_key, origin_section_id=source.id,
                              anchor=item.anchor, sort=item.order, title=item.title, content=item.content,
                              formatting=item.formatting.model_dump(mode="json"), include_in_ai=item.include_in_ai)
        db.add(target)
        new_sections[str(item.key)] = target
    await db.flush()
    rows = await _marker_rows(db, left.id)
    for item in right_doc.markers:
        source = rows[str(item.key)]
        retire_marker(source)
        db.add(SceneMarker(scene_id=right.id, logical_key=str(item.key), origin_marker_id=source.id,
                           section_id=new_sections[str(item.section_key)].id if item.section_key else None,
                           status=item.status, color=item.color, start_offset=item.start, end_offset=item.end,
                           last_start=item.last_start, excerpt=item.excerpt, excerpt_truncated=item.excerpt_truncated,
                           draft_excerpt=item.draft_excerpt, draft_excerpt_truncated=item.draft_excerpt_truncated,
                           draft_last_start=item.draft_last_start))
    await db.flush()
    await check_stored_bounds(db, left.id)
    await check_stored_bounds(db, right.id)


async def merge_document(db: AsyncSession, left: Scene, right: Scene) -> SceneDocument:
    documents, _ = await capture_documents(db, [left, right])
    ldoc, rdoc = documents[left.id], documents[right.id]
    if any(marker.status == "stale" for marker in rdoc.markers):
        raise _unmapped()
    existing_sections, right_sections = await _section_rows(db, left.id), await _section_rows(db, right.id)
    for section in rdoc.sections:
        target = existing_sections.get(str(section.key))
        source = right_sections[str(section.key)]
        if target is not None:
            try:
                same_format = TextFormatting.model_validate(target.formatting) == TextFormatting.model_validate(
                    source.formatting,
                )
            except ValidationError as exc:
                raise _unmapped() from exc
            if (target.active or source.origin_section_id != target.id or target.content != source.content
                    or target.title != source.title or target.include_in_ai != source.include_in_ai or not same_format):
                raise _unmapped()
    existing, right_rows = await _marker_rows(db, left.id), await _marker_rows(db, right.id)
    for marker in rdoc.markers:
        target = existing.get(str(marker.key))
        source = right_rows[str(marker.key)]
        # 重用墓碑须有拆分时写入的实际来源身份；UUID 或文字相同不构成证明。
        if target is not None and (target.status != "deleted" or source.origin_marker_id != target.id):
            raise _unmapped()
    result = ldoc.model_dump(mode="json")
    right_data = rdoc.model_dump(mode="json")
    offset = len(left.content + right.separator_before)
    result["content"] = left.content + right.separator_before + right.content
    fmt, rfmt = result["formatting"], right_data["formatting"]
    fmt["boundaries"].extend(offset + item for item in rfmt["boundaries"])
    for kind in ("inline", "blocks"):
        fmt[kind].extend({**item, "start": item["start"] + offset, "end": item["end"] + offset}
                         for item in rfmt[kind])
    by_anchor: dict[int, int] = {}
    for section in result["sections"]:
        by_anchor[section["anchor"]] = max(by_anchor.get(section["anchor"], -1), section["order"])
    left_anchors = set(by_anchor)
    for section in sorted(right_data["sections"], key=lambda item: (item["anchor"], item["order"])):
        anchor = section["anchor"] + offset
        order = by_anchor.get(anchor, -1) + 1 if anchor in left_anchors else section["order"]
        if order > 2_147_483_647:
            raise _unmapped()
        by_anchor[anchor] = order
        result["sections"].append({**section, "anchor": anchor, "order": order})
        if section["key"] in result["retired_section_keys"]:
            result["retired_section_keys"].remove(section["key"])
    for marker in right_data["markers"]:
        if marker["section_key"] is None:
            marker = {**marker, "start": marker["start"] + offset, "end": marker["end"] + offset}
        result["markers"].append(marker)
    return _document(result)


async def apply_merge(db: AsyncSession, left: Scene, right: Scene, document: SceneDocument) -> None:
    await _set_format(db, left.id, document.formatting)
    left_sections, right_sections = await _section_rows(db, left.id), await _section_rows(db, right.id)
    planned_sections = {str(section.key): section for section in document.sections}
    for source in right_sections.values():
        if not source.active:
            continue
        item = planned_sections.get(source.logical_key)
        if item is None:
            raise _unmapped()
        target = left_sections.get(source.logical_key)
        if target is None:
            target = SceneSection(scene_id=left.id, logical_key=source.logical_key, origin_section_id=source.id,
                                  anchor=item.anchor, sort=item.order, title=item.title, content=item.content)
            db.add(target)
            left_sections[source.logical_key] = target
        elif target.active or source.origin_section_id != target.id:
            raise _unmapped()
        target.anchor, target.sort, target.title, target.content = item.anchor, item.order, item.title, item.content
        target.formatting, target.include_in_ai, target.active = (
            item.formatting.model_dump(mode="json"), item.include_in_ai, True,
        )
        source.active = False
    await db.flush()
    left_rows, right_rows = await _marker_rows(db, left.id), await _marker_rows(db, right.id)
    markers = {str(marker.key): marker for marker in document.markers}
    for source in right_rows.values():
        if source.status != "active":
            continue
        item = markers.get(source.logical_key)
        if item is None:
            raise _unmapped()
        target = left_rows.get(source.logical_key)
        if target is None:
            target = SceneMarker(scene_id=left.id, logical_key=source.logical_key,
                                 origin_marker_id=source.id, status="active", color=source.color)
            db.add(target)
        elif target.status != "deleted" or source.origin_marker_id != target.id:
            raise _unmapped()
        target.status, target.color = item.status, item.color
        target.section_id = left_sections[str(item.section_key)].id if item.section_key else None
        target.start_offset, target.end_offset = item.start, item.end
        target.last_start, target.excerpt, target.excerpt_truncated = (
            item.last_start, item.excerpt, item.excerpt_truncated,
        )
        target.draft_excerpt, target.draft_excerpt_truncated, target.draft_last_start = (
            item.draft_excerpt, item.draft_excerpt_truncated, item.draft_last_start,
        )
        retire_marker(source)
    await db.flush()
    await check_stored_bounds(db, left.id)
