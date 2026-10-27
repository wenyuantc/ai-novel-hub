"""导出 TXT / Markdown / EPUB（EPUB 见 epub_service）。"""

import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.models import Chapter, Outline, Project
from app.services.project_service import get_owned_project

ExportFormat = Literal["txt", "md", "epub"]
AUTHOR_LIMIT = 100
_CHAPTER_PREFIX_RE = re.compile(r"^第\s*[0-9一二三四五六七八九十百千零〇两]+\s*章")
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


@dataclass
class ExportFile:
    filename: str
    content: str | bytes
    media_type: str
    # 导出时的提示（如跳过了正文为空的章节），经响应头 X-Export-Warnings 返回
    warnings: list[str] = field(default_factory=list)

    @property
    def body(self) -> bytes:
        return self.content if isinstance(self.content, bytes) else self.content.encode("utf-8")

    @property
    def warnings_header(self) -> str | None:
        """HTTP 头只能是 latin-1：UTF-8 百分号编码，多条以「；」分隔（前端 decodeURIComponent 后展示）。"""
        return quote("；".join(self.warnings), safe="") if self.warnings else None

    @property
    def content_disposition(self) -> str:
        # RFC 5987：ASCII 兜底名 + UTF-8 编码的真实文件名
        ext = self.filename.rsplit(".", 1)[-1]
        return f"attachment; filename=\"novel.{ext}\"; filename*=UTF-8''{quote(self.filename, safe='')}"


def chapter_heading(chapter: Chapter) -> str:
    title = chapter.title.strip()
    if _CHAPTER_PREFIX_RE.match(title):
        return title
    return f"第{chapter.chapter_no}章 {title}".rstrip()


def render_txt(project: Project, chapters: list[Chapter]) -> str:
    parts = [f"《{project.title}》"]
    meta = []
    if project.genre:
        meta.append(f"题材：{project.genre}")
    if project.premise:
        meta.append(f"简介：{project.premise}")
    if meta:
        parts.append("\n".join(meta))
    for ch in chapters:
        parts.append(f"{chapter_heading(ch)}\n\n{ch.content.strip()}")
    return "\n\n\n".join(parts) + "\n"


def render_md(project: Project, chapters: list[Chapter]) -> str:
    parts = [f"# {project.title}"]
    meta = []
    if project.genre:
        meta.append(f"> 题材：{project.genre}")
    if project.premise:
        meta.append(f"> 简介：{project.premise}")
    if meta:
        parts.append("\n>\n".join(meta))
    for ch in chapters:
        parts.append(f"## {chapter_heading(ch)}\n\n{ch.content.strip()}")
    return "\n\n".join(parts) + "\n"


def parse_chapter_ids(raw: list[str] | None) -> list[int] | None:
    """支持 ?chapter_ids=1,2,3 与 ?chapter_ids=1&chapter_ids=2 两种写法。"""
    if not raw:
        return None
    ids: list[int] = []
    for chunk in raw:
        for token in chunk.split(","):
            token = token.strip()
            if not token:
                continue
            if not token.isdigit():
                raise BadRequestError(f"chapter_ids 格式错误：{token}")
            ids.append(int(token))
    return ids or None


async def export_project(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    fmt: ExportFormat,
    chapter_ids: list[int] | None,
    *,
    author: str = "",
    include_synopsis: bool = True,
) -> ExportFile:
    project = await get_owned_project(db, user_id, project_id)
    stmt = select(Chapter).where(Chapter.project_id == project.id, Chapter.status != "generating")
    if chapter_ids is not None:
        stmt = stmt.where(Chapter.id.in_(chapter_ids))
    selected = list(await db.scalars(stmt.order_by(Chapter.chapter_no, Chapter.id)))
    chapters = [c for c in selected if c.content.strip()]
    if not chapters:
        raise BadRequestError("没有可导出的章节（章节正文为空）")
    base = _UNSAFE_FILENAME_RE.sub("_", project.title).strip() or "novel"
    if fmt == "epub":
        from app.services import epub_service

        outline_ids = {c.outline_id for c in chapters if c.outline_id is not None}
        outlines = (
            {o.id: o for o in await db.scalars(select(Outline).where(Outline.id.in_(outline_ids)))}
            if outline_ids
            else {}
        )
        skipped = [chapter_heading(c) for c in selected if not c.content.strip()]
        warnings = [f"已跳过正文为空的章节：{'、'.join(skipped)}"] if skipped else []
        data = epub_service.build_epub(
            project, chapters, outlines, author=author.strip()[:AUTHOR_LIMIT], include_synopsis=include_synopsis
        )
        return ExportFile(f"{base}.epub", data, "application/epub+zip", warnings)
    if fmt == "md":
        return ExportFile(f"{base}.md", render_md(project, chapters), "text/markdown; charset=utf-8")
    return ExportFile(f"{base}.txt", render_txt(project, chapters), "text/plain; charset=utf-8")
