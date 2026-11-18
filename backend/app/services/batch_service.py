"""批量顺序生成（后台任务 chapter.batch）：范围求值、预览、校验、断点与逐章执行循环。

- 整个批量是一个任务（resource_key = batch:{project_id}）。范围在发起时按 (chapter_no, id) 定下章节 id 列表
  （input.chapter_ids，最多 MAX_CHAPTERS 章），之后插入到范围内的章节不纳入。
- step_cursor 指向下一章在 chapter_ids 中的下标；checkpoint 见 new_checkpoint。正文、版本与完成收据同事务保存；
  重试和继续须核验原轮次，外部请求或写入结果未知时不自动重发。
- 逐章以独立 claim 调用 build_chapter_job → run_chapter_job（失败时精确回退原占用；成功时存版本和完成收据）；
  分析复用 analysis_service.build_analysis_job → run_analysis_job
  （回写随分析落库）。分析在本任务内串行执行，不另外排队、不占额外并发名额；部署开关或项目开关关闭时跳过。
- 每章 build_job 在循环里临时组装，所以第 N 章的上下文看到的是第 N-1 章正文与分析回写之后的数据库状态。
- 审核点：每生成 review_every 章且后面还有要处理的章节时产出 paused，任务转为待审核（不占名额、不锁范围）。
- 单章生成失败重试 MAX_RETRIES 次（退避 RETRY_DELAYS 秒）；仍失败时 skip_on_fail 决定跳过继续还是任务失败。
"""

import asyncio
import copy
import json
import logging
import math
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import String, cast, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, BadRequestError, ConflictError
from app.core.security import SecretCipher
from app.models import (
    Chapter,
    ChapterAnalysis,
    ChapterPlan,
    ChapterVersion,
    GenerationLog,
    Project,
    SceneReviewAnalysisReceipt,
    SceneReviewProposal,
    Task,
)
from app.models.entities import OPEN_TASK_STATUSES
from app.schemas.batch import BatchGenerateRequest, BatchPreview, BatchPreviewItem, BatchSkippedItem
from app.schemas.chapter import GenerateChapterRequest
from app.services import (
    ai_config_service,
    ai_dispatch,
    analysis_service,
    authorization_service,
    chapter_claim,
    collaboration_ai_guard,
    generation_service,
    project_access,
    project_ai_config,
    project_lifecycle,
    setting_ai_visibility,
    skill_service,
    style_service,
)
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.authorization_service import AuthenticatedActor
from app.services.collaboration_task_access import owner_ai_allowed, task_requires_owner_ai
from app.services.project_service import get_owned_project
from app.services.task_engine.execution_identity import (
    TaskExecutionIdentity,
    advance_checkpoint,
    matches_running,
    require_write_execution,
)

if TYPE_CHECKING:
    from app.services.task_engine.registry import Event, TaskContext

logger = logging.getLogger(__name__)

KIND = "chapter.batch"
MAX_CHAPTERS = 200
MAX_RETRIES = 2
# 单章失败后第 1 / 2 次重试前的等待秒数（测试中改为 0）
RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0)
# 预估耗时：每 3000 字约 2 分钟，开启分析时每章另加 1 分钟
MINUTES_PER_3000 = 2.0
ANALYZE_MINUTES = 1.0

OPEN_BATCH_CONFLICT = "已有进行中或待审核的批量生成"
NOTHING_TO_GENERATE = "范围内没有需要生成的章节"

ITEM_STATES = ("pending", "done", "skipped_written", "skipped_missing", "failed")


def _same_json(left: Any, right: Any) -> bool:
    """JSON 身份必须连同类型比较；Python 的 True == 1 不能证明相同断点。"""
    try:
        return json.dumps(left, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(
            right, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
    except (TypeError, ValueError):
        return False


def resource_key(project_id: int) -> str:
    return f"batch:{project_id}"


# ---------------- 范围与字数 ----------------


async def _range_chapters(
    db: AsyncSession, project_id: int, req: BatchGenerateRequest
) -> tuple[list[Chapter], Chapter, Chapter]:
    ends = {
        c.id: c
        for c in await db.scalars(
            select(Chapter).where(
                Chapter.project_id == project_id, Chapter.id.in_((req.start_chapter_id, req.end_chapter_id))
            )
        )
    }
    start, end = ends.get(req.start_chapter_id), ends.get(req.end_chapter_id)
    if start is None or end is None:
        raise BadRequestError("起止章节不存在或不属于该项目")
    if start.chapter_no > end.chapter_no:
        raise BadRequestError("起始章节不能晚于结束章节")
    chapters = list(
        await db.scalars(
            select(Chapter)
            .where(
                Chapter.project_id == project_id,
                Chapter.chapter_no >= start.chapter_no,
                Chapter.chapter_no <= end.chapter_no,
            )
            .order_by(Chapter.chapter_no, Chapter.id)
        )
    )
    if len(chapters) > MAX_CHAPTERS:
        raise BadRequestError(f"一次批量最多 {MAX_CHAPTERS} 章，当前范围有 {len(chapters)} 章")
    return chapters, start, end


async def plan_words(db: AsyncSession, chapters: list[Chapter]) -> dict[int, int]:
    """章节 id → 细纲 est_words（只含有细纲的章节）。"""
    plan_ids = {c.plan_id: c.id for c in chapters if c.plan_id is not None}
    if not plan_ids:
        return {}
    rows = await db.execute(select(ChapterPlan.id, ChapterPlan.est_words).where(ChapterPlan.id.in_(plan_ids)))
    return {plan_ids[pid]: words for pid, words in rows.all()}


def target_for(req: BatchGenerateRequest, chapter_id: int, plans: dict[int, int]) -> tuple[int, str]:
    """(目标字数, 来源 plan | uniform)：勾选使用细纲字数且章节有细纲时取 est_words，否则统一字数。"""
    if req.use_plan_words and chapter_id in plans:
        return max(300, min(20000, int(plans[chapter_id] or req.target_words))), "plan"
    return req.target_words, "uniform"


def is_written(chapter: Chapter) -> bool:
    return bool(chapter.content.strip())


def _chapter_nos(chapters: list[Chapter]) -> str:
    return "、".join(str(c.chapter_no) for c in chapters)


def batch_title(first_no: int, last_no: int) -> str:
    return f"批量生成 第{first_no}章" if first_no == last_no else f"批量生成 第{first_no}–{last_no}章"


async def open_batch(db: AsyncSession, project_id: int, *, exclude_id: int | None = None) -> Task | None:
    stmt = select(Task).where(Task.project_id == project_id, Task.kind == KIND, Task.status.in_(OPEN_TASK_STATUSES))
    if exclude_id is not None:
        stmt = stmt.where(Task.id != exclude_id)
    return await db.scalar(stmt.limit(1))


def analyze_enabled(settings: Any, project: Project) -> bool:
    return bool(settings.auto_analyze_enabled and project.auto_analyze)


# ---------------- 预览 ----------------


async def preview(
    db: AsyncSession, settings: Any, user_id: int, project_id: int, req: BatchGenerateRequest, *, cipher: SecretCipher,
) -> BatchPreview:
    from app.services.batch_cost_service import estimate_batch_cost

    # 预览是发起批量的前置步骤：与发起同一能力（作者或 accepted editor 的 ai.use；viewer 403、无协作来源 404）。
    project = await project_access.require_project(db, user_id, project_id, "ai.use")
    chapters, _, _ = await _range_chapters(db, project.id, req)
    plans = await plan_words(db, chapters)
    analyze = analyze_enabled(settings, project)
    items: list[BatchPreviewItem] = []
    skipped: list[BatchSkippedItem] = []
    for c in chapters:
        if is_written(c) and not req.include_written:
            skipped.append(
                BatchSkippedItem(chapter_id=c.id, chapter_no=c.chapter_no, title=c.title, word_count=c.word_count)
            )
            continue
        words, source = target_for(req, c.id, plans)
        items.append(
            BatchPreviewItem(
                chapter_id=c.id,
                chapter_no=c.chapter_no,
                title=c.title,
                target_words=words,
                words_source=source,  # type: ignore[arg-type]
                written=is_written(c),
            )
        )
    minutes = sum(i.target_words / 3000 * MINUTES_PER_3000 + (ANALYZE_MINUTES if analyze else 0) for i in items)
    warnings: list[str] = []
    if not items:
        warnings.append(NOTHING_TO_GENERATE)
    generating = [c for c in chapters if c.status == "generating"]
    if generating:
        warnings.append(f"第 {_chapter_nos(generating)} 章正在生成中，请等待其完成后再发起")
    outline_ids = {c.outline_id for c in chapters if c.outline_id is not None}
    if outline_ids:
        unbuilt = await db.scalar(
            select(ChapterPlan.id)
            .where(ChapterPlan.outline_id.in_(outline_ids), ChapterPlan.chapter_id.is_(None))
            .limit(1)
        )
        if unbuilt is not None:
            warnings.append("范围内有细纲还没建章，批量不会自动建章，请先到大纲页建章")
    if not analyze:
        warnings.append("项目已关闭自动分析，批量过程中不做分析")
    if await open_batch(db, project.id) is not None:
        warnings.append(OPEN_BATCH_CONFLICT)
    return BatchPreview(
        total=len(chapters),
        items=items,
        skipped=skipped,
        analyze=analyze,
        estimated_minutes=math.ceil(minutes),
        warnings=warnings,
        cost=await estimate_batch_cost(db, settings, cipher, user_id, project, req, chapters, items, analyze),
    )


# ---------------- 校验（创建 / 重试 / 继续） ----------------


async def _check_config(db: AsyncSession, ctx: "TaskContext", user_id: int, ai_config_id: int | None) -> int:
    cfg = await ai_config_service.resolve_config(db, user_id, ai_config_id)
    # 只做校验（解密 + 端点防护），每章运行时再按最新配置重新构建
    await ai_config_service.build_provider(
        cfg,
        ctx.cipher,
        ctx.factory,
        ctx.settings.ai_request_timeout,
        allow_private=ctx.settings.allow_private_ai_endpoints,
    )
    return cfg.id


async def _check_not_generating(db: AsyncSession, chapter_ids: list[int], *, include_written: bool) -> None:
    if not chapter_ids:
        return
    chapters = list(
        await db.scalars(
            select(Chapter)
            .where(Chapter.id.in_(chapter_ids), Chapter.status == "generating")
            .order_by(Chapter.chapter_no, Chapter.id)
        )
    )
    busy = [c for c in chapters if include_written or not is_written(c)]
    if busy:
        raise ConflictError(f"范围内有章节正在生成中：第 {_chapter_nos(busy)} 章，请等待其完成后再试")


def _admission_changed() -> ConflictError:
    return ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})


async def create_batch_task(
    db: AsyncSession, engine: Any, user_id: int, project_id: int, req: BatchGenerateRequest,
) -> Any:
    """HTTP 发起：任何写锁前完成能力短读、模型预检与 DNS（受理证明），再交中央受理在同一写事务内消费。

    作者与 accepted editor 共用：本人模型优先，仅本人无文本配置且作者授权时回退作者配置，本人配置无效不回退；
    viewer 403、待接受/非成员/已撤权/仅管理员身份 404，均不建任务、不抢占。
    """
    from app.services import task_service
    from app.services.task_engine.admission import prepare_task_admission

    actor = authorization_service.actor_snapshot(db, user_id)
    if (type(actor) is not AuthenticatedActor or actor.id != user_id
            or type(actor.token_version) is not int or actor.token_version < 0 or db.new or db.dirty or db.deleted):
        raise ConflictError("原认证身份无法验证", extra={"code": "task_actor_unavailable"})
    params = {"project_id": project_id, **req.model_dump(mode="json")}
    # 请求认证产生的只读事务在 DNS 前结束，不占用连接或锁。
    await db.rollback()
    ctx = engine.ctx
    async with ctx.sessionmaker() as read:
        await authorization_service.require_authenticated(read, actor)
        await project_access.require_project(read, actor.id, project_id, "ai.use")
    model = await project_ai_config.preflight_project_model(
        ctx.sessionmaker, actor, project_id, "text", cipher=ctx.cipher, config_id=req.ai_config_id,
        allow_private=ctx.settings.allow_private_ai_endpoints,
    )
    admission = await prepare_task_admission(ctx, actor, KIND, params, model_preflight=model)
    return await task_service.create_task(db, engine, user_id, KIND, params, admission=admission)


async def validate_start(
    db: AsyncSession, ctx: "TaskContext", *, user_id: int, project_id: int, req: BatchGenerateRequest,
    accepted_model: project_ai_config.ResolvedProjectModel,
) -> dict[str, Any]:
    """受理事务内校验；返回 TaskSpec 所需字段。不修改数据、不解析 DNS。

    中央已在 认证 → 发送写门禁 → 作品 屏障内消费锁外预检并重选同一模型（accepted_model）；
    这里按原 grant 锁后重验 ai.use，并在同一屏障内冻结共享向量选择（协作者未获作者授权 → keyword）。
    """
    if (type(accepted_model) is not project_ai_config.ResolvedProjectModel or accepted_model.actor_id != user_id
            or accepted_model.model_type != "text" or accepted_model.project_id != project_id
            or (req.ai_config_id is not None and req.ai_config_id != accepted_model.config_id)):
        raise _admission_changed()
    with db.no_autoflush:
        project = await project_access.require_project(
            db, user_id, project_id, "ai.use", for_update=True, visibility_write=True,
            grant_identity=accepted_model.grant_identity,
        )
    if project.user_id != accepted_model.project_owner_id:
        raise _admission_changed()
    chapters, _, _ = await _range_chapters(db, project.id, req)
    model_binding = accepted_model
    ai_config_id = model_binding.config_id
    embedding_selection = await project_ai_config.resolve_shared_embedding(
        db, user_id, project.id, cipher=ctx.cipher,
        allow_private=ctx.settings.allow_private_ai_endpoints, for_update=True,
    )
    if (embedding_selection.grant_identity != model_binding.grant_identity
            or embedding_selection.project_owner_id != project.user_id):
        raise _admission_changed()
    await style_service.validate_choice(db, user_id, req.style)
    # Skill 快照：之后编辑 Skill 不影响续跑与重试（重试复制 input）
    skill = await skill_service.snapshot(db, user_id, req.skill_id, req.skill_inputs)
    if not any(req.include_written or not is_written(c) for c in chapters):
        raise BadRequestError(NOTHING_TO_GENERATE)
    await _check_not_generating(db, [c.id for c in chapters], include_written=req.include_written)
    if await open_batch(db, project.id) is not None:
        raise ConflictError(OPEN_BATCH_CONFLICT)
    return {
        "project_id": project.id,
        "title": batch_title(chapters[0].chapter_no, chapters[-1].chapter_no),
        "input": {
            "request": {"project_id": project.id, **req.model_dump(mode="json")},
            "chapter_ids": [c.id for c in chapters],
            "ai_config_id": ai_config_id,
            "_project_model": project_ai_config.serialize_project_model(model_binding),
            "_shared_embedding": project_ai_config.serialize_shared_embedding(embedding_selection),
            "skill": skill,
        },
    }


async def revalidate(
    db: AsyncSession, ctx: "TaskContext", task: Task,
    accepted_model: project_ai_config.ResolvedProjectModel | None = None,
) -> int:
    """重试 / 继续前：项目、模型配置仍有效，剩余章节没有在生成中，项目没有其他未结束的批量。返回 ai_config_id。

    协作者任务按原 grant 锁后重验 ai.use，不借当前邀请补签；accepted_model 为中央受控重试已在锁外完成
    DNS 并在锁内接受的原模型绑定，必须与原任务绑定逐字一致（此时不再持锁解析 DNS）。
    """
    if ((isinstance(task.checkpoint, dict) and "_batch_outcome_unknown" in task.checkpoint)
            or (isinstance(task.input, dict) and "_batch_outcome_unknown" in task.input)):
        raise ConflictError("批量生成结果尚未核对，不能重试", extra={"code": "chapter_claim_changed"})
    req = BatchGenerateRequest.model_validate(task.input.get("request") or {})
    grant = chapter_claim._grant(task.input)
    if grant is None:
        project = await get_owned_project(db, task.user_id, int(task.project_id or 0))
    else:
        with db.no_autoflush:
            project = await project_access.require_project(
                db, task.user_id, int(task.project_id or 0), "ai.use", grant_identity=grant,
            )
    await _require_resume_receipt(db, task, task.checkpoint)
    model_binding, embedding_selection = _batch_bindings(task.input, task.user_id, project)
    if accepted_model is not None and accepted_model != model_binding:
        raise _admission_changed()
    await project_ai_config.revalidate_project_model(
        db, model_binding, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
    )
    await project_ai_config.revalidate_shared_embedding(
        db, embedding_selection, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
    )
    if accepted_model is None and grant is None:
        # 作者继续待审核批量：沿用原行为（按本人配置构造 provider 校验端点）。
        ai_config_id = await _check_config(db, ctx, task.user_id, model_binding.config_id)
    else:
        # 协作者可能使用作者配置：不能按本人重新解析；本地校验已由 revalidate_project_model 完成，
        # 真实端点/DNS 仍在每次发送时由守卫检查。
        ai_config_id = model_binding.config_id
    await style_service.validate_choice(db, task.user_id, req.style)
    remaining = list(task.input.get("chapter_ids") or [])[task.step_cursor :]
    await _check_not_generating(db, remaining, include_written=req.include_written)
    if await open_batch(db, project.id, exclude_id=task.id) is not None:
        raise ConflictError(OPEN_BATCH_CONFLICT)
    return ai_config_id


# ---------------- 断点与汇总 ----------------


def new_checkpoint(chapters: dict[int, Chapter], chapter_ids: list[int]) -> dict[str, Any]:
    """checkpoint 结构：phase（cursor 所指章节的阶段：generate | analyze）、current（正在生成的章节与抢占前状态，
    供 on_abort 回退）、since_review（距上个审核点已生成的章数）、items（每章结果，与 chapter_ids 一一对应）。"""
    items = []
    for cid in chapter_ids:
        c = chapters.get(cid)
        items.append(
            {
                "chapter_id": cid,
                "chapter_no": c.chapter_no if c else None,
                "title": c.title if c else "",
                "state": "pending",
                "target_words": 0,
                "words": 0,
                "analysis": None,
                "warning": "",
                "error": "",
            }
        )
    return {"phase": "generate", "current": None, "since_review": 0, "items": items}


def summarize(checkpoint: dict[str, Any] | None, cursor: int, total: int) -> dict[str, Any]:
    items: list[dict[str, Any]] = list((checkpoint or {}).get("items") or [])

    def brief(item: dict[str, Any], key: str) -> dict[str, Any]:
        return {k: item.get(k) for k in ("chapter_id", "chapter_no", "title")} | {key: item.get(key) or ""}

    counts = {s: sum(1 for i in items if i.get("state") == s) for s in ITEM_STATES}
    warnings = [f"第 {i.get('chapter_no')} 章：{i['warning']}" for i in items if i.get("warning")]
    return {
        "total": total,
        "done": counts["done"],
        "skipped_written": counts["skipped_written"],
        "skipped_missing": counts["skipped_missing"],
        "failed": [brief(i, "error") for i in items if i.get("state") == "failed"],
        "analysis_failed": [brief(i, "warning") for i in items if i.get("analysis") == "failed"],
        "remaining": max(0, total - cursor),
        "warnings": warnings,
    }


def finish_result(task: Task) -> dict[str, Any]:
    total = len(task.input.get("chapter_ids") or [])
    return {**summarize(task.checkpoint, task.step_cursor, total), "ended_early": True}


# ---------------- 执行 ----------------


def _batch_bindings(
    value: dict[str, Any], actor_id: int, project: Project,
) -> tuple[project_ai_config.ResolvedProjectModel, project_ai_config.ResolvedSharedEmbedding]:
    """只读取受理时绑定；缺失或损坏的旧批量任务不能按当前配置重签。

    协作者任务的模型与共享向量绑定必须携带与任务相同的原 grant；作者任务两者均不得带 grant。
    """
    grant = chapter_claim._grant(value)
    model = project_ai_config.deserialize_project_model(
        value.get("_project_model"), actor_id=actor_id, project_id=project.id, project_owner_id=project.user_id,
    )
    embedding = project_ai_config.deserialize_shared_embedding(
        value.get("_shared_embedding"), actor_id=actor_id, project_id=project.id, project_owner_id=project.user_id,
        task_grant=value.get("_collaboration_grant", project_ai_config.TASK_GRANT_ABSENT),
    )
    req = value.get("request")
    if (model is None or embedding is None or model.model_type != "text" or model.grant_identity != grant
            or embedding.grant_identity != grant or type(value.get("ai_config_id")) is not int
            or value["ai_config_id"] != model.config_id or not isinstance(req, dict)
            or (req.get("ai_config_id") is not None and (
                type(req["ai_config_id"]) is not int or req["ai_config_id"] != model.config_id
            ))):
        raise ConflictError("批量模型绑定已变化", extra={"code": "chapter_claim_changed"})
    return model, embedding


@dataclass
class BatchJob:
    task_id: int
    task_key: str | None
    user_id: int
    project_id: int
    req: BatchGenerateRequest
    chapter_ids: list[int]
    cursor: int
    checkpoint: dict[str, Any]
    queued_at: datetime
    execution_key: str
    grant_identity: tuple[str, int] | None
    original_input: dict[str, Any]
    persisted_checkpoint: dict[str, Any] | None
    persisted_cursor: int
    execution_identity: TaskExecutionIdentity
    task_execution_identity: TaskExecutionIdentity
    model_binding: project_ai_config.ResolvedProjectModel
    embedding_selection: project_ai_config.ResolvedSharedEmbedding
    visibility_stamp: ai_dispatch.VisibilityStamp
    model_cipher: SecretCipher = field(repr=False)
    allow_private: bool = False
    active_claim: chapter_claim.ChapterClaim | None = None
    active_attempt: collaboration_ai_guard.BatchAttemptIdentity | None = None
    active_chapter_job: generation_service.ChapterJob | None = None
    pending_review_claim: chapter_claim.ChapterClaim | None = None
    # 受控任务流发布证明（E5-c2）：当前逐章分析的原执行与收据引用，以及按绑定输入缓存的发布 guard。
    active_analysis: tuple[analysis_service.AnalysisExecution, analysis_service.AnalysisReference] | None = None
    publication_guards: dict[str, Any] = field(default_factory=dict, repr=False)
    # 最近一次批量级阶段文案；断点推进后重发同一文案只更新发布来源，不向订阅者广播变化。
    stage_text: str = ""
    verified_text: str = field(default="", repr=False)
    # 进度估算：当前章下标、目标字数与已报告过的最大进度（保证单调）
    position: int = 0
    target: int = 3000
    last_progress: int = 0
    warnings: list[str] = field(default_factory=list)
    # 创建任务时的 Skill 快照（tasks.input.skill）
    skill: dict[str, Any] | None = None

    @property
    def total(self) -> int:
        return len(self.chapter_ids)


async def build_batch_job(ctx: "TaskContext", task: Task, *, execution: TaskExecutionIdentity) -> BatchJob:
    """只读出请求、断点；每章的 ChapterJob 在循环中按最新数据组装。"""
    if ((isinstance(task.checkpoint, dict) and "_batch_outcome_unknown" in task.checkpoint)
            or (isinstance(task.input, dict) and "_batch_outcome_unknown" in task.input)):
        raise ConflictError("批量生成结果尚未核对，不能重试", extra={"code": "chapter_claim_changed"})
    if (not matches_running(execution, task) or execution.kind != KIND
            or execution.actor_token_version is None
            or task.status != "running" or not chapter_claim._uuid(task.statistics_key)
            or not isinstance(task.queued_at, datetime) or not chapter_claim._uuid(task.execution_key)
            or task.execution_key == task.statistics_key or not isinstance(task.input, dict)):
        raise ConflictError("批量执行身份不可验证", extra={"code": "chapter_claim_changed"})
    grant = chapter_claim._grant(task.input)
    if grant != execution.grant_identity:
        raise ConflictError("批量执行身份不可验证", extra={"code": "chapter_claim_changed"})
    req = BatchGenerateRequest.model_validate(task.input.get("request") or {})
    raw_ids = task.input.get("chapter_ids")
    if (not isinstance(raw_ids, list) or not raw_ids or any(type(cid) is not int or cid <= 0 for cid in raw_ids)
            or len(raw_ids) != len(set(raw_ids))):
        raise ConflictError("批量章节范围无法验证", extra={"code": "chapter_claim_changed"})
    chapter_ids = list(raw_ids)
    async with ctx.sessionmaker() as db:
        if grant is None:
            project = await get_owned_project(db, task.user_id, int(task.project_id or 0))
        else:
            # E4-g2：协作者批量在构建前按原领取身份锁内重验（令牌、accepted editor、原邀请轮次、作者模型授权）。
            await require_write_execution(db, execution)
            with db.no_autoflush:
                project = await project_access.require_project(
                    db, task.user_id, int(task.project_id or 0), "ai.use", grant_identity=grant,
                )
        model_binding, embedding_selection = _batch_bindings(execution.input, execution.actor_id, project)
        await project_ai_config.revalidate_project_model(
            db, model_binding, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
        )
        await project_ai_config.revalidate_shared_embedding(
            db, embedding_selection, cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
        )
        visibility_stamp = await ai_dispatch.read_visibility_stamp(db, project.id)
        if task.checkpoint is not None and not isinstance(task.checkpoint, dict):
            raise ConflictError("批量断点结构无法验证", extra={"code": "chapter_claim_changed"})
        checkpoint = copy.deepcopy(task.checkpoint)
        await _require_resume_receipt(db, task, checkpoint)
        if checkpoint is not None and checkpoint.get("current") is not None:
            raise ConflictError("批量章节占用尚未完成清理", extra={"code": "chapter_claim_changed"})
        if not checkpoint:
            rows = await db.scalars(select(Chapter).where(Chapter.id.in_(chapter_ids)))
            checkpoint = new_checkpoint({c.id: c for c in rows}, chapter_ids)
        elif (not isinstance(checkpoint.get("items"), list) or len(checkpoint["items"]) != len(chapter_ids)
              or any(not isinstance(item, dict) or type(item.get("chapter_id")) is not int
                     or item.get("chapter_id") != cid
                     for item, cid in zip(checkpoint["items"], chapter_ids, strict=True))):
            raise ConflictError("批量断点结构无法验证", extra={"code": "chapter_claim_changed"})
    if type(task.step_cursor) is not int or not 0 <= task.step_cursor <= len(chapter_ids):
        raise ConflictError("批量断点位置无法验证", extra={"code": "chapter_claim_changed"})
    checkpoint.setdefault("phase", "generate")
    if checkpoint["phase"] not in {"generate", "analyze"}:
        raise ConflictError("批量断点阶段无法继续", extra={"code": "chapter_claim_changed"})
    checkpoint.setdefault("since_review", 0)
    return BatchJob(
        task_id=task.id,
        task_key=task.statistics_key,
        user_id=task.user_id,
        project_id=project.id,
        req=req,
        chapter_ids=chapter_ids,
        cursor=task.step_cursor,
        checkpoint=checkpoint,
        queued_at=chapter_claim._utc(task.queued_at),
        execution_key=task.execution_key,
        grant_identity=grant,
        original_input=execution.input,
        persisted_checkpoint=copy.deepcopy(task.checkpoint),
        persisted_cursor=task.step_cursor,
        execution_identity=execution,
        task_execution_identity=execution,
        model_binding=model_binding,
        embedding_selection=embedding_selection,
        visibility_stamp=visibility_stamp,
        model_cipher=ctx.cipher,
        allow_private=ctx.settings.allow_private_ai_endpoints,
        position=task.step_cursor,
        skill=task.input.get("skill"),
    )


async def _lock_round_parent(db: AsyncSession, job: BatchJob) -> None:
    """断点/占用写屏障：认证/User → 发送写门禁 → 作品，持锁至调用方提交。

    作者沿用严格作者屏障。协作者（E4-g2）按原 grant 锁后重验：原令牌、accepted editor、原邀请轮次及（使用作者
    模型/向量时）当前作者模型授权；任一失效即受控失败关闭，不进入下一章，也不写新断点。
    """
    if job.grant_identity is None:
        await setting_ai_visibility.lock_author_visibility_write(db, job.user_id, job.project_id, manage=False)
        return
    try:
        project = await project_access.require_project(
            db, job.user_id, job.project_id, "ai.use", for_update=True, visibility_write=True,
            grant_identity=job.grant_identity,
        )
        token = job.execution_identity.actor_token_version
        if type(token) is not int or project.user_id != job.model_binding.project_owner_id:
            raise ConflictError("批量执行身份已变化")
        # 用户行已在上一步按 ID 升序锁定；这里只补核原令牌版本与账号状态。
        await authorization_service.require_authenticated(db, AuthenticatedActor(job.user_id, token), write=True)
        if task_requires_owner_ai(job.original_input) and not await owner_ai_allowed(db, job.project_id, job.user_id):
            raise ConflictError("作者模型授权已关闭")
    except VisibilityPolicyError:
        raise
    except Exception:  # noqa: BLE001  授权/存储失败一律受控关闭，不透传成员或 SQL 细节。
        raise collaboration_ai_guard.CollaborationSendBlocked() from None


async def _require_batch_round(db: AsyncSession, job: BatchJob):
    with db.no_autoflush:
        await _lock_round_parent(db, job)
        row = (await db.execute(select(
            Task.checkpoint, cast(Task.checkpoint, String).label("checkpoint_raw"), Task.step_cursor, Task.input,
        ).where(
            Task.id == job.task_id, Task.statistics_key == job.task_key, Task.user_id == job.user_id,
            Task.project_id == job.project_id, Task.kind == KIND, Task.status == "running",
            Task.queued_at == job.queued_at, Task.execution_key == job.execution_key,
            Task.admin_cancel_operation_id.is_(None),
        ).with_for_update())).one_or_none()
        if (row is None or not _same_json(row.checkpoint, job.persisted_checkpoint)
                or row.step_cursor != job.persisted_cursor or not _same_json(row.input, job.original_input)
                or chapter_claim._grant(row.input) != job.grant_identity):
            raise ConflictError("批量任务身份或断点已变化", extra={"code": "rich_review_task_changed"})
        return row


async def _write_checkpoint(db: AsyncSession, job: BatchJob) -> None:
    """原执行身份和整个原断点做 CAS；SQLite 与 PG 均不接受后来 attempt 的指针。"""
    try:
        with db.no_autoflush:
            row = await _require_batch_round(db, job)
            raw_checkpoint = row[1]
            changed = await db.execute(update(Task).where(
                Task.id == job.task_id, Task.statistics_key == job.task_key, Task.user_id == job.user_id,
                Task.project_id == job.project_id, Task.kind == KIND, Task.status == "running",
                Task.queued_at == job.queued_at, Task.execution_key == job.execution_key,
                Task.admin_cancel_operation_id.is_(None), Task.step_cursor == job.persisted_cursor,
                cast(Task.checkpoint, String) == raw_checkpoint if raw_checkpoint is not None
                else Task.checkpoint.is_(None),
            ).values(step_cursor=job.cursor, checkpoint=copy.deepcopy(job.checkpoint))
                .execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise ConflictError("批量任务身份或断点已变化", extra={"code": "rich_review_task_changed"})
    except BaseException:
        await chapter_claim._rollback(db)
        raise


async def _save(ctx: "TaskContext", job: BatchJob) -> None:
    async with ctx.sessionmaker() as db:
        await _write_checkpoint(db, job)
        await db.commit()
        job.persisted_checkpoint = copy.deepcopy(job.checkpoint)
        job.persisted_cursor = job.cursor
        job.task_execution_identity = advance_checkpoint(
            job.task_execution_identity, job.persisted_checkpoint, step_cursor=job.persisted_cursor,
        )


async def restore_current(ctx: "TaskContext", claim: chapter_claim.ChapterClaim) -> None:
    """仅清理执行体捕获的准确 attempt；不能重读数据库中的新 current。"""
    async with ctx.sessionmaker() as db:
        released = await chapter_claim.release_chapter_claim(db, claim)
        if not released:
            current = await db.scalar(select(Task.checkpoint).where(
                Task.id == claim.task_id, Task.statistics_key == claim.task_key,
                Task.queued_at == claim.queued_at, Task.execution_key == claim.execution_key,
            ))
            if isinstance(current, dict) and current.get("current") is not None:
                raise ConflictError("批量章节占用已变化，无法清理", extra={"code": "chapter_claim_changed"})
        await db.commit()


async def require_idle_snapshot(ctx: "TaskContext", task: Task, job: BatchJob | None) -> None:
    """无活动 attempt 时也须证明任务未被同执行体的另一 attempt 改写。"""
    if task.project_id is None:
        raise ConflictError("批量任务身份无法验证", extra={"code": "chapter_claim_changed"})
    checkpoint = job.persisted_checkpoint if job is not None else task.checkpoint
    cursor = job.persisted_cursor if job is not None else task.step_cursor
    original_input = job.original_input if job is not None else task.input
    if isinstance(checkpoint, dict) and checkpoint.get("current") is not None:
        raise ConflictError("批量章节仍有未确认占用", extra={"code": "chapter_claim_changed"})
    async with ctx.sessionmaker() as db:
        await project_lifecycle.lock_record(db, task.project_id)
        row = (await db.execute(select(Task.checkpoint, Task.step_cursor, Task.input).where(
            Task.id == task.id, Task.statistics_key == task.statistics_key, Task.user_id == task.user_id,
            Task.project_id == task.project_id, Task.status == task.status,
            Task.queued_at == task.queued_at, Task.execution_key == task.execution_key,
        ).with_for_update())).one_or_none()
        if (row is None or not _same_json(row.checkpoint, checkpoint) or row.step_cursor != cursor
                or not _same_json(row.input, original_input)):
            raise ConflictError("批量任务或章节占用已变化", extra={"code": "chapter_claim_changed"})
        await db.rollback()


def recovery_claim(task: Task) -> chapter_claim.ChapterClaim | None:
    """仅供确认旧进程已排空的恢复流程，从原 Task 持久快照构造清理身份。"""
    checkpoint = task.checkpoint
    current = checkpoint.get("current") if isinstance(checkpoint, dict) else None
    if current is None:
        return None
    try:
        pointer = current["_chapter_claim"]
        claim = chapter_claim.ChapterClaim(
            task_id=task.id, task_key=task.statistics_key, actor_id=task.user_id, project_id=task.project_id,
            chapter_id=current["chapter_id"], queued_at=task.queued_at, execution_key=task.execution_key,
            claim_key=pointer["claim_key"], prev_status=current["prev_status"],
            grant_identity=chapter_claim._grant(task.input), phase="running", kind="batch",
        )
        if (not chapter_claim._valid_identity(claim) or not chapter_claim._task_matches(task, claim, cleanup=True)
                or not chapter_claim._pointer(task, claim)):
            raise ValueError
        return claim
    except (KeyError, TypeError, ValueError):
        raise ConflictError("历史批量占用身份无法验证", extra={"code": "chapter_claim_changed"}) from None


@dataclass(frozen=True, slots=True, repr=False)
class BatchAbortSnapshot:
    """原执行的不可变清理依据；JSON 字符串防止调用方修改捕获后的快照。"""

    task_id: int
    task_key: str
    actor_id: int
    project_id: int
    queued_at: datetime
    execution_key: str | None
    cursor: int
    claim: chapter_claim.ChapterClaim | None
    _checkpoint_json: str
    _input_json: str

    @property
    def checkpoint(self) -> dict[str, Any] | None:
        return json.loads(self._checkpoint_json)

    @property
    def input(self) -> dict[str, Any]:
        return json.loads(self._input_json)

    def __repr__(self) -> str:
        return "<BatchAbortSnapshot>"


def capture_abort_snapshot(task: Task, *, recovery: bool = False) -> BatchAbortSnapshot:
    """本进程取原 job；仅在旧进程已排空的 recovery 中可使用持久 Task 快照。"""
    invalid = ConflictError("批量清理缺少原执行凭据", extra={"code": "chapter_claim_changed"})
    queued_without_execution = task.status == "queued" and task.execution_key is None
    if (task.kind != KIND or not chapter_claim._uuid(task.statistics_key)
            or not isinstance(task.queued_at, datetime)
            or not (queued_without_execution or chapter_claim._uuid(task.execution_key))
            or not chapter_claim._positive(task.id) or not chapter_claim._positive(task.user_id)
            or not chapter_claim._positive(task.project_id) or not isinstance(task.input, dict)):
        raise invalid
    job = getattr(task, "_batch_job", None)
    if job is not None:
        if (job.task_id != task.id or job.task_key != task.statistics_key or job.user_id != task.user_id
                or job.project_id != task.project_id or job.execution_key != task.execution_key
                or chapter_claim._utc(job.queued_at) != chapter_claim._utc(task.queued_at)
                or not _same_json(job.original_input, task.input)):
            raise invalid
        checkpoint, cursor, original_input, claim = (
            job.persisted_checkpoint, job.persisted_cursor, job.original_input, job.active_claim,
        )
        if claim is not None:
            _require_job_claim(job, claim)
    elif recovery or queued_without_execution:
        checkpoint, cursor, original_input, claim = task.checkpoint, task.step_cursor, task.input, recovery_claim(task)
    else:
        raise invalid
    if (type(cursor) is not int or cursor < 0 or (checkpoint is not None and not isinstance(checkpoint, dict))
            or (claim is None and isinstance(checkpoint, dict) and checkpoint.get("current") is not None)):
        raise invalid
    if queued_without_execution and isinstance(checkpoint, dict):
        review = checkpoint.get("rich_review")
        if checkpoint.get("phase") == "awaiting_review" or (
            isinstance(review, dict) and review.get("phase") == "awaiting_review"
        ):
            raise invalid
    if claim is not None:
        pointer = {"chapter_id": claim.chapter_id, "claim_key": claim.claim_key, "prev_status": claim.prev_status}
        if (not isinstance(checkpoint, dict) or not chapter_claim._uuid(checkpoint.get("_batch_attempt"))
                or not _same_json(checkpoint.get("current"), {
                    "chapter_id": claim.chapter_id, "prev_status": claim.prev_status, "_chapter_claim": pointer,
                })):
            raise invalid
    try:
        checkpoint_json = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, allow_nan=False)
        input_json = json.dumps(original_input, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise invalid from None
    return BatchAbortSnapshot(
        task_id=task.id, task_key=task.statistics_key, actor_id=task.user_id, project_id=task.project_id,
        queued_at=chapter_claim._utc(task.queued_at), execution_key=task.execution_key, cursor=cursor, claim=claim,
        _checkpoint_json=checkpoint_json, _input_json=input_json,
    )


async def restore_abort_snapshot(ctx: "TaskContext", snapshot: BatchAbortSnapshot) -> BatchAbortSnapshot:
    """失败后重试只凭原捕获值清理，并返回供终态 CAS 的新快照。"""
    if type(snapshot) is not BatchAbortSnapshot:
        raise ConflictError("批量清理凭据无效", extra={"code": "chapter_claim_changed"})
    try:
        checkpoint, original_input = snapshot.checkpoint, snapshot.input
        if (not chapter_claim._positive(snapshot.task_id) or not chapter_claim._uuid(snapshot.task_key)
                or not chapter_claim._positive(snapshot.actor_id) or not chapter_claim._positive(snapshot.project_id)
                or not isinstance(snapshot.queued_at, datetime)
                or not (snapshot.execution_key is None or chapter_claim._uuid(snapshot.execution_key))
                or type(snapshot.cursor) is not int or snapshot.cursor < 0 or not isinstance(original_input, dict)
                or (checkpoint is not None and not isinstance(checkpoint, dict))):
            raise ValueError
        if snapshot.execution_key is None and isinstance(checkpoint, dict):
            review = checkpoint.get("rich_review")
            if (checkpoint.get("current") is not None or checkpoint.get("phase") == "awaiting_review"
                    or (isinstance(review, dict) and review.get("phase") == "awaiting_review")):
                raise ValueError
        if snapshot.claim is not None:
            claim = snapshot.claim
            pointer = {"chapter_id": claim.chapter_id, "claim_key": claim.claim_key,
                       "prev_status": claim.prev_status}
            if (not chapter_claim._valid_identity(claim) or claim.kind != "batch" or claim.phase != "running"
                    or (claim.task_id, claim.task_key, claim.actor_id, claim.project_id, claim.execution_key)
                    != (snapshot.task_id, snapshot.task_key, snapshot.actor_id, snapshot.project_id,
                        snapshot.execution_key) or chapter_claim._utc(claim.queued_at) != snapshot.queued_at
                    or claim.grant_identity != chapter_claim._grant(original_input) or not isinstance(checkpoint, dict)
                    or not chapter_claim._uuid(checkpoint.get("_batch_attempt"))
                    or not _same_json(checkpoint.get("current"), {
                        "chapter_id": claim.chapter_id, "prev_status": claim.prev_status, "_chapter_claim": pointer,
                    })):
                raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise ConflictError("批量清理凭据无效", extra={"code": "chapter_claim_changed"}) from None
    if snapshot.claim is not None:
        await restore_current(ctx, snapshot.claim)
        if not isinstance(checkpoint, dict):
            raise ConflictError("批量清理断点无效", extra={"code": "chapter_claim_changed"})
        checkpoint["current"] = None
    async with ctx.sessionmaker() as db:
        await project_lifecycle.lock_record(db, snapshot.project_id)
        allowed = ("queued", "cancelled") if snapshot.execution_key is None else (
            "running", "failed", "cancelled", "interrupted",
        )
        row = (await db.execute(select(Task.checkpoint, Task.step_cursor, Task.input).where(
            Task.id == snapshot.task_id, Task.statistics_key == snapshot.task_key, Task.user_id == snapshot.actor_id,
            Task.project_id == snapshot.project_id, Task.kind == KIND, Task.queued_at == snapshot.queued_at,
            Task.execution_key == snapshot.execution_key,
            Task.status.in_(allowed),
        ).with_for_update())).one_or_none()
        if (row is None or not _same_json(row.checkpoint, checkpoint) or row.step_cursor != snapshot.cursor
                or not _same_json(row.input, original_input)):
            raise ConflictError("批量清理轮次或断点已变化", extra={"code": "chapter_claim_changed"})
        await db.rollback()
    return replace(snapshot, claim=None, _checkpoint_json=json.dumps(
        checkpoint, ensure_ascii=False, sort_keys=True, allow_nan=False,
    ))


def _uncertain_attempt_checkpoint(snapshot: BatchAbortSnapshot, operation_id: str | None) -> dict[str, Any]:
    """构造原占用释放后的唯一标记；损坏的快照不能用于系统清理。"""
    invalid = ConflictError("批量未决执行凭据无效", extra={"code": "chapter_claim_changed"})
    try:
        if (type(snapshot) is not BatchAbortSnapshot or type(snapshot.claim) is not chapter_claim.ChapterClaim
                or (operation_id is not None and not chapter_claim._uuid(operation_id))):
            raise invalid
        claim, checkpoint, original_input = snapshot.claim, snapshot.checkpoint, snapshot.input
        if (not chapter_claim._valid_identity(claim) or claim.kind != "batch" or claim.phase != "running"
                or (claim.task_id, claim.task_key, claim.actor_id, claim.project_id, claim.execution_key)
                != (snapshot.task_id, snapshot.task_key, snapshot.actor_id, snapshot.project_id, snapshot.execution_key)
                or not isinstance(snapshot.queued_at, datetime)
                or chapter_claim._utc(claim.queued_at) != chapter_claim._utc(snapshot.queued_at)
                or not isinstance(original_input, dict) or chapter_claim._grant(original_input) != claim.grant_identity
                or type(snapshot.cursor) is not int or snapshot.cursor < 0 or not isinstance(checkpoint, dict)
                or checkpoint.get("phase") != "generate"
                or any(key in checkpoint for key in ("_batch_completion", "rich_analysis", "_batch_outcome_unknown"))
                or not chapter_claim._uuid(checkpoint.get("_batch_attempt"))):
            raise invalid
        pointer = {"chapter_id": claim.chapter_id, "claim_key": claim.claim_key, "prev_status": claim.prev_status}
        if not _same_json(checkpoint.get("current"), {
            "chapter_id": claim.chapter_id, "prev_status": claim.prev_status, "_chapter_claim": pointer,
        }):
            raise invalid
        items, ids = checkpoint.get("items"), original_input.get("chapter_ids")
        if (not isinstance(items, list) or not isinstance(ids, list)
                or snapshot.cursor >= len(items) or snapshot.cursor >= len(ids)
                or not isinstance(items[snapshot.cursor], dict)
                or type(items[snapshot.cursor].get("chapter_id")) is not int
                or items[snapshot.cursor]["chapter_id"] != claim.chapter_id
                or type(ids[snapshot.cursor]) is not int or ids[snapshot.cursor] != claim.chapter_id):
            raise invalid
        checkpoint["current"] = None
        checkpoint["_batch_outcome_unknown"] = {
            "schema": 1, "task_id": claim.task_id, "task_key": claim.task_key,
            "actor_id": claim.actor_id, "project_id": claim.project_id, "chapter_id": claim.chapter_id,
            "queued_at": chapter_claim._utc(claim.queued_at).isoformat(), "execution_key": claim.execution_key,
            "claim_key": claim.claim_key, "prev_status": claim.prev_status,
            "grant": list(claim.grant_identity) if claim.grant_identity is not None else None,
            "attempt_key": checkpoint["_batch_attempt"], "cursor": snapshot.cursor, "operation_id": operation_id,
        }
        return checkpoint
    except (TypeError, ValueError, AttributeError, KeyError):
        raise invalid from None


async def release_uncertain_attempt(
    db: AsyncSession, snapshot: BatchAbortSnapshot, *, expected_operation_id: str | None,
) -> dict[str, Any]:
    """旧执行已排空后，同一调用方事务精确释放章节并保留未决标记；不提交。"""
    checkpoint = _uncertain_attempt_checkpoint(snapshot, expected_operation_id)
    claim = snapshot.claim
    assert claim is not None
    try:
        with db.no_autoflush:
            await project_lifecycle.lock_record(db, snapshot.project_id)
            row = (await db.execute(select(
                Task.checkpoint, Task.step_cursor, Task.input, Task.admin_cancel_operation_id,
            ).where(
                Task.id == snapshot.task_id, Task.statistics_key == snapshot.task_key,
                Task.user_id == snapshot.actor_id, Task.project_id == snapshot.project_id, Task.kind == KIND,
                Task.queued_at == snapshot.queued_at, Task.execution_key == snapshot.execution_key,
                Task.status == "running",
            ).with_for_update())).one_or_none()
            if (row is None or row.admin_cancel_operation_id != expected_operation_id
                    or row.step_cursor != snapshot.cursor or not _same_json(row.input, snapshot.input)
                    or not _same_json(row.checkpoint, snapshot.checkpoint)):
                raise ConflictError("批量任务或管理员意图已变化", extra={"code": "chapter_claim_changed"})
            if not await chapter_claim.release_chapter_claim(db, claim):
                raise ConflictError("批量章节占用已变化", extra={"code": "chapter_claim_changed"})
            released = (await db.execute(select(
                Task.checkpoint, cast(Task.checkpoint, String).label("checkpoint_raw"),
            ).where(Task.id == snapshot.task_id, Task.statistics_key == snapshot.task_key).with_for_update())).one()
            expected_released = snapshot.checkpoint
            expected_released["current"] = None
            if not _same_json(released.checkpoint, expected_released):
                raise ConflictError("批量章节释放断点已变化", extra={"code": "chapter_claim_changed"})
            changed = await db.execute(update(Task).where(
                Task.id == snapshot.task_id, Task.statistics_key == snapshot.task_key,
                Task.user_id == snapshot.actor_id, Task.project_id == snapshot.project_id, Task.kind == KIND,
                Task.queued_at == snapshot.queued_at, Task.execution_key == snapshot.execution_key,
                Task.status == "running", Task.step_cursor == snapshot.cursor,
                Task.admin_cancel_operation_id == expected_operation_id if expected_operation_id is not None
                else Task.admin_cancel_operation_id.is_(None),
                cast(Task.checkpoint, String) == released.checkpoint_raw,
            ).values(checkpoint=checkpoint, updated_at=Task.updated_at).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise ConflictError("批量未决记录已变化", extra={"code": "chapter_claim_changed"})
            chapter_claim._cache(db, Task, snapshot.task_id, {"checkpoint": copy.deepcopy(checkpoint)})
            return checkpoint
    except BaseException:
        await chapter_claim._rollback(db)
        raise


async def confirm_uncertain_attempt(
    db: AsyncSession, snapshot: BatchAbortSnapshot, *, status: str, expected_operation_id: str | None,
) -> bool:
    """在提交后的全新会话中，以一次查询确认原标记、原终态及旧章节占用已释放。"""
    checkpoint = _uncertain_attempt_checkpoint(snapshot, expected_operation_id)
    if status not in {"interrupted", "cancelled", "failed"}:
        return False
    claim = snapshot.claim
    assert claim is not None
    with db.no_autoflush:
        row = (await db.execute(select(
            Task.checkpoint, Task.step_cursor, Task.input, Chapter.generation_claim_key,
            Chapter.generation_claim_task_key,
        ).join(Chapter, (Chapter.id == claim.chapter_id) & (Chapter.project_id == snapshot.project_id)).where(
            Task.id == snapshot.task_id, Task.statistics_key == snapshot.task_key,
            Task.user_id == snapshot.actor_id, Task.project_id == snapshot.project_id, Task.kind == KIND,
            Task.queued_at == snapshot.queued_at, Task.status == status, Task.execution_key.is_(None),
            Task.admin_cancel_operation_id.is_(None),
        ))).one_or_none()
    return bool(row is not None and row.step_cursor == snapshot.cursor and _same_json(row.input, snapshot.input)
                and _same_json(row.checkpoint, checkpoint)
                and row.generation_claim_key != claim.claim_key
                and ((row.generation_claim_key is None) == (row.generation_claim_task_key is None)))


def _completion_receipt(
    claim: chapter_claim.ChapterClaim, cursor: int, version_id: int, content_revision: int, attempt_key: str,
    analysis_ref: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": 1, "task_id": claim.task_id, "task_key": claim.task_key, "actor_id": claim.actor_id,
        "project_id": claim.project_id, "chapter_id": claim.chapter_id,
        "queued_at": chapter_claim._utc(claim.queued_at).isoformat(), "execution_key": claim.execution_key,
        "claim_key": claim.claim_key, "prev_status": claim.prev_status,
        "grant": list(claim.grant_identity) if claim.grant_identity is not None else None,
        "cursor": cursor, "version_id": version_id, "content_revision": content_revision,
        "attempt_key": attempt_key, "analysis": copy.deepcopy(analysis_ref),
    }


def _valid_analysis_ref(value: Any, claim: chapter_claim.ChapterClaim) -> bool:
    if not isinstance(value, dict) or set(value) != {"run_key", "task_key", "chapter_id", "content_hash"}:
        return False
    digest = value.get("content_hash")
    return (chapter_claim._uuid(value.get("run_key")) and value.get("task_key") == claim.task_key
            and type(value.get("chapter_id")) is int and value["chapter_id"] == claim.chapter_id
            and isinstance(digest, str) and len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest))


async def _completion_version(
    db: AsyncSession, claim: chapter_claim.ChapterClaim, version_id: int,
) -> tuple[int, str] | None:
    row = (await db.execute(select(Chapter.word_count, GenerationLog.error).join(
        ChapterVersion, ChapterVersion.chapter_id == Chapter.id,
    ).join(GenerationLog, GenerationLog.id == ChapterVersion.generation_log_id).where(
        Chapter.id == claim.chapter_id, Chapter.project_id == claim.project_id,
        ChapterVersion.id == version_id, ChapterVersion.source == "generate",
        GenerationLog.task_id == claim.task_id, GenerationLog.project_id == claim.project_id,
        GenerationLog.chapter_id == claim.chapter_id, GenerationLog.actor_user_id == claim.actor_id,
        GenerationLog.status == "success",
    ))).one_or_none()
    return (row.word_count, row.error) if row is not None else None


async def _require_resume_receipt(db: AsyncSession, task: Task, checkpoint: Any) -> None:
    """重试沿用原收据时先验证旧任务/版本；损坏收据不能被当成完成事实。"""
    if checkpoint is not None and not isinstance(checkpoint, dict):
        raise ConflictError("批量断点结构无法验证", extra={"code": "chapter_claim_changed"})
    if not isinstance(checkpoint, dict) or "_batch_completion" not in checkpoint:
        return
    receipt = checkpoint.get("_batch_completion")
    invalid = ConflictError("批量完成记录无法验证，请核对后重试", extra={"code": "chapter_claim_changed"})
    if not isinstance(receipt, dict):
        raise invalid
    try:
        raw_grant = receipt["grant"]
        grant = tuple(raw_grant) if isinstance(raw_grant, list) else raw_grant
        claim = chapter_claim.ChapterClaim(
            task_id=receipt["task_id"], task_key=receipt["task_key"], actor_id=receipt["actor_id"],
            project_id=receipt["project_id"], chapter_id=receipt["chapter_id"],
            queued_at=datetime.fromisoformat(receipt["queued_at"]), execution_key=receipt["execution_key"],
            claim_key=receipt["claim_key"], prev_status=receipt["prev_status"], grant_identity=grant,
            phase="running", kind="batch",
        )
        cursor, version_id, revision = receipt["cursor"], receipt["version_id"], receipt["content_revision"]
        attempt_key = receipt["attempt_key"]
        analysis_ref = receipt["analysis"]
        if (not chapter_claim._valid_identity(claim) or type(cursor) is not int or cursor < 0
                or type(version_id) is not int or version_id <= 0 or type(revision) is not int or revision < 0
                or not chapter_claim._uuid(attempt_key) or not _valid_analysis_ref(analysis_ref, claim)
                or not _same_json(receipt, _completion_receipt(
                    claim, cursor, version_id, revision, attempt_key, analysis_ref,
                ))):
            raise invalid
    except (KeyError, TypeError, ValueError):
        raise invalid from None
    if (task.user_id != claim.actor_id or task.project_id != claim.project_id or task.step_cursor != cursor
            or not isinstance(task.input, dict) or chapter_claim._grant(task.input) != claim.grant_identity
            or checkpoint.get("phase") != "analyze" or checkpoint.get("current") is not None
            or checkpoint.get("_batch_attempt") != attempt_key
            or not _same_json(checkpoint.get("rich_analysis"), analysis_ref)):
        raise invalid
    items = checkpoint.get("items")
    ids = task.input.get("chapter_ids")
    if (not isinstance(items, list) or cursor >= len(items) or not isinstance(items[cursor], dict)
            or type(items[cursor].get("chapter_id")) is not int or items[cursor].get("chapter_id") != claim.chapter_id
            or items[cursor].get("state") != "done" or not isinstance(ids, list) or cursor >= len(ids)
            or type(ids[cursor]) is not int or ids[cursor] != claim.chapter_id):
        raise invalid
    with db.no_autoflush:
        origin = await db.scalar(select(Task).where(
            Task.id == claim.task_id, Task.statistics_key == claim.task_key, Task.user_id == claim.actor_id,
            Task.project_id == claim.project_id, Task.kind == KIND, Task.queued_at == claim.queued_at,
        ))
        if (origin is None or not isinstance(origin.input, dict) or chapter_claim._grant(origin.input) != grant
                or not _same_json(origin.input.get("chapter_ids"), ids)
                or not _same_json(origin.input.get("request"), task.input.get("request"))
                or origin.admin_cancel_operation_id is not None
                or origin.step_cursor != cursor or not isinstance(origin.checkpoint, dict)
                or not _same_json(origin.checkpoint.get("_batch_completion"), receipt)
                or origin.checkpoint.get("_batch_attempt") != attempt_key
                or not _same_json(origin.checkpoint.get("rich_analysis"), analysis_ref)
                or origin.checkpoint.get("phase") != "analyze" or origin.checkpoint.get("current") is not None):
            raise invalid
        if origin.status == "running":
            if origin.id != task.id or origin.execution_key != claim.execution_key:
                raise invalid
        elif origin.status not in {"failed", "cancelled", "interrupted", "paused"} or origin.execution_key is not None:
            raise invalid
        chapter = await db.get(Chapter, claim.chapter_id)
        if (chapter is None or chapter.project_id != claim.project_id or chapter.content_revision != revision
                or await _completion_version(db, claim, version_id) is None):
            raise invalid


async def record_chapter_completion(
    db: AsyncSession, claim: chapter_claim.ChapterClaim, *, version_id: int, content_revision: int,
) -> dict[str, Any]:
    """与正文、版本和精确释放在同一事务推进 analyze，禁止保存后再次生成。"""
    if (not chapter_claim._valid_identity(claim) or claim.kind != "batch" or claim.phase != "running"
            or type(version_id) is not int or version_id <= 0
            or type(content_revision) is not int or content_revision < 0):
        raise ConflictError("批量完成身份无效", extra={"code": "chapter_claim_changed"})
    with db.no_autoflush:
        task = await db.scalar(select(Task).where(
            Task.id == claim.task_id, Task.statistics_key == claim.task_key, Task.user_id == claim.actor_id,
            Task.project_id == claim.project_id, Task.kind == KIND, Task.status == "running",
            Task.queued_at == claim.queued_at, Task.execution_key == claim.execution_key,
            Task.admin_cancel_operation_id.is_(None),
        ).with_for_update().execution_options(populate_existing=True))
        if (task is None or not chapter_claim._task_matches(task, claim)
                or not isinstance(task.checkpoint, dict)):
            raise ConflictError("批量完成断点已变化", extra={"code": "chapter_claim_changed"})
        checkpoint = copy.deepcopy(task.checkpoint)
        items, cursor = checkpoint.get("items"), task.step_cursor
        if (checkpoint.get("phase") != "generate" or checkpoint.get("current") is not None
                or "_batch_completion" in checkpoint or not isinstance(items, list)
                or not chapter_claim._uuid(checkpoint.get("_batch_attempt"))
                or checkpoint.get("rich_analysis") is not None
                or type(cursor) is not int or cursor < 0 or cursor >= len(items)
                or not isinstance(items[cursor], dict) or type(items[cursor].get("chapter_id")) is not int
                or items[cursor].get("chapter_id") != claim.chapter_id):
            raise ConflictError("批量完成断点已变化", extra={"code": "chapter_claim_changed"})
        chapter = await db.scalar(select(Chapter).where(
            Chapter.id == claim.chapter_id, Chapter.project_id == claim.project_id,
        ).with_for_update().execution_options(populate_existing=True))
        version = await _completion_version(db, claim, version_id)
        if (chapter is None or chapter.status != "done" or chapter.content_revision != content_revision
                or any((chapter.generation_claim_key, chapter.generation_claim_task_key,
                        chapter.generation_claim_prev_status)) or version is None):
            raise ConflictError("批量完成版本无法验证", extra={"code": "chapter_claim_changed"})
        from app.services.rich_document_ai import analysis_fingerprints

        analysis_ref = {"run_key": str(uuid4()), "task_key": claim.task_key, "chapter_id": claim.chapter_id,
                        "content_hash": (await analysis_fingerprints(db, [chapter]))[chapter.id]}
        receipt = _completion_receipt(
            claim, cursor, version_id, content_revision, checkpoint["_batch_attempt"], analysis_ref,
        )
        items[cursor].update(state="done", words=version[0], warning=version[1] or "", error="")
        checkpoint["phase"] = "analyze"
        checkpoint["_batch_completion"] = receipt
        checkpoint["rich_analysis"] = analysis_ref
        task.checkpoint = checkpoint
    await db.flush()
    return receipt


async def confirm_chapter_completion(
    db: AsyncSession, claim: chapter_claim.ChapterClaim, receipt: dict[str, Any] | None,
) -> bool:
    """仅确认原执行体自己的完整收据和关联版本；无证据不推断提交失败。"""
    if (not chapter_claim._valid_identity(claim) or claim.kind != "batch" or claim.phase != "running"
            or not isinstance(receipt, dict)):
        return False
    cursor, version_id, revision = receipt.get("cursor"), receipt.get("version_id"), receipt.get("content_revision")
    attempt_key = receipt.get("attempt_key")
    analysis_ref = receipt.get("analysis")
    if (type(cursor) is not int or cursor < 0 or type(version_id) is not int or version_id <= 0
            or type(revision) is not int or revision < 0 or not chapter_claim._uuid(attempt_key)
            or not _valid_analysis_ref(analysis_ref, claim)
            or not _same_json(receipt, _completion_receipt(
                claim, cursor, version_id, revision, attempt_key, analysis_ref,
            ))):
        return False
    with db.no_autoflush:
        await project_lifecycle.lock_record(db, claim.project_id)
        task = await db.scalar(select(Task).where(
            Task.id == claim.task_id, Task.statistics_key == claim.task_key, Task.user_id == claim.actor_id,
            Task.project_id == claim.project_id, Task.kind == KIND, Task.status == "running",
            Task.queued_at == claim.queued_at, Task.execution_key == claim.execution_key,
        ).with_for_update().execution_options(populate_existing=True))
        if task is None or not chapter_claim._task_matches(task, claim) or not isinstance(task.checkpoint, dict):
            return False
        checkpoint = task.checkpoint
        items = checkpoint.get("items")
        if (task.step_cursor != cursor or checkpoint.get("phase") != "analyze"
                or checkpoint.get("current") is not None
                or not _same_json(checkpoint.get("_batch_completion"), receipt)
                or checkpoint.get("_batch_attempt") != attempt_key
                or not _same_json(checkpoint.get("rich_analysis"), analysis_ref)
                or not isinstance(items, list) or cursor >= len(items)
                or not isinstance(items[cursor], dict) or type(items[cursor].get("chapter_id")) is not int
                or items[cursor].get("chapter_id") != claim.chapter_id
                or items[cursor].get("state") != "done"):
            return False
        return await _completion_version(db, claim, version_id) is not None


def _sync_checkpoint(job: BatchJob, checkpoint: dict[str, Any]) -> None:
    """保持 runner 已捕获的 cp/items/item 引用，不能使用旧副本覆盖新收据。"""
    old_items, new_items = job.checkpoint.get("items"), checkpoint.get("items")
    if not isinstance(old_items, list) or not isinstance(new_items, list) or len(old_items) != len(new_items):
        raise ConflictError("批量断点结构已变化", extra={"code": "chapter_claim_changed"})
    for old, new in zip(old_items, new_items, strict=True):
        if not isinstance(old, dict) or not isinstance(new, dict):
            raise ConflictError("批量断点结构已变化", extra={"code": "chapter_claim_changed"})
        old.clear()
        old.update(copy.deepcopy(new))
    job.checkpoint.clear()
    job.checkpoint.update(copy.deepcopy({key: value for key, value in checkpoint.items() if key != "items"}))
    job.checkpoint["items"] = old_items
    job.persisted_checkpoint = copy.deepcopy(checkpoint)
    job.persisted_cursor = job.cursor
    job.task_execution_identity = advance_checkpoint(
        job.task_execution_identity, job.persisted_checkpoint, step_cursor=job.persisted_cursor,
    )


def _require_job_claim(job: BatchJob, claim: chapter_claim.ChapterClaim) -> None:
    checkpoint = job.persisted_checkpoint
    pointer = {"chapter_id": claim.chapter_id, "claim_key": claim.claim_key, "prev_status": claim.prev_status}
    expected_current = {"chapter_id": claim.chapter_id, "prev_status": claim.prev_status, "_chapter_claim": pointer}
    if (not chapter_claim._valid_identity(claim) or claim.kind != "batch" or claim.phase != "running"
            or job.active_claim != claim or claim.task_id != job.task_id or claim.task_key != job.task_key
            or claim.actor_id != job.user_id or claim.project_id != job.project_id
            or chapter_claim._utc(claim.queued_at) != chapter_claim._utc(job.queued_at)
            or claim.execution_key != job.execution_key or claim.grant_identity != job.grant_identity
            or not isinstance(checkpoint, dict) or not _same_json(checkpoint.get("current"), expected_current)
            or not chapter_claim._uuid(checkpoint.get("_batch_attempt"))):
        raise ConflictError("批量章节执行身份已变化", extra={"code": "chapter_claim_changed"})


async def _sync_released(
    ctx: "TaskContext", job: BatchJob, claim: chapter_claim.ChapterClaim, *, cleanup: bool = False,
) -> None:
    _require_job_claim(job, claim)
    expected = copy.deepcopy(job.persisted_checkpoint)
    if not isinstance(expected, dict):
        raise ConflictError("批量断点身份已变化", extra={"code": "chapter_claim_changed"})
    expected["current"] = None
    async with ctx.sessionmaker() as db:
        statement = select(Task).where(
            Task.id == job.task_id, Task.statistics_key == job.task_key, Task.user_id == job.user_id,
            Task.project_id == job.project_id, Task.status == "running", Task.queued_at == job.queued_at,
            Task.execution_key == job.execution_key,
        )
        if not cleanup:
            statement = statement.where(Task.admin_cancel_operation_id.is_(None))
        task = await db.scalar(statement)
        if (task is None or task.step_cursor != job.persisted_cursor or not _same_json(task.input, job.original_input)
                or not _same_json(task.checkpoint, expected)):
            raise ConflictError("批量章节占用已变化", extra={"code": "chapter_claim_changed"})
        _sync_checkpoint(job, expected)
    job.active_claim = None
    job.active_attempt = None
    job.active_chapter_job = None


async def _sync_pending_review(
    ctx: "TaskContext", job: BatchJob, claim: chapter_claim.ChapterClaim, identity: dict[str, Any],
) -> None:
    from app.services.rich_document_review import _read_payload, _round_time

    _require_job_claim(job, claim)
    if (identity.get("reason") != "rich_document_review" or identity.get("task_key") != job.task_key
            or type(identity.get("chapter_id")) is not int or identity.get("chapter_id") != claim.chapter_id
            or not chapter_claim._uuid(identity.get("token"))
            or not chapter_claim._uuid(identity.get("run_key"))):
        raise ConflictError("批量提案身份已变化", extra={"code": "chapter_claim_changed"})
    expected = copy.deepcopy(job.persisted_checkpoint)
    if not isinstance(expected, dict):
        raise ConflictError("批量提案断点已变化", extra={"code": "chapter_claim_changed"})
    expected["current"], expected["phase"] = None, "awaiting_review"
    expected["rich_review"] = {"phase": "awaiting_review", "token": identity["token"],
                               "run_key": identity["run_key"], "chapter_id": claim.chapter_id}
    async with ctx.sessionmaker() as db:
        with db.no_autoflush:
            await _lock_round_parent(db, job)
        task = await db.scalar(select(Task).where(
            Task.id == job.task_id, Task.statistics_key == job.task_key, Task.user_id == job.user_id,
            Task.project_id == job.project_id, Task.status == "running", Task.queued_at == job.queued_at,
            Task.execution_key == job.execution_key, Task.admin_cancel_operation_id.is_(None),
        ).with_for_update())
        if (task is None or task.step_cursor != job.persisted_cursor or not _same_json(task.input, job.original_input)
                or not _same_json(task.checkpoint, expected)):
            raise ConflictError("批量提案断点已变化", extra={"code": "chapter_claim_changed"})
        proposal = await db.scalar(select(SceneReviewProposal).where(
            SceneReviewProposal.task_id == job.task_id, SceneReviewProposal.task_key == job.task_key,
            SceneReviewProposal.chapter_id == claim.chapter_id, SceneReviewProposal.token == identity["token"],
            SceneReviewProposal.run_key == identity["run_key"], SceneReviewProposal.status == "pending",
        ))
        if proposal is None:
            raise ConflictError("批量提案身份已变化", extra={"code": "chapter_claim_changed"})
        payload = await _read_payload(db, proposal)
        receipt = {
            "version": 1, "task_id": claim.task_id, "task_key": claim.task_key, "actor_id": claim.actor_id,
            "project_id": claim.project_id, "chapter_id": claim.chapter_id, "queued_at": _round_time(claim.queued_at),
            "execution_key": claim.execution_key, "claim_key": claim.claim_key,
            "grant": list(claim.grant_identity) if claim.grant_identity is not None else None, "kind": "batch",
        }
        if not _same_json(payload.get("execution_receipt"), receipt):
            raise ConflictError("批量提案执行身份已变化", extra={"code": "chapter_claim_changed"})
        _sync_checkpoint(job, expected)
    job.pending_review_claim = claim
    job.active_claim = None
    job.active_attempt = None
    job.active_chapter_job = None


async def verify_batch_pending_event(
    db: AsyncSession, task: Task, claim: chapter_claim.ChapterClaim, data: dict[str, Any],
) -> None:
    """在输出事务持有的原 Task 锁内，复验本章待审提案与原 claim。"""
    from app.services.rich_document_review import (
        _read_payload,
        _require_proposal_round,
        _round_time,
        _stored_plans,
        pending_for_task,
        review_identity,
    )

    invalid = ConflictError("批量待审提案身份已变化", extra={"code": "rich_review_task_changed"})
    if (type(claim) is not chapter_claim.ChapterClaim or not chapter_claim._valid_identity(claim)
            or claim.kind != "batch" or claim.phase != "running"
            or (task.id, task.statistics_key, task.user_id, task.project_id, task.execution_key)
            != (claim.task_id, claim.task_key, claim.actor_id, claim.project_id, claim.execution_key)
            or task.queued_at is None or chapter_claim._utc(task.queued_at) != chapter_claim._utc(claim.queued_at)
            or task.status != "running" or task.admin_cancel_operation_id is not None
            or task.admin_cancel_requested_at is not None):
        raise invalid
    with db.no_autoflush:
        chapter = await db.scalar(select(Chapter).where(
            Chapter.id == claim.chapter_id, Chapter.project_id == claim.project_id,
        ).with_for_update())
        proposal = await pending_for_task(db, task, lock=True)
        if (chapter is None or proposal is None or chapter.status != claim.prev_status
                or any((chapter.generation_claim_key, chapter.generation_claim_task_key,
                        chapter.generation_claim_prev_status)) or not _same_json(review_identity(proposal), data)):
            raise invalid
        payload = await _read_payload(db, proposal)
        _require_proposal_round(task, proposal, chapter, payload, running=True)
        receipt = {
            "version": 1, "task_id": claim.task_id, "task_key": claim.task_key, "actor_id": claim.actor_id,
            "project_id": claim.project_id, "chapter_id": claim.chapter_id, "queued_at": _round_time(claim.queued_at),
            "execution_key": claim.execution_key, "claim_key": claim.claim_key,
            "grant": list(claim.grant_identity) if claim.grant_identity is not None else None, "kind": "batch",
        }
        if not _same_json(payload.get("execution_receipt"), receipt):
            raise invalid
        _stored_plans(payload)


async def _sync_completed(ctx: "TaskContext", job: BatchJob, claim: chapter_claim.ChapterClaim) -> None:
    _require_job_claim(job, claim)
    async with ctx.sessionmaker() as db:
        task = await db.scalar(select(Task).where(
            Task.id == job.task_id, Task.statistics_key == job.task_key, Task.queued_at == job.queued_at,
            Task.execution_key == job.execution_key, Task.status == "running",
        ))
        checkpoint = copy.deepcopy(task.checkpoint) if task is not None else None
        receipt = checkpoint.get("_batch_completion") if isinstance(checkpoint, dict) else None
        if not await confirm_chapter_completion(db, claim, receipt):
            raise generation_service.ChapterCommitUncertain("批量章节完成事实待核对")
        expected = copy.deepcopy(job.persisted_checkpoint)
        assert isinstance(expected, dict) and isinstance(receipt, dict) and isinstance(checkpoint, dict)
        version = await _completion_version(db, claim, receipt["version_id"])
        if version is None:
            raise generation_service.ChapterCommitUncertain("批量章节完成事实待核对")
        expected["current"], expected["phase"], expected["_batch_completion"] = None, "analyze", receipt
        expected["rich_analysis"] = copy.deepcopy(receipt["analysis"])
        expected["items"][job.cursor].update(state="done", words=version[0], warning=version[1] or "", error="")
        if not _same_json(expected, checkpoint):
            raise generation_service.ChapterCommitUncertain("批量章节完成事实待核对")
        _sync_checkpoint(job, checkpoint)
    job.active_claim = None
    job.active_attempt = None
    job.active_chapter_job = None


def progress(job: BatchJob, words: int) -> int:
    fraction = min(1.0, words / max(job.target, 1)) * 0.8
    value = int((job.position + fraction) * 100 / max(job.total, 1))
    job.last_progress = max(job.last_progress, max(1, min(95, value)))
    return job.last_progress


def _stage(job: BatchJob, index: int, text: str) -> "Event":
    job.stage_text = f"第 {index + 1}/{job.total} 章 · {text}"
    return "stage", {"stage": job.stage_text}


def _refresh_stage(job: BatchJob) -> "Event":
    """断点已由本执行提交、尚无事件时重发当前阶段：引擎随之核验并换上新的发布来源（E5-c2）。

    文案与 hub 当前阶段相同，作者与订阅者都看不到新帧；只是让受控任务流中仍在途的旧帧能以最新来源复核，
    不必等到模型首个 token 或任务结束。
    """
    return "stage", {"stage": job.stage_text}


async def _needs_work(ctx: "TaskContext", job: BatchJob, start: int) -> bool:
    """start 之后是否还有需要生成的章节（审核点只在后面还有活时暂停）。"""
    ids = job.chapter_ids[start:]
    if not ids:
        return False
    if job.req.include_written:
        async with ctx.sessionmaker() as db:
            return await db.scalar(select(Chapter.id).where(Chapter.id.in_(ids)).limit(1)) is not None
    async with ctx.sessionmaker() as db:
        rows = await db.scalars(select(Chapter.content).where(Chapter.id.in_(ids)))
        return any(not (c or "").strip() for c in rows)


async def _generate_chapter(
    ctx: "TaskContext", job: BatchJob, index: int, item: dict[str, Any]
) -> AsyncIterator["Event"]:
    """生成一章（含重试）；结果写入 item（state done / failed、words、warning、error）。"""
    if (type(index) is not int or index < 0 or index >= len(job.chapter_ids)
            or job.cursor != index or job.persisted_cursor != index or not isinstance(item, dict)
            or type(item.get("chapter_id")) is not int or item["chapter_id"] != job.chapter_ids[index]):
        raise ConflictError("批量章节执行位置已变化", extra={"code": "chapter_claim_changed"})
    cid = job.chapter_ids[index]
    settings = ctx.settings
    gen_req = GenerateChapterRequest(
        ai_config_id=job.req.ai_config_id,
        instruction=job.req.instruction,
        target_words=item["target_words"],
        auto_continue=job.req.auto_continue,
        style=job.req.style,
    )
    attempt = 0
    error = ""
    while True:
        if attempt:
            delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)] if RETRY_DELAYS else 0
            yield _stage(job, index, f"生成失败，第 {attempt} 次重试")
            if delay > 0:
                await asyncio.sleep(delay)
            yield "reset", {}
        attempt += 1
        retryable = True
        result: dict[str, Any] | None = None
        claim = None
        try:
            async with ctx.sessionmaker() as db:
                # 每次失败后 current 会清空；持久随机代次避免 A→B→空 的 ABA。
                attempt_key = str(uuid4())
                job.checkpoint["_batch_attempt"] = attempt_key
                await _write_checkpoint(db, job)
                claim = await chapter_claim.claim_batch_chapter(
                    db, task_id=job.task_id, task_key=job.task_key, actor_id=job.user_id,
                    project_id=job.project_id, chapter_id=cid, queued_at=job.queued_at,
                    execution_key=job.execution_key, grant_identity=job.grant_identity,
                )
                batch_attempt = collaboration_ai_guard.BatchAttemptIdentity(claim, index, attempt_key)
                # 即使 commit 的确认丢失，清理也只可使用本次申请的原身份。
                job.active_claim = claim
                pointer = {"chapter_id": cid, "claim_key": claim.claim_key, "prev_status": claim.prev_status}
                job.checkpoint["current"] = {"chapter_id": cid, "prev_status": claim.prev_status,
                                             "_chapter_claim": pointer}
                job.persisted_checkpoint = copy.deepcopy(job.checkpoint)
                job.persisted_cursor = job.cursor
                await db.commit()
                job.task_execution_identity = advance_checkpoint(
                    job.task_execution_identity, job.persisted_checkpoint, step_cursor=job.persisted_cursor,
                )
                job.active_attempt = batch_attempt
            # 本章占用与 attempt 已提交：立即以本 attempt 发布来源（构建与模型首个 token 可能较慢）。
            yield _refresh_stage(job)
            try:
                async with ctx.sessionmaker() as db:
                    chapter_job = await generation_service.build_chapter_job(
                        db,
                        user_id=job.user_id,
                        chapter_id=cid,
                        mode="generate",
                        req=gen_req,
                        prev_status=claim.prev_status,
                        ai_config_id=job.req.ai_config_id,
                        cipher=ctx.cipher,
                        factory=ctx.factory,
                        ai_timeout=settings.ai_request_timeout,
                        allow_private=settings.allow_private_ai_endpoints,
                        context_budget=settings.context_char_budget,
                        task_id=job.task_id,
                        task_key=job.task_key,
                        claim=claim,
                        batch_attempt=batch_attempt,
                        execution=job.execution_identity,
                        model_binding=job.model_binding,
                        embedding_selection=job.embedding_selection,
                        skill=job.skill,
                        usage_kind="chapter.generate.retry" if attempt > 1 else None,
                    )
                job.active_chapter_job = chapter_job
            except AppError:
                # 配置被删 / 章节被复位等：不重试；章节仍是本任务抢占的 generating，还原
                retryable = False
                await restore_current(ctx, claim)
                raise
            async with aclosing(generation_service.run_chapter_job(chapter_job, ctx.sessionmaker)) as events:
                async for event, data in events:
                    if event == "done":
                        result = data
                    elif event == "paused":
                        await _sync_pending_review(ctx, job, claim, data)
                        yield event, data
                        return
                    elif event == "error":
                        error = str(data.get("detail") or "生成失败")
                        retryable = data.get("retryable", True)
                    else:
                        yield event, data
        except VisibilityPolicyError:
            raise
        except AppError as exc:
            if claim is None:
                raise
            error = exc.detail
        if result is not None:
            assert claim is not None
            await _sync_completed(ctx, job, claim)
            return
        assert claim is not None
        # 失败清理只能接受原 attempt 被精确释放，不能采纳后来的 current。
        await _sync_released(ctx, job, claim)
        if not retryable or attempt > MAX_RETRIES:
            item.update(state="failed", error=error or "生成失败")
            return


async def _analyze_chapter(
    ctx: "TaskContext", job: BatchJob, index: int, item: dict[str, Any]
) -> AsyncIterator["Event"]:
    """在本任务内分析并回写；失败只记警告。模型原文不进入任务输出。"""
    cid = job.chapter_ids[index]
    settings = ctx.settings
    async with ctx.sessionmaker() as db:
        project = await db.get(Project, job.project_id)
        enabled = project is not None and analyze_enabled(settings, project)
    if not enabled:
        item["analysis"] = "skipped"
        return
    execution = analysis_service.AnalysisExecution(
        task_id=job.task_id, task_key=job.task_key, actor_id=job.user_id, project_id=job.project_id,
        queued_at=job.queued_at, execution_key=job.execution_key, grant_identity=job.grant_identity,
        task_kind=KIND, batch_cursor=job.cursor,
        input_hash=analysis_service.analysis_input_hash(job.execution_identity.input),
        actor_token_version=job.execution_identity.actor_token_version,
        model_binding=job.model_binding, embedding_selection=job.embedding_selection,
        task_identity=job.execution_identity,
    )
    review_ref = job.checkpoint.get("rich_analysis")
    if review_ref is None:
        item["analysis"] = "failed"
        note = "缺少原分析凭据，未自动重复；可手动分析"
        item["warning"] = f"{item['warning']}；{note}" if item.get("warning") else note
        return
    state, note = await _reserve_review_analysis(ctx, job, cid, review_ref)
    if state != "execute":
        item["analysis"] = "ok" if state == "completed" else "failed"
        if note:
            item["warning"] = f"{item['warning']}；{note}" if item.get("warning") else note
        if state == "completed":
            index_outcome = await analysis_service.enqueue_memory_for_execution(
                ctx.sessionmaker, execution, engine=ctx.task_engine, review_analysis=review_ref,
                cipher=ctx.cipher, allow_private=settings.allow_private_ai_endpoints,
            )
            _append_index_warning(item, index_outcome.warning)
        return
    job.active_analysis = (execution, analysis_service.capture_analysis_reference(review_ref))
    yield _stage(job, index, "分析中")
    error = ""
    try:
        async with ctx.sessionmaker() as db:
            analysis_job = await analysis_service.build_analysis_job(
                db,
                user_id=job.user_id,
                chapter_id=cid,
                ai_config_id=job.model_binding.config_id,
                cipher=ctx.cipher,
                factory=ctx.factory,
                ai_timeout=settings.ai_request_timeout,
                allow_private=settings.allow_private_ai_endpoints,
                task_id=job.task_id,
                task_key=job.task_key,
                execution=execution,
                review_analysis=review_ref,
                context_budget=settings.context_char_budget,
            )
            if review_ref is not None and analysis_job.content_hash != review_ref["content_hash"]:
                raise ConflictError("采纳后的章节已变化，未自动分析；请手动分析")
        done = False
        async with aclosing(analysis_service.run_analysis_job(analysis_job, ctx.sessionmaker)) as events:
            async for event, data in events:
                if event == "done":
                    done = True
                elif event == "error":
                    error = str(data.get("detail") or "分析失败")
        if not done and not error:
            error = "分析失败"
    except VisibilityPolicyError:
        raise
    except AppError as exc:
        error = exc.detail
    finally:
        job.active_analysis = None
    if error:
        if review_ref is not None:
            await _fail_review_analysis(ctx, job, review_ref)
        item["analysis"] = "failed"
        note = f"分析失败：{error}"
        item["warning"] = f"{item['warning']}；{note}" if item.get("warning") else note
    else:
        item["analysis"] = "ok"
        # 本章记忆已随分析写入；有向量模型时排队生成向量（与独立分析任务的 on_success 一致）
        index_outcome = await analysis_service.enqueue_memory_for_execution(
            ctx.sessionmaker, execution, engine=ctx.task_engine, review_analysis=review_ref,
            cipher=ctx.cipher, allow_private=settings.allow_private_ai_endpoints,
        )
        _append_index_warning(item, index_outcome.warning)


def _append_index_warning(item: dict[str, Any], note: str | None) -> None:
    if not note:
        return
    previous = item.get("warning") or ""
    if note not in previous.split("；"):
        item["warning"] = f"{previous}；{note}" if previous else note


async def _reserve_review_analysis(
    ctx: "TaskContext", job: BatchJob, chapter_id: int, ref: dict[str, Any],
) -> tuple[str, str]:
    """先持久登记一次模型调用；崩溃后结果未知时不自动重复发送。"""
    from sqlalchemy.exc import IntegrityError

    from app.services.rich_document_ai import analysis_fingerprints

    if (not isinstance(ref, dict) or type(ref.get("chapter_id")) is not int or ref["chapter_id"] != chapter_id
            or not all(isinstance(ref.get(key), str) for key in ("run_key", "task_key", "content_hash"))):
        return "blocked", "分析断点身份无效，未重复发送；请手动分析"
    async with ctx.sessionmaker() as db:
        await _require_batch_round(db, job)
        chapter = await db.scalar(select(Chapter).where(
            Chapter.id == chapter_id, Chapter.project_id == job.project_id,
        ).with_for_update())
        if chapter is None:
            return "blocked", "分析任务或章节已变化，未重复发送"
        if (await analysis_fingerprints(db, [chapter]))[chapter_id] != ref["content_hash"]:
            return "blocked", "采纳后的章节已变化，未自动分析；请手动分析"
        receipt = await db.get(SceneReviewAnalysisReceipt, ref["run_key"])
        if receipt is not None:
            if (receipt.task_key != ref["task_key"] or receipt.chapter_id != chapter_id
                    or receipt.content_hash != ref["content_hash"]):
                return "blocked", "分析轮次身份不一致，未重复发送"
            if receipt.status == "completed" and receipt.analysis_id is not None:
                analysis = await db.get(ChapterAnalysis, receipt.analysis_id)
                if (analysis is not None and analysis.chapter_id == chapter_id
                        and analysis.content_hash == ref["content_hash"]):
                    return "completed", ""
            return "blocked", "该次分析已开始或结果已移除，未自动重复；可手动分析"
        db.add(SceneReviewAnalysisReceipt(run_key=ref["run_key"], task_key=ref["task_key"], chapter_id=chapter_id,
                                          content_hash=ref["content_hash"], status="running"))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return "blocked", "该次分析已由另一执行轮次领取，未重复发送"
    return "execute", ""


async def _fail_review_analysis(ctx: "TaskContext", job: BatchJob, ref: dict[str, Any]) -> None:
    async with ctx.sessionmaker() as db:
        await _require_batch_round(db, job)
        if (not isinstance(job.persisted_checkpoint, dict)
                or job.persisted_checkpoint.get("phase") != "analyze"
                or not _same_json(job.persisted_checkpoint.get("rich_analysis"), ref)):
            raise ConflictError("批量分析凭据已变化", extra={"code": "analysis_execution_changed"})
        result = await db.execute(update(SceneReviewAnalysisReceipt).where(
            SceneReviewAnalysisReceipt.run_key == ref.get("run_key"),
            SceneReviewAnalysisReceipt.task_key == ref.get("task_key"),
            SceneReviewAnalysisReceipt.chapter_id == ref.get("chapter_id"),
            SceneReviewAnalysisReceipt.content_hash == ref.get("content_hash"),
            SceneReviewAnalysisReceipt.status == "running",
            SceneReviewAnalysisReceipt.analysis_id.is_(None),
        ).values(status="failed"))
        if result.rowcount != 1:
            raise ConflictError("批量分析凭据已变化", extra={"code": "analysis_execution_changed"})
        await db.commit()


async def run_batch_job(job: BatchJob, ctx: "TaskContext") -> AsyncIterator["Event"]:
    cp = job.checkpoint
    items: list[dict[str, Any]] = cp["items"]
    while job.cursor < job.total:
        i = job.cursor
        cid = job.chapter_ids[i]
        item = items[i]
        job.position = i

        if cp["phase"] == "generate":
            async with ctx.sessionmaker() as db:
                chapter = await db.get(Chapter, cid)
                if chapter is not None and chapter.project_id != job.project_id:
                    chapter = None
                if chapter is not None:
                    item.update(chapter_no=chapter.chapter_no, title=chapter.title)
                    written = is_written(chapter)
                    plans = await plan_words(db, [chapter])
            skip = None
            if chapter is None:
                skip = "skipped_missing"
            elif written and not job.req.include_written:
                skip = "skipped_written"
            if skip is not None:
                item.update(state=skip, error="")
                job.cursor = i + 1
                await _save(ctx, job)
                yield "chapter_end", {"index": i, "item": dict(item)}
                continue

            target, source = target_for(job.req, cid, plans)
            item.update(state="pending", target_words=target, words_source=source, error="", warning="", analysis=None)
            job.target = target
            yield "reset", {}
            yield (
                "chapter_start",
                {
                    "index": i,
                    "total": job.total,
                    "chapter_id": cid,
                    "chapter_no": item["chapter_no"],
                    "title": item["title"],
                    "target_words": target,
                },
            )
            yield _stage(job, i, "生成中")
            async for event in _generate_chapter(ctx, job, i, item):
                yield event
                if event[0] == "paused":
                    return
            if item["state"] == "failed":
                if job.req.skip_on_fail:
                    job.cursor = i + 1
                    await _save(ctx, job)
                    yield "chapter_end", {"index": i, "item": dict(item)}
                    continue
                # 停在该章：重试时从这里续跑
                await _save(ctx, job)
                yield "chapter_end", {"index": i, "item": dict(item)}
                yield "error", {"detail": f"第 {item['chapter_no']} 章生成失败：{item['error']}"}
                return
            cp["phase"] = "analyze"
            await _save(ctx, job)

        # phase == analyze：正文已写好，还没分析
        async for event in _analyze_chapter(ctx, job, i, item):
            yield event
        cp.pop("rich_analysis", None)
        cp.pop("_batch_completion", None)
        cp["phase"] = "generate"
        cp["since_review"] = int(cp.get("since_review") or 0) + 1
        job.cursor = i + 1
        job.position = i + 1
        await _save(ctx, job)
        yield "chapter_end", {"index": i, "item": dict(item)}

        review_every = job.req.review_every
        if review_every > 0 and cp["since_review"] >= review_every and await _needs_work(ctx, job, job.cursor):
            cp["since_review"] = 0
            await _save(ctx, job)
            yield "paused", summarize(cp, job.cursor, job.total)
            return

    yield "done", summarize(cp, job.cursor, job.total)
