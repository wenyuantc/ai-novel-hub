"""显式区块转正文：安全映射，不能确定时整笔拒绝。"""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.schemas.rich_document import SceneDocument, SceneDocumentOut, SceneDocumentSave


def _conflict() -> ConflictError:
    return ConflictError("区块转换涉及无法无损映射的格式或标记，请先调整后重试", extra={
        "code": "document_conversion_conflict",
    })


def conversion(document: SceneDocument, section_key: UUID) -> SceneDocument:
    selected = next((item for item in document.sections if item.key == section_key), None)
    if selected is None:
        raise NotFoundError("区块不存在")
    anchor, inserted = selected.anchor, selected.content
    length = len(inserted)
    text = document.content[:anchor] + inserted + document.content[anchor:]
    payload = document.model_dump(mode="json")
    payload["content"] = text
    formatting = payload["formatting"]
    remove_anchor_boundary = False
    if length and anchor in formatting["boundaries"]:
        siblings = any(item.key != section_key and item.anchor == anchor for item in document.sections)
        dependent_blocks = any(item.start == anchor or item.end == anchor for item in document.formatting.blocks)
        if siblings or dependent_blocks:
            # 相邻结构依赖这个零宽边界，整数锚点不足以决定转换后保留在哪一侧。
            raise _conflict()
        remove_anchor_boundary = True
    inline = []
    for style in formatting["inline"]:
        start, end = style["start"], style["end"]
        if start < anchor < end:
            inline.extend([{**style, "end": anchor}, {**style, "start": anchor + length, "end": end + length}])
        elif start >= anchor:
            inline.append({**style, "start": start + length, "end": end + length})
        else:
            inline.append(style)
    inline.extend({**style.model_dump(mode="json"), "start": anchor + style.start, "end": anchor + style.end}
                  for style in selected.formatting.inline)
    blocks = []
    for style in formatting["blocks"]:
        start, end = style["start"], style["end"]
        if start < anchor < end or start == end == anchor:
            raise _conflict()
        if start >= anchor:
            blocks.append({**style, "start": start + length, "end": end + length})
        else:
            blocks.append(style)
    if not length and (selected.formatting.blocks or selected.formatting.boundaries):
        raise _conflict()
    blocks.extend({**style.model_dump(mode="json"), "start": anchor + style.start, "end": anchor + style.end}
                  for style in selected.formatting.blocks)
    boundaries = {value if value <= anchor else value + length for value in formatting["boundaries"]
                  if not (remove_anchor_boundary and value == anchor)}
    boundaries.update(anchor + value for value in selected.formatting.boundaries)
    if selected.formatting.blocks:
        boundaries.update((anchor, anchor + length))
    formatting.update(inline=inline, blocks=blocks, boundaries=sorted(boundaries))

    remaining = []
    for item in payload["sections"]:
        if item["key"] == str(section_key):
            continue
        if item["anchor"] > anchor or (item["anchor"] == anchor and item["order"] > selected.order):
            item["anchor"] += length
        remaining.append(item)
    payload["sections"] = remaining
    payload["retired_section_keys"].append(str(section_key))
    for marker in payload["markers"]:
        if marker["status"] != "active":
            continue
        if marker["section_key"] == str(section_key):
            marker["section_key"] = None
            marker["start"] += anchor
            marker["end"] += anchor
        elif marker["section_key"] is None:
            start, end = marker["start"], marker["end"]
            if start < anchor < end:
                raise _conflict()
            if start >= anchor:
                marker["start"], marker["end"] = start + length, end + length
    try:
        return SceneDocument.model_validate(payload)
    except ValidationError as exc:
        raise _conflict() from exc


async def convert_section(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, scene_id: int,
    section_key: UUID, expected_revision: int,
) -> SceneDocumentOut:
    from app.services import rich_document_service

    current = await rich_document_service.get_document(db, user_id, project_id, chapter_id, scene_id)
    if current.content_revision != expected_revision:
        raise ConflictError("场景已被其他操作修改，请核对最新内容后重试", extra={
            "code": "stale_scene_revision", "current_revision": current.content_revision,
        })
    converted = conversion(current.document, section_key)
    body = SceneDocumentSave.model_validate({
        "expected_content_revision": expected_revision, "document": converted.model_dump(mode="json"),
        "add_section_keys": [], "delete_section_keys": [str(section_key)],
        "add_marker_keys": [], "delete_marker_keys": [],
        "mapped_section_keys": [str(item.key) for item in current.document.sections if item.key != section_key],
        "mapped_marker_keys": [str(item.key) for item in current.document.markers],
    })
    return await rich_document_service.save_document(
        db, user_id, project_id, chapter_id, scene_id, body, converted_section_key=section_key,
    )
