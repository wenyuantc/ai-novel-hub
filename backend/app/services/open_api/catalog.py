"""授权小说、目录、正文、封面及现有下载能力的只读适配。"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.core.errors import NotFoundError
from app.models import Chapter, Outline, Project, ProjectCover
from app.schemas import open_api as out
from app.schemas.chapter import ChapterStatus
from app.schemas.project import ProjectStatus
from app.services import chapter_service, cover_service, export_service
from app.services.api_key_service import ApiKeyPrincipal, get_authorized_project
from app.services.open_api.common import contains_literal, page_models

PREFIX = "/api/open/v1"


def key_info(principal: ApiKeyPrincipal) -> out.KeyInfo:
    return out.KeyInfo(
        id=principal.key_id,
        name=principal.name,
        all_projects=principal.all_projects,
        project_ids=sorted(principal.project_ids),
        expires_at=principal.expires_at,
    )


async def _novel_outs(db: AsyncSession, projects: list[Project], url_prefix: str) -> list[out.Novel]:
    if not projects:
        return []
    ids = [project.id for project in projects]
    stats = {
        pid: (count, words, updated)
        for pid, count, words, updated in (
            await db.execute(
                select(
                    Chapter.project_id,
                    func.count(Chapter.id),
                    func.sum(Chapter.word_count),
                    func.max(Chapter.updated_at),
                )
                .where(Chapter.project_id.in_(ids))
                .group_by(Chapter.project_id)
            )
        ).all()
    }
    covers = dict(
        (
            await db.execute(
                select(ProjectCover.project_id, ProjectCover.updated_at).where(ProjectCover.project_id.in_(ids))
            )
        ).all()
    )
    novels = []
    for project in projects:
        count, words, updated = stats.get(project.id, (0, 0, None))
        novel = out.Novel.model_validate(project)
        novel.chapter_count = int(count)
        novel.total_words = int(words or 0)
        novel.content_updated_at = out.as_utc(updated) if updated is not None else None
        cover_updated = covers.get(project.id)
        novel.cover_updated_at = out.as_utc(cover_updated) if cover_updated is not None else None
        novel.cover_url = f"{url_prefix}/novels/{project.id}/cover" if cover_updated is not None else None
        novels.append(novel)
    return novels


async def list_novels(
    db: AsyncSession,
    principal: ApiKeyPrincipal,
    *,
    page: int,
    page_size: int,
    q: str | None = None,
    status: ProjectStatus | None = None,
    url_prefix: str = PREFIX,
) -> out.Page[out.Novel]:
    stmt = select(Project).where(Project.user_id == principal.user_id)
    if not principal.all_projects:
        stmt = stmt.where(Project.id.in_(principal.project_ids))
    if q:
        stmt = stmt.where(Project.title.ilike(contains_literal(q), escape="\\"))
    if status is not None:
        stmt = stmt.where(Project.status == status)
    projects, total = await page_models(
        db, stmt.order_by(Project.updated_at.desc(), Project.id.desc()), page, page_size
    )
    return out.Page(items=await _novel_outs(db, projects, url_prefix), total=total, page=page, page_size=page_size)


async def get_novel(
    db: AsyncSession, principal: ApiKeyPrincipal, novel_id: int, *, url_prefix: str = PREFIX
) -> out.Novel:
    project = await get_authorized_project(db, principal, novel_id)
    return (await _novel_outs(db, [project], url_prefix))[0]


async def _chapter_outs[T: out.ChapterBrief](db: AsyncSession, chapters: list[Chapter], schema: type[T]) -> list[T]:
    outline_ids = {chapter.outline_id for chapter in chapters if chapter.outline_id is not None}
    volumes = (
        {
            oid: (number, title)
            for oid, number, title in (
                await db.execute(
                    select(Outline.id, Outline.volume_no, Outline.volume_title).where(Outline.id.in_(outline_ids))
                )
            ).all()
        }
        if outline_ids
        else {}
    )
    results = []
    for chapter in chapters:
        result = schema.model_validate(chapter)
        result.volume_no, result.volume_title = volumes.get(chapter.outline_id, (None, None))
        results.append(result)
    return results


async def list_chapters(
    db: AsyncSession,
    principal: ApiKeyPrincipal,
    novel_id: int,
    *,
    page: int,
    page_size: int,
    status: ChapterStatus | None = None,
    updated_after: datetime | None = None,
) -> out.Page[out.ChapterBrief]:
    await get_authorized_project(db, principal, novel_id)
    stmt = (
        select(Chapter)
        .where(Chapter.project_id == novel_id)
        .options(
            load_only(
                Chapter.id,
                Chapter.project_id,
                Chapter.chapter_no,
                Chapter.title,
                Chapter.status,
                Chapter.word_count,
                Chapter.outline_id,
                Chapter.created_at,
                Chapter.updated_at,
            )
        )
    )
    if status is not None:
        stmt = stmt.where(Chapter.status == status)
    if updated_after is not None:
        stmt = stmt.where(Chapter.updated_at > out.as_utc(updated_after))
    chapters, total = await page_models(db, stmt.order_by(Chapter.chapter_no, Chapter.id), page, page_size)
    return out.Page(
        items=await _chapter_outs(db, chapters, out.ChapterBrief), total=total, page=page, page_size=page_size
    )


async def get_chapter(
    db: AsyncSession, principal: ApiKeyPrincipal, novel_id: int, chapter_id: int
) -> out.ChapterDetail:
    await get_authorized_project(db, principal, novel_id)
    chapter = await chapter_service.get_project_chapter(db, principal.user_id, novel_id, chapter_id)
    # 与目录共用卷信息组装；detail schema 才会读取正文列。
    return (await _chapter_outs(db, [chapter], out.ChapterDetail))[0]


async def get_cover(db: AsyncSession, principal: ApiKeyPrincipal, novel_id: int) -> ProjectCover:
    await get_authorized_project(db, principal, novel_id)
    return await cover_service.get_cover(db, principal.user_id, novel_id)


async def export_novel(
    db: AsyncSession,
    principal: ApiKeyPrincipal,
    novel_id: int,
    fmt: export_service.ExportFormat,
    chapter_ids: list[str] | None,
    *,
    include_synopsis: bool,
) -> export_service.ExportFile:
    await get_authorized_project(db, principal, novel_id)
    ids = export_service.parse_chapter_ids(chapter_ids)
    if ids:
        found = set(await db.scalars(select(Chapter.id).where(Chapter.project_id == novel_id, Chapter.id.in_(ids))))
        if set(ids) != found:
            raise NotFoundError("章节不存在")
    return await export_service.export_project(
        db,
        principal.user_id,
        novel_id,
        fmt,
        ids,
        author=principal.username,
        include_synopsis=include_synopsis,
    )
