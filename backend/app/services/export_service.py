"""导出 TXT / Markdown。"""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.models import Chapter, Project
from app.services.project_service import get_owned_project

ExportFormat = Literal["txt", "md"]
_CHAPTER_PREFIX_RE = re.compile(r"^第\s*[0-9一二三四五六七八九十百千零〇两]+\s*章")
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


@dataclass
class ExportFile:
    filename: str
    content: str
    media_type: str

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
    db: AsyncSession, user_id: int, project_id: int, fmt: ExportFormat, chapter_ids: list[int] | None
) -> ExportFile:
    project = await get_owned_project(db, user_id, project_id)
    stmt = select(Chapter).where(Chapter.project_id == project.id, Chapter.status != "generating")
    if chapter_ids is not None:
        stmt = stmt.where(Chapter.id.in_(chapter_ids))
    chapters = [c for c in await db.scalars(stmt.order_by(Chapter.chapter_no, Chapter.id)) if c.content.strip()]
    if not chapters:
        raise BadRequestError("没有可导出的章节（章节正文为空）")
    base = _UNSAFE_FILENAME_RE.sub("_", project.title).strip() or "novel"
    if fmt == "md":
        return ExportFile(f"{base}.md", render_md(project, chapters), "text/markdown; charset=utf-8")
    return ExportFile(f"{base}.txt", render_txt(project, chapters), "text/plain; charset=utf-8")
