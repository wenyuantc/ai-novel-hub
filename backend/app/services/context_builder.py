"""章节生成上下文组装（MVP 不依赖向量检索）。

组成：作品信息 + 世界观 + 角色卡 + 当前卷大纲 + 更早章节标题/摘要 + 最近 N 章全文。
超出字符预算时的裁剪顺序：
  1. 从最早开始丢弃「更早章节摘要」
  2. 从较早的一章开始，截掉最近章节正文的开头（保留结尾，利于衔接）
  3. 依次截断世界观、角色卡、当前卷大纲的尾部
  4. 最终硬截断兜底，保证不超过预算
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts import format_project_info
from app.models import Chapter, Outline, Project
from app.services import story_service
from app.services.project_service import project_info

RECENT_FULL_CHAPTERS = 2
SUMMARY_CHARS = 200
ELLIPSIS = "……"


@dataclass
class PriorChapter:
    chapter_no: int
    title: str
    content: str


@dataclass
class ContextParts:
    project_text: str
    world_text: str = ""
    characters_text: str = ""
    outline_text: str = ""
    summaries: list[str] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)

    def render(self) -> str:
        sections = [f"【作品信息】\n{self.project_text}"]
        if self.world_text:
            sections.append(f"【世界观】\n{self.world_text}")
        if self.characters_text:
            sections.append(f"【主要角色】\n{self.characters_text}")
        if self.outline_text:
            sections.append(f"【当前卷大纲】\n{self.outline_text}")
        if self.summaries:
            sections.append("【前情提要（更早章节）】\n" + "\n".join(self.summaries))
        if self.recent:
            sections.append("【最近章节正文】\n" + "\n\n".join(self.recent))
        return "\n\n".join(sections)


def _chapter_heading(no: int, title: str) -> str:
    return f"第{no}章 {title}".rstrip()


def _summary_line(ch: PriorChapter, summary_chars: int) -> str:
    text = " ".join(ch.content.split())
    snippet = text[:summary_chars] + (ELLIPSIS if len(text) > summary_chars else "")
    return f"{_chapter_heading(ch.chapter_no, ch.title)}：{snippet}"


def _cut_tail(text: str, keep: int) -> str:
    """保留开头 keep 个字符。"""
    if keep <= 0:
        return ""
    return text if len(text) <= keep else text[: max(0, keep - len(ELLIPSIS))] + ELLIPSIS


def assemble_context(
    *,
    project_text: str,
    world_text: str,
    characters_text: str,
    outline_text: str,
    prior_chapters: list[PriorChapter],
    budget: int,
    recent_count: int = RECENT_FULL_CHAPTERS,
    summary_chars: int = SUMMARY_CHARS,
) -> str:
    """纯函数：根据素材与字符预算组装上下文。prior_chapters 需按章节号升序。"""
    prior = [c for c in prior_chapters if c.content.strip()]
    recent_src = prior[-recent_count:] if recent_count > 0 else []
    earlier = prior[: len(prior) - len(recent_src)]

    parts = ContextParts(
        project_text=project_text,
        world_text=world_text.strip(),
        characters_text=characters_text.strip(),
        outline_text=outline_text.strip(),
        summaries=[_summary_line(c, summary_chars) for c in earlier],
    )
    recent_bodies = [c.content.strip() for c in recent_src]
    recent_heads = [_chapter_heading(c.chapter_no, c.title) for c in recent_src]

    def sync_recent() -> None:
        parts.recent = [f"{h}\n{b}" for h, b in zip(recent_heads, recent_bodies, strict=True) if b]

    sync_recent()

    def over() -> int:
        return len(parts.render()) - budget

    # 1. 丢弃最早的章节摘要
    excess = over()
    while excess > 0 and parts.summaries:
        excess -= len(parts.summaries.pop(0)) + 1
        if not parts.summaries:
            excess = over()
    excess = over()

    # 2. 截掉最近章节正文开头（较早的一章先截），保留结尾
    for i in range(len(recent_bodies)):
        if excess <= 0:
            break
        body = recent_bodies[i]
        keep = len(body) - excess - len(ELLIPSIS)
        recent_bodies[i] = ELLIPSIS + body[-keep:] if keep > 0 else ""
        sync_recent()
        excess = over()

    # 3. 截断世界观、角色卡、当前卷大纲
    for attr in ("world_text", "characters_text", "outline_text"):
        if excess <= 0:
            break
        text: str = getattr(parts, attr)
        setattr(parts, attr, _cut_tail(text, len(text) - excess))
        excess = over()

    rendered = parts.render()
    # 4. 兜底
    return _cut_tail(rendered, budget) if len(rendered) > budget else rendered


def _current_volume_outlines(outlines: list[Outline], chapter: Chapter) -> list[Outline]:
    if not outlines:
        return []
    volume_no: int | None = None
    if chapter.outline_id is not None:
        volume_no = next((o.volume_no for o in outlines if o.id == chapter.outline_id), None)
    if volume_no is None:
        candidates = [o for o in outlines if o.chapter_no <= chapter.chapter_no]
        if candidates:
            volume_no = max(candidates, key=lambda o: o.chapter_no).volume_no
    if volume_no is None:
        volume_no = outlines[0].volume_no
    return [o for o in outlines if o.volume_no == volume_no]


async def build_chapter_context(db: AsyncSession, project: Project, chapter: Chapter, budget: int) -> str:
    world = await story_service.list_items(db, story_service.WORLD, project.id)
    characters = await story_service.list_items(db, story_service.CHARACTERS, project.id)
    outlines = await story_service.list_items(db, story_service.OUTLINES, project.id)
    rows = await db.execute(
        select(Chapter.chapter_no, Chapter.title, Chapter.content)
        .where(
            Chapter.project_id == project.id,
            Chapter.id != chapter.id,
            Chapter.chapter_no < chapter.chapter_no,
        )
        .order_by(Chapter.chapter_no, Chapter.id)
    )
    prior = [PriorChapter(chapter_no=no, title=title, content=content) for no, title, content in rows.all()]
    return assemble_context(
        project_text=format_project_info(project_info(project)),
        world_text=story_service.format_world(world),
        characters_text=story_service.format_characters(characters),
        outline_text=story_service.format_outlines(_current_volume_outlines(outlines, chapter)),
        prior_chapters=prior,
        budget=budget,
    )


async def find_chapter_outline(db: AsyncSession, chapter: Chapter) -> Outline | None:
    if chapter.outline_id is None:
        return None
    return await db.scalar(
        select(Outline).where(Outline.id == chapter.outline_id, Outline.project_id == chapter.project_id)
    )
