"""场景富文档的持久化投影和标记墓碑；不修复或猜测坏身份。"""

from collections import defaultdict
from itertools import batched, chain

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Scene, SceneDocumentMeta, SceneMarker, SceneSection
from app.schemas.rich_document import SceneDocument

CAPTURE_BATCH = 200


def retire_marker(row: SceneMarker) -> None:
    """仅保留可验证的身份与来源，不在墓碑中留存正文或草稿片段。"""
    row.status, row.start_offset, row.end_offset, row.last_start, row.excerpt = "deleted", None, None, None, ""
    row.excerpt_truncated = False
    row.draft_excerpt, row.draft_excerpt_truncated, row.draft_last_start = None, False, None


async def capture_documents(db: AsyncSession, scenes: list[Scene]) -> tuple[dict[int, SceneDocument], bool]:
    """读取已 flush 的结构；调用方须显式持久化变更并由外层核对章 revision。"""
    ids = [scene.id for scene in scenes]
    if not ids:
        return {}, False
    id_set = set(ids)
    if any(isinstance(row, SceneDocumentMeta | SceneSection | SceneMarker) and row.scene_id in id_set
           for row in chain(db.new, db.dirty, db.deleted)):
        raise ConflictError("富文档变更尚未写入事务，不能记录历史", extra={"code": "rich_document_pending"})
    meta: dict[int, SceneDocumentMeta] = {}
    section_rows: list[SceneSection] = []
    marker_rows: list[SceneMarker] = []
    with db.no_autoflush:
        for batch in batched(ids, CAPTURE_BATCH):
            meta.update({row.scene_id: row for row in await db.scalars(
                select(SceneDocumentMeta).where(SceneDocumentMeta.scene_id.in_(batch))
            )})
            section_rows.extend(await db.scalars(
                select(SceneSection).where(SceneSection.scene_id.in_(batch))
                .order_by(SceneSection.scene_id, SceneSection.anchor, SceneSection.sort, SceneSection.id)
            ))
            marker_rows.extend(await db.scalars(
                select(SceneMarker).where(SceneMarker.scene_id.in_(batch), SceneMarker.status != "deleted")
                .order_by(SceneMarker.scene_id, SceneMarker.id)
            ))
    sections: dict[int, list[SceneSection]] = defaultdict(list)
    markers: dict[int, list[SceneMarker]] = defaultdict(list)
    by_id = {row.id: row for row in section_rows}
    for row in section_rows:
        sections[row.scene_id].append(row)
    for row in marker_rows:
        if row.section_id is not None and (
            row.section_id not in by_id or by_id[row.section_id].scene_id != row.scene_id
        ):
            raise ConflictError("正文标记的区块身份无效，不能生成历史", extra={"code": "rich_document_invalid"})
        markers[row.scene_id].append(row)
    result: dict[int, SceneDocument] = {}
    for scene in scenes:
        source = sections[scene.id]
        try:
            result[scene.id] = SceneDocument.model_validate({
                "content": scene.content,
                "formatting": meta[scene.id].formatting if scene.id in meta else {},
                "sections": [{
                    "key": row.logical_key, "anchor": row.anchor, "order": row.sort,
                    "title": row.title, "content": row.content, "formatting": row.formatting,
                    "include_in_ai": row.include_in_ai,
                } for row in source if row.active],
                "retired_section_keys": [row.logical_key for row in source if not row.active],
                "markers": [{
                    "key": row.logical_key,
                    "section_key": by_id[row.section_id].logical_key if row.section_id is not None else None,
                    "status": row.status, "color": row.color, "start": row.start_offset, "end": row.end_offset,
                    "last_start": row.last_start, "excerpt": row.excerpt, "excerpt_truncated": row.excerpt_truncated,
                    "draft_excerpt": row.draft_excerpt, "draft_excerpt_truncated": row.draft_excerpt_truncated,
                    "draft_last_start": row.draft_last_start,
                } for row in markers[scene.id]],
            })
        except ValidationError as exc:
            raise ConflictError("场景富文档无效，不能生成历史", extra={"code": "rich_document_invalid"}) from exc
    return result, bool(meta or section_rows or marker_rows)
