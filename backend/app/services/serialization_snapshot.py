"""连载定稿的读者可见正文。场景、标题及章号使用阅读器同一快照。"""

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models import Scene
from app.services.rich_document_render import load_chapter_projections


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
    """场景 ID 永不复用；取最早的场景，退场后也仍然标识原章。调用方已经核验作品归属。"""
    scene_id = await db.scalar(select(func.min(Scene.id)).where(Scene.chapter_id == chapter_id))
    if scene_id is None:
        raise ConflictError("章节缺少可识别的场景，请刷新后重试", extra={"code": "scene_invariant_error"})
    return scene_id
