"""book.import 反推任务：装批 → 通读（每批提交断点）→ 合并 + 汇总 → 写入设定（只写空字段）→ 提取风格 → 排队深度分析。

- 断点：step_cursor = 当前批在 checkpoint.batches 中的下标；checkpoint 结构见 new_checkpoint。每批的摘要写入、
  结果合并与断点在同一事务提交；写入设定、风格、分析入队各自与「阶段前进」同一事务提交。
  中断（重启 → interrupted）后重试沿用断点（validate_retry），已完成的批次不再调用 AI。
- 装批在第一次运行时完成：反推范围内的章节按顺序装批，每批约 BATCH_CHARS 字；单章超过 BATCH_CHARS 时按句末切成多段，
  分到相邻几批。装批只存章节 id 与分段编号，正文在执行时按 id 读取（执行期间的编辑以最新内容为准）；
  章节已删除则跳过并记警告。
- 每次 AI 调用失败（提供方错误、输出无法解析）自动重试 MAX_RETRIES 次（退避 RETRY_DELAYS 秒）；仍失败时任务暂停
  （pause_reason.reason = failed）：继续 = 重做当前步骤；跳过（apply_skip）= 通读跳过本批 / 汇总改用代码合并的结果；
  结束 = 保留已写入的数据。风格提取失败只记警告。取消与中断都保留已写入的项目、章节与设定。
- 写入设定只写空字段：项目字段为空才写；角色 / 组织 / 职业 / 世界观同名（角色含别名）已存在则跳过；
  关系只在双方都存在且两人之间还没有关系时写入（relationship_service.create_relation，source = import）。
- 每次 AI 调用都写生成记录（kind = book_import.digest / book_import.synthesize / book_import.style）。
"""

import asyncio
import copy
import math
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts import (
    build_book_digest_messages,
    build_book_synthesize_messages,
    build_style_extract_messages,
    format_project_info,
)
from app.ai.prompts.registry import prompt_meta
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ThinkingLevel
from app.ai.templating import PromptOverride
from app.core.errors import AppError, BadRequestError, ConflictError
from app.models import Career, Chapter, Character, Organization, Outline, Project, Task, WorldSetting, WritingStyle
from app.models.entities import OPEN_TASK_STATUSES
from app.schemas.career import MAX_STAGES, CareerCreate, StageIn
from app.schemas.organization import MemberCreate, OrganizationCreate
from app.schemas.project import AUDIENCE_MAX, MAX_TAGS, SELLING_POINTS_MAX, SYNOPSIS_MAX, TAG_MAX_LEN, THEME_MAX
from app.services import (
    ai_config_service,
    analysis_service,
    career_service,
    generation_log_service,
    organization_service,
    project_lifecycle,
    prompt_service,
    relationship_service,
    story_service,
    style_service,
    wizard_service,
)
from app.services.book_import import merge
from app.services.entity_ai_service import normalize_org_draft
from app.services.project_service import get_owned_project, project_info

if TYPE_CHECKING:
    from app.services.task_engine.registry import Event, TaskContext

KIND = "book.import"
BATCH_CHARS = 20000
MAX_RETRIES = 2
# AI 调用失败后第 1 / 2 次重试前的等待秒数（测试中改为 0）
RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0)
SUMMARY_LIMIT = 150
SYNTH_SUMMARY_CHARS = 20000
SYNTH_DRAFT_CHARS = 20000
SYNTH_CHARACTERS = 60
SAMPLE_EDGE = 10
STYLE_SAMPLE_CHARS = 2000
STYLE_MIN_CHARS = 200
STYLE_NAME_LIMIT = 100
MAX_WARNINGS = 100
GENRE_MAX = 100

OPEN_CONFLICT = "已有进行中或待处理的拆书任务，请等待其完成或结束后再试"
SKIP_CONFLICT = "只有因 AI 调用失败而暂停的拆书任务可以跳过"
# 阶段依次推进；断点里的 phase 是下一步要做的阶段
PHASES = ("digest", "synthesize", "apply", "style", "analyze", "done")
STAT_KEYS = ("summaries", "characters", "organizations", "world", "careers", "relations", "analyses_queued")
_SENTENCE_END = set("。！？!?…\n")
CAREER_KIND_LABELS = {"main": "主修", "sub": "副修"}


def resource_key(user_id: int) -> str:
    return f"book_import:{user_id}"


# ---------------- 装批（纯函数，预估也用） ----------------


def split_parts(content: str, limit: int | None = None) -> list[str]:
    """超过 limit（默认 BATCH_CHARS）的正文按句末切成若干段（每段 ≤ limit，尽量等长）；找不到句末时硬切。"""
    limit = BATCH_CHARS if limit is None else limit
    if len(content) <= limit:
        return [content]
    target = math.ceil(len(content) / math.ceil(len(content) / limit))
    parts: list[str] = []
    pos = 0
    while len(content) - pos > limit:
        hi = pos + limit
        cut = next((i for i in range(pos + target, hi) if content[i - 1] in _SENTENCE_END), None)
        cut = cut or next((i for i in range(pos + target, pos, -1) if content[i - 1] in _SENTENCE_END), None)
        cut = cut or hi
        parts.append(content[pos:cut])
        pos = cut
    parts.append(content[pos:])
    return parts


def plan_batches(items: list[tuple[int, int, list[int]]], limit: int | None = None) -> list[list[list[int]]]:
    """items = [(章节 id, 章节号, 各段长度)] → 每批的分段 [[章节 id, 段下标, 总段数, 章节号], ...]，每批约 limit 字。"""
    limit = BATCH_CHARS if limit is None else limit
    batches: list[list[list[int]]] = []
    cur: list[list[int]] = []
    size = 0
    for cid, no, lengths in items:
        for k, length in enumerate(lengths):
            if cur and size + length > limit:
                batches.append(cur)
                cur, size = [], 0
            cur.append([cid, k, len(lengths), no])
            size += length
    if cur:
        batches.append(cur)
    return batches


# ---------------- 断点与汇总 ----------------


def new_checkpoint(batches: list[list[list[int]]]) -> dict[str, Any]:
    """checkpoint：phase、batches（每批的分段 [章节 id, 段下标, 总段数, 章节号]、state pending|done|skipped、error）、
    part_summaries（分段章节已完成各段的摘要 {章节 id: {段下标: 摘要}}，全部段完成后拼接写入）、findings（各批合并结果，
    见 merge）、final（汇总结果）、stats（写入数量）、skipped（跳过的步骤与章号）、warnings、pause_reason。"""
    return {
        "phase": "digest" if batches else "style",
        "batches": [{"parts": parts, "state": "pending", "error": ""} for parts in batches],
        "part_summaries": {},
        "findings": merge.empty_findings(),
        "final": None,
        "stats": {**dict.fromkeys(STAT_KEYS, 0), "style_id": None, "project_fields": []},
        "skipped": [],
        "warnings": [] if batches else ["反推范围内没有可通读的章节，已跳过通读与汇总"],
        "pause_reason": None,
    }


def summarize(checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    """任务结果 / 暂停载荷 / TaskOut.checkpoint：不含体积较大的中间结果（findings、final）。"""
    cp = checkpoint or {}
    batches = cp.get("batches") or []
    return {
        "phase": cp.get("phase") or "digest",
        "batches_total": len(batches),
        "batches_done": sum(1 for b in batches if b.get("state") == "done"),
        "batches_skipped": sum(1 for b in batches if b.get("state") == "skipped"),
        "stats": dict(cp.get("stats") or {}),
        "skipped": list(cp.get("skipped") or []),
        "warnings": list(cp.get("warnings") or []),
        "pause_reason": cp.get("pause_reason"),
    }


def finish_result(task: Task) -> dict[str, Any]:
    return {**summarize(task.checkpoint), "ended_early": True}


def _warn(cp: dict[str, Any], text: str) -> None:
    warnings = cp.setdefault("warnings", [])
    if text not in warnings and len(warnings) < MAX_WARNINGS:
        warnings.append(text)


def apply_skip(cp: dict[str, Any], cursor: int) -> int:
    """跳过因失败暂停的当前步骤（就地修改断点），返回新的 step_cursor。"""
    reason = cp.get("pause_reason") or {}
    if cp.get("phase") == "digest" and cursor < len(cp.get("batches") or []):
        batch = cp["batches"][cursor]
        batch["state"], batch["error"] = "skipped", str(reason.get("error") or "")
        nos = sorted({p[3] for p in batch["parts"]})
        cp["skipped"].append({"stage": "digest", "batch_index": cursor, "chapter_nos": nos})
        # 分段章节被跳过的段记为空摘要，其余段完成后照常拼接写入
        for cid, k, m, _ in batch["parts"]:
            if m > 1:
                cp["part_summaries"].setdefault(str(cid), {})[str(k)] = ""
        cursor += 1
    elif cp.get("phase") == "synthesize":
        cp["final"] = fallback_final(cp.get("findings") or {})
        cp["skipped"].append({"stage": "synthesize", "chapter_nos": []})
        cp["phase"] = "apply"
    cp["pause_reason"] = None
    return cursor


# ---------------- 规范化（纯函数） ----------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "；".join(t for t in (_text(v) for v in value) if t)
    return str(value).strip()


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe(fn: Callable[[Any], list[dict[str, Any]]], value: Any) -> list[dict[str, Any]]:
    """向导的 normalize 在一条都没有时抛 JSONExtractError；拆书里某类为空是正常的。"""
    try:
        return fn(value)
    except JSONExtractError:
        return []


def _characters(value: Any) -> list[dict[str, Any]]:
    rows = _safe(wizard_service.normalize_characters, {"characters": value if isinstance(value, list) else []})
    return [
        {
            "name": r["name"],
            "aliases": r["aliases"],
            "role_type": r["role_type"],
            "profile": r["profile"],
            "personality": r["personality"],
            "appearance": r["appearance"],
        }
        for r in rows
    ]


def _organizations(value: Any) -> list[dict[str, Any]]:
    out = []
    for item in value if isinstance(value, list) else []:
        try:
            draft, members = normalize_org_draft(item)
        except JSONExtractError:
            continue
        out.append(
            {
                "name": draft["name"],
                "org_type": draft["org_type"],
                "description": draft["description"] or draft["purpose"],
                "members": members,
            }
        )
    return out


def _careers(value: Any) -> list[dict[str, Any]]:
    drafts, _ = career_service.normalize_career_drafts({"careers": value if isinstance(value, list) else []})
    return [{k: d[k] for k in ("name", "kind", "description", "stages")} for d in drafts]


def _relations(obj: dict[str, Any]) -> list[dict[str, Any]]:
    value = obj.get("relationships") or obj.get("relations") or []
    return wizard_service.normalize_character_relations({"relationships": value})


def _entities(obj: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "characters": _characters(obj.get("characters")),
        "organizations": _organizations(obj.get("organizations")),
        "world": _safe(wizard_service.normalize_world, obj.get("world") or []),
        "careers": _careers(obj.get("careers")),
        "relations": _relations(obj),
    }


def normalize_digest(obj: Any) -> dict[str, Any]:
    """通读输出 → {summaries: {章节号: [摘要]}, characters, organizations, world, careers, relations}。"""
    if not isinstance(obj, dict):
        raise JSONExtractError("JSON 结构不符合预期")
    summaries: dict[int, list[str]] = {}
    for c in obj.get("chapters") or []:
        if isinstance(c, dict):
            no, text = _int(c.get("chapter_no")), _text(c.get("summary"))
            if no is not None and text:
                summaries.setdefault(no, []).append(text)
    return {"summaries": summaries, **_entities(obj)}


def _tags(value: Any) -> list[str]:
    """类型标签：去重，超长的丢弃，最多 MAX_TAGS 个（AI 输出不满足约束时丢弃而不是报错）。"""
    if isinstance(value, str):
        value = value.replace("，", ",").replace("、", ",").split(",")
    out: list[str] = []
    for v in value if isinstance(value, list) else []:
        v = _text(v)
        if v and len(v) <= TAG_MAX_LEN and v not in out:
            out.append(v)
    return out[:MAX_TAGS]


def _final_characters(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _safe(wizard_service.normalize_characters, {"characters": items})


def normalize_final(obj: Any) -> dict[str, Any]:
    """汇总输出 → final：project 字段截断到列长度；各类实体与写入时的结构一致。"""
    if not isinstance(obj, dict):
        raise JSONExtractError("JSON 结构不符合预期")
    p = obj.get("project") if isinstance(obj.get("project"), dict) else {}
    entities = _entities(obj)
    # 角色按写入时的结构规范化（含别名、定位与是否始终注入上下文）
    entities["characters"] = _final_characters(obj.get("characters") if isinstance(obj.get("characters"), list) else [])
    return {
        "project": {
            "synopsis": _text(p.get("synopsis"))[:SYNOPSIS_MAX],
            "theme": _text(p.get("theme"))[:THEME_MAX],
            "tags": _tags(p.get("tags")),
            "selling_points": _text(p.get("selling_points"))[:SELLING_POINTS_MAX],
            "audience": _text(p.get("audience"))[:AUDIENCE_MAX],
            "genre": _text(p.get("genre"))[:GENRE_MAX],
        },
        **entities,
    }


def fallback_final(findings: dict[str, Any]) -> dict[str, Any]:
    """汇总被跳过时由代码合并结果生成 final：角色按出现次数取前 60 个，项目字段留空。"""
    return {
        "project": {"synopsis": "", "theme": "", "tags": [], "selling_points": "", "audience": "", "genre": ""},
        "characters": _final_characters(merge.top_characters(findings.get("characters") or [], SYNTH_CHARACTERS)),
        "organizations": list(findings.get("organizations") or []),
        "world": list(findings.get("world") or []),
        "careers": list(findings.get("careers") or []),
        "relations": list(findings.get("relations") or []),
    }


def fill_final(final: dict[str, Any], findings: dict[str, Any]) -> dict[str, Any]:
    """汇总输出里某类为空（模型漏掉）时用代码合并的结果补上。"""
    fallback = fallback_final(findings)
    out = {k: (v if v or k == "project" else fallback[k]) for k, v in final.items()}
    # 模型给了角色但漏了别名：并回各批合并出的别名（写入时按别名判重、上下文按别名识别人物都依赖它）
    out["characters"] = merge.restore_aliases(out.get("characters") or [], findings.get("characters") or [])
    return out


# ---------------- 汇总的输入文本（纯函数） ----------------


def sample_summaries(lines: list[str], limit: int = SYNTH_SUMMARY_CHARS) -> str:
    """超过 limit 字时保留首尾各 10 章，中间等距抽样。"""
    if sum(len(x) + 1 for x in lines) <= limit:
        return "\n".join(lines)
    if len(lines) <= SAMPLE_EDGE * 2:
        return "\n".join(lines)[:limit]
    head, middle, tail = lines[:SAMPLE_EDGE], lines[SAMPLE_EDGE:-SAMPLE_EDGE], lines[-SAMPLE_EDGE:]
    budget = limit - sum(len(x) + 1 for x in head + tail)
    avg = math.ceil(sum(len(x) + 1 for x in middle) / len(middle))
    count = max(0, min(len(middle), budget // avg))
    while True:
        picked = [middle[int(i * len(middle) / count)] for i in range(count)] if count else []
        # 各行长短不一：按平均长度估算的条数仍可能超出预算，逐条减少
        if count == 0 or sum(len(x) + 1 for x in picked) <= budget:
            return "\n".join([*head, *picked, *tail])
        count -= 1


def _fit(sections: list[list[str]], budget: int) -> list[str]:
    """把若干段行文本放进总预算：每段依次分到「剩余预算 / 剩余段数」，用不完的留给后面的段。"""
    out: list[str] = []
    remaining = budget
    for i, lines in enumerate(sections):
        share = remaining // (len(sections) - i)
        used = 0
        kept: list[str] = []
        for line in lines:
            if used + len(line) + 1 > share:
                break
            kept.append(line)
            used += len(line) + 1
        out.append("\n".join(kept))
        remaining -= used
    return out


def draft_texts(findings: dict[str, Any]) -> dict[str, str]:
    characters = [
        f"{c['name']}｜{'、'.join(c.get('aliases') or [])}｜{c.get('role_type') or ''}｜{c.get('profile') or ''}"
        for c in merge.top_characters(findings.get("characters") or [], SYNTH_CHARACTERS)
    ]
    organizations = []
    for o in findings.get("organizations") or []:
        members = "、".join(f"{m['name']}（{m['position']}）" if m.get("position") else m["name"] for m in o["members"])
        kind = f"（{o['org_type']}）" if o.get("org_type") else ""
        line = f"{o['name']}{kind}：{o.get('description') or ''}"
        organizations.append(f"{line}；成员：{members}" if members else line)
    world = [
        f"〔{story_service.WORLD_CATEGORY_LABELS.get(w['category'], '其他')}〕{w['title']}：{w.get('content') or ''}"
        for w in findings.get("world") or []
    ]
    careers = [
        f"{c['name']}（{CAREER_KIND_LABELS.get(c.get('kind'), '主修')}）："
        f"{' → '.join(s['name'] for s in c['stages'])}；{c.get('description') or ''}"
        for c in findings.get("careers") or []
    ]
    relations = [
        f"{r['a']} 是 {r['b']} 的 {r['role_a']}（{r['b']} 是 {r['a']} 的 {r['role_b']}）：{r.get('description') or ''}"
        for r in findings.get("relations") or []
    ]
    keys = ("characters", "organizations", "world", "careers", "relations")
    texts = _fit([characters, organizations, world, careers, relations], SYNTH_DRAFT_CHARS)
    return dict(zip(keys, texts, strict=True))


# ---------------- 校验（创建 / 重试 / 继续） ----------------


async def open_import(db: AsyncSession, user_id: int, *, exclude_id: int | None = None) -> Task | None:
    stmt = select(Task).where(Task.user_id == user_id, Task.kind == KIND, Task.status.in_(OPEN_TASK_STATUSES))
    if exclude_id is not None:
        stmt = stmt.where(Task.id != exclude_id)
    return await db.scalar(stmt.limit(1))


async def _check_config(db: AsyncSession, ctx: "TaskContext", user_id: int, ai_config_id: int | None) -> int:
    cfg = await ai_config_service.resolve_config(db, user_id, ai_config_id)
    await ai_config_service.build_provider(
        cfg,
        ctx.cipher,
        ctx.factory,
        ctx.settings.ai_request_timeout,
        allow_private=ctx.settings.allow_private_ai_endpoints,
    )
    return cfg.id


def _reverse_range(value: Any) -> list[int]:
    if (
        not isinstance(value, list | tuple)
        or len(value) != 2
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
        or not 1 <= value[0] <= value[1]
    ):
        raise BadRequestError("反推范围不正确")
    return [int(value[0]), int(value[1])]


async def validate_start(
    db: AsyncSession, ctx: "TaskContext", *, user_id: int, params: dict[str, Any]
) -> dict[str, Any]:
    """创建任务前同步校验；返回 TaskSpec 所需字段。不修改数据。"""
    project = await get_owned_project(db, user_id, int(params["project_id"]), lock=True)
    request = {
        "project_id": project.id,
        "reverse_chapter_nos": _reverse_range(params.get("reverse_chapter_nos")),
        "extract_style": bool(params.get("extract_style", True)),
        "deep_analyze": bool(params.get("deep_analyze", False)),
        "ai_config_id": params.get("ai_config_id"),
    }
    ai_config_id = await _check_config(db, ctx, user_id, request["ai_config_id"])
    if await open_import(db, user_id) is not None:
        raise ConflictError(OPEN_CONFLICT)
    return {
        "project_id": project.id,
        "title": f"拆书反推：《{project.title}》",
        "input": {"request": request, "ai_config_id": ai_config_id},
    }


async def revalidate(db: AsyncSession, ctx: "TaskContext", task: Task) -> int:
    """重试 / 继续 / 跳过前：项目与模型配置仍有效，用户没有其他未结束的拆书任务。返回 ai_config_id。"""
    request = task.input.get("request") or {}
    await get_owned_project(db, task.user_id, int(task.project_id or 0))
    ai_config_id = await _check_config(db, ctx, task.user_id, request.get("ai_config_id"))
    if await open_import(db, task.user_id, exclude_id=task.id) is not None:
        raise ConflictError(OPEN_CONFLICT)
    return ai_config_id


# ---------------- 执行 ----------------


@dataclass
class ImportJob:
    task_id: int
    user_id: int
    project_id: int
    request: dict[str, Any]
    ai_config_id: int
    provider: LLMProvider
    provider_name: str
    model: str
    thinking: ThinkingLevel
    max_tokens: int | None
    cursor: int
    checkpoint: dict[str, Any]
    last_progress: int = 1

    @property
    def total(self) -> int:
        return len(self.checkpoint.get("batches") or [])


async def _plan(db: AsyncSession, project_id: int, nos: list[int]) -> list[list[list[int]]]:
    rows = await db.execute(
        select(Chapter.id, Chapter.chapter_no, Chapter.content)
        .where(Chapter.project_id == project_id, Chapter.chapter_no >= nos[0], Chapter.chapter_no <= nos[1])
        .order_by(Chapter.chapter_no, Chapter.id)
    )
    items = [(cid, no, [len(p) for p in split_parts(content)]) for cid, no, content in rows.all() if content.strip()]
    return plan_batches(items)


async def build_import_job(ctx: "TaskContext", task: Task) -> ImportJob:
    request = task.input.get("request") or {}
    settings = ctx.settings
    async with ctx.sessionmaker() as db:
        project = await get_owned_project(db, task.user_id, int(task.project_id or 0))
        cfg = await ai_config_service.resolve_config(db, task.user_id, request.get("ai_config_id"))
        provider = await ai_config_service.build_provider(
            cfg,
            ctx.cipher,
            ctx.factory,
            settings.ai_request_timeout,
            allow_private=settings.allow_private_ai_endpoints,
        )
        checkpoint = copy.deepcopy(task.checkpoint) if task.checkpoint else None
        if not checkpoint:
            checkpoint = new_checkpoint(await _plan(db, project.id, _reverse_range(request.get("reverse_chapter_nos"))))
    # 继续 / 重试 / 跳过：从断点重做当前步骤
    checkpoint["pause_reason"] = None
    return ImportJob(
        task_id=task.id,
        user_id=task.user_id,
        project_id=project.id,
        request=request,
        ai_config_id=cfg.id,
        provider=provider,
        provider_name=cfg.provider,
        model=cfg.model,
        thinking=cfg.thinking_level,  # type: ignore[arg-type]
        max_tokens=ai_config_service.max_output_tokens(cfg),
        cursor=min(task.step_cursor, len(checkpoint.get("batches") or [])),
        checkpoint=checkpoint,
    )


async def _write_checkpoint(db: AsyncSession, job: ImportJob) -> None:
    """写入断点（不 commit）。"""
    await db.execute(
        update(Task)
        .where(Task.id == job.task_id)
        .values(step_cursor=job.cursor, checkpoint=copy.deepcopy(job.checkpoint))
    )


async def _save(ctx: "TaskContext", job: ImportJob) -> None:
    async with ctx.sessionmaker() as db:
        await _write_checkpoint(db, job)
        await db.commit()


def progress(job: ImportJob) -> int:
    """通读 0–80（按已处理批数），汇总 85、写入 90、风格 / 分析 95；单调。"""
    phase = job.checkpoint.get("phase")
    if phase == "digest":
        value = int(job.cursor * 80 / max(job.total, 1))
    else:
        value = {"synthesize": 85, "apply": 90}.get(phase or "", 95)
    job.last_progress = max(job.last_progress, max(1, min(95, value)))
    return job.last_progress


def _log_fields(base: dict[str, Any], status: str, error: str, pieces: list[str], started: float) -> dict[str, Any]:
    return {
        **base,
        "status": status,
        "error": error,
        "output_chars": len("".join(pieces)),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


async def _call(
    ctx: "TaskContext",
    job: ImportJob,
    messages: list[ChatMessage],
    *,
    parse: Callable[[Any], Any],
    log_kind: str,
    template_key: str,
    override: PromptOverride | None,
    chapter_id: int | None,
    stage: str,
    params: dict[str, Any],
    out: dict[str, Any],
) -> AsyncIterator["Event"]:
    """调用一次 AI（含自动重试），实时转发输出；成功 out["value"] = parse(JSON)，仍失败 out["error"] = 错误信息。"""
    attempt = 0
    while True:
        if attempt:
            delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)] if RETRY_DELAYS else 0
            yield "stage", {"stage": f"{stage} · 失败，第 {attempt} 次重试"}
            if delay > 0:
                await asyncio.sleep(delay)
        yield "reset", {}
        attempt += 1
        pieces: list[str] = []
        started = time.monotonic()
        error = ""
        base = {
            "project_id": job.project_id,
            "chapter_id": chapter_id,
            "kind": log_kind,
            "ai_config_id": job.ai_config_id,
            "provider": job.provider_name,
            "model": job.model,
            "params": {"prompt": prompt_meta(template_key, override), "attempt": attempt, **params},
            "messages": messages,
            "task_id": job.task_id,
        }

        try:
            stream = job.provider.generate_stream(messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking)
            async with aclosing(stream) as chunks:
                async for piece in chunks:
                    pieces.append(piece)
                    yield "delta", {"text": piece}
            text = "".join(pieces)
            if not text.strip():
                raise AIProviderError("模型未返回任何内容")
            value = parse(extract_json(text))
        except AIProviderError as exc:
            error = str(exc)
        except JSONExtractError as exc:
            error = f"AI 输出解析失败：{exc}"
        except asyncio.CancelledError:
            # 被取消（取消任务 / 服务关停）：屏蔽取消写一条 cancelled 记录后照常向上传递
            with anyio.CancelScope(shield=True):
                await generation_log_service.save_log(
                    ctx.sessionmaker, **_log_fields(base, "cancelled", "任务已取消", pieces, started)
                )
            raise
        status = "error" if error else "success"
        await generation_log_service.save_log(ctx.sessionmaker, **_log_fields(base, status, error, pieces, started))
        if not error:
            out["value"] = value
            return
        if attempt > MAX_RETRIES:
            out["error"] = error
            return


def _chapter_range(parts: list[list[int]]) -> str:
    nos = [p[3] for p in parts]
    return f"第 {nos[0]} 章" if nos[0] == nos[-1] else f"第 {nos[0]}–{nos[-1]} 章"


def _label(chapter: Chapter, k: int, m: int) -> str:
    title = f"第{chapter.chapter_no}章 {chapter.title}".strip()
    return f"【{title}（第{k + 1}/{m}段）】" if m > 1 else f"【{title}】"


async def _write_summary(db: AsyncSession, outline_id: int | None, summary: str, stats: dict[str, Any]) -> None:
    """只写空摘要。"""
    if outline_id is None or not summary:
        return
    outline = await db.get(Outline, outline_id)
    if outline is not None and not (outline.summary or "").strip():
        outline.summary = summary[:SUMMARY_LIMIT]
        stats["summaries"] = int(stats.get("summaries") or 0) + 1


def _join(texts: list[str]) -> str:
    seen: list[str] = []
    for t in texts:
        if t and t not in seen:
            seen.append(t)
    return "；".join(seen)


async def _digest_batch(ctx: "TaskContext", job: ImportJob, index: int) -> AsyncIterator["Event"]:
    """通读一批。成功时摘要写入、结果合并与断点同一事务提交；失败时只在 checkpoint.pause_reason 记下原因。"""
    cp = job.checkpoint
    batch = cp["batches"][index]
    async with ctx.sessionmaker() as db:
        project = await db.get(Project, job.project_id)
        if project is None:
            raise BadRequestError("项目不存在")
        ids = {p[0] for p in batch["parts"]}
        chapters = {
            c.id: c
            for c in await db.scalars(select(Chapter).where(Chapter.id.in_(ids), Chapter.project_id == job.project_id))
        }
        info = format_project_info(await project_info(db, project))
        override = await prompt_service.get_override(db, job.user_id, "book_import.digest")
    blocks: list[str] = []
    present: list[tuple[Chapter, int, int]] = []
    for cid, k, m, no in batch["parts"]:
        chapter = chapters.get(cid)
        if chapter is None:
            _warn(cp, f"第 {no} 章已被删除，通读时已跳过")
            continue
        parts = split_parts(chapter.content)
        if k >= len(parts) or not parts[k].strip():
            # 正文在装批后被改短：这一段已不存在，记为空摘要以便其余段照常拼接
            if m > 1:
                cp["part_summaries"].setdefault(str(cid), {})[str(k)] = ""
            continue
        blocks.append(f"{_label(chapter, k, m)}\n{parts[k].strip()}")
        present.append((chapter, k, m))
    first_id = present[0][0].id if present else None
    stage = f"通读 第 {index + 1}/{job.total} 批（{_chapter_range(batch['parts'])}）"
    yield "stage", {"stage": stage}
    normalized: dict[str, Any] = {"summaries": {}}
    if blocks:
        messages = build_book_digest_messages(
            project_info=info,
            known_characters=merge.known_characters_text(cp["findings"].get("characters") or []),
            chapters_text="\n\n".join(blocks),
            override=override,
        )
        out: dict[str, Any] = {}
        async for event in _call(
            ctx,
            job,
            messages,
            parse=normalize_digest,
            log_kind="book_import.digest",
            template_key="book_import.digest",
            override=override,
            chapter_id=first_id,
            stage=stage,
            params={"batch_index": index},
            out=out,
        ):
            yield event
        if "error" in out:
            cp["pause_reason"] = {"reason": "failed", "phase": "digest", "batch_index": index, "error": out["error"]}
            return
        normalized = out["value"]
    stats = cp["stats"]
    summaries: dict[int, list[str]] = normalized["summaries"]
    async with ctx.sessionmaker() as db:
        for chapter, k, m in present:
            text = _join(summaries.get(chapter.chapter_no, []))
            if not text and len(present) == 1 and len(summaries) == 1:
                # 只有一章时不强求章节号对得上
                text = _join(next(iter(summaries.values())))
            if m == 1:
                await _write_summary(db, chapter.outline_id, text, stats)
                continue
            cache = cp["part_summaries"].setdefault(str(chapter.id), {})
            cache[str(k)] = text
            if len(cache) >= m:
                joined = _join([cache.get(str(i), "") for i in range(m)])
                await _write_summary(db, chapter.outline_id, joined, stats)
                cp["part_summaries"].pop(str(chapter.id), None)
        if blocks:
            cp["findings"] = merge.accumulate(cp["findings"], {k: v for k, v in normalized.items() if k != "summaries"})
        batch["state"], batch["error"] = "done", ""
        job.cursor = index + 1
        await _write_checkpoint(db, job)
        await db.commit()
    yield "batch_end", {"index": index, "total": job.total, "state": "done"}


async def _synthesize(ctx: "TaskContext", job: ImportJob) -> AsyncIterator["Event"]:
    cp = job.checkpoint
    nos = _reverse_range(job.request.get("reverse_chapter_nos"))
    async with ctx.sessionmaker() as db:
        project = await db.get(Project, job.project_id)
        if project is None:
            raise BadRequestError("项目不存在")
        info = format_project_info(await project_info(db, project))
        rows = await db.execute(
            select(Outline.chapter_no, Outline.title, Outline.summary)
            .where(
                Outline.project_id == job.project_id,
                Outline.chapter_no >= nos[0],
                Outline.chapter_no <= nos[1],
                Outline.summary != "",
            )
            .order_by(Outline.chapter_no, Outline.id)
        )
        lines = [f"第{no}章 {title}：{summary}" for no, title, summary in rows.all()]
        override = await prompt_service.get_override(db, job.user_id, "book_import.synthesize")
    stage = "汇总设定"
    yield "stage", {"stage": stage}
    drafts = draft_texts(cp["findings"])
    messages = build_book_synthesize_messages(
        project_info=info, summaries=sample_summaries(lines), **drafts, override=override
    )
    out: dict[str, Any] = {}
    async for event in _call(
        ctx,
        job,
        messages,
        parse=normalize_final,
        log_kind="book_import.synthesize",
        template_key="book_import.synthesize",
        override=override,
        chapter_id=None,
        stage=stage,
        params={},
        out=out,
    ):
        yield event
    if "error" in out:
        cp["pause_reason"] = {"reason": "failed", "phase": "synthesize", "batch_index": None, "error": out["error"]}
        return
    cp["final"] = fill_final(out["value"], cp["findings"])
    cp["phase"] = "apply"
    await _save(ctx, job)


def _keys(name: str, aliases: list[str] | None = None) -> set[str]:
    return {merge.norm(n) for n in [name, *(aliases or [])]} - {""}


async def apply_final(db: AsyncSession, project: Project, final: dict[str, Any], cp: dict[str, Any]) -> None:
    """把 final 写入项目（只写空字段、同名跳过）；不 commit。"""
    stats = cp["stats"]
    written: list[str] = []
    for field_name, value in final.get("project", {}).items():
        if field_name == "tags":
            if value and not project.tags:
                project.tags = list(value)
                written.append(field_name)
        elif value and not (getattr(project, field_name) or "").strip():
            setattr(project, field_name, value)
            written.append(field_name)
    stats["project_fields"] = written

    characters = list(await db.scalars(select(Character).where(Character.project_id == project.id)))
    taken: set[str] = set()
    for c in characters:
        taken |= _keys(c.name, c.aliases)
    for row in final.get("characters") or []:
        keys = _keys(row["name"], row.get("aliases"))
        if not keys or keys & taken:
            continue
        values = {
            k: row[k]
            for k in (
                "name",
                "role_type",
                "profile",
                "aliases",
                "age",
                "gender",
                "appearance",
                "personality",
                "background",
                "always_in_context",
            )
            if k in row
        }
        characters.append(await story_service.new_character(db, project.id, values))
        taken |= keys
        stats["characters"] += 1
    lookup: dict[str, int] = {}
    for c in characters:
        for key in _keys(c.name, c.aliases):
            lookup.setdefault(key, c.id)

    org_rows = await db.scalars(select(Organization.name).where(Organization.project_id == project.id))
    org_names = {merge.norm(n) for n in org_rows}
    for o in final.get("organizations") or []:
        if not merge.norm(o.get("name")) or merge.norm(o["name"]) in org_names:
            continue
        org = await organization_service.new_org(
            db,
            project.id,
            OrganizationCreate(
                name=o["name"][:100],
                org_type=(o.get("org_type") or "")[:50],
                purpose=o.get("purpose") or "",
                description=o.get("description") or "",
            ),
        )
        org_names.add(merge.norm(o["name"]))
        stats["organizations"] += 1
        for m in o.get("members") or []:
            cid = lookup.get(merge.norm(m.get("name")))
            if cid is None:
                continue
            try:
                await organization_service.new_member(
                    db, org, MemberCreate(character_id=cid, position=(m.get("position") or "")[:50])
                )
            except AppError:
                continue

    career_rows = await db.scalars(select(Career.name).where(Career.project_id == project.id))
    career_names = {merge.norm(n) for n in career_rows}
    for c in final.get("careers") or []:
        if not merge.norm(c.get("name")) or merge.norm(c["name"]) in career_names or not c.get("stages"):
            continue
        try:
            data = CareerCreate(
                name=c["name"][:100],
                kind=c.get("kind") if c.get("kind") in ("main", "sub") else "main",
                description=c.get("description") or "",
                stages=[StageIn(name=s["name"][:50], description=s.get("description") or "") for s in c["stages"]][
                    :MAX_STAGES
                ],
            )
            await career_service.new_career(db, project.id, data)
        except (AppError, ValidationError) as exc:
            _warn(cp, f"职业「{c['name']}」未能写入：{getattr(exc, 'detail', '格式不正确')}")
            continue
        career_names.add(merge.norm(c["name"]))
        stats["careers"] += 1

    title_rows = await db.scalars(select(WorldSetting.title).where(WorldSetting.project_id == project.id))
    titles = {merge.norm(t) for t in title_rows}
    sort = await story_service.next_sort(db, WorldSetting, project.id)
    for w in final.get("world") or []:
        if not merge.norm(w.get("title")) or merge.norm(w["title"]) in titles:
            continue
        await story_service.new_world(
            db, project.id, {
                "title": w["title"][:200],
                "content": w.get("content") or "",
                "category": wizard_service.normalize_world_category(w.get("category")),
                "sort": sort,
            },
        )
        titles.add(merge.norm(w["title"]))
        sort += 1
        stats["world"] += 1
    for r in final.get("relations") or []:
        a, b = lookup.get(merge.norm(r.get("a"))), lookup.get(merge.norm(r.get("b")))
        if a is None or b is None or a == b or not r.get("role_a"):
            continue
        if await relationship_service.find_pair(db, a, b) is not None:
            continue
        try:
            await relationship_service.create_relation(
                db,
                project.id,
                a_id=a,
                b_id=b,
                role_a=r["role_a"],
                role_b=r.get("role_b") or r["role_a"],
                category=r.get("category") or "other",
                intimacy=int(r.get("intimacy") or 0),
                description=r.get("description") or "",
                source="import",
                note="拆书导入",
            )
        except AppError as exc:
            _warn(cp, f"「{r['a']}」与「{r['b']}」的关系未能写入：{exc.detail}")
            continue
        stats["relations"] += 1


async def _apply(ctx: "TaskContext", job: ImportJob) -> None:
    cp = job.checkpoint
    final = cp.get("final") or fallback_final(cp.get("findings") or {})
    async with ctx.sessionmaker() as db:
        project = await project_lifecycle.lock_active(db, job.project_id)
        await apply_final(db, project, final, cp)
        cp["phase"] = "style"
        await _write_checkpoint(db, job)
        await db.commit()


async def _style_sample(db: AsyncSession, project_id: int, nos: list[int]) -> str:
    """反推范围内的第一、中间、最后一章，各取前 2000 字。"""
    ids = list(
        await db.scalars(
            select(Chapter.id)
            .where(
                Chapter.project_id == project_id,
                Chapter.chapter_no >= nos[0],
                Chapter.chapter_no <= nos[1],
                Chapter.word_count > 0,
            )
            .order_by(Chapter.chapter_no, Chapter.id)
        )
    )
    if not ids:
        return ""
    picks = list(dict.fromkeys([ids[0], ids[len(ids) // 2], ids[-1]]))
    chapters = {c.id: c for c in await db.scalars(select(Chapter).where(Chapter.id.in_(picks)))}
    return "\n\n".join(chapters[i].content.strip()[:STYLE_SAMPLE_CHARS] for i in picks if i in chapters)


def style_name(title: str, taken: set[str]) -> str:
    """「《书名》原文风格」，重名时依次加「（2）」「（3）」……，按列长度截断书名。"""
    n = 1
    while True:
        tail = "原文风格" + (f"（{n}）" if n > 1 else "")
        room = STYLE_NAME_LIMIT - len(tail) - 2
        name = f"《{title[:room]}》{tail}"
        if name not in taken:
            return name
        n += 1


async def _style(ctx: "TaskContext", job: ImportJob) -> AsyncIterator["Event"]:
    cp = job.checkpoint
    stage = "提取写作风格"
    yield "stage", {"stage": stage}
    async with ctx.sessionmaker() as db:
        sample = await _style_sample(db, job.project_id, _reverse_range(job.request.get("reverse_chapter_nos")))
        override = await prompt_service.get_override(db, job.user_id, "style.extract")
    draft: dict[str, Any] | None = None
    if len(sample.strip()) < STYLE_MIN_CHARS:
        _warn(cp, f"样章不足 {STYLE_MIN_CHARS} 字，未提取写作风格")
    else:
        messages = build_style_extract_messages(sample_text=sample, override=override)
        out: dict[str, Any] = {}
        async for event in _call(
            ctx,
            job,
            messages,
            parse=lambda obj: style_service.normalize_draft(obj)[0],
            log_kind="book_import.style",
            template_key="style.extract",
            override=override,
            chapter_id=None,
            stage=stage,
            params={},
            out=out,
        ):
            yield event
        if "error" in out:
            _warn(cp, f"写作风格提取失败：{out['error']}")
        else:
            draft = out["value"]
    async with ctx.sessionmaker() as db:
        if draft is not None:
            project = await db.get(Project, job.project_id)
            if project is None:
                raise BadRequestError("项目不存在")
            taken = set(await db.scalars(select(WritingStyle.name).where(WritingStyle.user_id == job.user_id)))
            values = {k: str(draft.get(k) or "").strip()[:limit] for k, limit in style_service.FIELD_LIMITS.items()}
            style = WritingStyle(user_id=job.user_id, name=style_name(project.title, taken), **values)
            db.add(style)
            await db.flush()
            if project.default_style_id is None:
                project.default_style_id = style.id
            cp["stats"]["style_id"] = style.id
        cp["phase"] = "analyze"
        await _write_checkpoint(db, job)
        await db.commit()


async def _analyze(ctx: "TaskContext", job: ImportJob) -> None:
    cp = job.checkpoint
    async with ctx.sessionmaker() as db:
        project = await db.get(Project, job.project_id)
        if project is None:
            raise BadRequestError("项目不存在")
        ids = await analysis_service.enqueue_pending(db, project, ai_config_id=job.request.get("ai_config_id"))
        cp["stats"]["analyses_queued"] = len(ids)
        cp["phase"] = "done"
        await _write_checkpoint(db, job)
        await db.commit()


def _paused(job: ImportJob) -> "Event":
    return "paused", summarize(job.checkpoint)


async def run_import_job(job: ImportJob, ctx: "TaskContext") -> AsyncIterator["Event"]:
    cp = job.checkpoint
    if cp["phase"] == "digest":
        while job.cursor < job.total:
            index = job.cursor
            if cp["batches"][index]["state"] != "pending":
                job.cursor += 1
                continue
            async for event in _digest_batch(ctx, job, index):
                yield event
            if cp.get("pause_reason"):
                await _save(ctx, job)
                yield _paused(job)
                return
        cp["phase"] = "synthesize"
        await _save(ctx, job)
    if cp["phase"] == "synthesize":
        async for event in _synthesize(ctx, job):
            yield event
        if cp.get("pause_reason"):
            await _save(ctx, job)
            yield _paused(job)
            return
    if cp["phase"] == "apply":
        yield "stage", {"stage": "写入设定"}
        await _apply(ctx, job)
    if cp["phase"] == "style":
        if job.request.get("extract_style", True):
            async for event in _style(ctx, job):
                yield event
        else:
            cp["phase"] = "analyze"
            await _save(ctx, job)
    if cp["phase"] == "analyze":
        if job.request.get("deep_analyze"):
            yield "stage", {"stage": "排队深度分析"}
            await _analyze(ctx, job)
        else:
            cp["phase"] = "done"
            await _save(ctx, job)
    yield "done", summarize(cp)


async def skip_paused(db: AsyncSession, engine: Any, user_id: int, task_id: int) -> Any:
    """跳过因失败暂停的当前步骤后续跑（同一任务）。"""
    from app.services import task_service

    task = await task_service.get_owned_task(db, user_id, task_id)
    reason = (task.checkpoint or {}).get("pause_reason") or {}
    if task.kind != KIND or task.status != "paused" or reason.get("reason") != "failed":
        raise ConflictError(SKIP_CONFLICT)
    await revalidate(db, engine.ctx, task)
    cp = copy.deepcopy(task.checkpoint)
    cursor = apply_skip(cp, task.step_cursor)
    result = await db.execute(
        update(Task).where(
            Task.id == task.id, Task.statistics_key == task.statistics_key, Task.status == "paused",
            Task.admin_cancel_operation_id.is_(None),
        ).values(checkpoint=cp, step_cursor=cursor)
    )
    await db.commit()
    if result.rowcount != 1:
        raise ConflictError("任务状态已变化，请刷新后重试")
    await db.refresh(task)
    return await task_service.resume_task(db, engine, user_id, task_id)
