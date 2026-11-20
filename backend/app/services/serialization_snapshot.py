"""连载定稿的读者可见正文。场景、标题及章号使用阅读器同一快照。"""

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.services.rich_document_render import load_chapter_projections
from app.services.scene_move_history import chapter_identity


@dataclass(frozen=True)
class FrozenDraft:
    chapter_no: int
    title: str
    content: str
    blocks: list[dict] | None
    fingerprint: str
    content_revision: int
    status: str


def edition_fingerprint(chapter_no: int, title: str, content: str, blocks: list[dict] | None) -> str:
    payload = json.dumps(
        [chapter_no, title, content, blocks], ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def capture_draft(db: AsyncSession, project_id: int, chapter_id: int) -> FrozenDraft:
    projections = await load_chapter_projections(db, project_id, chapter_ids=[chapter_id])
    if not projections:
        raise NotFoundError("章节不存在")
    projection = projections[0]
    chapter = projection.chapter
    blocks = [block.model_dump(mode="json") for block in projection.blocks] if projection.blocks is not None else None
    return FrozenDraft(
        chapter_no=chapter.chapter_no, title=chapter.title, content=chapter.content, blocks=blocks,
        fingerprint=edition_fingerprint(chapter.chapter_no, chapter.title, chapter.content, blocks),
        content_revision=chapter.content_revision, status=chapter.status,
    )


async def chapter_scene_id(db: AsyncSession, chapter_id: int) -> int:
    return await chapter_identity(db, chapter_id)
