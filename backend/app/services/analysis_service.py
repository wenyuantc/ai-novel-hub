"""章节分析（后台任务 chapter.analyze）：提示词组装、运行与解析、落库、客观指标、过期判断、状态与历史查询、排队。

- 每次分析单独保存一行 chapter_analyses（历史保留）；章节的最新分析 = max(id)。
- 过期：最新分析的 content_hash（分析时正文的 sha256）与当前正文的 hash 不同。
- 状态：有排队 / 运行中的 chapter.analyze 任务 → analyzing；没有分析 → none；hash 相同 → analyzed；否则 stale。
- 排队：分析任务 serial_key = analysis:{project_id}，同项目串行、按入队顺序执行，不同项目并行；不写生成记录。
  enqueue_auto（AI 生成 / 润色 / 重写成功后的 on_success）继承原执行任务的真实 actor、受理令牌轮次与冻结模型选择入队，
  任务与配额归该 actor；缺少可信原身份的历史任务失败关闭，不用当前数据库值补签。同章已有排队中的分析则不再入队
  （运行中的不算，便于分析最新正文）。
- 设定回写见 writeback_service（与分析结果同一事务落库）；剧情记忆见 memory_service.sync_from_analysis（同一事务），
  任务成功后有向量模型时按原执行的完成收据受理 kb.index 记忆任务（AnalysisHandler.on_success；批量分析阶段同样以
  原 batch 执行与分析 reference 受理，同一 reference 只受理一次）。
- 评分 overall 由服务端对 pacing / engagement / coherence 三项求平均后复算；metrics 由本地规则计算，不依赖模型。
"""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import and_, false, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.util import identity_key

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts import build_analysis_messages
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError, UnauthorizedError
from app.core.security import SecretCipher
from app.models import Chapter, ChapterAnalysis, Character, Project, ProjectCollaborator, Task, User
from app.models.entities import (
    ACTIVE_TASK_STATUSES,
    LIFE_STATUSES,
    MEMBER_STATUSES,
    OPEN_TASK_STATUSES,
    RELATION_STATUSES,
)
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
    ai_dispatch,
    authorization_service,
    career_service,
    cast_service,
    foreshadow_service,
    memory_service,
    planning_context_service,
    project_access,
    project_ai_config,
    project_lifecycle,
    prompt_service,
    setting_field_service,
    task_statistics_service,
    writeback_service,
)
from app.services.ai_dispatch import (
    VisibilityPolicyError,
    guard_provider,
    read_visibility_stamp,
    visibility_sessionmaker,
)
from app.services.authorization_service import AuthenticatedActor
from app.services.chapter_service import count_words, get_manual_chapter, get_owned_chapter
from app.services.collaboration_task_access import GRANT_KEY, task_grant_identity, task_visible_to_actor
from app.services.context_builder import MEMBER_STATUS_LABELS, format_career_line, resolve_effective_cast
from app.services.generation_service import Event
from app.services.mentions.context import load_candidates
from app.services.project_access import require_project, resolve_access
from app.services.project_service import get_owned_project, project_info
from app.services.setting_ai_context import build_scope
from app.services.setting_field_formatting import format_entry_fields
from app.services.system_settings_snapshot import settings_for_consumer
from app.services.task_engine.execution_identity import TaskExecutionIdentity, require_live_worker

if TYPE_CHECKING:
    from app.schemas.task import TaskOut
    from app.services.collaboration_ai_guard import SendGuard
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
ANALYSIS_KEYWORD_WARNING = "未配置向量模型，剧情记忆将使用关键词检索。"
ANALYSIS_OWNER_EMBEDDING_WARNING = "作者未授权使用共享向量模型，剧情记忆将使用关键词检索。"
ANALYSIS_INDEX_PENDING_WARNING = "剧情记忆向量索引暂未生成，请使用关键词检索。"
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
        .join(Chapter, Chapter.id == ChapterAnalysis.chapter_id)
        .where(ChapterAnalysis.chapter_id.in_(chapter_ids), ChapterAnalysis.project_id == Chapter.project_id)
        .group_by(ChapterAnalysis.chapter_id)
    )
    rows = await db.scalars(select(ChapterAnalysis).where(ChapterAnalysis.id.in_(latest)))
    return {a.chapter_id: a for a in rows}


@dataclass(frozen=True)
class _TaskReference:
    task_id: int
    project_id: int
    statistics_key: str
    chapter_id: int | None
    was_open: bool
    queued_at: datetime | None
    grant: tuple[str, int] | None


async def _active_tasks(db: AsyncSession, chapter_ids: list[int], actor_id: int | None) -> dict[int, _TaskReference]:
    if not chapter_ids or actor_id is None:
        return {}
    newest = (
        select(func.max(Task.id))
        .join(Chapter, Chapter.id == Task.chapter_id)
        .where(Task.kind == KIND, Task.status.in_(ACTIVE_TASK_STATUSES), Task.chapter_id.in_(chapter_ids),
               Task.project_id == Chapter.project_id, Task.statistics_key.is_not(None),
               task_visible_to_actor(db, actor_id))
        .group_by(Task.chapter_id)
    )
    with db.no_autoflush:
        rows = await db.execute(select(
            Task.chapter_id, Task.id, Task.project_id, Task.statistics_key, Task.queued_at, Task.input,
        ).where(
            Task.id.in_(newest),
        ))
    return {cid: _TaskReference(tid, project_id, key, cid, True, queued_at, task_grant_identity(task_input))
            for cid, tid, project_id, key, queued_at, task_input in rows}


async def _recheck_task_references(
    db: AsyncSession, actor_id: int, project_id: int, refs: dict[int, _TaskReference], *, active_only: bool = False,
) -> set[int]:
    """新事务、同一 SQL 快照核验本人、成员资格和原任务轮次。"""
    original = db.info.get("authenticated_actor")
    if isinstance(original, AuthenticatedActor) and original.id != actor_id:
        raise UnauthorizedError("未登录或登录已过期")
    visible: set[int] = set()
    factory = visibility_sessionmaker(db)
    async with factory() as verify:
        if isinstance(original, AuthenticatedActor):
            verify.info["authenticated_actor"] = original
        # 不按响应中全部 ID 拼接 IN 参数；作品历史可能超过数据库单语句参数上限。
        condition = and_(Task.project_id == project_id, task_visible_to_actor(verify, actor_id)) if refs else false()
        if active_only:
            condition = and_(condition, Task.kind == KIND, Task.status.in_(ACTIVE_TASK_STATUSES))
        else:
            chapter_ids = {ref.chapter_id for ref in refs.values()}
            if len(chapter_ids) == 1:
                condition = and_(condition, Task.chapter_id == next(iter(chapter_ids)))
        stmt = select(
            User.is_active, User.token_version, Project.id.label("project_id"), Project.user_id.label("owner_id"),
            Project.deleted_at, Project.deletion_started_at, ProjectCollaborator.status.label("member_status"),
            ProjectCollaborator.role.label("member_role"), ProjectCollaborator.invitation_id.label("member_invitation"),
            ProjectCollaborator.revision.label("member_revision"),
            Task.id.label("task_id"), Task.project_id.label("task_project_id"), Task.statistics_key,
            Task.chapter_id, Task.queued_at, Task.input, Task.status.label("task_status"),
        ).select_from(User).outerjoin(Project, Project.id == project_id).outerjoin(
            ProjectCollaborator, and_(ProjectCollaborator.project_id == Project.id,
                                      ProjectCollaborator.user_id == actor_id),
        ).outerjoin(Task, condition).where(User.id == actor_id)
        rows = (await verify.execute(stmt)).all()
    if not rows or not rows[0].is_active or (
        isinstance(original, AuthenticatedActor) and rows[0].token_version != original.token_version
    ):
        raise UnauthorizedError("未登录或登录已过期")
    fact = rows[0]
    if fact.project_id is None or fact.deleted_at is not None or (
        fact.owner_id != actor_id and fact.member_status != "accepted"
    ):
        raise NotFoundError("分析结果不存在")
    if fact.deletion_started_at is not None:
        raise ConflictError("作品正在删除，请稍后重试", extra={"code": "project_deleting"})
    for row in rows:
        if row.task_id is None or row.task_id not in refs:
            continue
        ref = refs[row.task_id]
        if (row.task_project_id, row.statistics_key, row.chapter_id, row.queued_at) != (
            ref.project_id, ref.statistics_key, ref.chapter_id, ref.queued_at,
        ):
            continue
        if ref.was_open:
            if task_grant_identity(row.input) != ref.grant:
                continue
            if fact.owner_id != actor_id and (
                fact.member_role != "editor" or ref.grant != (fact.member_invitation, fact.member_revision)
            ):
                continue
        if not ref.was_open and row.task_status in OPEN_TASK_STATUSES:
            continue
        visible.add(row.task_id)
    return visible


def _state(
    chapter: Chapter, latest: ChapterAnalysis | None, active_task_id: int | None, current_hash: str, *,
    analyzing: bool,
) -> ChapterAnalysisState:
    if analyzing:
        status = "analyzing"
    elif latest is None:
        status = "none"
    else:
        status = "analyzed" if latest.content_hash == current_hash else "stale"
    return ChapterAnalysisState(
        chapter_id=chapter.id,
        status=status,
        latest_analysis_id=latest.id if latest else None,
        scores=latest.scores if latest else None,
        analyzed_at=latest.created_at if latest else None,
        active_task_id=active_task_id,
    )


async def _states(
    db: AsyncSession, chapters: list[Chapter], actor_id: int, project_id: int,
) -> list[ChapterAnalysisState]:
    from app.services.rich_document_ai import analysis_fingerprints

    ids = [c.id for c in chapters]
    latest = await latest_analyses(db, ids)
    active = await _active_tasks(db, ids, actor_id)
    fingerprints = await analysis_fingerprints(db, chapters)
    visible = await _recheck_task_references(db, actor_id, project_id, {ref.task_id: ref for ref in active.values()},
                                             active_only=True)
    return [_state(c, latest.get(c.id), ref.task_id if (ref := active.get(c.id)) and ref.task_id in visible else None,
                   fingerprints[c.id], analyzing=bool(ref and ref.task_id in visible))
            for c in chapters]


async def _task_actor(db: AsyncSession, user_id: int, project_id: int) -> int:
    project, access, _ = await resolve_access(db, user_id, project_id)
    if project.user_id != user_id and not access.is_collaborator:
        raise NotFoundError("分析结果不存在")
    return user_id


async def _manual_chapter(db: AsyncSession, user_id: int, chapter_id: int) -> Chapter:
    with db.no_autoflush:
        project_id = await db.scalar(select(Chapter.project_id).where(Chapter.id == chapter_id))
        if project_id is None:
            raise NotFoundError("章节不存在")
        return await get_manual_chapter(db, user_id, project_id, chapter_id, "content.read")


async def _visible_task_ids(
    db: AsyncSession, actor_id: int | None, analyses: list[ChapterAnalysis],
) -> dict[int, _TaskReference]:
    ids = {analysis.task_id for analysis in analyses if analysis.task_id is not None}
    if actor_id is None or not ids:
        return {}
    pairs = {(analysis.task_id, analysis.project_id, analysis.chapter_id) for analysis in analyses}
    with db.no_autoflush:
        rows = await db.execute(
            select(Task.id, Task.project_id, Task.statistics_key, Task.chapter_id, Task.status,
                   Task.queued_at, Task.input).where(
                Task.id.in_(ids), Task.statistics_key.is_not(None), task_visible_to_actor(db, actor_id),
            )
        )
    return {task_id: _TaskReference(task_id, project_id, key, chapter_id, status in OPEN_TASK_STATUSES,
                                    queued_at, task_grant_identity(task_input))
            for task_id, project_id, key, chapter_id, status, queued_at, task_input in rows
            if (task_id, project_id, chapter_id) in pairs}


async def project_states(
    db: AsyncSession, user_id: int, project_id: int, *, manual: bool = False,
) -> list[ChapterAnalysisState]:
    if manual:
        await require_project(db, user_id, project_id, "content.read")
        actor_id = await _task_actor(db, user_id, project_id)
    else:
        await get_owned_project(db, user_id, project_id)
        actor_id = user_id
    chapters = list(
        await db.scalars(
            select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_no, Chapter.id)
        )
    )
    return await _states(db, chapters, actor_id, project_id)


async def chapter_state(db: AsyncSession, user_id: int, chapter_id: int) -> ChapterAnalysisState:
    chapter = await _manual_chapter(db, user_id, chapter_id)
    return (await _states(db, [chapter], await _task_actor(db, user_id, chapter.project_id), chapter.project_id))[0]


def _brief(a: ChapterAnalysis, current_hash: str, visible_task_ids: set[int]) -> AnalysisBrief:
    return AnalysisBrief.model_validate(a).model_copy(update={
        "stale": a.content_hash != current_hash,
        "task_id": a.task_id if a.task_id in visible_task_ids else None,
    })


async def list_analyses(db: AsyncSession, user_id: int, chapter_id: int) -> list[AnalysisBrief]:
    """历史列表（新的在前，不含大字段）。"""
    from app.services.rich_document_ai import analysis_fingerprints

    chapter = await _manual_chapter(db, user_id, chapter_id)
    actor_id = await _task_actor(db, user_id, chapter.project_id)
    current = (await analysis_fingerprints(db, [chapter]))[chapter.id]
    rows = list(await db.scalars(
        select(ChapterAnalysis).where(
            ChapterAnalysis.chapter_id == chapter.id, ChapterAnalysis.project_id == chapter.project_id,
        ).order_by(ChapterAnalysis.id.desc())
    ))
    refs = await _visible_task_ids(db, actor_id, rows)
    task_ids = await _recheck_task_references(db, actor_id, chapter.project_id, refs)
    return [_brief(a, current, task_ids) for a in rows]


async def get_owned_analysis(db: AsyncSession, user_id: int, analysis_id: int) -> ChapterAnalysis:
    analysis = await db.scalar(
        select(ChapterAnalysis)
        .join(Project, Project.id == ChapterAnalysis.project_id)
        .where(ChapterAnalysis.id == analysis_id, Project.user_id == user_id, *project_lifecycle.active_conditions())
    )
    if analysis is None:
        raise NotFoundError("分析结果不存在")
    return analysis


async def get_analysis(
    db: AsyncSession, user_id: int, analysis_id: int, *, manual: bool = False,
) -> AnalysisOut:
    from app.services.rich_document_ai import analysis_fingerprints

    if manual:
        with db.no_autoflush:
            project_id = await db.scalar(select(ChapterAnalysis.project_id).where(ChapterAnalysis.id == analysis_id))
            if project_id is None:
                raise NotFoundError("分析结果不存在")
            await require_project(db, user_id, project_id, "content.read")
            actor_id = await _task_actor(db, user_id, project_id)
            cached = db.identity_map.get(identity_key(ChapterAnalysis, analysis_id))
            if cached is not None and db.is_modified(cached, include_collections=False):
                raise ConflictError("分析结果已有未提交修改，请先完成当前操作")
            analysis = await db.scalar(select(ChapterAnalysis).where(
                ChapterAnalysis.id == analysis_id, ChapterAnalysis.project_id == project_id,
            ).join(Chapter, Chapter.id == ChapterAnalysis.chapter_id).where(
                Chapter.project_id == project_id,
            ).execution_options(populate_existing=True))
            if analysis is None:
                raise NotFoundError("分析结果不存在")
    else:
        analysis = await get_owned_analysis(db, user_id, analysis_id)
        actor_id = user_id
    chapter = await db.get(Chapter, analysis.chapter_id)
    current = (await analysis_fingerprints(db, [chapter]))[chapter.id] if chapter is not None else content_hash("")
    refs = await _visible_task_ids(db, actor_id, [analysis])
    changes = await writeback_service.list_changes(db, analysis.id, project_id=analysis.project_id if manual else None)
    brief = _brief(analysis, current, await _recheck_task_references(db, actor_id, analysis.project_id, refs))
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


async def queued_analysis(
    db: AsyncSession, chapter_id: int, *, actor_id: int, grant_identity: tuple[str, int] | None = None,
) -> Task | None:
    """只对同一真实用户和原邀请轮次去重，不返回他人的私人任务。"""
    with db.no_autoflush:
        tasks = await db.scalars(select(Task).where(
            Task.kind == KIND, Task.chapter_id == chapter_id, Task.user_id == actor_id, Task.status == "queued",
        ))
        for task in tasks:
            if _personal_analysis_grant_matches(task.input, grant_identity):
                return task
    return None


def _personal_analysis_grant_matches(value: object, grant_identity: tuple[str, int] | None) -> bool:
    if type(value) is not dict:
        return False
    if grant_identity is None:
        return GRANT_KEY not in value
    raw = value.get(GRANT_KEY)
    return (type(raw) is dict and set(raw) == {"invitation_id", "revision"}
            and task_grant_identity(value) == grant_identity)


def _request_actor(db: AsyncSession, user_id: int) -> AuthenticatedActor:
    actor = authorization_service.actor_snapshot(db, user_id)
    if (type(actor) is not AuthenticatedActor or actor.id != user_id
            or type(actor.token_version) is not int or actor.token_version < 0):
        raise ConflictError("原认证身份无法验证", extra={"code": "task_actor_unavailable"})
    return actor


async def _preflight_analysis_model(
    ctx: "TaskContext", actor: AuthenticatedActor, project_id: int, ai_config_id: int | None,
) -> project_ai_config.ProjectModelPreflight:
    """锁外短读：真实 actor 对作品的 ai.use（viewer 403、无协作读取来源 404），再固定文本模型来源。

    模型选择本人优先；仅本人无该类配置且当前 accepted editor 的 allow_owner_ai 为真时才回退作者配置，
    本人配置无效直接报错不回退（project_ai_config.preflight_project_model）。
    """
    async with ctx.sessionmaker() as read:
        await authorization_service.require_authenticated(read, actor)
        await require_project(read, actor.id, project_id, "ai.use")
    return await project_ai_config.preflight_project_model(
        ctx.sessionmaker, actor, project_id, "text", cipher=ctx.cipher, config_id=ai_config_id,
        allow_private=ctx.settings.allow_private_ai_endpoints,
    )


async def _lock_analysis_admission(
    db: AsyncSession, actor: AuthenticatedActor, project_id: int, grant: tuple[str, int] | None,
) -> Project:
    """锁序与中央受理一致：认证/授权门禁（runtime → gate → User）→ 发送写门禁 → 作品；锁后按原 grant 重验。"""
    await authorization_service.require_authenticated(db, actor, write=True)
    with db.no_autoflush:
        return await require_project(
            db, actor.id, project_id, "ai.use", for_update=True, visibility_write=True, grant_identity=grant,
        )


async def request_analyze(
    db: AsyncSession, engine: "TaskEngine", user_id: int, chapter_id: int, req: AnalyzeRequest
) -> tuple["TaskOut", bool]:
    """手动分析：同章已有本人同轮次排队中的分析 → 返回那条任务（created=False）；否则创建任务。

    作者与 accepted editor 共用：先在任何写锁前完成模型预检与 DNS（受理证明），再由中央任务受理在
    同一写事务内消费证明、按原 grant 重选模型并入队；Task 归真实 actor。
    """
    from app.services import task_service
    from app.services.task_engine.admission import prepare_task_admission

    actor = _request_actor(db, user_id)
    params = {"chapter_id": chapter_id, **req.model_dump(mode="json")}
    # 请求认证产生的只读事务在 DNS 前结束，不占用连接或锁。
    await db.rollback()
    ctx = engine.ctx
    async with ctx.sessionmaker() as read:
        await authorization_service.require_authenticated(read, actor)
        project_id = await read.scalar(select(Chapter.project_id).where(Chapter.id == chapter_id))
    if project_id is None:
        raise NotFoundError("章节不存在")
    model = await _preflight_analysis_model(ctx, actor, project_id, req.ai_config_id)
    admission = await prepare_task_admission(ctx, actor, KIND, params, model_preflight=model)
    grant = model.binding.grant_identity
    await _lock_analysis_admission(db, actor, project_id, grant)
    with db.no_autoflush:
        chapter = await db.scalar(select(Chapter.id).where(Chapter.id == chapter_id, Chapter.project_id == project_id))
        if chapter is None:
            raise NotFoundError("章节不存在")
        existing = await queued_analysis(db, chapter_id, actor_id=actor.id, grant_identity=grant)
    if existing is not None:
        return await task_service.task_out(db, engine, existing), False
    return await task_service.create_task(db, engine, user_id, KIND, params, admission=admission), True


async def pending_chapter_ids(
    db: AsyncSession, project_id: int, *, actor_id: int, grant_identity: tuple[str, int] | None = None,
) -> list[int]:
    """按 chapter_no 顺序：正文非空、未分析或已过期、且没有排队中 / 运行中分析的章节 id。"""
    from app.services.rich_document_ai import analysis_fingerprints

    chapters = list(
        await db.scalars(
            select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.chapter_no, Chapter.id)
        )
    )
    latest = await latest_analyses(db, [c.id for c in chapters])
    fingerprints = await analysis_fingerprints(db, chapters)
    rows = await db.execute(select(Task.chapter_id, Task.input).where(
        Task.kind == KIND, Task.status.in_(("queued", "running")), Task.project_id == project_id,
        Task.user_id == actor_id,
    ))
    queued = {row.chapter_id for row in rows if _personal_analysis_grant_matches(row.input, grant_identity)}
    targets = []
    for c in chapters:
        if not c.content.strip() or c.id in queued:
            continue
        a = latest.get(c.id)
        if a is None or a.content_hash != fingerprints[c.id]:
            targets.append(c.id)
    return targets


async def analyze_pending(
    db: AsyncSession, engine: "TaskEngine", user_id: int, project_id: int, req: AnalyzeRequest
) -> AnalyzePendingResult:
    """一键分析：按 chapter_no 顺序为未分析 / 已过期且正文非空的章节入队。

    跳过本人同轮次已有排队中或运行中分析的章节（状态为「分析中」，不属于未分析 / 已过期），避免重复点击时重复消耗。
    与 enqueue_auto 不同：自动分析允许在运行中再排一次，因为触发它的生成刚改过正文。
    候选章节在锁外预检时确定并逐章准备受理证明；锁内重算后只为仍待分析且已有证明的章节入队，
    预检后才新出现的章节留待下次（不在持锁时做 DNS）。
    """
    from app.services import task_service
    from app.services.task_engine.admission import prepare_task_admission

    actor = _request_actor(db, user_id)
    await db.rollback()
    ctx = engine.ctx
    model = await _preflight_analysis_model(ctx, actor, project_id, req.ai_config_id)
    grant = model.binding.grant_identity
    async with ctx.sessionmaker() as read:
        await authorization_service.require_authenticated(read, actor)
        await require_project(read, actor.id, project_id, "ai.use", grant_identity=grant)
        candidates = await pending_chapter_ids(read, project_id, actor_id=actor.id, grant_identity=grant)
    admissions = {}
    for cid in candidates:
        params = {"chapter_id": cid, **req.model_dump(mode="json")}
        admissions[cid] = (params, await prepare_task_admission(ctx, actor, KIND, params, model_preflight=model))
    # 与 request_analyze 相同：门禁先于作品锁，避免与已持门禁的执行体互等。
    await _lock_analysis_admission(db, actor, project_id, grant)
    with db.no_autoflush:
        targets = await pending_chapter_ids(db, project_id, actor_id=actor.id, grant_identity=grant)
    task_ids: list[int] = []
    for cid in targets:
        if cid not in admissions:
            continue
        params, admission = admissions[cid]
        task_ids.append((await task_service.create_task(db, engine, user_id, KIND, params, admission=admission)).id)
    return AnalyzePendingResult(queued=len(task_ids), task_ids=task_ids)


async def enqueue_pending(
    db: AsyncSession, project: Project, *, ai_config_id: int, execution: "TaskExecutionIdentity",
    expected_cursor: int, expected_checkpoint: dict[str, Any], cipher: SecretCipher, allow_private: bool = False,
) -> list[int]:
    """在后台任务里为 pending_chapter_ids 的章节排队分析（拆书导入的深度分析）；返回新任务 id。

    任务上下文里没有引擎与请求：不逐章重走 validate_and_claim（调用方已校验过 AI 配置），直接插入 queued 行，
    同项目串行；当前任务结束时引擎的完成回调会唤醒调度器。不 commit。
    """
    from app.services import task_service
    from app.services.task_engine.execution_identity import matches_running

    try:
        if (db.new or db.dirty or db.deleted or type(execution) is not TaskExecutionIdentity
                or execution.kind != "book.import"
                or execution.project_id != project.id or execution.actor_id != project.user_id
                or execution.grant_identity is not None or type(execution.actor_token_version) is not int
                or execution.actor_token_version < 0 or type(expected_cursor) is not int
                or expected_cursor < execution.step_cursor or type(expected_checkpoint) is not dict
                or expected_checkpoint.get("phase") != "analyze"):
            raise AnalysisExecutionChanged()
        checkpoint_json = json.dumps(
            expected_checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        original = execution.input
        model, selection = analysis_bindings(original, actor_id=execution.actor_id, project_id=execution.project_id)
        if type(ai_config_id) is not int or ai_config_id != model.config_id:
            raise AnalysisExecutionChanged()
        with db.no_autoflush:
            await authorization_service.require_authenticated(
                db, AuthenticatedActor(execution.actor_id, execution.actor_token_version), write=True,
                targets=(model.project_owner_id,),
            )
            locked_project = await project_access.require_project(
                db, execution.actor_id, execution.project_id, "ai.use", for_update=True, visibility_write=True,
            )
            if locked_project.user_id != execution.actor_id:
                raise AnalysisExecutionChanged()
            source = await db.scalar(select(Task).where(
                Task.id == execution.task_id, Task.statistics_key == execution.task_key,
            ).with_for_update().execution_options(populate_existing=True))
            await require_live_worker(db, execution.worker_key)
            step = replace(execution, step_cursor=expected_cursor, checkpoint_json=checkpoint_json)
            if source is None or source.admin_cancel_operation_id is not None or not matches_running(step, source):
                raise AnalysisExecutionChanged()
            await project_ai_config.lock_config_owner(db, execution.actor_id, write=False)
            await project_ai_config.revalidate_project_model(db, model, cipher=cipher, allow_private=allow_private)
            await project_ai_config.revalidate_shared_embedding(
                db, selection, cipher=cipher, allow_private=allow_private,
            )
    except Exception:  # noqa: BLE001  保留固定错误，不从当前行补原任务或模型。
        raise AnalysisExecutionChanged() from None
    ids = await pending_chapter_ids(db, project.id, actor_id=execution.actor_id)
    if not ids:
        return []
    chapters = {c.id: c for c in await db.scalars(select(Chapter).where(Chapter.id.in_(ids)))}
    tasks = [
        Task(
            user_id=execution.actor_id,
            project_id=project.id,
            chapter_id=cid,
            kind=KIND,
            title=task_title(chapters[cid])[:200],
            serial_key=serial_key(project.id),
            status="queued",
            input={"request": {"chapter_id": cid, "ai_config_id": ai_config_id}, "chapter_id": cid,
                   "_task_actor_token_version": execution.actor_token_version,
                   "_project_model": project_ai_config.serialize_project_model(model),
                   "_shared_embedding": project_ai_config.serialize_shared_embedding(selection)},
            stage=task_service.STAGE_LABELS["queued"],
        )
        for cid in ids
    ]
    db.add_all(tasks)
    await db.flush()
    for task in tasks:
        await task_statistics_service.record_created(db, task)
    return [t.id for t in tasks]


async def enqueue_auto(
    ctx: "TaskContext", project_id: int | None, chapter_id: int | None, *, execution: "TaskExecutionIdentity",
) -> int | None:
    """AI 生成 / 润色 / 重写成功后自动排队分析。返回新任务 id；不入队时返回 None。

    部署开关或项目开关关闭、章节已删除或正文为空、同章本人同轮次已有排队中的分析 → 不入队。
    继承原生成任务的 actor、令牌、模型选择与协作授权轮次（E4-g1）；入队前在锁内核验原单章完成收据，
    协作者还须仍是同一 accepted editor 轮次、模型来源授权仍有效。无可信来源或已撤权时抛错不入队
    （由引擎记为成功回调失败，不影响已成功的章节），不能推断为作者任务。
    """
    if project_id is None or chapter_id is None:
        return None
    async with ctx.sessionmaker() as db:
        if not (await settings_for_consumer(db, ctx.settings)).auto_analyze_enabled:
            return None
    from app.services import generation_service

    try:
        if (type(execution) is not TaskExecutionIdentity
                or execution.kind not in {"chapter.generate", "chapter.polish", "chapter.revise"}
                or execution.project_id != project_id or execution.chapter_id != chapter_id
                or type(execution.actor_token_version) is not int or execution.actor_token_version < 0):
            raise AnalysisExecutionChanged()
        grant = execution.grant_identity
        model, selection = analysis_bindings(execution.input, actor_id=execution.actor_id, project_id=project_id)
        if (grant is None) != (model.project_owner_id == execution.actor_id) or model.grant_identity != grant:
            raise AnalysisExecutionChanged()
        actor = AuthenticatedActor(execution.actor_id, execution.actor_token_version)
        async with ctx.sessionmaker() as db:
            with db.no_autoflush:
                await authorization_service.require_authenticated(
                    db, actor, write=True, targets=(model.project_owner_id,),
                )
                project = await project_access.require_project(
                    db, actor.id, project_id, "ai.use", for_update=True, visibility_write=True,
                    grant_identity=grant,
                )
                if project.user_id != model.project_owner_id:
                    raise AnalysisExecutionChanged()
                if not project.auto_analyze:
                    return None
                if await generation_service.completed_success_chapter_receipt(db, execution) is None:
                    raise AnalysisExecutionChanged()
                for owner_id in sorted({actor.id, project.user_id}):
                    await project_ai_config.lock_config_owner(db, owner_id, write=False)
                await project_ai_config.revalidate_project_model(
                    db, model, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
                )
                await project_ai_config.revalidate_shared_embedding(
                    db, selection, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
                )
            task = await _enqueue_auto_locked(db, project, chapter_id, actor, model, selection, grant_identity=grant)
            if task is None:
                return None
            await db.commit()
            return task.id
    except Exception:  # noqa: BLE001
        raise AnalysisExecutionChanged() from None


async def enqueue_auto_in_transaction(
    db: AsyncSession, project: Project, chapter_id: int, *, actor: AuthenticatedActor, cipher: SecretCipher,
    allow_private: bool = False, review_source: object = None,
) -> Task | None:
    """接受提案的真实 HTTP 操作者在同事务内选择模型并排分析。"""
    if (type(actor) is not AuthenticatedActor or db.info.get("authenticated_actor") != actor
            or type(actor.token_version) is not int or actor.token_version < 0):
        raise UnauthorizedError("未登录或登录已过期")
    grant = None
    proposal_source = None
    with db.no_autoflush:
        if review_source is not None:
            from app.services.rich_document_review import (
                ReviewAutoSource,
                require_review_auto_source,
                serialize_applied_review_source,
            )

            if type(review_source) is not ReviewAutoSource or review_source.chapter_id != chapter_id:
                raise AnalysisExecutionChanged()
            locked, original_input, grant = await require_review_auto_source(db, review_source, actor)
            if locked.id != project.id or locked.user_id != review_source.owner_id:
                raise AnalysisExecutionChanged()
            project = locked
            model, selection = analysis_bindings(original_input, actor_id=actor.id, project_id=project.id)
            if (model.project_owner_id != project.user_id or model.grant_identity != grant
                    or selection.grant_identity != grant):
                raise AnalysisExecutionChanged()
            proposal_source = serialize_applied_review_source(review_source)
            try:
                for owner_id in sorted({actor.id, project.user_id}):
                    await project_ai_config.lock_config_owner(db, owner_id, write=False)
                await project_ai_config.revalidate_project_model(db, model, cipher=cipher, allow_private=allow_private)
                await project_ai_config.revalidate_shared_embedding(
                    db, selection, cipher=cipher, allow_private=allow_private,
                )
            except BadRequestError:
                raise AutoAnalysisUnavailable() from None
        else:
            if actor.id != project.user_id:
                raise AnalysisExecutionChanged()
            await authorization_service.require_authenticated(db, actor, write=True)
            project = await project_access.require_project(
                db, actor.id, project.id, "ai.use", for_update=True, visibility_write=True,
            )
            if project.user_id != actor.id:
                raise AnalysisExecutionChanged()
            try:
                model = await project_ai_config.resolve_project_model(
                    db, actor.id, project.id, "text", cipher=cipher, allow_private=allow_private, for_update=True,
                )
                selection = await project_ai_config.resolve_shared_embedding(
                    db, actor.id, project.id, cipher=cipher, allow_private=allow_private, for_update=True,
                )
            except BadRequestError:
                # 仅模型确定不可用时允许上游保留已授权的正文并明确提示；认证和存储错误不能跳过。
                raise AutoAnalysisUnavailable() from None
    return await _enqueue_auto_locked(
        db, project, chapter_id, actor, model, selection, grant_identity=grant, proposal_source=proposal_source,
    )


async def _enqueue_auto_locked(
    db: AsyncSession, project: Project, chapter_id: int, actor: AuthenticatedActor,
    model: project_ai_config.ResolvedProjectModel, selection: project_ai_config.ResolvedSharedEmbedding,
    *, grant_identity: tuple[str, int] | None = None, proposal_source: dict[str, Any] | None = None,
) -> Task | None:
    """调用方已经验证真实 actor、模型来源和作品写屏障，入队与上游提交同事务。"""
    from app.services import task_service

    with db.no_autoflush:
        chapter = await db.get(Chapter, chapter_id)
        if chapter is None or chapter.project_id != project.id or not chapter.content.strip():
            return None
        if await queued_analysis(db, chapter.id, actor_id=actor.id, grant_identity=grant_identity) is not None:
            return None
    task = Task(
        user_id=actor.id,
        project_id=project.id,
        chapter_id=chapter.id,
        kind=KIND,
        title=task_title(chapter)[:200],
        serial_key=serial_key(project.id),
        status="queued",
        input={"request": {"chapter_id": chapter.id, "ai_config_id": model.config_id},
               "chapter_id": chapter.id, "auto": True, "_task_actor_token_version": actor.token_version,
               "_project_model": project_ai_config.serialize_project_model(model),
               "_shared_embedding": project_ai_config.serialize_shared_embedding(selection),
               **({GRANT_KEY: {"invitation_id": grant_identity[0], "revision": grant_identity[1]}}
                  if grant_identity is not None else {}),
               **({"_analysis_proposal_source": deepcopy(proposal_source)} if proposal_source is not None else {})},
        stage=task_service.STAGE_LABELS["queued"],
    )
    db.add(task)
    await task_statistics_service.record_created(db, task)
    return task


# ---------------- 任务：校验 / 组装 / 运行 ----------------


class AnalysisExecutionChanged(VisibilityPolicyError):
    """原任务或授权无法继续证明，不能降级为批量任务的普通分析警告。"""

    def __init__(self) -> None:
        super().__init__("分析任务执行身份或授权已变化，已停止", extra={"code": "analysis_execution_changed"})


class AutoAnalysisUnavailable(Exception):
    """原操作者已验证，但本次没有可用模型来创建自动分析任务。"""


class AnalysisCommitUncertain(Exception):
    """分析结果提交状态未知；保留原执行轮次，不得自动重发或派生索引。"""


@dataclass(frozen=True, slots=True, repr=False)
class AnalysisExecution:
    """由实际领取的 Task 捕获；不得用稍后重读的新任务替旧执行体续签。"""

    task_id: int
    task_key: str
    actor_id: int
    project_id: int
    queued_at: datetime
    execution_key: str
    grant_identity: tuple[str, int] | None
    task_kind: str
    task_chapter_id: int | None = None
    batch_cursor: int | None = None
    input_hash: str = ""
    actor_token_version: int | None = None
    model_binding: project_ai_config.ResolvedProjectModel | None = None
    embedding_selection: project_ai_config.ResolvedSharedEmbedding | None = None
    task_identity: "TaskExecutionIdentity | None" = None
    proposal_source: object = None
    worker_key: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class AnalysisPending:
    execution: AnalysisExecution
    nonce: str


@dataclass(frozen=True, slots=True, repr=False)
class AnalysisReference:
    """批量分析完成的原始持久收据身份；不从后来的 Task 重签。"""

    run_key: str
    task_key: str
    chapter_id: int
    content_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {"run_key": self.run_key, "task_key": self.task_key, "chapter_id": self.chapter_id,
                "content_hash": self.content_hash}


def _execution_uuid(value: object) -> bool:
    try:
        return type(value) is str and str(UUID(value)) == value
    except (TypeError, ValueError):
        return False


def capture_analysis_reference(value: object) -> AnalysisReference:
    if (type(value) is not dict or set(value) != {"run_key", "task_key", "chapter_id", "content_hash"}
            or not _execution_uuid(value["run_key"]) or not _execution_uuid(value["task_key"])
            or type(value["chapter_id"]) is not int or value["chapter_id"] <= 0
            or type(value["content_hash"]) is not str or re.fullmatch(r"[0-9a-f]{64}", value["content_hash"]) is None):
        raise AnalysisExecutionChanged()
    return AnalysisReference(value["run_key"], value["task_key"], value["chapter_id"], value["content_hash"])


def analysis_input_hash(value: object) -> str:
    """将原任务输入固定为类型敏感的 JSON 摘要，不在收据中保存请求正文。"""
    if type(value) is not dict:
        raise AnalysisExecutionChanged()
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise AnalysisExecutionChanged() from None
    return hashlib.sha256(encoded).hexdigest()


def analysis_bindings(
    value: object, *, actor_id: int, project_id: int,
) -> tuple[project_ai_config.ResolvedProjectModel, project_ai_config.ResolvedSharedEmbedding]:
    """只从服务端冻结的原输入读取文本、共享向量及协作授权选择。"""
    if type(value) is not dict:
        raise AnalysisExecutionChanged()
    try:
        raw_model = value.get("_project_model", project_ai_config.PROJECT_MODEL_ABSENT)
        if type(raw_model) is not dict:
            raise AnalysisExecutionChanged()
        owner_id = raw_model.get("project_owner_id")
        grant = task_grant_identity(value)
        if GRANT_KEY in value and grant is None:
            raise AnalysisExecutionChanged()
        model = project_ai_config.deserialize_project_model(
            raw_model, actor_id=actor_id, project_id=project_id, project_owner_id=owner_id,
        )
        selection = project_ai_config.deserialize_shared_embedding(
            value.get("_shared_embedding", project_ai_config.SHARED_EMBEDDING_ABSENT), actor_id=actor_id,
            project_id=project_id, project_owner_id=owner_id,
            task_grant=value.get(GRANT_KEY, project_ai_config.TASK_GRANT_ABSENT),
        )
        if (model is None or model.model_type != "text" or selection is None
                or model.grant_identity != grant or selection.grant_identity != grant):
            raise AnalysisExecutionChanged()
        return model, selection
    except Exception:  # noqa: BLE001  拒绝缺证/损坏绑定，不暴露配置或私有输入。
        raise AnalysisExecutionChanged() from None


def _valid_analysis_bindings(execution: AnalysisExecution) -> bool:
    model, selection = execution.model_binding, execution.embedding_selection
    if (type(model) is not project_ai_config.ResolvedProjectModel
            or type(selection) is not project_ai_config.ResolvedSharedEmbedding
            or model.grant_identity != execution.grant_identity
            or selection.grant_identity != execution.grant_identity):
        return False
    try:
        payload = {"_project_model": project_ai_config.serialize_project_model(model),
                   "_shared_embedding": project_ai_config.serialize_shared_embedding(selection)}
        if execution.grant_identity is not None:
            payload[GRANT_KEY] = {"invitation_id": execution.grant_identity[0],
                                  "revision": execution.grant_identity[1]}
        return analysis_bindings(payload, actor_id=execution.actor_id, project_id=execution.project_id) == (
            model, selection,
        )
    except Exception:  # noqa: BLE001
        return False


def _analysis_proposal_source(value: dict[str, Any], grant: tuple[str, int] | None) -> object:
    """作者不能携带来源；协作者的提案来源存在时必须完整有效。

    E4-g1：协作者手动分析（HTTP 受理时按 grant 鉴权）与单章任务成功后的自动派生（入队时锁内核验原
    完成收据与 grant）不带提案来源，此后每个受控检查点仍按原 grant 重验。
    """
    key = "_analysis_proposal_source"
    if grant is None:
        if key in value:
            raise AnalysisExecutionChanged()
        return None
    if key not in value:
        return None
    try:
        from app.services.rich_document_review import capture_applied_review_source

        return capture_applied_review_source(value.get(key))
    except Exception:  # noqa: BLE001
        raise AnalysisExecutionChanged() from None


def _valid_execution(execution: object) -> bool:
    return bool(
        type(execution) is AnalysisExecution
        and all(type(value) is int and value > 0 for value in (
            execution.task_id, execution.actor_id, execution.project_id,
        ))
        and _execution_uuid(execution.task_key) and _execution_uuid(execution.execution_key)
        and execution.task_key != execution.execution_key and isinstance(execution.queued_at, datetime)
        and (execution.worker_key is None or _execution_uuid(execution.worker_key))
        and (execution.task_identity is None or (
            type(execution.task_identity) is TaskExecutionIdentity
            and execution.task_identity.worker_key == execution.worker_key
        ))
        and type(execution.input_hash) is str and re.fullmatch(r"[0-9a-f]{64}", execution.input_hash) is not None
        and type(execution.actor_token_version) is int and execution.actor_token_version >= 0
        and _valid_analysis_bindings(execution)
        and (execution.grant_identity is not None or execution.proposal_source is None)
        and execution.task_kind in {KIND, "chapter.batch"}
        # E4-g2：协作者批量的逐章分析在原 batch 执行内进行，不带提案来源；每个检查点仍按原 grant 重验。
        and (execution.task_kind == KIND or execution.proposal_source is None)
        and (execution.task_chapter_id is None or (
            type(execution.task_chapter_id) is int and execution.task_chapter_id > 0
        ))
        and (execution.task_kind != KIND or execution.task_chapter_id is not None)
        and ((execution.task_kind == KIND and execution.batch_cursor is None)
             or (execution.task_kind == "chapter.batch" and type(execution.batch_cursor) is int
                 and execution.batch_cursor >= 0))
        and (execution.grant_identity is None or (
            type(execution.grant_identity) is tuple and len(execution.grant_identity) == 2
            and _execution_uuid(execution.grant_identity[0]) and type(execution.grant_identity[1]) is int
            and execution.grant_identity[1] > 0
        ))
    )


async def _lock_analysis_config_owners(db: AsyncSession, execution: AnalysisExecution) -> None:
    """配置门禁先于各模型行，作者与协作者按同一用户顺序取得。"""
    for owner_id in sorted({execution.actor_id, execution.model_binding.project_owner_id}):
        await project_ai_config.lock_config_owner(db, owner_id, write=False)


def capture_analysis_execution(task: Task) -> AnalysisExecution:
    """只从引擎已领取的原 Task 创建快照，不从当前数据库推断新轮次。"""
    return _capture_analysis_execution(task, allow_admin_cancel=False)


def analysis_execution_from_claim(identity: "TaskExecutionIdentity") -> AnalysisExecution:
    """只转换领取事务冻结的身份，不能从后续重读的 Task 推断原输入。"""

    if type(identity) is not TaskExecutionIdentity or identity.kind != KIND:
        raise AnalysisExecutionChanged()
    try:
        original_input = identity.input
        if (not isinstance(original_input, dict)
                or type(original_input.get("_task_actor_token_version")) is not int
                or original_input.get("_task_actor_token_version") != identity.actor_token_version
                or task_grant_identity(original_input) != identity.grant_identity
                or (GRANT_KEY in original_input and identity.grant_identity is None)):
            raise AnalysisExecutionChanged()
        model, selection = analysis_bindings(original_input, actor_id=identity.actor_id, project_id=identity.project_id)
        proposal_source = _analysis_proposal_source(original_input, identity.grant_identity)
        execution = AnalysisExecution(
            task_id=identity.task_id, task_key=identity.task_key, actor_id=identity.actor_id,
            project_id=identity.project_id, queued_at=identity.queued_at, execution_key=identity.execution_key,
            grant_identity=identity.grant_identity, task_kind=identity.kind, task_chapter_id=identity.chapter_id,
            input_hash=analysis_input_hash(original_input), actor_token_version=identity.actor_token_version,
            model_binding=model, embedding_selection=selection,
            task_identity=identity, worker_key=identity.worker_key,
            proposal_source=proposal_source,
        )
    except (TypeError, ValueError):
        raise AnalysisExecutionChanged() from None
    if not _valid_execution(execution):
        raise AnalysisExecutionChanged()
    return execution


def capture_analysis_completion_for_recovery(task: Task) -> AnalysisExecution:
    """系统 drain 对原 running 任务取凭据；取消意图仍须由调用方同事务核验。"""
    return _capture_analysis_execution(task, allow_admin_cancel=True)


def _capture_analysis_execution(task: Task, *, allow_admin_cancel: bool) -> AnalysisExecution:
    if not isinstance(task, Task) or task.status != "running" or not isinstance(task.input, dict):
        raise AnalysisExecutionChanged()
    grant = task_grant_identity(task.input)
    if GRANT_KEY in task.input and grant is None:
        raise AnalysisExecutionChanged()
    model, selection = analysis_bindings(task.input, actor_id=task.user_id, project_id=task.project_id)
    proposal_source = _analysis_proposal_source(task.input, grant)
    execution = AnalysisExecution(
        task.id, task.statistics_key, task.user_id, task.project_id, task.queued_at, task.execution_key,
        grant, task.kind, task.chapter_id, task.step_cursor if task.kind == "chapter.batch" else None,
        analysis_input_hash(task.input), task.input.get("_task_actor_token_version"), model, selection,
        proposal_source=proposal_source, worker_key=task.worker_key,
    )
    if (not _valid_execution(execution)
            or (task.admin_cancel_requested_at is not None
                and (not allow_admin_cancel or task.admin_cancel_operation_id is None)) or (
        task.admin_cancel_operation_id is not None and (
            not allow_admin_cancel or not _execution_uuid(task.admin_cancel_operation_id)
        )
    )):
        raise AnalysisExecutionChanged()
    return execution


async def _lock_analysis_execution(
    db: AsyncSession, execution: AnalysisExecution, *, status: str, read_lock: bool = False,
) -> None:
    """须先持认证及作品屏障；标量查询不从 ORM identity map 猜测任务身份。"""
    if not _valid_execution(execution):
        raise AnalysisExecutionChanged()
    with db.no_autoflush:
        project = await db.scalar(select(Project.user_id).where(
            Project.id == execution.project_id, *project_lifecycle.active_conditions(),
        ).with_for_update(read=read_lock))
        if project != execution.model_binding.project_owner_id:
            raise AnalysisExecutionChanged()
        row = (await db.execute(select(Task.input).where(
            Task.id == execution.task_id, Task.statistics_key == execution.task_key,
            Task.user_id == execution.actor_id, Task.project_id == execution.project_id,
            Task.chapter_id == execution.task_chapter_id,
            Task.kind == execution.task_kind, Task.queued_at == execution.queued_at,
            Task.execution_key == execution.execution_key, Task.status == status,
            Task.worker_key == execution.worker_key,
            Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
        ).with_for_update(read=read_lock))).one_or_none()
        if row is not None:
            await require_live_worker(db, execution.worker_key, read_only=read_lock)
        if (row is None or not isinstance(row.input, dict) or task_grant_identity(row.input) != execution.grant_identity
                or analysis_input_hash(row.input) != execution.input_hash
                or analysis_bindings(row.input, actor_id=execution.actor_id, project_id=execution.project_id)
                != (execution.model_binding, execution.embedding_selection)):
            raise AnalysisExecutionChanged()
        await project_access.require_project(
            db, execution.actor_id, execution.project_id, "ai.use", grant_identity=execution.grant_identity,
        )
        if execution.proposal_source is not None:
            from app.services.rich_document_review import require_applied_review_analysis_source

            await require_applied_review_analysis_source(
                db, execution.proposal_source, execution, read_lock=read_lock,
            )


async def _require_batch_analysis(
    db: AsyncSession, execution: AnalysisExecution, reference: AnalysisReference, *, read_lock: bool,
    status: str = "running", streaming: bool = False,
) -> None:
    """同一 batch 执行中的每章也有独立 attempt，须锁内核验断点及运行收据。

    ``streaming`` 仅供受控任务流帧租约（E5-c2，``read_lock`` 必须为真）：收据为 running，或已由本执行
    （及其同作者同作品的批量祖先）完成；全程共享锁，不升锁。
    """
    if streaming and (not read_lock or status != "running"):
        raise AnalysisExecutionChanged()
    from app.models import SceneReviewAnalysisReceipt

    if (not _valid_execution(execution) or execution.task_kind != "chapter.batch"
            or type(reference) is not AnalysisReference or execution.batch_cursor is None
            or status not in {"running", "completed"}):
        raise AnalysisExecutionChanged()
    if capture_analysis_reference(reference.as_dict()) != reference:
        raise AnalysisExecutionChanged()
    with db.no_autoflush:
        row = (await db.execute(select(Task.step_cursor, Task.checkpoint).where(
            Task.id == execution.task_id, Task.statistics_key == execution.task_key,
            Task.user_id == execution.actor_id, Task.project_id == execution.project_id,
            Task.kind == execution.task_kind, Task.queued_at == execution.queued_at,
            Task.execution_key == execution.execution_key, Task.status == "running",
            Task.worker_key == execution.worker_key,
            Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
        ).with_for_update(read=read_lock))).one_or_none()
        if row is None or row.step_cursor != execution.batch_cursor or not isinstance(row.checkpoint, dict):
            raise AnalysisExecutionChanged()
        checkpoint = row.checkpoint
        items = checkpoint.get("items")
        if (checkpoint.get("phase") != "analyze" or not isinstance(items, list)
                or execution.batch_cursor >= len(items) or not isinstance(items[execution.batch_cursor], dict)
                or type(items[execution.batch_cursor].get("chapter_id")) is not int
                or items[execution.batch_cursor]["chapter_id"] != reference.chapter_id
                or json.dumps(checkpoint.get("rich_analysis"), sort_keys=True, separators=(",", ":"))
                != json.dumps(reference.as_dict(), sort_keys=True, separators=(",", ":"))):
            raise AnalysisExecutionChanged()
        receipt = (await db.execute(select(
            SceneReviewAnalysisReceipt.status, SceneReviewAnalysisReceipt.analysis_id,
        ).join(Chapter, Chapter.id == SceneReviewAnalysisReceipt.chapter_id).where(
            SceneReviewAnalysisReceipt.run_key == reference.run_key,
            SceneReviewAnalysisReceipt.task_key == reference.task_key,
            SceneReviewAnalysisReceipt.chapter_id == reference.chapter_id,
            SceneReviewAnalysisReceipt.content_hash == reference.content_hash,
            Chapter.project_id == execution.project_id,
        ).with_for_update(read=read_lock))).one_or_none()
    if streaming and receipt is not None and receipt.status == "completed":
        status = "completed"
    if receipt is None or receipt.status != status:
        raise AnalysisExecutionChanged()
    ancestors: set[int] = set()
    cursor_id: int | None = execution.task_id
    origin = None
    with db.no_autoflush:
        # retry_of_id 的数据库 FK 删除时会置空；只沿真实同作者/作品的 batch 祖先追到原 UUID。
        for _ in range(256):
            if cursor_id is None or cursor_id in ancestors:
                raise AnalysisExecutionChanged()
            entry = (await db.execute(select(
                Task.id, Task.statistics_key, Task.retry_of_id, Task.checkpoint, Task.status, Task.execution_key,
                Task.admin_cancel_operation_id, Task.admin_cancel_requested_at,
            ).where(
                Task.id == cursor_id, Task.user_id == execution.actor_id, Task.project_id == execution.project_id,
                Task.kind == "chapter.batch",
            ).with_for_update(read=True))).one_or_none()
            if entry is None:
                raise AnalysisExecutionChanged()
            ancestors.add(entry.id)
            if entry.id != execution.task_id and (
                entry.status not in {"failed", "cancelled", "interrupted"} or entry.execution_key is not None
                or entry.admin_cancel_operation_id is not None or entry.admin_cancel_requested_at is not None
            ):
                raise AnalysisExecutionChanged()
            if entry.statistics_key == reference.task_key:
                origin = entry
                break
            cursor_id = entry.retry_of_id
    if (origin is None or not isinstance(origin.checkpoint, dict)
            or json.dumps(origin.checkpoint.get("rich_analysis"), sort_keys=True, separators=(",", ":"))
            != json.dumps(reference.as_dict(), sort_keys=True, separators=(",", ":"))):
        raise AnalysisExecutionChanged()
    if status == "running":
        if receipt.analysis_id is not None:
            raise AnalysisExecutionChanged()
        return
    if type(receipt.analysis_id) is not int or receipt.analysis_id <= 0:
        raise AnalysisExecutionChanged()
    with db.no_autoflush:
        analysis = (await db.execute(select(ChapterAnalysis.task_id).where(
            ChapterAnalysis.id == receipt.analysis_id, ChapterAnalysis.project_id == execution.project_id,
            ChapterAnalysis.chapter_id == reference.chapter_id, ChapterAnalysis.content_hash == reference.content_hash,
        ).with_for_update(read=streaming))).one_or_none()
        if analysis is None or analysis.task_id is None:
            raise AnalysisExecutionChanged()
        if analysis.task_id not in ancestors:
            raise AnalysisExecutionChanged()


async def require_completed_batch_analysis(
    db: AsyncSession, execution: AnalysisExecution, reference: AnalysisReference,
) -> None:
    """索引派生时在已持作品/当前 Task 锁的事务中证明本章分析已完成。"""
    await _require_batch_analysis(db, execution, reference, read_lock=False, status="completed")


async def _analysis_write_barrier(
    db: AsyncSession, execution: AnalysisExecution, *, status: str = "running",
) -> None:
    """认证/User → 发送写门禁 → 作品 → 原任务，持锁至提交。"""
    try:
        with db.no_autoflush:
            await authorization_service.require_authenticated(
                db, AuthenticatedActor(execution.actor_id, execution.actor_token_version), write=True,
                targets=(execution.model_binding.project_owner_id,),
            )
            project = await project_access.require_project(
                db, execution.actor_id, execution.project_id, "ai.use", for_update=True, visibility_write=True,
                grant_identity=execution.grant_identity,
            )
            if project.user_id != execution.model_binding.project_owner_id:
                raise AnalysisExecutionChanged()
            await _lock_analysis_execution(db, execution, status=status)
    except Exception:  # noqa: BLE001  授权和存储异常均失败关闭，避免将 SQL 或私有参数变成批量警告。
        raise AnalysisExecutionChanged() from None


async def _analysis_initial_check(db: AsyncSession, execution: AnalysisExecution) -> None:
    """只做构建前筛选；真正授权必须在发送 lease 或写入事务锁内重新检查。"""
    if not _valid_execution(execution):
        raise AnalysisExecutionChanged()
    try:
        with db.no_autoflush:
            row = (await db.execute(select(User.is_active, Project.user_id, Task.input).select_from(Task).join(
                Project, Project.id == Task.project_id,
            ).join(User, User.id == Task.user_id).where(
                Task.id == execution.task_id, Task.statistics_key == execution.task_key,
                Task.user_id == execution.actor_id, Task.project_id == execution.project_id,
                Task.chapter_id == execution.task_chapter_id,
                Task.kind == execution.task_kind, Task.queued_at == execution.queued_at,
                Task.execution_key == execution.execution_key, Task.status == "running",
                Task.worker_key == execution.worker_key,
                Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
                User.token_version == execution.actor_token_version,
                *project_lifecycle.active_conditions(),
            ))).one_or_none()
        if (row is None or not row.is_active or row.user_id != execution.model_binding.project_owner_id
                or not isinstance(row.input, dict) or task_grant_identity(row.input) != execution.grant_identity
                or analysis_input_hash(row.input) != execution.input_hash
                or analysis_bindings(row.input, actor_id=execution.actor_id, project_id=execution.project_id)
                != (execution.model_binding, execution.embedding_selection)):
            raise AnalysisExecutionChanged()
        await project_access.require_project(
            db, execution.actor_id, execution.project_id, "ai.use", grant_identity=execution.grant_identity,
        )
    except Exception:  # noqa: BLE001
        raise AnalysisExecutionChanged() from None


async def _require_analysis_pending(db: AsyncSession, pending: AnalysisPending) -> None:
    """发送 lease 的 Task 锁内复验准备凭据，不能在凭据被移除后继续出站。"""
    execution = pending.execution
    with db.no_autoflush:
        checkpoint = await db.scalar(select(Task.checkpoint).where(
            Task.id == execution.task_id, Task.statistics_key == execution.task_key,
            Task.queued_at == execution.queued_at, Task.execution_key == execution.execution_key,
            Task.worker_key == execution.worker_key,
            Task.status == "running",
        ).with_for_update(read=True))
    if (not isinstance(checkpoint, dict) or "_analysis_completion" in checkpoint
            or checkpoint.get("_analysis_pending") != _pending_document(pending)):
        raise AnalysisCommitUncertain("分析准备凭据已变化，请核对")


def _pending_document(pending: AnalysisPending) -> dict[str, Any]:
    execution = pending.execution
    queued = execution.queued_at
    if queued.tzinfo is None:
        queued = queued.replace(tzinfo=UTC)
    return {
        "schema": 1, "task_id": execution.task_id, "task_key": execution.task_key,
        "actor_id": execution.actor_id, "project_id": execution.project_id,
        "chapter_id": execution.task_chapter_id, "queued_at": queued.astimezone(UTC).isoformat(),
        "execution_key": execution.execution_key, "task_kind": execution.task_kind,
        "grant": list(execution.grant_identity) if execution.grant_identity is not None else None,
        "input_hash": execution.input_hash, "actor_token_version": execution.actor_token_version,
        "nonce": pending.nonce,
    }


async def _system_receipt_task(db: AsyncSession, execution: AnalysisExecution, *, status: str = "running"):
    """系统收据核对/清理只触碰原任务断点，先锁作品再锁任务。"""
    with db.no_autoflush:
        project = await db.execute(update(Project).where(
            Project.id == execution.project_id, Project.user_id == execution.model_binding.project_owner_id,
        ).values(
            id=Project.id, updated_at=Project.updated_at,
        ).execution_options(synchronize_session=False))
        if project.rowcount != 1:
            return None
        row = (await db.execute(select(Task.checkpoint, Task.input, Task.result).where(
            Task.id == execution.task_id, Task.statistics_key == execution.task_key,
            Task.user_id == execution.actor_id, Task.project_id == execution.project_id,
            Task.chapter_id == execution.task_chapter_id, Task.kind == execution.task_kind,
            Task.queued_at == execution.queued_at, Task.execution_key == execution.execution_key,
            Task.worker_key == execution.worker_key,
            Task.status == status,
        ).with_for_update())).one_or_none()
        if row is not None:
            await require_live_worker(db, execution.worker_key)
        return row


async def begin_analysis_pending(
    sessionmaker: async_sessionmaker[AsyncSession], execution: AnalysisExecution,
) -> AnalysisPending:
    """模型或向量可能出站前先持久标记原运行体；确认丢失绝不继续发送。"""
    if not _valid_execution(execution) or execution.task_kind != KIND:
        raise AnalysisExecutionChanged()
    pending = AnalysisPending(execution, str(uuid4()))
    try:
        async with sessionmaker() as db:
            await _analysis_write_barrier(db, execution)
            row = await _system_receipt_task(db, execution)
            if row is None or (row.checkpoint is not None and not isinstance(row.checkpoint, dict)):
                raise AnalysisCommitUncertain()
            if (execution.task_identity is None
                    or json.dumps(row.checkpoint, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                  allow_nan=False) != execution.task_identity.checkpoint_json):
                raise AnalysisExecutionChanged()
            checkpoint = deepcopy(row.checkpoint or {})
            if "_analysis_pending" in checkpoint or "_analysis_completion" in checkpoint:
                raise AnalysisCommitUncertain()
            checkpoint["_analysis_pending"] = _pending_document(pending)
            changed = await db.execute(update(Task).where(
                Task.id == execution.task_id, Task.statistics_key == execution.task_key,
                Task.queued_at == execution.queued_at, Task.execution_key == execution.execution_key,
                Task.worker_key == execution.worker_key,
                Task.status == "running",
            ).values(checkpoint=checkpoint).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise AnalysisCommitUncertain()
            try:
                await db.commit()
            except BaseException:  # 包括 commit 期间取消，不能推断 pending 未提交。
                raise AnalysisCommitUncertain("分析准备结果未知，请核对") from None
    except AnalysisExecutionChanged:
        raise
    except Exception:  # noqa: BLE001  marker 可能已提交；不能继续发送或开放自动重试。
        raise AnalysisCommitUncertain("分析准备结果未知，请核对") from None
    return pending


async def discard_analysis_pending(
    sessionmaker: async_sessionmaker[AsyncSession], pending: AnalysisPending,
) -> None:
    """仅在明确未尝试结果提交时系统清理本次 pending，不写作品数据。"""
    try:
        async with sessionmaker() as db:
            row = await _system_receipt_task(db, pending.execution)
            if row is None:
                return  # 原任务已消失或换轮次，不能碰替代行。
            if (not isinstance(row.checkpoint, dict) or "_analysis_completion" in row.checkpoint
                    or row.checkpoint.get("_analysis_pending") != _pending_document(pending)):
                raise AnalysisCommitUncertain()
            checkpoint = deepcopy(row.checkpoint)
            checkpoint.pop("_analysis_pending")
            identity = pending.execution.task_identity
            original = identity.checkpoint if identity is not None else None
            if _result_digest(checkpoint) != _result_digest(original or {}):
                raise AnalysisCommitUncertain()
            changed = await db.execute(update(Task).where(
                Task.id == pending.execution.task_id, Task.statistics_key == pending.execution.task_key,
                Task.queued_at == pending.execution.queued_at, Task.execution_key == pending.execution.execution_key,
                Task.worker_key == pending.execution.worker_key,
                Task.status == "running",
            ).values(checkpoint=original).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise AnalysisCommitUncertain()
            try:
                await db.commit()
            except BaseException:
                raise AnalysisCommitUncertain("分析状态清理结果未知，请核对") from None
    except Exception:  # noqa: BLE001
        raise AnalysisCommitUncertain("分析状态清理结果未知，请核对") from None


def _result_digest(result: dict[str, Any]) -> str:
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


_ANALYSIS_RECEIPT_FIELDS = (
    "id", "project_id", "chapter_id", "task_id", "chapter_no", "content_hash", "summary", "plot_points",
    "hooks", "conflict", "emotion", "changes_raw", "foreshadow_candidates", "scores", "metrics",
    "suggestions", "warnings", "model", "created_at",
)


def _analysis_digest(analysis: Any) -> str:
    values = {key: getattr(analysis, key) for key in _ANALYSIS_RECEIPT_FIELDS}
    created = values["created_at"]
    if not isinstance(created, datetime):
        raise AnalysisCommitUncertain("分析身份无法验证，请核对")
    created = created.replace(tzinfo=UTC) if created.tzinfo is None else created.astimezone(UTC)
    values["created_at"] = created.isoformat()
    return _result_digest(values)


async def _write_analysis_completion(
    db: AsyncSession, pending: AnalysisPending, analysis: ChapterAnalysis, result: dict[str, Any],
) -> dict[str, Any]:
    row = await _system_receipt_task(db, pending.execution)
    if (row is None or not isinstance(row.checkpoint, dict) or "_analysis_completion" in row.checkpoint
            or row.checkpoint.get("_analysis_pending") != _pending_document(pending)):
        raise AnalysisCommitUncertain("分析凭据已变化，请核对")
    receipt = {
        **_pending_document(pending), "analysis_id": analysis.id, "chapter_no": analysis.chapter_no,
        "content_hash": analysis.content_hash, "result_hash": _result_digest(result),
        "analysis_hash": _analysis_digest(analysis),
        "change_counts": deepcopy(result["change_counts"]),
    }
    checkpoint = deepcopy(row.checkpoint)
    checkpoint.pop("_analysis_pending")
    checkpoint["_analysis_completion"] = receipt
    changed = await db.execute(update(Task).where(
        Task.id == pending.execution.task_id, Task.statistics_key == pending.execution.task_key,
        Task.queued_at == pending.execution.queued_at, Task.execution_key == pending.execution.execution_key,
        Task.worker_key == pending.execution.worker_key,
        Task.status == "running",
    ).values(checkpoint=checkpoint).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise AnalysisCommitUncertain("分析凭据已变化，请核对")
    return receipt


async def completed_analysis_receipt(
    db: AsyncSession, execution: AnalysisExecution, *, status: str = "running",
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """在同一锁事务核验原 Task、分析父归属和结果哈希；不能靠整数 ID 补身份。"""
    if (not _valid_execution(execution) or execution.task_kind != KIND
            or status not in {"running", "succeeded"}):
        return None
    row = await _system_receipt_task(db, execution, status=status)
    verified = await _validated_analysis_receipt(db, execution, row)
    if status == "succeeded" and (
        verified is None or not isinstance(row.result, dict)
        or json.dumps(row.result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        != json.dumps(verified[1], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ):
        return None
    return verified


async def _validated_analysis_receipt(
    db: AsyncSession, execution: AnalysisExecution, row: Any,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if (row is None or not isinstance(row.input, dict) or task_grant_identity(row.input) != execution.grant_identity
            or not isinstance(row.checkpoint, dict) or "_analysis_pending" in row.checkpoint):
        return None
    try:
        if (analysis_input_hash(row.input) != execution.input_hash
                or analysis_bindings(row.input, actor_id=execution.actor_id, project_id=execution.project_id)
                != (execution.model_binding, execution.embedding_selection)):
            return None
    except AnalysisExecutionChanged:
        return None
    receipt = row.checkpoint.get("_analysis_completion")
    if not isinstance(receipt, dict) or not _execution_uuid(receipt.get("nonce")):
        return None
    pending = AnalysisPending(execution, receipt["nonce"])
    if any(type(receipt.get(key)) is not type(value) or receipt.get(key) != value
           for key, value in _pending_document(pending).items()):
        return None
    if (set(receipt) != set(_pending_document(pending)) | {
        "analysis_id", "chapter_no", "content_hash", "result_hash", "analysis_hash", "change_counts",
    } or type(receipt.get("analysis_id")) is not int or receipt["analysis_id"] <= 0
            or type(receipt.get("chapter_no")) is not int or not isinstance(receipt.get("change_counts"), dict)
            or not all(type(key) is str and type(value) is int and value >= 0
                       for key, value in receipt["change_counts"].items())):
        return None
    with db.no_autoflush:
        analysis = (await db.execute(select(*(
            getattr(ChapterAnalysis, key) for key in _ANALYSIS_RECEIPT_FIELDS
        )).where(ChapterAnalysis.id == receipt["analysis_id"]).with_for_update())).one_or_none()
    if (analysis is None or analysis.project_id != execution.project_id
            or analysis.chapter_id != execution.task_chapter_id or analysis.task_id != execution.task_id
            or analysis.chapter_no != receipt["chapter_no"] or analysis.content_hash != receipt["content_hash"]):
        return None
    result = {
        "analysis_id": analysis.id, "chapter_id": analysis.chapter_id, "chapter_no": analysis.chapter_no,
        "summary": analysis.summary, "scores": analysis.scores, "metrics": analysis.metrics,
        "warnings": analysis.warnings, "change_counts": receipt["change_counts"],
    }
    if (_result_digest(result) != receipt.get("result_hash")
            or _analysis_digest(analysis) != receipt.get("analysis_hash")):
        return None
    return receipt, result


async def completed_terminal_analysis_receipt(
    db: AsyncSession, task: Task,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """验证已成功并清空执行密钥的任务，仅供原任务删除/裁剪。"""
    if (not isinstance(task, Task) or not _execution_uuid(task.statistics_key)
            or task.kind != KIND or task.status != "succeeded" or task.execution_key is not None
            or task.admin_cancel_operation_id is not None or task.admin_cancel_requested_at is not None
            or db.is_modified(task, include_collections=False)):
        return None
    try:
        expected_model, _ = analysis_bindings(task.input, actor_id=task.user_id, project_id=task.project_id)
    except AnalysisExecutionChanged:
        return None
    with db.no_autoflush:
        project = await db.execute(update(Project).where(
            Project.id == task.project_id, Project.user_id == expected_model.project_owner_id,
        ).values(id=Project.id, updated_at=Project.updated_at).execution_options(synchronize_session=False))
        if project.rowcount != 1:
            return None
        row = (await db.execute(select(Task.checkpoint, Task.input, Task.result).where(
            Task.id == task.id, Task.statistics_key == task.statistics_key, Task.user_id == task.user_id,
            Task.project_id == task.project_id, Task.chapter_id == task.chapter_id, Task.kind == KIND,
            Task.queued_at == task.queued_at, Task.status == "succeeded", Task.execution_key.is_(None),
            Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
        ).with_for_update())).one_or_none()
    if row is None or not isinstance(row.checkpoint, dict):
        return None
    receipt = row.checkpoint.get("_analysis_completion")
    if not isinstance(receipt, dict) or not _execution_uuid(receipt.get("execution_key")):
        return None
    try:
        model, selection = analysis_bindings(row.input, actor_id=task.user_id, project_id=task.project_id)
        proposal_source = _analysis_proposal_source(row.input, task_grant_identity(row.input))
    except AnalysisExecutionChanged:
        return None
    execution = AnalysisExecution(
        task.id, task.statistics_key, task.user_id, task.project_id, task.queued_at,
        receipt["execution_key"], task_grant_identity(row.input), KIND, task.chapter_id,
        input_hash=receipt.get("input_hash", ""),
        actor_token_version=receipt.get("actor_token_version"),
        model_binding=model, embedding_selection=selection,
        proposal_source=proposal_source,
    )
    if not _valid_execution(execution):
        return None
    verified = await _validated_analysis_receipt(db, execution, row)
    if (verified is None or not isinstance(row.result, dict)
            or json.dumps(row.result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            != json.dumps(verified[1], ensure_ascii=False, sort_keys=True, separators=(",", ":"))):
        return None
    return verified


@dataclass(frozen=True, slots=True)
class AnalysisIndexOutcome:
    state: Literal["keyword", "unavailable", "queued", "current"]
    task_id: int | None = None
    reason: str | None = None

    @property
    def warning(self) -> str | None:
        if self.state == "keyword":
            if self.reason == "owner_embedding_not_allowed":
                return ANALYSIS_OWNER_EMBEDDING_WARNING
            return ANALYSIS_KEYWORD_WARNING
        if self.state == "unavailable":
            return ANALYSIS_INDEX_PENDING_WARNING
        return None


async def enqueue_memory_for_execution(
    sessionmaker: async_sessionmaker[AsyncSession], execution: AnalysisExecution, *, engine: "TaskEngine | None" = None,
    review_analysis: dict[str, Any] | None = None, cipher: SecretCipher | None = None, allow_private: bool = False,
) -> AnalysisIndexOutcome:
    """核对原结果；单章分析或批量分析阶段（vector）经收据式受理派生向量任务，其余只核对不入队。

    E6-c：授权编辑者与作者同样派生（子任务归真实触发者、携带原邀请轮次，向量为受理时冻结的作者配置）；
    无授权编辑者的冻结选择为 keyword，仍只维护关键词。

    批量分析以原 batch 任务的 UUID、queued_at、execution_key、断点和原分析 reference 受理；
    同一 reference 只受理一次，重试沿用原收据而不为旧授权补签。"""
    if not _valid_execution(execution):
        raise AnalysisExecutionChanged()
    reference = capture_analysis_reference(review_analysis) if execution.task_kind == "chapter.batch" else None
    if execution.task_kind == KIND and review_analysis is not None:
        raise AnalysisExecutionChanged()
    try:
        async with sessionmaker() as db:
            if execution.task_kind == KIND:
                await _analysis_write_barrier(db, execution, status="succeeded")
                if await completed_analysis_receipt(db, execution, status="succeeded") is None:
                    raise AnalysisExecutionChanged()
            else:
                await _analysis_write_barrier(db, execution, status="running")
                await require_completed_batch_analysis(db, execution, reference)
    except Exception:  # noqa: BLE001  包括身份、权限和无法确认结果的存储故障。
        raise AnalysisExecutionChanged() from None
    if cipher is not None and execution.embedding_selection.mode == "vector":
        # 作者自己触发的旧行为不变（design §6.3）：原分析完成收据、子任务与统计同事务受理，
        # 提交确认未知时只按本次保留的 attempt UUID 核对，绝不生成第二个任务。
        from app.services import kb_index_service

        attempt = kb_index_service.new_analysis_index_attempt()
        try:
            async with sessionmaker() as db:
                admission = await kb_index_service.enqueue_memory_for_analysis(
                    db, execution, attempt=attempt, reference=reference, cipher=cipher,
                    allow_private=allow_private,
                )
                try:
                    await db.commit()
                except Exception:  # noqa: BLE001
                    await db.rollback()
                    async with sessionmaker() as verify:
                        admission = await kb_index_service.confirm_analysis_index_attempt(
                            verify, execution, attempt=attempt, admission=admission, cipher=cipher,
                            allow_private=allow_private, reference=reference,
                        )
        except Exception:  # noqa: BLE001  不输出 SQL 或私有参数；派生失败不影响已成功的分析。
            raise AnalysisExecutionChanged() from None
        if admission.state == "no_work" or (admission.state == "existing" and admission.child_task_key is None):
            # 已有收据且当时无需向量化：同样是“已是最新”，不能报告为已排队。
            return AnalysisIndexOutcome("current")
        if admission.state == "created" and engine is not None:
            engine.notify()
        return AnalysisIndexOutcome("queued", task_id=admission.task_id)
    return AnalysisIndexOutcome(
        "keyword" if execution.embedding_selection.mode == "keyword" else "unavailable",
        reason=execution.embedding_selection.reason,
    )


@dataclass(slots=True)
class AnalysisProgress:
    task_execution_identity: "TaskExecutionIdentity | None" = None


@dataclass(frozen=True)
class AnalysisJob:
    project_id: int
    chapter_id: int
    chapter_no: int
    content: str
    content_hash: str
    provider: LLMProvider
    model: str
    messages: list[ChatMessage]
    chapter_created_at: datetime | None = None
    thinking: ThinkingLevel = "off"
    max_tokens: int | None = None
    task_id: int | None = None
    task_key: str | None = None
    review_analysis: dict[str, Any] | None = None
    execution: AnalysisExecution | None = None
    binding: project_ai_config.ResolvedProjectModel | None = field(default=None, repr=False)
    cipher: SecretCipher | None = field(default=None, repr=False)
    visibility_stamp: ai_dispatch.VisibilityStamp | None = field(default=None, repr=False)
    pending: AnalysisPending | None = field(default=None, repr=False)
    batch_reference: AnalysisReference | None = field(default=None, repr=False)
    send_guard: "SendGuard | None" = field(default=None, repr=False)
    progress_state: AnalysisProgress = field(default_factory=AnalysisProgress, repr=False)
    allow_private: bool = False

    @property
    def task_execution_identity(self) -> "TaskExecutionIdentity | None":
        return self.progress_state.task_execution_identity


def _admission_changed() -> ConflictError:
    return ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})


async def validate_analysis_task(
    db: AsyncSession,
    *,
    user_id: int,
    chapter_id: int,
    project_id: int | None,
    req: AnalyzeRequest,
    model: project_ai_config.ResolvedProjectModel,
    cipher: SecretCipher,
    allow_private: bool,
) -> tuple[Chapter, project_ai_config.ResolvedSharedEmbedding]:
    """受理事务内：中央已按 User → 发送写门禁 → 作品锁重选并接受原预检模型；不再解析 DNS、不修改数据。

    作者与 accepted editor 共用：按原 grant 锁后重验 ai.use（viewer 403、撤权/非成员 404），
    章节必须属于该作品且正文非空；共享向量选择在同一屏障内冻结（协作者未获作者授权 → keyword）。
    """
    if (type(model) is not project_ai_config.ResolvedProjectModel or model.actor_id != user_id
            or model.model_type != "text" or (project_id is not None and project_id != model.project_id)
            or (req.ai_config_id is not None and req.ai_config_id != model.config_id)):
        raise _admission_changed()
    with db.no_autoflush:
        project = await require_project(
            db, user_id, model.project_id, "ai.use", for_update=True, visibility_write=True,
            grant_identity=model.grant_identity,
        )
        if project.user_id != model.project_owner_id:
            raise _admission_changed()
        cached = db.identity_map.get(identity_key(Chapter, chapter_id))
        if cached is not None and db.is_modified(cached, include_collections=False):
            raise ConflictError("章节存在未提交的修改，请先保存")
        chapter = await db.scalar(select(Chapter).where(
            Chapter.id == chapter_id, Chapter.project_id == project.id,
        ).execution_options(populate_existing=True))
        if chapter is None:
            raise NotFoundError("章节不存在")
    if not chapter.content.strip():
        raise BadRequestError(EMPTY_CONTENT)
    selection = await project_ai_config.resolve_shared_embedding(
        db, user_id, project.id, cipher=cipher, allow_private=allow_private, for_update=True,
    )
    if selection.grant_identity != model.grant_identity or selection.project_owner_id != project.user_id:
        raise _admission_changed()
    return chapter, selection


def _state_card(
    c: Character | SimpleNamespace,
    memberships: list,
    career_line: str,
    field_text: str = "",
    hidden: frozenset[str] = frozenset(),
) -> str:
    head = f"■ {c.name}"
    if c.aliases:
        head += f"（别名：{'、'.join(c.aliases)}）"
    if "role_type" not in hidden and c.role_type:
        head += f" · {c.role_type}"
    lines = [head]
    if "life_status" not in hidden:
        lines.append(f"存活状态：{LIFE_LABELS.get(c.life_status, c.life_status)}")
    if "current_state" not in hidden:
        lines.append(f"当前状态：{c.current_state.strip() or '（未记录）'}")
    if field_text:
        lines.append(field_text)
    if memberships:
        orgs = []
        for m, o in memberships:
            extra = [x for x in (m.position.strip(), MEMBER_STATUS_LABELS.get(m.status, "")) if x]
            orgs.append(o.name + (f"（{'，'.join(extra)}）" if extra else ""))
        lines.append(f"所属组织：{'、'.join(orgs)}")
    if career_line:
        lines.append(career_line)
    return "\n".join(lines)


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
    task_key: str | None = None,
    execution: AnalysisExecution | None = None,
    pending: AnalysisPending | None = None,
    review_analysis: dict[str, Any] | None = None,
    context_budget: int = 12_000,
) -> AnalysisJob:
    """任务开始运行时按最新正文与设定组装提示词。"""
    from app.services.rich_document_ai import (
        analysis_fingerprints,
        count_visible_chapter_sections,
        render_visible_sections,
        visible_sections,
    )

    batch_reference = None
    if execution is not None:
        if (not _valid_execution(execution) or execution.actor_id != user_id
                or (execution.task_kind == KIND and execution.task_chapter_id != chapter_id)
                or (execution.task_kind == KIND and (
                    type(pending) is not AnalysisPending or pending.execution != execution
                    or not _execution_uuid(pending.nonce)
                ))
                or (execution.task_kind != KIND and pending is not None)
                or (task_id is not None and task_id != execution.task_id)
                or (task_key is not None and task_key != execution.task_key)):
            raise AnalysisExecutionChanged()
        await _analysis_initial_check(db, execution)
        task_id, task_key = execution.task_id, execution.task_key
        if execution.task_kind == "chapter.batch":
            batch_reference = capture_analysis_reference(review_analysis)
            if batch_reference.chapter_id != chapter_id:
                raise AnalysisExecutionChanged()
        elif review_analysis is not None:
            raise AnalysisExecutionChanged()
    elif task_id is not None or task_key is not None:
        raise AnalysisExecutionChanged()
    if execution is None:
        chapter = await get_owned_chapter(db, user_id, chapter_id)
        project = await get_owned_project(db, user_id, chapter.project_id)
    else:
        project = await project_access.require_project(
            db, user_id, execution.project_id, "ai.use", grant_identity=execution.grant_identity,
        )
        chapter = await db.scalar(select(Chapter).where(
            Chapter.id == chapter_id, Chapter.project_id == execution.project_id,
        ))
        if chapter is None or project.user_id != execution.model_binding.project_owner_id:
            raise AnalysisExecutionChanged()
    if not chapter.content.strip():
        raise BadRequestError(EMPTY_CONTENT)
    binding = execution.model_binding if execution is not None else None
    if binding is not None and ai_config_id is not None and ai_config_id != binding.config_id:
        raise AnalysisExecutionChanged()
    cfg = await ai_config_service.resolve_config(
        db, binding.config_owner_id if binding is not None else user_id,
        binding.config_id if binding is not None else ai_config_id,
    )
    if execution is not None:
        if db.is_modified(cfg, include_collections=False):
            raise AnalysisExecutionChanged()
        try:
            await project_ai_config.revalidate_project_model(
                db, binding, cipher=cipher, allow_private=allow_private, lock_config=False,
            )
            await project_ai_config.revalidate_shared_embedding(
                db, execution.embedding_selection, cipher=cipher, allow_private=allow_private, lock_config=False,
            )
        except Exception:  # noqa: BLE001  选择后的授权或绑定失败不能降为批量普通警告。
            raise AnalysisExecutionChanged() from None
        if binding.config_fingerprint != project_ai_config._fingerprint(cfg):
            raise AnalysisExecutionChanged()
    fields_revision = await setting_field_service.fields_revision(db, project.id)
    stamp = await read_visibility_stamp(db, project.id)
    planning_revision = await planning_context_service.planning_revision(db, project.id)
    author_context = ""
    author_budget = min(3_000, max(0, ai_config_service.context_char_budget(cfg, context_budget) // 4))
    if author_budget >= 100:
        count = await count_visible_chapter_sections(db, project.id, chapter.id)
        if count:
            row_budget = min(32, count, max(1, author_budget // 256))
            visible = await visible_sections(db, project.id, chapter_ids=[chapter.id], max_rows=row_budget,
                                             text_limit=min(2_000, max(1, author_budget // row_budget)))
            author_context = render_visible_sections(visible, author_budget)
    analysis_text = "\n\n".join(part for part in (chapter.content, author_context) if part)
    fingerprint = (await analysis_fingerprints(db, [chapter]))[chapter.id]
    if batch_reference is not None and batch_reference.content_hash != fingerprint:
        raise AnalysisExecutionChanged()
    candidates = await load_candidates(db, project.id)
    legacy_char = {entry.ref.id: entry for entry in candidates if entry.ref.kind == "character" and entry.mode is None}
    characters = await cast_service.list_characters(db, project.id)
    mentioned = set(cast_service.match_characters(analysis_text, [
        char for char in characters if char.id in legacy_char and not legacy_char[char.id].tracking_explicit
    ]))
    legacy = [
        ("character", char.id) for char in characters if char.id in legacy_char
        and (cast_service.is_protagonist(char) or char.id in mentioned)
    ]
    # 分析原本列出全部组织；只对存量档案保留这个默认，不引入原来不读的世界观。
    legacy.extend(entry.key for entry in candidates if entry.ref.kind == "organization" and entry.mode is None)
    explicit = await planning_context_service.chapter_explicit_entries(db, project, chapter)
    explicit.extend(("character", cid) for cid in await cast_service.cast_ids(db, chapter.id))
    scope = await build_scope(
        db, project.id, stamp=stamp, candidates=candidates,
        texts=(analysis_text,), legacy=legacy,
        explicit=explicit,
        kinds=("character", "organization", "career"),
    )
    effective = await resolve_effective_cast(db, project, chapter, characters, scope=scope)
    orgs = sorted(effective.organizations.values(), key=lambda org: (org.sort, org.id))
    organization_text = {
        org.id: format_entry_fields(effective.entry_fields["organization", org.id]) for org in orgs
    }
    organizations_text = (
        "\n\n".join("\n".join(part for part in (org.name, organization_text[org.id]) if part) for org in orgs)
        if any(organization_text.values()) else "、".join(org.name for org in orgs)
    )
    foreshadows = await foreshadow_service.analysis_candidates(db, project.id, chapter.chapter_no)
    messages = build_analysis_messages(
        await project_info(db, project, actor_user_id=user_id),
        chapter_no=chapter.chapter_no,
        title=chapter.title,
        content=analysis_text,
        cast_text="\n\n".join(
            _state_card(
                c,
                effective.memberships.get(c.id, []),
                format_career_line(effective.careers_by_character.get(c.id, []), effective.stages),
                format_entry_fields(effective.entry_fields["character", c.id]),
                hidden=scope.hidden("character", c.id),
            )
            for c in effective.characters
        ),
        relations_text="\n".join(line.render() for line in effective.relations),
        organizations_text=organizations_text,
        careers_text="\n".join(
            "\n".join(part for part in (
                career_service.ladder_line(c, effective.stages.get(c.id, [])),
                format_entry_fields(effective.entry_fields["career", c.id]),
            ) if part)
            for c in sorted(effective.careers.values(), key=lambda c: (c.sort, c.id))
        ),
        foreshadows_text="\n".join(foreshadow_service.analysis_line(f) for f in foreshadows),
        override=await prompt_service.get_override(db, user_id, "analysis.chapter"),
    )
    await setting_field_service.check_fields_revision(db, project.id, fields_revision)
    await planning_context_service.check_planning_revision(db, project.id, planning_revision)
    await scope.verify(db)
    send_guard = None
    if execution is not None:
        from app.services.collaboration_ai_guard import for_task

        send_guard = for_task(
            execution.task_identity, binding, scope.stamp, cipher=cipher, allow_private=allow_private,
            analysis_execution=execution, analysis_pending=pending, analysis_reference=batch_reference,
            extra_bindings=(execution.embedding_selection,),
        )
        bound = await send_guard.prepare_provider(visibility_sessionmaker(db), factory, request_timeout=ai_timeout)
        if bound.model != cfg.model:
            raise AnalysisExecutionChanged()
        guarded = bound.provider
    else:
        # 保留作者的无任务提示词构造测试；run_analysis_job 不接受未绑定执行身份的结果写入。
        provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
        guarded = guard_provider(provider, scope.stamp, visibility_sessionmaker(db))
    progress = AnalysisProgress(execution.task_identity if execution is not None else None)
    if pending is not None:
        from app.services.task_engine.execution_identity import advance_checkpoint

        checkpoint = deepcopy(execution.task_identity.checkpoint or {})
        checkpoint["_analysis_pending"] = _pending_document(pending)
        progress.task_execution_identity = advance_checkpoint(execution.task_identity, checkpoint)
    return AnalysisJob(
        project_id=project.id,
        chapter_id=chapter.id,
        chapter_no=chapter.chapter_no,
        content=chapter.content,
        content_hash=fingerprint,
        provider=guarded,
        model=cfg.model,
        messages=messages,
        chapter_created_at=chapter.created_at,
        thinking=cfg.thinking_level,  # type: ignore[arg-type]
        max_tokens=ai_config_service.max_output_tokens(cfg),
        task_id=task_id,
        task_key=task_key,
        review_analysis=review_analysis,
        execution=execution,
        binding=binding,
        cipher=cipher if execution is not None else None,
        visibility_stamp=scope.stamp,
        pending=pending,
        batch_reference=batch_reference,
        send_guard=send_guard,
        progress_state=progress,
        allow_private=allow_private,
    )


async def run_analysis_job(job: AnalysisJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    """流式输出模型原文（delta）；解析成功后在同一事务内保存分析与回写条目，done 返回概要。"""
    from app.services.rich_document_ai import analysis_fingerprints

    pieces: list[str] = []
    saving = False
    commit_started = False

    async def discard_pending() -> None:
        if job.pending is not None:
            await ai_dispatch._wait_send_cleanup(discard_analysis_pending(sessionmaker, job.pending))
            job.progress_state.task_execution_identity = job.execution.task_identity

    try:
        if (job.execution is None or job.binding is None or job.cipher is None or job.visibility_stamp is None
                or job.send_guard is None):
            raise AnalysisExecutionChanged()
        if job.execution.task_kind == KIND and (job.pending is None or job.pending.execution != job.execution):
            raise AnalysisCommitUncertain("分析准备凭据缺失，请核对")
        if job.execution.task_kind == "chapter.batch" and job.batch_reference is None:
            raise AnalysisExecutionChanged()
        stream_iter = job.provider.generate_stream(
            job.messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking
        )
        async with aclosing(stream_iter) as stream:
            async with aclosing(coalesce_ready_text(stream)) as ready:
                async for piece in ready:
                    pieces.append(piece)
                    yield "delta", {"text": piece}
        raw = "".join(pieces)
        if not raw.strip():
            raise AIProviderError("模型未返回任何内容")
        data, warnings = normalize_analysis(extract_json(raw))
        if job.execution.embedding_selection.mode == "keyword":
            warnings.append(ANALYSIS_OWNER_EMBEDDING_WARNING
                            if job.execution.embedding_selection.reason == "owner_embedding_not_allowed"
                            else ANALYSIS_KEYWORD_WARNING)
        else:
            warnings.append(ANALYSIS_INDEX_PENDING_WARNING)
        saving = True
        async with sessionmaker() as db:
            # 同一事务持认证、发送写门禁、作品和原任务锁；等待模型期间不持锁。
            await _analysis_write_barrier(db, job.execution)
            await _lock_analysis_config_owners(db, job.execution)
            await job.send_guard.require_task_write(db)
            if job.batch_reference is not None:
                await _require_batch_analysis(db, job.execution, job.batch_reference, read_lock=False)
            await ai_dispatch.require_visibility_stamp(db, job.visibility_stamp)
            try:
                await project_ai_config.revalidate_project_model(
                    db, job.binding, cipher=job.cipher, allow_private=job.allow_private, lock_config=True,
                )
                await project_ai_config.revalidate_shared_embedding(
                    db, job.execution.embedding_selection, cipher=job.cipher,
                    allow_private=job.allow_private, lock_config=True,
                )
            except Exception:  # noqa: BLE001
                raise AnalysisExecutionChanged() from None
            chapter = await db.get(Chapter, job.chapter_id)
            if (
                chapter is None or chapter.project_id != job.project_id
                or (job.chapter_created_at is not None and chapter.created_at != job.chapter_created_at)
            ):
                raise BadRequestError("章节已被删除，分析结果未保存")
            current = (await analysis_fingerprints(db, [chapter]))[chapter.id]
            if chapter.chapter_no != job.chapter_no or current != job.content_hash:
                raise BadRequestError("章节正文或章节位置已变化，分析结果未保存，请重新分析")
            review_receipt = None
            if job.batch_reference is not None:
                from app.models import SceneReviewAnalysisReceipt

                ref = job.batch_reference
                review_receipt = await db.get(SceneReviewAnalysisReceipt, ref.run_key, with_for_update=True)
                if (review_receipt is None or review_receipt.status != "running"
                        or review_receipt.task_key != ref.task_key
                        or review_receipt.chapter_id != job.chapter_id
                        or review_receipt.content_hash != ref.content_hash):
                    raise BadRequestError("提案分析轮次已变化，结果未保存")
            analysis = ChapterAnalysis(
                project_id=job.project_id,
                chapter_id=job.chapter_id,
                task_id=job.execution.task_id,
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
            if review_receipt is not None:
                review_receipt.analysis_id, review_receipt.status = analysis.id, "completed"
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
            completion = (await _write_analysis_completion(db, job.pending, analysis, result)
                          if job.pending is not None else None)
            commit_started = True
            try:
                await db.commit()
            except BaseException:
                # 提交可能已经发生；只核对本执行体刚写的精确收据，不能以当前整数 ID 猜测。
                try:
                    await db.rollback()
                except BaseException:
                    pass
                confirmed = None
                if completion is not None:
                    try:
                        async with sessionmaker() as proof_db:
                            confirmed = await completed_analysis_receipt(proof_db, job.execution)
                    except BaseException:
                        pass
                if confirmed is None or confirmed != (completion, result):
                    raise AnalysisCommitUncertain("分析保存结果未知，请核对") from None
            if completion is not None:
                from app.services.task_engine.execution_identity import advance_checkpoint

                checkpoint = deepcopy(job.execution.task_identity.checkpoint or {})
                checkpoint["_analysis_completion"] = deepcopy(completion)
                job.progress_state.task_execution_identity = advance_checkpoint(job.execution.task_identity, checkpoint)
        saving = False
        yield "done", result
    except AnalysisCommitUncertain:
        raise
    except JSONExtractError as exc:
        await discard_pending()
        yield "error", {"detail": f"AI 输出解析失败：{exc}，请重新生成"}
    except VisibilityPolicyError:
        if not commit_started:
            await discard_pending()
        raise
    except AIProviderError as exc:
        await discard_pending()
        yield "error", {"detail": str(exc)}
    except AppError as exc:
        await discard_pending()
        yield "error", {"detail": exc.detail}
    except asyncio.CancelledError:
        if not commit_started:
            await discard_pending()
        raise
    except GeneratorExit:
        # 引擎发布门禁拒绝事件后经 aclose 关闭本生成器；尚未开始提交时只清理本轮 pending，
        # 否则收尾会因残留 pending 永远停在“结果待核对”（不能遗留占用）。
        if not commit_started:
            await discard_pending()
        raise
    except Exception:  # noqa: BLE001
        if not commit_started:
            await discard_pending()
        if saving:
            raise AnalysisExecutionChanged() from None
        logger.exception("章节 %s 分析失败", job.chapter_id)
        yield "error", {"detail": "分析失败：服务器内部错误"}


async def coalesce_ready_text(stream: AsyncIterator[str], *, max_chars: int = 256) -> AsyncIterator[str]:
    """只合并同一流中已就绪的文本；不等待网络或跨越错误，取消时关闭预读。"""
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("片段大小无效")
    pending = None
    ended = False
    try:
        while not ended:
            if pending is None:
                pending = asyncio.ensure_future(anext(stream))
            try:
                text = await pending
            except StopAsyncIteration:
                break
            pending = None
            for _ in range(64):
                if len(text) >= max_chars:
                    break
                pending = asyncio.ensure_future(anext(stream))
                # 仅让已就绪的异步迭代器运行一次，不等待网络数据或定时器。
                await asyncio.sleep(0)
                if not pending.done():
                    break
                try:
                    next_text = pending.result()
                except StopAsyncIteration:
                    pending = None
                    ended = True
                    break
                except BaseException:
                    # 先交付错误前已取得的片段；下一轮 await 原任务时再抛原错误。
                    break
                pending = None
                text += next_text
            if text:
                yield text
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            try:
                await pending
            except (Exception, asyncio.CancelledError):
                pass
