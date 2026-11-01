import re

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.models import Chapter, ChapterPlan, ChapterVersion, Outline, Project
from app.schemas.chapter import ChapterCreate, ChaptersFromOutlines, ChapterUpdate
from app.services import memory_service, project_lifecycle, style_service, version_service
from app.services.audit_writer import AuditOperation
from app.services.project_service import get_manageable_project, get_owned_project

_WS_RE = re.compile(r"\s+")


def count_words(text: str) -> int:
    """字数统计：按非空白字符计（中文惯例）。"""
    return len(_WS_RE.sub("", text or ""))


async def commit_versioned(db: AsyncSession) -> None:
    """提交含版本写入的事务；并发写入撞上 (chapter_id, seq) 唯一约束时返回 409。"""
    try:
        await version_service.flush_versioned(db)
        await db.commit()
    except ConflictError:
        await db.rollback()
        raise
    except IntegrityError as exc:
        await db.rollback()
        if not version_service.is_version_conflict(exc):
            raise
        raise ConflictError("正文同时被其他操作修改，请刷新后重试") from exc


async def get_owned_chapter(db: AsyncSession, user_id: int, chapter_id: int, *, lock: bool = False) -> Chapter:
    chapter = await db.scalar(
        select(Chapter)
        .join(Project, Project.id == Chapter.project_id)
        .where(Chapter.id == chapter_id, Project.user_id == user_id, *project_lifecycle.active_conditions())
    )
    if chapter is None:
        raise NotFoundError("章节不存在")
    if lock:
        await get_owned_project(db, user_id, chapter.project_id, lock=True)
    return chapter


async def get_project_chapter(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, *, lock: bool = False
) -> Chapter:
    await get_owned_project(db, user_id, project_id, lock=lock)
    return await _chapter_in_project(db, project_id, chapter_id)


async def get_manageable_chapter(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, *, lock: bool = False,
    permission: str = "projects.read",
) -> Chapter:
    await get_manageable_project(db, user_id, project_id, lock=lock, permission=permission)
    return await _chapter_in_project(db, project_id, chapter_id)


async def _chapter_in_project(db: AsyncSession, project_id: int, chapter_id: int) -> Chapter:
    chapter = await db.scalar(select(Chapter).where(Chapter.id == chapter_id, Chapter.project_id == project_id))
    if chapter is None:
        raise NotFoundError("章节不存在")
    return chapter


async def list_chapters(db: AsyncSession, user_id: int, project_id: int) -> list[Chapter]:
    await get_manageable_project(db, user_id, project_id)
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
    async with AuditOperation(db, user_id, "chapter.create", "chapter", None, project_id=project_id) as audit:
        await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
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
        if data.content.strip():
            await db.flush()
            version = await version_service.record_manual(db, chapter)
            audit.version(version, project_id)
        await db.flush()
        audit.target(chapter)
        audit.summary.changed_fields = ["title", "chapter_no", "content"]
        audit.summary.word_count_after = chapter.word_count
        await audit.commit()
        await db.refresh(chapter)
        return chapter


async def create_from_outlines(
    db: AsyncSession, user_id: int, project_id: int, data: ChaptersFromOutlines
) -> list[Chapter]:
    """为大纲条目批量创建章节占位（1 纲 1 章）；已关联章节的、已展开为细纲的大纲条目跳过（后者按细纲建章）。"""
    async with AuditOperation(
        db, user_id, "chapter.create_from_outlines", "project", None, project_id=project_id
    ) as audit:
        project = await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
        audit.target(project)
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
        expanded = set(
            await db.scalars(select(ChapterPlan.outline_id).where(ChapterPlan.project_id == project_id).distinct())
        )
        created = []
        for o in outlines:
            if o.id in linked or o.id in expanded:
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
        await db.flush()
        audit.affected([c.id for c in created])
        await audit.commit()
        for c in created:
            await db.refresh(c)
        return created


async def update_chapter(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, data: ChapterUpdate
) -> Chapter:
    async with AuditOperation(db, user_id, "chapter.update", "chapter", chapter_id, project_id=project_id) as audit:
        chapter = await get_manageable_chapter(
            db, user_id, project_id, chapter_id, lock=True, permission="projects.edit"
        )
        audit.target(chapter)
        project = await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
        audit.track(chapter, data.model_fields_set)
        fields = data.model_fields_set
        if project.user_id != user_id:
            if "style_id" in fields:
                raise ForbiddenError("管理他人项目时不能修改私有风格绑定")
            if chapter.status == "generating":
                raise ConflictError("该章节正在生成中，请等待完成后再编辑")
        # 生成中修改正文/标题会在生成完成时被静默覆盖；只允许单独复位状态
        if (
            chapter.status == "generating"
            and data.status is None
            and (data.content is not None or data.title is not None)
        ):
            raise ConflictError("该章节正在生成中，请等待完成或先复位状态后再编辑")
        if "outline_id" in fields:
            if data.outline_id is not None:
                await _get_outline(db, project_id, data.outline_id)
            if data.outline_id != chapter.outline_id and chapter.plan_id is not None:
                # 改挂到别的大纲：解除与原细纲的绑定（细纲回到未建章状态），保持双向一致
                plan = await db.scalar(select(ChapterPlan).where(ChapterPlan.id == chapter.plan_id))
                if plan is not None:
                    plan.chapter_id = None
                chapter.plan_id = None
            chapter.outline_id = data.outline_id
        if "style_id" in fields:
            # 只能指向可见的风格（预设或本人所有）；null = 继承项目默认
            await style_service.ensure_assignable(db, user_id, data.style_id)
            chapter.style_id = data.style_id
        if data.chapter_no is not None:
            if data.chapter_no != chapter.chapter_no:
                # 剧情记忆按章号召回：该章的记忆跟随
                await memory_service.on_chapter_renumbered(db, chapter.id, data.chapter_no)
            chapter.chapter_no = data.chapter_no
        if data.title is not None:
            chapter.title = data.title.strip()
        content_changed = data.content is not None and data.content != chapter.content
        if data.content is not None:
            chapter.content = data.content
            chapter.word_count = count_words(data.content)
        if data.status is not None:
            chapter.status = data.status
        if content_changed:
            # 手动保存：按 10 分钟窗口合并，与正文修改同一事务提交
            version = await version_service.record_manual(db, chapter)
            audit.version(version, project_id)
        await audit.commit(commit_versioned, flush_fn=version_service.flush_versioned)
        await db.refresh(chapter)
        return chapter


async def delete_chapter(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> None:
    async with AuditOperation(db, user_id, "chapter.delete", "chapter", chapter_id, project_id=project_id) as audit:
        chapter = await get_manageable_chapter(
            db, user_id, project_id, chapter_id, lock=True, permission="projects.edit"
        )
        audit.target(chapter)
        audit.summary.word_count_before = chapter.word_count
        if audit.enabled:
            audit.summary.version_count = int(
                await db.scalar(select(func.count(ChapterVersion.id)).where(ChapterVersion.chapter_id == chapter.id))
                or 0
            )
        # 先删该章的自动记忆与检索单元（手动记忆保留，chapter_id 由外键置空）
        await memory_service.on_chapters_deleted(db, [chapter.id])
        await db.delete(chapter)
        await audit.commit()
