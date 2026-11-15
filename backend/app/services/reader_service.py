"""阅读器：目录（只含已写章节）与阅读进度写入。进度的读取（含兜底）在 project_service（随 ProjectOut 返回）。"""

from datetime import UTC, datetime

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import Chapter, Outline, Project
from app.schemas.reader import ReaderChapter, ReadingProgressIn, TocItem
from app.services import project_access
from app.services.chapter_service import manual_chapter_views
from app.services.project_service import WRITTEN, get_owned_project


async def save_progress(db: AsyncSession, user_id: int, project_id: int, data: ReadingProgressIn) -> None:
    """整体替换进度；章节必须属于该项目（否则 404）。不改项目的 updated_at（书架「最近更新」不因阅读而变化）。"""
    await get_owned_project(db, user_id, project_id, lock=True)
    chapter_no = await db.scalar(
        select(Chapter.chapter_no).where(Chapter.id == data.chapter_id, Chapter.project_id == project_id)
    )
    if chapter_no is None:
        raise NotFoundError("章节不存在")
    progress = {
        "chapter_id": data.chapter_id,
        "chapter_no": chapter_no,
        "ratio": float(data.ratio),
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    await db.execute(
        update(Project)
        .where(Project.id == project_id)
        .values(reading_progress=progress, updated_at=Project.updated_at)
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def toc(db: AsyncSession, user_id: int, project_id: int) -> list[TocItem]:
    """已写章节，按 (chapter_no, id) 排序；卷信息经 outline_id 取大纲的 volume_no / volume_title。"""
    await project_access.require_project(db, user_id, project_id, "content.read")
    rows = await db.execute(
        select(
            Chapter.id,
            Chapter.chapter_no,
            Chapter.title,
            Chapter.word_count,
            Outline.volume_no,
            Outline.volume_title,
        )
        .outerjoin(Outline, and_(Outline.id == Chapter.outline_id, Outline.project_id == Chapter.project_id))
        .where(Chapter.project_id == project_id, WRITTEN)
        .order_by(Chapter.chapter_no, Chapter.id)
    )
    return [
        TocItem(id=i, chapter_no=no, title=t, word_count=wc, volume_no=vn, volume_title=vt)
        for i, no, t, wc, vn, vt in rows.all()
    ]


async def chapter(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> ReaderChapter:
    """一次读取正文和排版；不携带作者区块、标记或其他私有资料。"""
    from app.services.rich_document_render import load_chapter_projections

    await project_access.require_project(db, user_id, project_id, "content.read")
    projections = await load_chapter_projections(db, project_id, chapter_ids=[chapter_id])
    if not projections:
        raise NotFoundError("章节不存在")
    projection = projections[0]
    chapter = (await manual_chapter_views(db, user_id, project_id, [projection.chapter], full=True))[0]
    return ReaderChapter(chapter=chapter, blocks=projection.blocks)
