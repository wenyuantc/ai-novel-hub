import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Chapter, Outline, Project
from app.schemas.chapter import ChapterCreate, ChaptersFromOutlines, ChapterUpdate
from app.services.project_service import get_owned_project

_WS_RE = re.compile(r"\s+")


def count_words(text: str) -> int:
    """字数统计：按非空白字符计（中文惯例）。"""
    return len(_WS_RE.sub("", text or ""))


async def get_owned_chapter(db: AsyncSession, user_id: int, chapter_id: int) -> Chapter:
    chapter = await db.scalar(
        select(Chapter)
        .join(Project, Project.id == Chapter.project_id)
        .where(Chapter.id == chapter_id, Project.user_id == user_id)
    )
    if chapter is None:
        raise NotFoundError("章节不存在")
    return chapter


async def get_project_chapter(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> Chapter:
    await get_owned_project(db, user_id, project_id)
    chapter = await db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.project_id == project_id))
    if chapter is None:
        raise NotFoundError("章节不存在")
    return chapter


async def list_chapters(db: AsyncSession, user_id: int, project_id: int) -> list[Chapter]:
    await get_owned_project(db, user_id, project_id)
    rows = await db.scalars(
        select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_no, Chapter.id)
    )
    return list(rows)


async def _get_outline(db: AsyncSession, project_id: int, outline_id: int) -> Outline:
    outline = await db.scalar(select(Outline).where(Outline.id == outline_id, Outline.project_id == project_id))
    if outline is None:
        raise BadRequestError("大纲条目不存在或不属于该项目")
    return outline


async def _next_chapter_no(db: AsyncSession, project_id: int) -> int:
    current = await db.scalar(select(func.max(Chapter.chapter_no)).where(Chapter.project_id == project_id))
    return (current or 0) + 1


async def create_chapter(db: AsyncSession, user_id: int, project_id: int, data: ChapterCreate) -> Chapter:
    await get_owned_project(db, user_id, project_id)
    title = (data.title or "").strip()
    chapter_no = data.chapter_no
    if data.outline_id is not None:
        outline = await _get_outline(db, project_id, data.outline_id)
        title = title or outline.title
        chapter_no = chapter_no or outline.chapter_no
    if chapter_no is None:
        chapter_no = await _next_chapter_no(db, project_id)
    chapter = Chapter(
        project_id=project_id,
        outline_id=data.outline_id,
        chapter_no=chapter_no,
        title=title or f"第{chapter_no}章",
        content=data.content,
        status="draft",
        word_count=count_words(data.content),
    )
    db.add(chapter)
    await db.commit()
    await db.refresh(chapter)
    return chapter


async def create_from_outlines(
    db: AsyncSession, user_id: int, project_id: int, data: ChaptersFromOutlines
) -> list[Chapter]:
    """为大纲条目批量创建章节占位；已关联章节的大纲条目跳过。"""
    await get_owned_project(db, user_id, project_id)
    stmt = select(Outline).where(Outline.project_id == project_id)
    if data.outline_ids is not None:
        stmt = stmt.where(Outline.id.in_(data.outline_ids))
    outlines = list(await db.scalars(stmt.order_by(Outline.volume_no, Outline.chapter_no, Outline.sort)))
    if data.outline_ids is not None and len(outlines) != len(set(data.outline_ids)):
        raise BadRequestError("部分大纲条目不存在或不属于该项目")
    linked = set(
        await db.scalars(
            select(Chapter.outline_id).where(Chapter.project_id == project_id, Chapter.outline_id.is_not(None))
        )
    )
    created = []
    for o in outlines:
        if o.id in linked:
            continue
        chapter = Chapter(
            project_id=project_id,
            outline_id=o.id,
            chapter_no=o.chapter_no,
            title=o.title,
            content="",
            status="draft",
            word_count=0,
        )
        db.add(chapter)
        created.append(chapter)
    await db.commit()
    for c in created:
        await db.refresh(c)
    return created


async def update_chapter(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, data: ChapterUpdate
) -> Chapter:
    chapter = await get_project_chapter(db, user_id, project_id, chapter_id)
    fields = data.model_fields_set
    # 生成中修改正文/标题会在生成完成时被静默覆盖；只允许单独复位状态
    if chapter.status == "generating" and data.status is None and (data.content is not None or data.title is not None):
        raise ConflictError("该章节正在生成中，请等待完成或先复位状态后再编辑")
    if "outline_id" in fields:
        if data.outline_id is not None:
            await _get_outline(db, project_id, data.outline_id)
        chapter.outline_id = data.outline_id
    if data.chapter_no is not None:
        chapter.chapter_no = data.chapter_no
    if data.title is not None:
        chapter.title = data.title.strip()
    if data.content is not None:
        chapter.content = data.content
        chapter.word_count = count_words(data.content)
    if data.status is not None:
        chapter.status = data.status
    await db.commit()
    await db.refresh(chapter)
    return chapter


async def delete_chapter(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> None:
    chapter = await get_project_chapter(db, user_id, project_id, chapter_id)
    await db.delete(chapter)
    await db.commit()
