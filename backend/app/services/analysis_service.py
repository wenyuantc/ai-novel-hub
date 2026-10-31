"""章节分析（后台任务 chapter.analyze）：提示词组装、运行与解析、落库、客观指标、过期判断、状态与历史查询、排队。

- 每次分析单独保存一行 chapter_analyses（历史保留）；章节的最新分析 = max(id)。
- 过期：最新分析的 content_hash（分析时正文的 sha256）与当前正文的 hash 不同。
- 状态：有排队 / 运行中的 chapter.analyze 任务 → analyzing；没有分析 → none；hash 相同 → analyzed；否则 stale。
- 排队：分析任务 serial_key = analysis:{project_id}，同项目串行、按入队顺序执行，不同项目并行；不写生成记录。
  enqueue_auto（AI 生成 / 润色 / 重写成功后的 on_success）以项目所有者为 user_id 直接入队，不做 AI 配置的请求级校验，
  配置在运行时解析；同章已有排队中的分析则不再入队（运行中的不算，便于分析最新正文）。
- 设定回写见 writeback_service（与分析结果同一事务落库）；剧情记忆见 memory_service.sync_from_analysis（同一事务），
  任务成功后有向量模型时排队 kb.index 记忆任务生成向量（AnalysisHandler.on_success）。
- 评分 overall 由服务端对 pacing / engagement / coherence 三项求平均后复算；metrics 由本地规则计算，不依赖模型。
"""

import hashlib
import logging
import re
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts import build_analysis_messages
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.core.security import SecretCipher
from app.models import Chapter, ChapterAnalysis, Character, Organization, Project, Task
from app.models.entities import ACTIVE_TASK_STATUSES, LIFE_STATUSES, MEMBER_STATUSES, RELATION_STATUSES
from app.schemas.analysis import (
    AnalysisBrief,
    AnalysisChangeOut,
    AnalysisOut,
    AnalyzePendingResult,
    AnalyzeRequest,
    ChapterAnalysisState,
)
from app.services import (
    ai_config_service,
    career_service,
    cast_service,
    foreshadow_service,
    memory_service,
    organization_service,
    project_lifecycle,
    prompt_service,
    relationship_service,
    task_statistics_service,
    writeback_service,
)
from app.services.chapter_service import count_words, get_owned_chapter
from app.services.context_builder import MEMBER_STATUS_LABELS, format_career_line
from app.services.generation_service import Event
from app.services.project_service import get_owned_project, project_info

if TYPE_CHECKING:
    from app.schemas.task import TaskOut
    from app.services.task_engine.engine import TaskEngine
    from app.services.task_engine.registry import TaskContext

logger = logging.getLogger(__name__)

KIND = "chapter.analyze"
EMPTY_CONTENT = "章节正文为空，无法分析"
SUMMARY_MAX = 1000
TEXT_MAX = 500
MAX_ITEMS = 20
MAX_SUGGESTIONS = 10
MAX_FORESHADOWS = 50
HOOK_TYPES = ("悬念", "冲突", "情感", "反转", "其他")
SUGGESTION_CATEGORIES = ("节奏", "对话", "描写", "冲突", "人物", "其他")
PRIORITIES = ("high", "medium", "low")
SCORE_KEYS = ("pacing", "engagement", "coherence")
LIFE_LABELS = {"alive": "存活", "dead": "已死亡", "missing": "失踪", "other": "其他"}

_LIFE_ALIASES = {"死": "dead", "亡": "dead", "失踪": "missing", "下落不明": "missing", "存活": "alive", "活": "alive"}
_REL_STATUS_ALIASES = {"破裂": "broken", "决裂": "broken", "过去": "past", "复杂": "complicated", "正常": "normal"}
_MEMBER_STATUS_ALIASES = {"离开": "left", "退出": "left", "开除": "expelled", "逐出": "expelled", "阵亡": "dead"}
_PRIORITY_ALIASES = {"高": "high", "中": "medium", "低": "low"}
_ACTION_ALIASES = {"加入": "join", "离开": "leave", "退出": "leave", "变更": "update", "更新": "update"}
_FS_ACTION_ALIASES = {"埋": "plant", "铺": "plant", "回收": "resolve", "揭": "resolve", "收": "resolve"}
_REF_RE = re.compile(r"^#?\s*(\d+)$")
_QUOTE_RE = re.compile(r"「([^」]*)」|『([^』]*)』|“([^”]*)”|\"([^\"]*)\"")
_BLANK_LINE_RE = re.compile(r"\n\s*\n")
_WS_RE = re.compile(r"\s+")


# ---------------- 纯函数：hash / 指标 / 规范化 ----------------


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def compute_metrics(content: str) -> dict[str, Any]:
    """字数（非空白字符）、段落数（按空行切分；全文没有空行时按非空行）、平均段长、对白占比（引号内字数 / 总字数）。"""
    text = (content or "").strip()
    words = count_words(text)
    paragraphs = [p for p in _BLANK_LINE_RE.split(text) if p.strip()]
    if len(paragraphs) <= 1:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) > 1:
            paragraphs = lines
    n = len(paragraphs)
    dialogue = sum(len(_WS_RE.sub("", next(g for g in m.groups() if g is not None))) for m in _QUOTE_RE.finditer(text))
    return {
        "words": words,
        "paragraphs": n,
        "avg_paragraph_chars": round(words / n, 1) if n else 0,
        "dialogue_ratio": round(min(dialogue / words, 1.0), 3) if words else 0,
    }


def _text(value: Any, limit: int = TEXT_MAX) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, list):
        text = "；".join(_text(v, limit) for v in value if _text(v, limit))
    elif isinstance(value, dict):
        text = "；".join(f"{k}：{_text(v, limit)}" for k, v in value.items() if _text(v, limit))
    else:
        text = str(value).strip()
    return text[:limit]


def _opt_text(obj: dict, key: str, limit: int = TEXT_MAX) -> str | None:
    if key not in obj or obj[key] is None:
        return None
    return _text(obj[key], limit)


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp_int(value: Any, low: int, high: int, default: int | None) -> int | None:
    n = _num(value)
    return default if n is None else max(low, min(high, int(round(n))))


def _enum(value: Any, allowed: tuple[str, ...], aliases: dict[str, str]) -> str | None:
    v = _text(value, 50).lower()
    if not v:
        return None
    if v in allowed:
        return v
    for key, mapped in aliases.items():
        if key in v:
            return mapped
    return None


def _dicts(value: Any) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _ref_id(value: Any) -> int | None:
    """只保留整数编号（也接受 "5" / "#5" 这样的纯编号字符串）；其他一律视为没有引用。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        m = _REF_RE.match(value.strip())
        return int(m.group(1)) if m and int(m.group(1)) > 0 else None
    return None


def normalize_foreshadows(value: Any, warnings: list[str]) -> list[dict[str, Any]]:
    """伏笔候选 → {action, ref_id, text, keyword, expected_resolve_chapter, partial}；兼容没有 ref_id 的旧输出。"""
    items = []
    for f in _dicts(value)[:MAX_FORESHADOWS]:
        text = _text(f.get("text") or f.get("content"))
        keyword = _text(f.get("keyword"), 100)
        if not text and not keyword:
            continue
        action = _enum(f.get("action"), ("plant", "resolve"), _FS_ACTION_ALIASES)
        if action is None:
            warnings.append(f"伏笔「{keyword or text[:20]}」的动作「{_text(f.get('action'), 20)}」无法识别，已忽略")
            continue
        expected = _clamp_int(f.get("expected_resolve_chapter"), 1, 100000, None)
        items.append(
            {
                "action": action,
                "ref_id": _ref_id(f.get("ref_id")),
                "text": text,
                "keyword": keyword,
                "expected_resolve_chapter": expected if expected and expected >= 1 else None,
                "partial": f.get("partial") is True,
            }
        )
    return items


def normalize_scores(value: Any) -> dict[str, float | None]:
    """三项截断到 1–10（一位小数）；overall = 有值项的平均（服务端复算，忽略模型给出的 overall）。"""
    obj = value if isinstance(value, dict) else {}
    scores: dict[str, float | None] = {}
    for key in SCORE_KEYS:
        n = _num(obj.get(key))
        scores[key] = None if n is None else round(max(1.0, min(10.0, n)), 1)
    present = [v for v in scores.values() if v is not None]
    return {"overall": round(sum(present) / len(present), 1) if present else None, **scores}


def normalize_analysis(obj: Any) -> tuple[dict[str, Any], list[str]]:
    """模型输出 → 规范化结构与 warnings。枚举 / 取值范围越界截断，非法项丢弃并记 warning。"""
    if not isinstance(obj, dict):
        raise JSONExtractError("JSON 结构不符合预期")
    warnings: list[str] = []

    plot_points = [
        {"text": _text(p.get("text") or p.get("content")), "importance": _clamp_int(p.get("importance"), 1, 5, 3)}
        for p in _dicts(obj.get("plot_points"))
    ]
    plot_points = [p for p in plot_points if p["text"]][:MAX_ITEMS]
    hooks = []
    for h in _dicts(obj.get("hooks"))[:MAX_ITEMS]:
        text = _text(h.get("text") or h.get("content"))
        if not text:
            continue
        htype = _text(h.get("type"), 10)
        hooks.append(
            {
                "type": htype if htype in HOOK_TYPES else "其他",
                "strength": _clamp_int(h.get("strength"), 1, 10, 5),
                "text": text,
            }
        )
    raw_conflict = obj.get("conflict") if isinstance(obj.get("conflict"), dict) else {}
    types = raw_conflict.get("types")
    conflict = {
        "level": _clamp_int(raw_conflict.get("level"), 1, 10, None),
        "types": [t for t in (_text(x, 50) for x in types) if t][:10] if isinstance(types, list) else [],
    }
    raw_emotion = obj.get("emotion") if isinstance(obj.get("emotion"), dict) else {}
    emotion = {k: _text(raw_emotion.get(k), 100) for k in ("tone", "start", "middle", "end")}

    character_changes = []
    for c in _dicts(obj.get("character_changes")):
        name = _text(c.get("name"), 100)
        if not name:
            continue
        item: dict[str, Any] = {"name": name, "current_state": _opt_text(c, "current_state", 2000), "life_status": None}
        if c.get("life_status") not in (None, ""):
            life = _enum(c.get("life_status"), LIFE_STATUSES, _LIFE_ALIASES)
            if life is None:
                warnings.append(f"角色「{name}」的存活状态「{_text(c.get('life_status'), 20)}」无法识别，已忽略")
            item["life_status"] = life
        if item["current_state"] is not None or item["life_status"] is not None:
            character_changes.append(item)

    relationship_changes = []
    for r in _dicts(obj.get("relationship_changes")):
        a, b = _text(r.get("a"), 100), _text(r.get("b"), 100)
        if not a or not b:
            continue
        status = None
        if r.get("status") not in (None, ""):
            status = _enum(r.get("status"), RELATION_STATUSES, _REL_STATUS_ALIASES)
            if status is None:
                warnings.append(f"「{a}」与「{b}」的关系状态「{_text(r.get('status'), 20)}」无法识别，已忽略")
        new_rel = None
        if isinstance(r.get("new_relation"), dict):
            nr = r["new_relation"]
            role_a = _text(nr.get("role_a"), 20)
            role_b = _text(nr.get("role_b"), 20) or role_a
            if role_a:
                new_rel = {
                    "role_a": role_a,
                    "role_b": role_b,
                    "category": _text(nr.get("category"), 16).lower() or "other",
                }
        relationship_changes.append(
            {
                "a": a,
                "b": b,
                "intimacy": _clamp_int(r.get("intimacy"), -100, 100, None),
                "status": status,
                "description": _opt_text(r, "description", 2000),
                "new_relation": new_rel,
            }
        )

    career_changes = []
    for c in _dicts(obj.get("career_changes")):
        item = {
            "name": _text(c.get("name"), 100),
            "career": _text(c.get("career"), 100),
            "stage": _text(c.get("stage"), 50),
        }
        if all(item.values()):
            career_changes.append(item)

    organization_changes = []
    for o in _dicts(obj.get("organization_changes")):
        name, org = _text(o.get("name"), 100), _text(o.get("organization"), 100)
        if not name or not org:
            continue
        action = _enum(o.get("action"), ("join", "leave", "update"), _ACTION_ALIASES) or "update"
        mstatus = None
        if o.get("status") not in (None, ""):
            mstatus = _enum(o.get("status"), MEMBER_STATUSES, _MEMBER_STATUS_ALIASES)
            if mstatus is None:
                warnings.append(f"「{name}」在「{org}」的成员状态「{_text(o.get('status'), 20)}」无法识别，已忽略")
        organization_changes.append(
            {
                "name": name,
                "organization": org,
                "action": action,
                "position": _opt_text(o, "position", 50),
                "status": mstatus,
            }
        )

    suggestions = []
    for s in _dicts(obj.get("suggestions")):
        text = _text(s.get("text") or s.get("content"), 1000)
        if not text:
            continue
        category = _text(s.get("category"), 10)
        suggestions.append(
            {
                "id": len(suggestions) + 1,
                "category": category if category in SUGGESTION_CATEGORIES else "其他",
                "priority": _enum(s.get("priority"), PRIORITIES, _PRIORITY_ALIASES) or "medium",
                "text": text,
            }
        )
        if len(suggestions) >= MAX_SUGGESTIONS:
            break

    scores = normalize_scores(obj.get("scores"))
    if scores["overall"] is None:
        warnings.append("模型没有给出评分")
    summary = _text(obj.get("summary"), SUMMARY_MAX)
    if not summary:
        warnings.append("模型没有给出摘要")
    return (
        {
            "summary": summary,
            "plot_points": plot_points,
            "hooks": hooks,
            "conflict": conflict,
            "emotion": emotion,
            "character_changes": character_changes,
            "relationship_changes": relationship_changes,
            "career_changes": career_changes,
            "organization_changes": organization_changes,
            "foreshadows": normalize_foreshadows(obj.get("foreshadows"), warnings),
            "scores": scores,
            "suggestions": suggestions,
        },
        warnings,
    )


# ---------------- 状态 / 历史查询 ----------------


async def latest_analyses(db: AsyncSession, chapter_ids: list[int]) -> dict[int, ChapterAnalysis]:
    """每章最新一次分析（一条查询）。"""
    if not chapter_ids:
        return {}
    latest = (
        select(func.max(ChapterAnalysis.id))
        .where(ChapterAnalysis.chapter_id.in_(chapter_ids))
        .group_by(ChapterAnalysis.chapter_id)
    )
    rows = await db.scalars(select(ChapterAnalysis).where(ChapterAnalysis.id.in_(latest)))
    return {a.chapter_id: a for a in rows}


async def _active_tasks(db: AsyncSession, chapter_ids: list[int]) -> dict[int, int]:
    if not chapter_ids:
        return {}
    rows = await db.execute(
        select(Task.chapter_id, func.max(Task.id))
        .where(Task.kind == KIND, Task.status.in_(ACTIVE_TASK_STATUSES), Task.chapter_id.in_(chapter_ids))
        .group_by(Task.chapter_id)
    )
    return {cid: tid for cid, tid in rows.all()}


def _state(chapter: Chapter, latest: ChapterAnalysis | None, active_task_id: int | None) -> ChapterAnalysisState:
    if active_task_id is not None:
        status = "analyzing"
    elif latest is None:
        status = "none"
    else:
        status = "analyzed" if latest.content_hash == content_hash(chapter.content) else "stale"
    return ChapterAnalysisState(
        chapter_id=chapter.id,
        status=status,
        latest_analysis_id=latest.id if latest else None,
        scores=latest.scores if latest else None,
        analyzed_at=latest.created_at if latest else None,
        active_task_id=active_task_id,
    )


async def _states(db: AsyncSession, chapters: list[Chapter]) -> list[ChapterAnalysisState]:
    ids = [c.id for c in chapters]
    latest = await latest_analyses(db, ids)
    active = await _active_tasks(db, ids)
    return [_state(c, latest.get(c.id), active.get(c.id)) for c in chapters]


async def project_states(db: AsyncSession, user_id: int, project_id: int) -> list[ChapterAnalysisState]:
    await get_owned_project(db, user_id, project_id)
    chapters = list(
        await db.scalars(
            select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_no, Chapter.id)
        )
    )
    return await _states(db, chapters)


async def chapter_state(db: AsyncSession, user_id: int, chapter_id: int) -> ChapterAnalysisState:
    chapter = await get_owned_chapter(db, user_id, chapter_id)
    return (await _states(db, [chapter]))[0]


def _brief(a: ChapterAnalysis, current_hash: str) -> AnalysisBrief:
    return AnalysisBrief.model_validate(a).model_copy(update={"stale": a.content_hash != current_hash})


async def list_analyses(db: AsyncSession, user_id: int, chapter_id: int) -> list[AnalysisBrief]:
    """历史列表（新的在前，不含大字段）。"""
    chapter = await get_owned_chapter(db, user_id, chapter_id)
    current = content_hash(chapter.content)
    rows = await db.scalars(
        select(ChapterAnalysis).where(ChapterAnalysis.chapter_id == chapter.id).order_by(ChapterAnalysis.id.desc())
    )
    return [_brief(a, current) for a in rows]


async def get_owned_analysis(db: AsyncSession, user_id: int, analysis_id: int) -> ChapterAnalysis:
    analysis = await db.scalar(
        select(ChapterAnalysis)
        .join(Project, Project.id == ChapterAnalysis.project_id)
        .where(ChapterAnalysis.id == analysis_id, Project.user_id == user_id, *project_lifecycle.active_conditions())
    )
    if analysis is None:
        raise NotFoundError("分析结果不存在")
    return analysis


async def get_analysis(db: AsyncSession, user_id: int, analysis_id: int) -> AnalysisOut:
    analysis = await get_owned_analysis(db, user_id, analysis_id)
    content = await db.scalar(select(Chapter.content).where(Chapter.id == analysis.chapter_id)) or ""
    brief = _brief(analysis, content_hash(content))
    changes = await writeback_service.list_changes(db, analysis.id)
    return AnalysisOut(
        **brief.model_dump(),
        summary=analysis.summary,
        plot_points=analysis.plot_points,
        hooks=analysis.hooks,
        conflict=analysis.conflict,
        emotion=analysis.emotion,
        changes_raw=analysis.changes_raw,
        foreshadow_candidates=analysis.foreshadow_candidates,
        suggestions=analysis.suggestions,
        warnings=analysis.warnings,
        changes=[AnalysisChangeOut.model_validate(c) for c in changes],
    )


# ---------------- 排队 ----------------


def task_title(chapter: Chapter) -> str:
    from app.services.task_engine.handlers.chapter import chapter_title

    return chapter_title(chapter.chapter_no, chapter.title, "analyze")


def serial_key(project_id: int) -> str:
    return f"analysis:{project_id}"


async def queued_analysis(db: AsyncSession, chapter_id: int) -> Task | None:
    return await db.scalar(
        select(Task).where(Task.kind == KIND, Task.chapter_id == chapter_id, Task.status == "queued").limit(1)
    )


async def request_analyze(
    db: AsyncSession, engine: "TaskEngine", user_id: int, chapter_id: int, req: AnalyzeRequest
) -> tuple["TaskOut", bool]:
    """手动分析：同章已有排队中的分析 → 返回那条任务（created=False）；否则创建任务。"""
    from app.services import task_service

    chapter = await get_owned_chapter(db, user_id, chapter_id, lock=True)
    existing = await queued_analysis(db, chapter.id)
    if existing is not None:
        return await task_service.task_out(db, engine, existing), False
    params = {"chapter_id": chapter.id, **req.model_dump(mode="json")}
    return await task_service.create_task(db, engine, user_id, KIND, params), True


async def pending_chapter_ids(db: AsyncSession, project_id: int) -> list[int]:
    """按 chapter_no 顺序：正文非空、未分析或已过期、且没有排队中 / 运行中分析的章节 id。"""
    chapters = list(
        await db.scalars(
            select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_no, Chapter.id)
        )
    )
    latest = await latest_analyses(db, [c.id for c in chapters])
    queued = set(
        await db.scalars(
            select(Task.chapter_id).where(
                Task.kind == KIND, Task.status.in_(("queued", "running")), Task.project_id == project_id
            )
        )
    )
    targets = []
    for c in chapters:
        if not c.content.strip() or c.id in queued:
            continue
        a = latest.get(c.id)
        if a is None or a.content_hash != content_hash(c.content):
            targets.append(c.id)
    return targets


async def analyze_pending(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, req: AnalyzeRequest
) -> AnalyzePendingResult:
    """一键分析：按 chapter_no 顺序为未分析 / 已过期且正文非空的章节入队。

    跳过已有排队中或运行中分析的章节（状态为「分析中」，不属于未分析 / 已过期），避免重复点击时重复消耗。
    与 enqueue_auto 不同：自动分析允许在运行中再排一次，因为触发它的生成刚改过正文。
    """
    from app.services import task_service

    await get_owned_project(db, user_id, project_id, lock=True)
    task_ids: list[int] = []
    for cid in await pending_chapter_ids(db, project_id):
        params = {"chapter_id": cid, **req.model_dump(mode="json")}
        task_ids.append((await task_service.create_task(db, engine, user_id, KIND, params)).id)
    return AnalyzePendingResult(queued=len(task_ids), task_ids=task_ids)


async def enqueue_pending(db: AsyncSession, project: Project, *, ai_config_id: int | None) -> list[int]:
    """在后台任务里为 pending_chapter_ids 的章节排队分析（拆书导入的深度分析）；返回新任务 id。

    任务上下文里没有引擎与请求：不逐章重走 validate_and_claim（调用方已校验过 AI 配置），直接插入 queued 行，
    同项目串行；当前任务结束时引擎的完成回调会唤醒调度器。不 commit。
    """
    from app.services import task_service

    await project_lifecycle.lock_active(db, project.id)
    ids = await pending_chapter_ids(db, project.id)
    if not ids:
        return []
    chapters = {c.id: c for c in await db.scalars(select(Chapter).where(Chapter.id.in_(ids)))}
    tasks = [
        Task(
            user_id=project.user_id,
            project_id=project.id,
            chapter_id=cid,
            kind=KIND,
            title=task_title(chapters[cid])[:200],
            serial_key=serial_key(project.id),
            status="queued",
            input={"request": {"chapter_id": cid, "ai_config_id": ai_config_id}, "chapter_id": cid},
            stage=task_service.STAGE_LABELS["queued"],
        )
        for cid in ids
    ]
    db.add_all(tasks)
    await db.flush()
    for task in tasks:
        await task_statistics_service.record_created(db, task)
    return [t.id for t in tasks]


async def enqueue_auto(ctx: "TaskContext", project_id: int | None, chapter_id: int | None) -> int | None:
    """AI 生成 / 润色 / 重写成功后自动排队分析。返回新任务 id；不入队时返回 None。

    部署开关或项目开关关闭、章节已删除或正文为空、同章已有排队中的分析 → 不入队。
    任务归属项目所有者（计入其并发上限，可在任务面板看到与取消）；调度由引擎在当前任务结束后唤醒。
    """
    from app.services import task_service

    if project_id is None or chapter_id is None or not ctx.settings.auto_analyze_enabled:
        return None
    async with ctx.sessionmaker() as db:
        project = await db.get(Project, project_id)
        if (project is None or not project.auto_analyze
                or project.deleted_at is not None or project.deletion_started_at is not None):
            return None
        try:
            await project_lifecycle.lock_active(db, project.id)
        except (ConflictError, NotFoundError):
            return None
        chapter = await db.get(Chapter, chapter_id)
        if chapter is None or chapter.project_id != project.id or not chapter.content.strip():
            return None
        if await queued_analysis(db, chapter.id) is not None:
            return None
        task = Task(
            user_id=project.user_id,
            project_id=project.id,
            chapter_id=chapter.id,
            kind=KIND,
            title=task_title(chapter)[:200],
            serial_key=serial_key(project.id),
            status="queued",
            input={"request": {"chapter_id": chapter.id}, "chapter_id": chapter.id, "auto": True},
            stage=task_service.STAGE_LABELS["queued"],
        )
        db.add(task)
        await task_statistics_service.record_created(db, task)
        await db.commit()
        return task.id


# ---------------- 任务：校验 / 组装 / 运行 ----------------


@dataclass
class AnalysisJob:
    project_id: int
    chapter_id: int
    chapter_no: int
    content: str
    content_hash: str
    provider: LLMProvider
    model: str
    messages: list[ChatMessage]
    thinking: ThinkingLevel = "off"
    max_tokens: int | None = None
    task_id: int | None = None


async def validate_analyze(
    db: AsyncSession,
    *,
    user_id: int,
    chapter_id: int,
    ai_config_id: int | None,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
) -> tuple[Chapter, int]:
    """创建任务时同步校验归属、正文与 AI 配置；返回 (章节, ai_config_id)。不修改数据。"""
    chapter = await get_owned_chapter(db, user_id, chapter_id, lock=True)
    if not chapter.content.strip():
        raise BadRequestError(EMPTY_CONTENT)
    cfg = await ai_config_service.resolve_config(db, user_id, ai_config_id)
    await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    return chapter, cfg.id


def _state_card(c: Character, memberships: list, career_line: str) -> str:
    head = f"■ {c.name}"
    if c.aliases:
        head += f"（别名：{'、'.join(c.aliases)}）"
    if c.role_type:
        head += f" · {c.role_type}"
    lines = [head, f"存活状态：{LIFE_LABELS.get(c.life_status, c.life_status)}"]
    lines.append(f"当前状态：{c.current_state.strip() or '（未记录）'}")
    if memberships:
        orgs = []
        for m, o in memberships:
            extra = [x for x in (m.position.strip(), MEMBER_STATUS_LABELS.get(m.status, "")) if x]
            orgs.append(o.name + (f"（{'，'.join(extra)}）" if extra else ""))
        lines.append(f"所属组织：{'、'.join(orgs)}")
    if career_line:
        lines.append(career_line)
    return "\n".join(lines)


async def _involved(db: AsyncSession, chapter: Chapter, characters: list[Character]) -> list[Character]:
    """本章涉及角色：主角 ∪ 手动指定出场 ∪ 本章正文中提及的角色（名字 / 别名，规则同选角）。"""
    manual = set(await cast_service.cast_ids(db, chapter.id))
    mentioned = set(cast_service.match_characters(chapter.content, characters))
    return [c for c in characters if cast_service.is_protagonist(c) or c.id in manual or c.id in mentioned]


async def build_analysis_job(
    db: AsyncSession,
    *,
    user_id: int,
    chapter_id: int,
    ai_config_id: int | None,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
    task_id: int | None = None,
) -> AnalysisJob:
    """任务开始运行时按最新正文与设定组装提示词。"""
    chapter = await get_owned_chapter(db, user_id, chapter_id)
    project = await get_owned_project(db, user_id, chapter.project_id)
    if not chapter.content.strip():
        raise BadRequestError(EMPTY_CONTENT)
    cfg = await ai_config_service.resolve_config(db, user_id, ai_config_id)
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)

    characters = await cast_service.list_characters(db, project.id)
    involved = await _involved(db, chapter, characters)
    ids = [c.id for c in involved]
    by_char: dict[int, list] = {}
    for m, o in await organization_service.memberships_of(db, ids):
        by_char.setdefault(m.character_id, []).append((m, o))
    careers_by_char: dict[int, list] = {}
    involved_careers = {}
    for a, career in await career_service.assignments_of(db, ids):
        careers_by_char.setdefault(a.character_id, []).append((a, career))
        involved_careers.setdefault(career.id, career)
    stages = await career_service.stages_of(db, list(involved_careers))
    names = {c.id: c.name for c in involved}
    relation_lines = [
        f"{relationship_service.describe_relation(names[r.a_id], names[r.b_id], t)}"
        f"（亲密度 {r.intimacy}，{relationship_service.STATUS_LABELS.get(r.status, r.status)}）"
        + (f"：{r.description.strip()}" if r.description.strip() else "")
        for r, t in await relationship_service.relations_among(db, ids)
    ]
    orgs = await db.scalars(
        select(Organization).where(Organization.project_id == project.id).order_by(Organization.sort, Organization.id)
    )
    foreshadows = await foreshadow_service.analysis_candidates(db, project.id, chapter.chapter_no)
    messages = build_analysis_messages(
        await project_info(db, project),
        chapter_no=chapter.chapter_no,
        title=chapter.title,
        content=chapter.content,
        cast_text="\n\n".join(
            _state_card(c, by_char.get(c.id, []), format_career_line(careers_by_char.get(c.id, []), stages))
            for c in involved
        ),
        relations_text="\n".join(relation_lines),
        organizations_text="、".join(o.name for o in orgs),
        careers_text="\n".join(
            career_service.ladder_line(c, stages.get(c.id, []))
            for c in sorted(involved_careers.values(), key=lambda c: (c.sort, c.id))
        ),
        foreshadows_text="\n".join(foreshadow_service.analysis_line(f) for f in foreshadows),
        override=await prompt_service.get_override(db, user_id, "analysis.chapter"),
    )
    return AnalysisJob(
        project_id=project.id,
        chapter_id=chapter.id,
        chapter_no=chapter.chapter_no,
        content=chapter.content,
        content_hash=content_hash(chapter.content),
        provider=provider,
        model=cfg.model,
        messages=messages,
        thinking=cfg.thinking_level,  # type: ignore[arg-type]
        max_tokens=ai_config_service.max_output_tokens(cfg),
        task_id=task_id,
    )


async def run_analysis_job(job: AnalysisJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    """流式输出模型原文（delta）；解析成功后在同一事务内保存分析与回写条目，done 返回概要。"""
    pieces: list[str] = []
    try:
        stream_iter = job.provider.generate_stream(
            job.messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking
        )
        async with aclosing(stream_iter) as stream:
            async for piece in stream:
                pieces.append(piece)
                yield "delta", {"text": piece}
        raw = "".join(pieces)
        if not raw.strip():
            raise AIProviderError("模型未返回任何内容")
        data, warnings = normalize_analysis(extract_json(raw))
        async with sessionmaker() as db:
            chapter = await db.get(Chapter, job.chapter_id)
            if chapter is None:
                yield "error", {"detail": "章节已被删除，分析结果未保存"}
                return
            task_id = job.task_id if job.task_id is not None and await db.get(Task, job.task_id) else None
            analysis = ChapterAnalysis(
                project_id=job.project_id,
                chapter_id=job.chapter_id,
                task_id=task_id,
                content_hash=job.content_hash,
                chapter_no=job.chapter_no,
                summary=data["summary"],
                plot_points=data["plot_points"],
                hooks=data["hooks"],
                conflict=data["conflict"],
                emotion=data["emotion"],
                changes_raw={
                    k: data[k]
                    for k in ("character_changes", "relationship_changes", "career_changes", "organization_changes")
                },
                foreshadow_candidates=data["foreshadows"],
                scores=data["scores"],
                metrics=compute_metrics(job.content),
                suggestions=data["suggestions"],
                warnings=warnings,
                model=job.model[:200],
            )
            db.add(analysis)
            await db.flush()
            changes = await writeback_service.apply_analysis(db, analysis, data)
            # 剧情记忆：替换本章自动记忆（按类型 + 内容继承钉住 / 屏蔽），与分析同一事务
            await memory_service.sync_from_analysis(db, chapter, analysis)
            await db.commit()
            counts: dict[str, int] = {}
            for c in changes:
                counts[c.status] = counts.get(c.status, 0) + 1
            result = {
                "analysis_id": analysis.id,
                "chapter_id": job.chapter_id,
                "chapter_no": job.chapter_no,
                "summary": analysis.summary,
                "scores": analysis.scores,
                "metrics": analysis.metrics,
                "warnings": warnings,
                "change_counts": counts,
            }
        yield "done", result
    except JSONExtractError as exc:
        yield "error", {"detail": f"AI 输出解析失败：{exc}，请重新生成"}
    except AIProviderError as exc:
        yield "error", {"detail": str(exc)}
    except AppError as exc:
        yield "error", {"detail": exc.detail}
    except Exception:  # noqa: BLE001
        logger.exception("章节 %s 分析失败", job.chapter_id)
        yield "error", {"detail": "分析失败：服务器内部错误"}
