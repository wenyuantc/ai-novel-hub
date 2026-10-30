"""本章选角（D4）与章节出场角色。

本章涉及角色 = 主角 ∪ 本章手动指定的出场角色 ∪ 自动匹配（本章梗概 + 上一章结尾约 1500 字中出现的名字 / 别名）。
自动匹配规则：
- 名字 / 别名长度 < 2 的跳过（避免单字误命中）；
- 长词优先：先匹配较长的词并占用其位置，较短的词只有出现在未被占用的位置上才算命中
  （「林远山」命中后，其中的「林远」不再单独计）。
"""

from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.models import Chapter, ChapterCharacter, Character, Project
from app.services.chapter_service import get_manageable_chapter

PROTAGONIST_ROLES = frozenset({"主角", "protagonist"})
PREV_TAIL_CHARS = 1500
MIN_TERM_LEN = 2


def is_protagonist_role(role_type: str | None) -> bool:
    return (role_type or "").strip().lower() in PROTAGONIST_ROLES


def is_protagonist(c: Character) -> bool:
    """主角判定：开启「始终注入上下文」，或定位为主角 / protagonist。"""
    return bool(c.always_in_context) or is_protagonist_role(c.role_type)


def character_terms(c: Character) -> list[str]:
    terms: list[str] = []
    for t in [c.name, *(c.aliases or [])]:
        t = (t or "").strip()
        if len(t) >= MIN_TERM_LEN and t not in terms:
            terms.append(t)
    return terms


def match_characters(text: str, characters: list[Character]) -> list[int]:
    """返回在 text 中被提及的角色 id（按 characters 原顺序）。纯函数。"""
    if not text:
        return []
    owners: dict[str, set[int]] = {}
    for c in characters:
        for t in character_terms(c):
            owners.setdefault(t, set()).add(c.id)
    covered = [False] * len(text)
    matched: set[int] = set()
    for term in sorted(owners, key=len, reverse=True):
        n = len(term)
        hits: list[int] = []
        start = 0
        while (i := text.find(term, start)) >= 0:
            # 完全落在更长词已占用的位置内 → 不算命中
            if not all(covered[i : i + n]):
                hits.append(i)
            start = i + 1
        if hits:
            matched |= owners[term]
            for i in hits:
                covered[i : i + n] = [True] * n
    return [c.id for c in characters if c.id in matched]


@dataclass
class Cast:
    involved: list[Character] = field(default_factory=list)
    others: list[Character] = field(default_factory=list)

    @property
    def involved_ids(self) -> list[int]:
        return [c.id for c in self.involved]


async def list_characters(db: AsyncSession, project_id: int) -> list[Character]:
    rows = await db.scalars(
        select(Character).where(Character.project_id == project_id).order_by(Character.sort, Character.id)
    )
    return list(rows)


async def cast_ids(db: AsyncSession, chapter_id: int) -> list[int]:
    rows = await db.scalars(
        select(ChapterCharacter.character_id)
        .join(Character, Character.id == ChapterCharacter.character_id)
        .where(ChapterCharacter.chapter_id == chapter_id)
        .order_by(Character.sort, Character.id)
    )
    return list(rows)


async def previous_tail(db: AsyncSession, chapter: Chapter, chars: int = PREV_TAIL_CHARS) -> str:
    content = await db.scalar(
        select(Chapter.content)
        .where(
            Chapter.project_id == chapter.project_id,
            Chapter.id != chapter.id,
            Chapter.chapter_no < chapter.chapter_no,
        )
        .order_by(Chapter.chapter_no.desc(), Chapter.id.desc())
        .limit(1)
    )
    content = (content or "").strip()
    return content[-chars:]


async def select_cast(
    db: AsyncSession,
    project: Project,
    chapter: Chapter,
    outline_summary: str,
    *,
    characters: list[Character] | None = None,
) -> Cast:
    if characters is None:
        characters = await list_characters(db, project.id)
    manual = set(await cast_ids(db, chapter.id))
    text = f"{outline_summary or ''}\n{await previous_tail(db, chapter)}"
    auto = set(match_characters(text, characters))

    ordered: list[Character] = []
    seen: set[int] = set()
    for group in (
        [c for c in characters if is_protagonist(c)],
        [c for c in characters if c.id in manual],
        [c for c in characters if c.id in auto],
    ):
        for c in group:
            if c.id not in seen:
                seen.add(c.id)
                ordered.append(c)
    return Cast(involved=ordered, others=[c for c in characters if c.id not in seen])


# ---------------- 出场角色读写 ----------------


async def get_cast(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> list[int]:
    chapter = await get_manageable_chapter(db, user_id, project_id, chapter_id)
    return await cast_ids(db, chapter.id)


async def set_cast(db: AsyncSession, user_id: int, project_id: int, chapter_id: int, ids: list[int]) -> list[int]:
    """整体替换本章出场角色；所有 id 必须属于本项目。"""
    chapter = await get_manageable_chapter(db, user_id, project_id, chapter_id)
    wanted = list(dict.fromkeys(ids))
    if wanted:
        found = set(
            await db.scalars(select(Character.id).where(Character.id.in_(wanted), Character.project_id == project_id))
        )
        if len(found) != len(wanted):
            raise BadRequestError("部分角色不存在或不属于该项目")
    await db.execute(delete(ChapterCharacter).where(ChapterCharacter.chapter_id == chapter.id))
    db.add_all(ChapterCharacter(chapter_id=chapter.id, character_id=cid) for cid in wanted)
    await db.commit()
    return await cast_ids(db, chapter.id)
