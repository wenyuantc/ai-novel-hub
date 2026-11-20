"""修改影响评估任务（consistency.impact）：单章，最多两次模型调用。

1. 取改动前事实：台账过期 → 现有事实即改动前，调用模型重抽并 save_ledger（旧值进入 prev_*）；
   台账已新鲜 → 不调用模型，上一版（prev_facts）即改动前。
2. 确定性比较：按 (类别, 主体键, 归一化陈述) 求集合差；完全相同 → 未发现剧情事实变化，不再调用模型。
3. 集合不同才调用一次模型（consistency.impact，只发送两边字面不同的事实陈述），判断哪些是真正的剧情变化；
   输出只能引用输入给出的编号，其余丢弃。
4. 受影响章节（零模型调用）：变化事实的主体键在后续章节**新鲜**台账中的命中；台账缺失或过期的后续章节记入 uncovered。
5. 落库 consistency_impacts（每章一行、覆盖）；同一后续章节仍在清单且其正文指纹未变时继承原确认状态。

受理、模型绑定、发送守卫与落库重验复用 check.py 的骨架：与检查共用 resource_key（互斥），每次出站经 for_task
发送 lease，每次落库事务内 ``guard.require_task_write``。评估只写台账与 consistency_impacts，
不改正文、摘要、记忆、设定与 consistency_issues，不写生成记录；模型原文不进入任务输出。
"""

import asyncio
import logging
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts.consistency import EXTRACT_KEY, IMPACT_KEY, build_consistency_impact_messages
from app.ai.providers.base import AIProviderError, ChatMessage
from app.ai.templating import PromptOverride
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.models import Chapter, ConsistencyImpact, Task
from app.schemas.consistency import MAX_IMPACT_EVIDENCE, MAX_IMPACT_SUBJECTS
from app.services import collaboration_ai_guard, project_ai_config
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.consistency import check, impact, ledger
from app.services.consistency.issues import CATEGORY_LABELS, _inline
from app.services.consistency.model_call import ModelTarget, complete
from app.services.mentions.matcher import text_hash
from app.services.rich_document_ai import analysis_fingerprints
from app.services.task_engine.execution_identity import TaskExecutionIdentity
from app.services.task_engine.registry import Event, TaskContext

logger = logging.getLogger(__name__)

KIND = check.IMPACT_KIND
CHANGED_DURING = "章节正文在评估期间发生变化，请重新评估"
NO_CHANGE_LIST = "模型输出中没有变化列表"
NO_SUBJECT = "变化的事实没有明确主体，无法按主体查找后续章节"


# ---------------- 差异（纯函数） ----------------


def _norm(text: Any) -> str:
    value = unicodedata.normalize("NFKC", text if isinstance(text, str) else "")
    return "".join(value.split()).casefold()


def fact_key(fact: dict[str, Any]) -> tuple[str, str, str]:
    """确定性比较的身份：类别、主体键、归一化陈述（全半角归一、去空白、大小写折叠）。"""
    return str(fact.get("category") or ""), str(fact.get("subject_key") or ""), _norm(fact.get("statement"))


def literal_diff(
    old: list[dict[str, Any]], new: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(改动前独有, 改动后独有)。两者皆空 = 事实集合完全相同。"""
    old_keys, new_keys = {fact_key(fact) for fact in old}, {fact_key(fact) for fact in new}
    return ([fact for fact in old if fact_key(fact) not in new_keys],
            [fact for fact in new if fact_key(fact) not in old_keys])


def _fact_lines(prefix: str, facts: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[{prefix}{index}] {CATEGORY_LABELS.get(fact.get('category'), fact.get('category'))}｜"
        f"{_inline(fact.get('subject'))}｜{_inline(fact.get('statement'))}｜{_inline(fact.get('time_hint'))}"
        for index, fact in enumerate(facts, start=1)
    )


def build_impact_messages(
    old_only: list[dict[str, Any]], new_only: list[dict[str, Any]], override: PromptOverride | None = None,
) -> list[ChatMessage]:
    """只发送两边字面不同的事实陈述（不含引句与正文）。编号 A* = 改动前，B* = 改动后。"""
    return build_consistency_impact_messages(
        before_text=_fact_lines("A", old_only), after_text=_fact_lines("B", new_only), override=override,
    )


def _change(kind: str, before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    base = after if after is not None else before
    return {
        "kind": kind,
        "category": base["category"],
        "subject": str(base.get("subject") or ""),
        "subject_key": str(base.get("subject_key") or ""),
        "before": str(before.get("statement") or "") if before is not None else "",
        "after": str(after.get("statement") or "") if after is not None else "",
    }


def parse_changes(
    text: str, old_only: list[dict[str, Any]], new_only: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str], list[str]]:
    """模型原文 → (变化列表, 变化涉及的主体键, warning)。

    只接受本次输入给出的编号，每个编号至多使用一次；非法条目丢弃并记 warning。输出不是 JSON 抛 JSONExtractError，
    没有变化列表抛 AIProviderError（不能把畸形输出当成「没有变化」）。
    """
    raw = extract_json(text)
    entries = raw.get("changes") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise AIProviderError(NO_CHANGE_LIST)
    before = {f"A{index}": fact for index, fact in enumerate(old_only, start=1)}
    after = {f"B{index}": fact for index, fact in enumerate(new_only, start=1)}
    used: set[str] = set()
    changes: list[dict[str, Any]] = []
    keys: set[str] = set()
    dropped = 0
    for entry in entries:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        kind = entry.get("kind")
        before_id, after_id = entry.get("before_id"), entry.get("after_id")
        before_id = before_id.strip().upper() if isinstance(before_id, str) else None
        after_id = after_id.strip().upper() if isinstance(after_id, str) else None
        wanted = {"added": (None, after_id), "removed": (before_id, None), "changed": (before_id, after_id)}.get(kind)
        if wanted is None:
            dropped += 1
            continue
        old_fact = before.get(wanted[0]) if wanted[0] is not None else None
        new_fact = after.get(wanted[1]) if wanted[1] is not None else None
        ids = {value for value in wanted if value is not None}
        if ((kind in ("removed", "changed") and old_fact is None) or (kind in ("added", "changed") and new_fact is None)
                or ids & used):
            dropped += 1
            continue
        used |= ids
        changes.append(_change(kind, old_fact, new_fact))
        keys |= {str(fact.get("subject_key") or "") for fact in (old_fact, new_fact) if fact is not None}
    warnings = [f"模型给出的 {dropped} 处变化引用无效，已忽略"] if dropped else []
    return changes, keys - {""}, warnings


# ---------------- 受影响章节 ----------------


@dataclass(frozen=True)
class Affected:
    items: list[dict[str, Any]]
    uncovered: list[int]


async def find_affected(db: AsyncSession, project_id: int, chapter: Chapter, keys: set[str]) -> Affected:
    """章号更大的非空章节里，新鲜台账中主体键命中的事实按章聚合；台账缺失或过期的章节记入 uncovered。只读。"""
    later = [
        row for row in await db.scalars(
            select(Chapter).where(Chapter.project_id == project_id, Chapter.chapter_no > chapter.chapter_no)
            .order_by(Chapter.chapter_no, Chapter.id)
        ) if row.content.strip()
    ]
    ledgers = await ledger.ledgers_by_chapter(db, project_id, [row.id for row in later])
    fingerprints = await analysis_fingerprints(db, [row for row in later if row.id in ledgers])
    items: list[dict[str, Any]] = []
    uncovered: list[int] = []
    for row in later:
        current = ledgers.get(row.id)
        if not ledger.is_fresh(current, fingerprints.get(row.id)):
            uncovered.append(row.id)
            continue
        facts = [fact for fact in current.facts or [] if isinstance(fact, dict) and fact.get("subject_key") in keys]
        if not facts:
            continue
        current_hash = text_hash(row.content)
        subjects = list(dict.fromkeys(str(fact.get("subject") or "") for fact in facts))[:MAX_IMPACT_SUBJECTS]
        items.append({
            "chapter_id": row.id,
            "subjects": subjects,
            "evidence": [
                {"fact_id": str(fact.get("id") or ""), "quote": str(fact.get("quote") or ""),
                 "start": fact.get("start"), "end": fact.get("end"), "text_hash": current_hash}
                for fact in facts[:MAX_IMPACT_EVIDENCE]
            ],
            "fact_count": len(facts),
            "status": "pending",
        })
    return Affected(items, uncovered)


def inherit_statuses(items: list[dict[str, Any]], previous: list[dict[str, Any]]) -> None:
    """同一后续章节在新旧结果中都存在、且其正文指纹与上次记录证据时相同：继承原确认状态。"""
    old = {item.get("chapter_id"): item for item in previous if isinstance(item, dict)}
    for item in items:
        before = old.get(item["chapter_id"])
        if before is None or before.get("status") not in ("reviewed", "dismissed"):
            continue
        hashes = {value.get("text_hash") for value in before.get("evidence") or [] if isinstance(value, dict)}
        if hashes == {value["text_hash"] for value in item["evidence"]}:
            item["status"] = before["status"]


# ---------------- 受理 ----------------


def normalize_request(project_id: int, params: dict[str, Any]) -> dict[str, Any]:
    ai_config_id, chapter_id = params.get("ai_config_id"), params.get("chapter_id")
    if ai_config_id is not None and (type(ai_config_id) is not int or ai_config_id <= 0):
        raise BadRequestError("模型配置无效")
    if type(chapter_id) is not int or chapter_id <= 0:
        raise BadRequestError("要评估的章节无效")
    return {"project_id": project_id, "chapter_id": chapter_id, "ai_config_id": ai_config_id}


async def require_assessable(db: AsyncSession, project_id: int, chapter_id: int) -> Chapter:
    """章节存在、正文非空且有改动前的台账；否则 404 / 400（提示先运行一次一致性检查）。只读。"""
    chapter = await db.scalar(
        select(Chapter).where(Chapter.id == chapter_id, Chapter.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    if chapter is None:
        raise NotFoundError(impact.CHAPTER_NOT_FOUND)
    fingerprint = (await analysis_fingerprints(db, [chapter]))[chapter.id]
    current = (await ledger.ledgers_by_chapter(db, project_id, [chapter.id])).get(chapter.id)
    block = impact.assess_block(chapter, current, fingerprint)
    if block is not None:
        raise BadRequestError(impact.block_message(block))
    return chapter


def _title(chapter: Chapter) -> str:
    return f"修改影响评估 · 第{chapter.chapter_no}章"


async def create_assess_task(
    db: AsyncSession, engine: Any, user_id: int, project_id: int, chapter_id: int, ai_config_id: int | None,
) -> Any:
    """HTTP 发起评估；受理流程（ai.use 短读、模型预检、中央受控受理）见 check.admit_task。"""
    request = normalize_request(project_id, {"chapter_id": chapter_id, "ai_config_id": ai_config_id})

    async def precheck(read: AsyncSession) -> None:
        await require_assessable(read, project_id, chapter_id)

    return await check.admit_task(db, engine, user_id, project_id, KIND, request, precheck)


def _task_input(request: dict[str, Any], binding: project_ai_config.ResolvedProjectModel) -> dict[str, Any]:
    return {
        "request": request,
        "chapter_id": request["chapter_id"],
        "ai_config_id": binding.config_id,
        "_project_model": project_ai_config.serialize_project_model(binding),
    }


async def validate_start(
    db: AsyncSession, ctx: TaskContext, *, user_id: int, params: dict[str, Any],
    admission: project_ai_config.ProjectModelPreflight, accepted_model: project_ai_config.ResolvedProjectModel,
) -> tuple[int, str, dict[str, Any]]:
    """受理事务内校验；返回 (project_id, 标题, task.input)。不修改数据、不解析 DNS。"""
    project_id = params.get("project_id")
    if type(project_id) is not int:
        raise check._binding_changed()  # noqa: SLF001
    request = normalize_request(project_id, params)
    binding = await check._accepted_binding(  # noqa: SLF001
        db, user_id=user_id, project_id=project_id, ai_config_id=request["ai_config_id"],
        allow_private=ctx.settings.allow_private_ai_endpoints, admission=admission, accepted_model=accepted_model,
    )
    chapter = await require_assessable(db, project_id, request["chapter_id"])
    await check.require_free(db, project_id)
    return project_id, _title(chapter), _task_input(request, binding)


async def validate_retry(
    db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task,
    admission: project_ai_config.ProjectModelPreflight, accepted_model: project_ai_config.ResolvedProjectModel,
) -> tuple[int, str, dict[str, Any]]:
    """重试：沿用原章节与原模型绑定；仍须有改动前的台账（上次已重抽成功时直接比较上一版，不再调用抽取）。"""
    data = task.input
    chapter_id = data.get("chapter_id") if type(data) is dict else None
    request, binding = await check.retry_binding(
        db, ctx, user_id=user_id, task=task, kind=KIND, admission=admission, accepted_model=accepted_model,
    )
    if type(chapter_id) is not int or request.get("chapter_id") != chapter_id:
        raise check._retry_changed()  # noqa: SLF001
    chapter = await require_assessable(db, task.project_id, chapter_id)
    await check.require_free(db, task.project_id)
    return task.project_id, _title(chapter), _task_input(request, binding)


# ---------------- 执行 ----------------


@dataclass
class ImpactJob:
    project_id: int
    actor_id: int
    task_id: int
    chapter_id: int
    target: ModelTarget = field(repr=False)
    guard: collaboration_ai_guard.SendGuard = field(repr=False)
    execution: TaskExecutionIdentity = field(repr=False)
    char_budget: int = ledger.DEFAULT_CHAR_BUDGET
    extract_override: PromptOverride | None = None
    impact_override: PromptOverride | None = None
    warnings: list[str] = field(default_factory=list)
    progress: int = 1
    # 已发布的进度日志全文（事件核验用）；模型原文不进入任务输出
    log: str = ""
    cancelled: bool = False
    # 影响记录提交后才有值；done 事件与终态核验逐字比对
    result: dict[str, Any] | None = None


async def build_impact_job(ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity) -> ImpactJob:
    data = execution.input if type(execution) is TaskExecutionIdentity and type(execution.input) is dict else {}
    chapter_id, request = data.get("chapter_id"), data.get("request")
    if type(chapter_id) is not int or type(request) is not dict or request.get("chapter_id") != chapter_id:
        raise ConflictError("任务执行身份已变化")
    bound = await check.bind_execution(ctx, task, execution, kind=KIND, prompt_keys=(EXTRACT_KEY, IMPACT_KEY))
    return ImpactJob(
        project_id=execution.project_id,
        actor_id=execution.actor_id,
        task_id=execution.task_id,
        chapter_id=chapter_id,
        target=bound.target,
        guard=bound.guard,
        execution=execution,
        char_budget=bound.char_budget,
        extract_override=bound.overrides[EXTRACT_KEY],
        impact_override=bound.overrides[IMPACT_KEY],
    )


def _log(job: ImpactJob, text: str) -> Event:
    job.log += text
    return "delta", {"text": text}


def _warn(job: ImpactJob, text: str) -> None:
    if len(job.warnings) < check.MAX_WARNINGS:
        job.warnings.append(text[:check.WARNING_LIMIT])


async def _save_impact(
    db: AsyncSession, job: ImpactJob, source: ledger.ChapterSource, *, base_source_hash: str | None,
    changes: list[dict[str, Any]], affected: Affected,
) -> None:
    """在调用方事务内覆盖本章的影响记录（不 commit）。调用方须已 require_task_write（持作品行锁）。"""
    if await db.scalar(select(Chapter.id).where(
        Chapter.id == job.chapter_id, Chapter.project_id == job.project_id,
    ).with_for_update()) is None:
        raise NotFoundError(impact.CHAPTER_NOT_FOUND)
    row = await db.scalar(
        select(ConsistencyImpact)
        .where(ConsistencyImpact.chapter_id == job.chapter_id, ConsistencyImpact.project_id == job.project_id)
        .execution_options(populate_existing=True)
    )
    if row is None:
        row = ConsistencyImpact(project_id=job.project_id, chapter_id=job.chapter_id, acknowledged=[], revision=1)
        db.add(row)
    else:
        if row.assessed_at is not None:
            inherit_statuses(affected.items, row.items or [])
        # 影响项整体替换：递增修订，让基于旧清单的确认请求按 CAS 失败
        row.revision = row.revision + 1
    row.source_hash = source.source_hash
    row.text_hash = source.text_hash
    row.base_source_hash = base_source_hash
    row.changes = changes
    row.items = affected.items
    row.uncovered = affected.uncovered
    row.actor_id = job.actor_id
    row.task_id = job.task_id
    row.model = job.target.model[:200]
    row.assessed_at = datetime.now(UTC)
    await db.flush()


async def run_impact_job(job: ImpactJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    """事件：stage（阶段文案）、delta（进度日志，不含模型原文）、done（概要）、error。"""
    try:
        async with sessionmaker() as db:
            source = await ledger.load_source(db, job.project_id, job.chapter_id, char_budget=job.char_budget)
            if source is None:
                raise NotFoundError(impact.CHAPTER_NOT_FOUND)
            if not source.content.strip():
                raise BadRequestError(impact.EMPTY_CHAPTER)
            current = (await ledger.ledgers_by_chapter(db, job.project_id, [job.chapter_id])).get(job.chapter_id)
            if current is None:
                raise BadRequestError(impact.NO_BASELINE)
            fresh = ledger.is_fresh(current, source.source_hash)
            if fresh and current.prev_facts is None:
                raise BadRequestError(impact.UNCHANGED)
            if fresh:
                old, new = list(current.prev_facts), list(current.facts or [])
                base_source_hash = current.prev_source_hash
            else:
                old, new, base_source_hash = list(current.facts or []), [], current.source_hash
        extracted = not fresh
        if fresh:
            # 台账已新鲜（例如刚做过一次检查）：不调用抽取，直接比较上一版
            yield _log(job, "事实台账与当前正文一致，直接与上一版比较\n")
        else:
            yield "stage", {"stage": "抽取本章事实"}
            text = await complete(job.target, ledger.build_extract_messages(source, job.extract_override))
            if not text.strip():
                raise AIProviderError(check.EMPTY_OUTPUT)
            new, warnings = ledger.parse_extraction(text, source)
            async with sessionmaker() as db:
                await job.guard.require_task_write(db)
                # save_ledger 锁内重验章节仍存在且指纹未变，并把旧事实移入 prev_*
                if await ledger.save_ledger(db, source, new, model=job.target.model, task_id=job.task_id) is None:
                    raise ConflictError(CHANGED_DURING)
                await db.commit()
            for warning in warnings:
                _warn(job, warning)
            yield _log(job, f"重新抽取 {len(new)} 条事实\n")
        job.progress = 40

        old_only, new_only = literal_diff(old, new)
        changes: list[dict[str, Any]] = []
        keys: set[str] = set()
        if old_only or new_only:
            yield "stage", {"stage": "判定事实变化"}
            text = await complete(job.target, build_impact_messages(old_only, new_only, job.impact_override))
            if not text.strip():
                raise AIProviderError(check.EMPTY_OUTPUT)
            changes, keys, warnings = parse_changes(text, old_only, new_only)
            for warning in warnings:
                _warn(job, warning)
        job.progress = 75
        yield _log(job, f"发现 {len(changes)} 处剧情事实变化\n" if changes else "未发现剧情事实变化\n")

        affected = Affected([], [])
        if changes and keys:
            yield "stage", {"stage": "查找受影响章节"}
            async with sessionmaker() as db:
                chapter = await db.scalar(select(Chapter).where(
                    Chapter.id == job.chapter_id, Chapter.project_id == job.project_id,
                ))
                if chapter is None:
                    raise NotFoundError(impact.CHAPTER_NOT_FOUND)
                affected = await find_affected(db, job.project_id, chapter, keys)
            yield _log(job, f"后续 {len(affected.items)} 章可能受影响，{len(affected.uncovered)} 章无法判断\n")
        elif changes:
            _warn(job, NO_SUBJECT)

        yield "stage", {"stage": "保存评估结果"}
        async with sessionmaker() as db:
            # require_task_write 持有作品行锁直到提交：与作者的确认状态 CAS 串行。
            await job.guard.require_task_write(db)
            await _save_impact(
                db, job, source, base_source_hash=base_source_hash, changes=changes, affected=affected,
            )
            await db.commit()
        job.progress = 95
        job.result = {
            "chapter_id": job.chapter_id,
            "extracted": extracted,
            "no_change": not changes,
            "changes": len(changes),
            "items": len(affected.items),
            "uncovered": len(affected.uncovered),
            "warnings": list(job.warnings),
        }
        yield "done", job.result
    except asyncio.CancelledError:
        job.cancelled = True
        raise
    except JSONExtractError as exc:
        yield "error", {"detail": f"AI 输出解析失败：{exc}，请重试"}
    except AIProviderError as exc:
        yield "error", {"detail": str(exc)}
    except VisibilityPolicyError:
        yield "error", {"detail": check.STOPPED}
    except AppError as exc:
        yield "error", {"detail": exc.detail}
    except Exception:  # noqa: BLE001
        logger.error("修改影响评估失败 project=%s task=%s", job.project_id, job.task_id)
        yield "error", {"detail": "修改影响评估失败：服务器内部错误"}
