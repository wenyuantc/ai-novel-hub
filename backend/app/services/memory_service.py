"""剧情记忆：从章节分析规则抽取（不额外调用 AI）、重新分析时替换并继承标记、手动记忆、召回与注入。

抽取（extract_items，纯函数）：
- summary：章节摘要 1 条，重要度 3；
- plot_point：重要度 ≥ 3 的情节点；
- hook：强度 ≥ 6 的钩子，重要度 ceil(strength / 2)；
- character：每条角色变化「名字：当前状态」（只有存活变化时写存活状态），有存活变化的重要度 5，其余 3；
- foreshadow：埋设「埋下：…」/ 回收「回收：…」，重要度 4。
相关角色：character 类为该角色；其余按正文提及匹配项目角色（cast_service.match_characters）。

同步（sync_from_analysis，与分析落库同一事务，不 commit）：取该章原有自动记忆
按 content_key（sha1(kind + 规范化内容) 前 16 位）
建映射，新记忆能匹配上的复用原行（保留 pinned / muted，正文相同时向量也保留），匹配不上的旧记忆删除；手动记忆不受影响。
检索单元 enabled = not muted、pinned 同步。向量由分析任务成功后排队的 kb.index 记忆任务生成。

召回（recall）：范围 = chapter_no < 本章、不在「前情给了全文」的章节；summary 类再排除「前情给了摘要」的章节。
先放钉住且未屏蔽的（按章号，直到字数上限），再从检索结果（最终分 ≥ memory_min_score）补足到 memory_top_k 条，
最后按章号排序。
"""

import hashlib
import logging
import math
import unicodedata
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, NotFoundError
from app.models import Chapter, ChapterAnalysis, Character, KbSettings, Project, StoryMemory
from app.schemas.memory import (
    MemoryCreate,
    MemoryOut,
    MemoryPage,
    MemorySearchHit,
    MemorySearchRequest,
    MemorySearchResult,
    MemoryUpdate,
)
from app.services import cast_service, retrieval_service
from app.services.project_service import get_owned_project
from app.services.retrieval_service import ChunkInput, Embedder, SearchFilter

logger = logging.getLogger(__name__)

CONTENT_MAX = 1000
KIND_LABELS = {
    "summary": "摘要",
    "plot_point": "情节",
    "hook": "钩子",
    "character": "人物",
    "foreshadow": "伏笔",
    "manual": "备忘",
}
LIFE_LABELS = {"alive": "存活", "dead": "已死亡", "missing": "失踪", "other": "存活状态有变"}
PLOT_MIN_IMPORTANCE = 3
HOOK_MIN_STRENGTH = 6
AUTO_ONLY_FIELDS = ("chapter_no", "content", "characters", "importance")


# ---------------- 纯函数 ----------------


def normalize_content(text: str) -> str:
    """去掉空白与标点 / 符号，转小写（content_key 的规范化）。"""
    norm = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(ch for ch in norm if not ch.isspace() and unicodedata.category(ch)[0] not in ("P", "S"))


def content_key(kind: str, content: str) -> str:
    return hashlib.sha1(f"{kind}\x00{normalize_content(content)}".encode()).hexdigest()[:16]


def memory_text(m: StoryMemory) -> str:
    """检索文本：内容 + 相关角色名（名字参与关键词匹配）。"""
    names = " ".join(m.characters or [])
    return f"{m.content}\n{names}" if names else m.content


def _clip(text: Any) -> str:
    return " ".join(str(text or "").split())[:CONTENT_MAX]


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_items(analysis: ChapterAnalysis) -> list[dict[str, Any]]:
    """分析结果 → [{kind, content, importance, character}]（character 只有人物类有值）。同 content_key 去重。"""
    items: list[dict[str, Any]] = []
    summary = _clip(analysis.summary)
    if summary:
        items.append({"kind": "summary", "content": summary, "importance": 3})
    for p in analysis.plot_points or []:
        if isinstance(p, dict) and _int(p.get("importance"), 0) >= PLOT_MIN_IMPORTANCE and _clip(p.get("text")):
            items.append(
                {"kind": "plot_point", "content": _clip(p.get("text")), "importance": _int(p["importance"], 3)}
            )
    for h in analysis.hooks or []:
        strength = _int(h.get("strength"), 0) if isinstance(h, dict) else 0
        if strength >= HOOK_MIN_STRENGTH and _clip(h.get("text")):
            items.append(
                {"kind": "hook", "content": _clip(h.get("text")), "importance": min(5, math.ceil(strength / 2))}
            )
    for c in (analysis.changes_raw or {}).get("character_changes") or []:
        if not isinstance(c, dict) or not _clip(c.get("name")):
            continue
        name = _clip(c.get("name"))[:100]
        state = _clip(c.get("current_state"))
        life = c.get("life_status")
        life_label = LIFE_LABELS.get(life, "") if life else ""
        if state and life_label:
            text = f"{name}：{state}（{life_label}）"
        else:
            text = f"{name}：{state or life_label}"
        if text.endswith("："):
            continue
        items.append(
            {"kind": "character", "content": text[:CONTENT_MAX], "importance": 5 if life else 3, "character": name}
        )
    for f in analysis.foreshadow_candidates or []:
        if not isinstance(f, dict):
            continue
        body = _clip(f.get("text") or f.get("keyword"))
        if not body:
            continue
        prefix = "回收" if f.get("action") == "resolve" else "埋下"
        items.append({"kind": "foreshadow", "content": f"{prefix}：{body}"[:CONTENT_MAX], "importance": 4})
    seen: set[str] = set()
    unique = []
    for item in items:
        key = content_key(item["kind"], item["content"])
        if key not in seen:
            seen.add(key)
            unique.append({**item, "content_key": key})
    return unique


def _related(item: dict[str, Any], characters: list[Character]) -> list[str]:
    if item.get("character"):
        return [item["character"]]
    ids = set(cast_service.match_characters(item["content"], characters))
    return [c.name for c in characters if c.id in ids][:20]


# ---------------- 同步 ----------------


def _chunk(m: StoryMemory) -> ChunkInput:
    return ChunkInput(ref_id=m.id, text=memory_text(m), chapter_no=m.chapter_no, enabled=not m.muted, pinned=m.pinned)


async def sync_from_analysis(db: AsyncSession, chapter: Chapter, analysis: ChapterAnalysis) -> list[StoryMemory]:
    """用本次分析替换该章的自动记忆（按 content_key 继承钉住 / 屏蔽）。不 commit。"""
    characters = list(
        await db.scalars(
            select(Character).where(Character.project_id == chapter.project_id).order_by(Character.sort, Character.id)
        )
    )
    old_rows = list(
        await db.scalars(
            select(StoryMemory)
            .where(StoryMemory.chapter_id == chapter.id, StoryMemory.origin == "auto")
            .order_by(StoryMemory.id)
        )
    )
    old: dict[str, list[StoryMemory]] = {}
    for m in old_rows:
        old.setdefault(m.content_key, []).append(m)
    kept: list[StoryMemory] = []
    for item in extract_items(analysis):
        matches = old.get(item["content_key"])
        m = matches.pop(0) if matches else None
        if m is None:
            m = StoryMemory(
                project_id=chapter.project_id, chapter_id=chapter.id, origin="auto", pinned=False, muted=False
            )
            db.add(m)
        m.chapter_no = chapter.chapter_no
        m.kind = item["kind"]
        m.content = item["content"]
        m.importance = max(1, min(5, int(item["importance"])))
        m.characters = _related(item, characters)
        m.analysis_id = analysis.id
        m.content_key = item["content_key"]
        kept.append(m)
    stale = [m for rows in old.values() for m in rows]
    if stale:
        await retrieval_service.delete_refs(db, "memory", [m.id for m in stale])
        for m in stale:
            await db.delete(m)
    await db.flush()
    await retrieval_service.upsert_many(db, chapter.project_id, "memory", [_chunk(m) for m in kept])
    return kept


async def index_memories(db: AsyncSession, project_id: int, memories: list[StoryMemory]) -> None:
    """为已落库的记忆写入检索单元（关键词部分；钉住 / 屏蔽同步）。不 commit。项目备份导入时调用。"""
    await retrieval_service.upsert_many(db, project_id, "memory", [_chunk(m) for m in memories])


async def on_chapters_deleted(db: AsyncSession, chapter_ids: list[int]) -> None:
    """删除章节前调用：删除这些章节的自动记忆与检索单元。手动记忆保留（chapter_id 由外键置空，章号不变）。不 commit。"""
    if not chapter_ids:
        return
    rows = list(
        await db.scalars(
            select(StoryMemory).where(StoryMemory.chapter_id.in_(chapter_ids), StoryMemory.origin == "auto")
        )
    )
    if rows:
        await retrieval_service.delete_refs(db, "memory", [m.id for m in rows])
        for m in rows:
            await db.delete(m)
        await db.flush()


async def on_chapter_renumbered(db: AsyncSession, chapter_id: int, chapter_no: int) -> None:
    """章节号被手动修改：该章的记忆与检索单元跟随。不 commit。"""
    rows = list(await db.scalars(select(StoryMemory).where(StoryMemory.chapter_id == chapter_id)))
    for m in rows:
        m.chapter_no = chapter_no
    if rows:
        await retrieval_service.set_flags(db, "memory", [m.id for m in rows], chapter_no=chapter_no)
        await db.flush()


# ---------------- 手动管理 ----------------


async def get_project_memory(db: AsyncSession, user_id: int, project_id: int, memory_id: int) -> StoryMemory:
    await get_owned_project(db, user_id, project_id)
    m = await db.scalar(select(StoryMemory).where(StoryMemory.id == memory_id, StoryMemory.project_id == project_id))
    if m is None:
        raise NotFoundError("记忆不存在")
    return m


async def list_memories(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    *,
    kind: str | None = None,
    origin: str | None = None,
    chapter_from: int | None = None,
    chapter_to: int | None = None,
    pinned: bool | None = None,
    muted: bool | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> MemoryPage:
    await get_owned_project(db, user_id, project_id)
    conds = [StoryMemory.project_id == project_id]
    if kind:
        conds.append(StoryMemory.kind == kind)
    if origin:
        conds.append(StoryMemory.origin == origin)
    if chapter_from is not None:
        conds.append(StoryMemory.chapter_no >= chapter_from)
    if chapter_to is not None:
        conds.append(StoryMemory.chapter_no <= chapter_to)
    if pinned is not None:
        conds.append(StoryMemory.pinned.is_(pinned))
    if muted is not None:
        conds.append(StoryMemory.muted.is_(muted))
    if q and q.strip():
        conds.append(StoryMemory.content.contains(q.strip(), autoescape=True))
    total = await db.scalar(select(func.count(StoryMemory.id)).where(*conds)) or 0
    rows = await db.scalars(
        select(StoryMemory)
        .where(*conds)
        .order_by(StoryMemory.chapter_no, StoryMemory.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return MemoryPage(items=[MemoryOut.model_validate(m) for m in rows], total=total, page=page, page_size=page_size)


async def _chapter_id_for(db: AsyncSession, project_id: int, chapter_no: int) -> int | None:
    return await db.scalar(
        select(Chapter.id)
        .where(Chapter.project_id == project_id, Chapter.chapter_no == chapter_no)
        .order_by(Chapter.id)
        .limit(1)
    )


async def create_memory(db: AsyncSession, user_id: int, project_id: int, data: MemoryCreate) -> MemoryOut:
    await get_owned_project(db, user_id, project_id)
    m = StoryMemory(
        project_id=project_id,
        chapter_id=await _chapter_id_for(db, project_id, data.chapter_no),
        chapter_no=data.chapter_no,
        kind="manual",
        origin="manual",
        content=data.content,
        characters=data.characters,
        importance=data.importance,
        pinned=data.pinned,
        muted=False,
        content_key=content_key("manual", data.content),
    )
    db.add(m)
    await db.flush()
    await retrieval_service.upsert_many(db, project_id, "memory", [_chunk(m)])
    await db.commit()
    await db.refresh(m)
    return MemoryOut.model_validate(m)


async def update_memory(
    db: AsyncSession, user_id: int, project_id: int, memory_id: int, data: MemoryUpdate
) -> MemoryOut:
    m = await get_project_memory(db, user_id, project_id, memory_id)
    fields = data.model_dump(exclude_unset=True)
    edits = {k: v for k, v in fields.items() if k in AUTO_ONLY_FIELDS and v is not None}
    if edits and m.origin != "manual":
        raise BadRequestError("自动记忆只能钉住或屏蔽，不能修改内容")
    if fields.get("pinned") is not None:
        m.pinned = fields["pinned"]
    if fields.get("muted") is not None:
        m.muted = fields["muted"]
    if "chapter_no" in edits and edits["chapter_no"] != m.chapter_no:
        m.chapter_no = edits["chapter_no"]
        m.chapter_id = await _chapter_id_for(db, project_id, m.chapter_no)
    if "content" in edits:
        m.content = edits["content"]
        m.content_key = content_key(m.kind, m.content)
    if "characters" in edits:
        m.characters = edits["characters"]
    if "importance" in edits:
        m.importance = edits["importance"]
    await retrieval_service.upsert_many(db, project_id, "memory", [_chunk(m)])
    await db.commit()
    await db.refresh(m)
    return MemoryOut.model_validate(m)


async def delete_memory(db: AsyncSession, user_id: int, project_id: int, memory_id: int) -> None:
    m = await get_project_memory(db, user_id, project_id, memory_id)
    if m.origin != "manual":
        raise BadRequestError("自动记忆不能删除，可以屏蔽它")
    await retrieval_service.delete_refs(db, "memory", [m.id])
    await db.delete(m)
    await db.commit()


# ---------------- 召回 ----------------


@dataclass
class RecalledMemory:
    memory: StoryMemory
    # 相关度名次：钉住的为 -1（不参与「丢弃最不相关」的裁剪），检索结果从 0 起
    rank: int
    line: str

    @property
    def pinned(self) -> bool:
        return self.rank < 0


def memory_line(m: StoryMemory) -> str:
    return f"- 第{m.chapter_no}章（{KIND_LABELS.get(m.kind, m.kind)}）：{' '.join(m.content.split())}"


def _range_conditions(project_id: int, chapter_no: int, excluded: set[int], summary_chapters: set[int]):
    conds = [StoryMemory.project_id == project_id, StoryMemory.chapter_no < chapter_no]
    if excluded:
        conds.append(StoryMemory.chapter_no.not_in(sorted(excluded)))
    if summary_chapters:
        conds.append(or_(StoryMemory.kind != "summary", StoryMemory.chapter_no.not_in(sorted(summary_chapters))))
    return conds


async def recall(
    db: AsyncSession,
    project: Project,
    chapter_no: int,
    query: str,
    *,
    embedder: Embedder | None,
    excluded_chapter_nos: set[int] | None = None,
    summary_chapter_nos: set[int] | None = None,
    settings: KbSettings | None = None,
) -> list[RecalledMemory]:
    """召回第 chapter_no 章之前的相关记忆（见模块说明）。出错时返回空列表（只记日志，不阻断生成）。"""
    try:
        return await _recall(
            db,
            project,
            chapter_no,
            query,
            embedder=embedder,
            excluded=set(excluded_chapter_nos or ()),
            summary_chapters=set(summary_chapter_nos or ()),
            settings=settings,
        )
    except Exception:  # noqa: BLE001
        logger.warning("剧情记忆召回失败（项目 %s，第 %s 章），本次不注入", project.id, chapter_no, exc_info=True)
        return []


async def _recall(
    db: AsyncSession,
    project: Project,
    chapter_no: int,
    query: str,
    *,
    embedder: Embedder | None,
    excluded: set[int],
    summary_chapters: set[int],
    settings: KbSettings | None,
) -> list[RecalledMemory]:
    from app.services import kb_service

    settings = settings or await kb_service.load_settings(db, project.id)
    limit_chars, top_k = settings.memory_max_chars, settings.memory_top_k
    conds = _range_conditions(project.id, chapter_no, excluded, summary_chapters)
    picked: list[RecalledMemory] = []
    used = 0

    def take(m: StoryMemory, rank: int) -> bool:
        nonlocal used
        line = memory_line(m)
        if used + len(line) + (1 if picked else 0) > limit_chars:
            return False
        used += len(line) + (1 if picked else 0)
        picked.append(RecalledMemory(memory=m, rank=rank, line=line))
        return True

    pinned_rows = await db.scalars(
        select(StoryMemory)
        .where(*conds, StoryMemory.pinned.is_(True), StoryMemory.muted.is_(False))
        .order_by(StoryMemory.chapter_no, StoryMemory.id)
    )
    for m in pinned_rows:
        if not take(m, -1):
            break
    if query.strip() and len(picked) < top_k and used < limit_chars:
        excluded_ids = {r.memory.id for r in picked}
        if summary_chapters:
            excluded_ids |= set(
                await db.scalars(
                    select(StoryMemory.id).where(
                        StoryMemory.project_id == project.id,
                        StoryMemory.kind == "summary",
                        StoryMemory.chapter_no.in_(sorted(summary_chapters)),
                    )
                )
            )
        result = await retrieval_service.search(
            db,
            project.id,
            "memory",
            query,
            top_k=top_k * 2,
            min_score=settings.memory_min_score,
            embedder=embedder,
            filters=SearchFilter(chapter_lt=chapter_no, exclude_chapter_nos=excluded, exclude_ref_ids=excluded_ids),
        )
        rows = {
            m.id: m
            for m in await db.scalars(
                select(StoryMemory).where(
                    *conds, StoryMemory.muted.is_(False), StoryMemory.id.in_([h.ref_id for h in result.hits])
                )
            )
        }
        for rank, hit in enumerate(result.hits):
            if len(picked) >= top_k:
                break
            m = rows.get(hit.ref_id)
            if m is not None:
                take(m, rank)
    picked.sort(key=lambda r: (r.memory.chapter_no, r.memory.id))
    return picked


def format_lines(items: list[RecalledMemory]) -> str:
    return "\n".join(r.line for r in items)


async def search_debug(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    req: MemorySearchRequest,
    *,
    env: retrieval_service.EmbedEnv,
) -> MemorySearchResult:
    from app.services import kb_service

    await get_owned_project(db, user_id, project_id)
    embedder = await env.embedder(db, user_id)
    settings = await kb_service.load_settings(db, project_id)
    result = await retrieval_service.search(
        db,
        project_id,
        "memory",
        req.query,
        top_k=req.top_k or settings.memory_top_k,
        min_score=req.min_score if req.min_score is not None else settings.memory_min_score,
        embedder=embedder,
        filters=SearchFilter(enabled_only=not req.include_muted, chapter_lt=req.before_chapter_no),
    )
    rows = {
        m.id: m
        for m in await db.scalars(
            select(StoryMemory).where(
                StoryMemory.project_id == project_id, StoryMemory.id.in_([h.ref_id for h in result.hits])
            )
        )
    }
    return MemorySearchResult(
        hits=[
            MemorySearchHit(
                memory=MemoryOut.model_validate(rows[h.ref_id]),
                score=h.score,
                vector_score=h.vector_score,
                keyword_score=h.keyword_score,
            )
            for h in result.hits
            if h.ref_id in rows
        ],
        mode=result.mode,
        warnings=result.warnings,
        candidates=result.candidates,
    )
