"""一致性检查任务（consistency.check）：预览、受理校验、逐章抽取、跨章比对与合并、概要。

一个任务内完成：先逐章抽取事实台账（每章一次独立提交），再跨章比对并在单事务内合并问题。
授权沿用大纲 / 实体任务的模板（E4-g3a）：作者与 accepted editor 共用，受理、构建前、每次出站
（for_task 发送 lease）、每次落库事务（``guard.require_task_write``）、每个事件发布与终态写入前
都按原 grant 与模型绑定重验；撤权 / 降级 / 关作者模型授权后停止且不再保存结果，已保存的台账与问题保留。

断点：通用任务类型的发送守卫要求 Task 的 step_cursor / checkpoint 在运行期间与领取时逐字一致，
所以本任务不在运行中改写 Task 断点。进度记录写在 ``consistency_runs.stats``（与每章台账同一事务提交），
续跑依靠台账自身：重试沿用原 chapter_ids，台账仍新鲜的章节直接复用、不再调用模型。

检查只读正文、伏笔记录和本域三张表；不写正文、设定、伏笔、剧情记忆与章节分析，也不写生成记录。
"""

import asyncio
import logging
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.json_extract import JSONExtractError
from app.ai.prompts.consistency import COMPARE_KEY, EXTRACT_KEY
from app.ai.providers.base import AIProviderError
from app.ai.templating import PromptOverride
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.models import AIConfig, Chapter, ConsistencyIssue, ConsistencyRun, Task
from app.models.entities import ACTIVE_TASK_STATUSES
from app.schemas.consistency import CheckPreview, CheckScope
from app.services import (
    ai_config_service,
    authorization_service,
    collaboration_ai_guard,
    project_access,
    project_ai_config,
    project_lifecycle,
    prompt_service,
)
from app.services.ai_dispatch import VisibilityPolicyError, read_visibility_stamp, visibility_sessionmaker
from app.services.consistency import issues, ledger
from app.services.consistency.model_call import ModelTarget, complete
from app.services.project_service import get_execution_project
from app.services.task_engine.execution_identity import (
    TaskExecutionIdentity,
    matches_running,
    require_live_worker,
    require_write_execution,
)
from app.services.task_engine.registry import Event, TaskContext

logger = logging.getLogger(__name__)

KIND = "consistency.check"
# 单次检查需要重新抽取（调用模型）的章数上限；可复用台账的章节不计入
MAX_EXTRACT = 300
# 进度分配：抽取 70%，比对 25%，单调且不超过 95
EXTRACT_SHARE = 70
COMPARE_SHARE = 25
MAX_WARNINGS = 50
WARNING_LIMIT = 200

NO_CHAPTERS = "范围内没有可检查的章节"
OPEN_CHECK = "已有进行中的一致性检查，请等待其完成后再试"
OPEN_IMPACT = "已有进行中的修改影响评估，请等待其完成后再试"
# 修改影响评估（services/consistency/impact_task.py）与检查共用同一个 resource_key：二者都会改写事实台账。
IMPACT_KIND = "consistency.impact"
STOPPED = "权限或模型授权已变化，已停止发送"
EMPTY_OUTPUT = "模型未返回任何内容"
ISSUE_NOT_FOUND = "问题不存在"
ISSUE_NO_CHAPTERS = "该问题的证据章节均已删除或没有正文，无法重新核查"


def resource_key(project_id: int) -> str:
    return f"consistency:{project_id}"


def _too_many(count: int) -> BadRequestError:
    return BadRequestError(f"需要重新抽取的章节过多（{count} 章，上限 {MAX_EXTRACT} 章），请缩小检查范围")


def _binding_changed() -> ConflictError:
    return ConflictError("任务模型预检身份已变化")


def _retry_changed() -> ConflictError:
    return ConflictError("原任务模型绑定无法验证，请重新创建任务")


# ---------------- 范围、预览 ----------------


@dataclass(frozen=True)
class CheckPlan:
    """一次检查的范围求值结果（受理时定稿）。"""

    # 范围内的非空章节，按 (chapter_no, id) 排序
    chapters: tuple[Chapter, ...]
    # 范围内章数（含空章）与其中的空章数
    total: int
    empty: int
    # 需要重新抽取（台账缺失或过期）与可复用的章数
    to_extract: int
    reusable: int
    # 写入 consistency_runs.scope 的范围描述（不含 chapter_ids）
    scope: dict[str, Any]
    title: str

    @property
    def chapter_ids(self) -> list[int]:
        return [chapter.id for chapter in self.chapters]


def normalize_request(project_id: int, params: dict[str, Any]) -> dict[str, Any]:
    """任务原始请求（task.input.request）的规范形状；重试时原样复用。"""
    ai_config_id = params.get("ai_config_id")
    if ai_config_id is not None and (type(ai_config_id) is not int or ai_config_id <= 0):
        raise BadRequestError("模型配置无效")
    if params.get("mode") == "issue":
        issue_id = params.get("issue_id")
        if type(issue_id) is not int or issue_id <= 0:
            raise BadRequestError("要重新核查的问题无效")
        return {"project_id": project_id, "mode": "issue", "issue_id": issue_id, "ai_config_id": ai_config_id}
    try:
        scope = CheckScope.model_validate({
            key: params.get(key) for key in ("mode", "start_chapter_id", "end_chapter_id")
            if params.get(key) is not None
        })
    except ValidationError:
        raise BadRequestError("检查范围无效") from None
    return {"project_id": project_id, **scope.model_dump(mode="json"), "ai_config_id": ai_config_id}


async def issue_chapter_ids(db: AsyncSession, project_id: int, issue_id: int) -> list[int]:
    """问题证据中仍带章节 id 的章节（是否仍存在由调用方按作品过滤）。问题不存在或不属于该作品 → 404。"""
    evidence = await db.scalar(select(ConsistencyIssue.evidence).where(
        ConsistencyIssue.id == issue_id, ConsistencyIssue.project_id == project_id,
    ))
    if evidence is None:
        raise NotFoundError(ISSUE_NOT_FOUND)
    return sorted(issues._evidence_chapters(evidence)[0])  # noqa: SLF001


async def plan_check(db: AsyncSession, project_id: int, request: dict[str, Any]) -> CheckPlan:
    """按规范化后的请求求值范围并统计台账状态。只读；调用方须已校验作品权限。"""
    statement = select(Chapter).where(Chapter.project_id == project_id)
    scope: dict[str, Any] = {"mode": request["mode"], "start_chapter_id": None, "end_chapter_id": None}
    if request["mode"] == "issue":
        chapter_ids = await issue_chapter_ids(db, project_id, request["issue_id"])
        if not chapter_ids:
            raise BadRequestError(ISSUE_NO_CHAPTERS)
        statement = statement.where(Chapter.id.in_(chapter_ids))
        scope["issue_id"] = request["issue_id"]
        title = f"一致性检查 · 重新核查问题 #{request['issue_id']}"
    elif request["mode"] == "range":
        ends = {
            row.id: row.chapter_no
            for row in await db.execute(select(Chapter.id, Chapter.chapter_no).where(
                Chapter.project_id == project_id,
                Chapter.id.in_((request["start_chapter_id"], request["end_chapter_id"])),
            ))
        }
        if request["start_chapter_id"] not in ends or request["end_chapter_id"] not in ends:
            raise BadRequestError("起止章节不存在或不属于该项目")
        first, last = ends[request["start_chapter_id"]], ends[request["end_chapter_id"]]
        if first > last:
            raise BadRequestError("起始章节不能晚于结束章节")
        statement = statement.where(Chapter.chapter_no >= first, Chapter.chapter_no <= last)
        scope.update(start_chapter_id=request["start_chapter_id"], end_chapter_id=request["end_chapter_id"])
        title = f"一致性检查 · 第{first}章" if first == last else f"一致性检查 · 第{first}–{last}章"
    else:
        title = "一致性检查 · 全书"
    rows = list(await db.scalars(statement.order_by(Chapter.chapter_no, Chapter.id)))
    chapters = tuple(chapter for chapter in rows if chapter.content.strip())
    states = await ledger.ledger_states(db, project_id, list(chapters))
    reusable = sum(1 for state in states.values() if state == "fresh")
    return CheckPlan(
        chapters=chapters,
        total=len(rows),
        empty=len(rows) - len(chapters),
        to_extract=len(chapters) - reusable,
        reusable=reusable,
        scope=scope,
        title=title,
    )


async def open_check(db: AsyncSession, project_id: int, kind: str = KIND) -> tuple[int, str | None, int] | None:
    """本作品进行中（排队或运行）的检查任务（或指定种类的任务）：(id, task_key, user_id)。"""
    row = (await db.execute(
        select(Task.id, Task.statistics_key, Task.user_id)
        .where(Task.project_id == project_id, Task.kind == kind, Task.status.in_(ACTIVE_TASK_STATUSES))
        .order_by(Task.id)
        .limit(1)
    )).one_or_none()
    return (row.id, row.statistics_key, row.user_id) if row is not None else None


async def require_free(db: AsyncSession, project_id: int) -> None:
    """检查与修改影响评估互斥：任一在进行中都拒绝新的发起（最终由 resource_key 的部分唯一索引保证）。"""
    if await open_check(db, project_id) is not None:
        raise ConflictError(OPEN_CHECK)
    if await open_check(db, project_id, IMPACT_KIND) is not None:
        raise ConflictError(OPEN_IMPACT)


async def preview(db: AsyncSession, user_id: int, project_id: int, scope: CheckScope) -> CheckPreview:
    """发起前的预览：不建任务、不校验模型配置。"""
    await project_access.require_project(db, user_id, project_id, "ai.use")
    plan = await plan_check(db, project_id, normalize_request(project_id, scope.model_dump(mode="json")))
    warnings = []
    if not plan.chapters:
        warnings.append(NO_CHAPTERS)
    if plan.to_extract > MAX_EXTRACT:
        warnings.append(f"需要重新抽取 {plan.to_extract} 章，超过单次上限 {MAX_EXTRACT} 章，请缩小检查范围")
    if await open_check(db, project_id) is not None:
        warnings.append("已有进行中的一致性检查")
    elif await open_check(db, project_id, IMPACT_KIND) is not None:
        warnings.append("已有进行中的修改影响评估")
    return CheckPreview(
        total=plan.total, to_extract=plan.to_extract, reusable=plan.reusable, empty=plan.empty, warnings=warnings,
    )


def _require_startable(plan: CheckPlan) -> None:
    if not plan.chapters:
        raise BadRequestError(ISSUE_NO_CHAPTERS if plan.scope["mode"] == "issue" else NO_CHAPTERS)
    if plan.to_extract > MAX_EXTRACT:
        raise _too_many(plan.to_extract)


# ---------------- 受理 ----------------


async def admit_task(
    db: AsyncSession, engine: Any, user_id: int, project_id: int, kind: str, request: dict[str, Any],
    precheck: Callable[[AsyncSession], Awaitable[None]],
) -> Any:
    """HTTP 发起：任何写锁前完成能力短读、业务预检、模型预检与 DNS（受理证明），再交中央受理在同一写事务内消费。

    检查与修改影响评估共用。作者与 accepted editor 共用：本人模型优先，仅本人无文本配置且作者授权时回退作者配置，
    本人配置无效不回退；viewer 403、待接受 / 非成员 / 已撤权 / 仅管理员身份 404，均不建任务、不调用模型。
    precheck 在只读会话内给出能同步判定的业务错误（受理锁内还会完整重验）。
    """
    from app.services import task_service
    from app.services.task_engine.admission import prepare_task_admission

    actor = authorization_service.actor_snapshot(db, user_id)
    if (type(actor) is not authorization_service.AuthenticatedActor or actor.id != user_id
            or type(actor.token_version) is not int or actor.token_version < 0 or db.new or db.dirty or db.deleted):
        raise ConflictError("原认证身份无法验证", extra={"code": "task_actor_unavailable"})
    # 请求认证产生的只读事务在 DNS 前结束，不占用连接或锁。
    await db.rollback()
    ctx = engine.ctx
    async with ctx.sessionmaker() as read:
        await authorization_service.require_authenticated(read, actor)
        await project_access.require_project(read, actor.id, project_id, "ai.use")
        await precheck(read)
        await require_free(read, project_id)
    model = await project_ai_config.preflight_project_model(
        ctx.sessionmaker, actor, project_id, "text", cipher=ctx.cipher, config_id=request["ai_config_id"],
        allow_private=ctx.settings.allow_private_ai_endpoints, for_task_admission=True,
    )
    admission = await prepare_task_admission(ctx, actor, kind, request, model_preflight=model)
    return await task_service.create_task(db, engine, user_id, kind, request, admission=admission)


async def create_check_task(
    db: AsyncSession, engine: Any, user_id: int, project_id: int, params: dict[str, Any],
) -> Any:
    """HTTP 发起检查；受理流程见 admit_task。"""
    request = normalize_request(project_id, params)

    async def precheck(read: AsyncSession) -> None:
        _require_startable(await plan_check(read, project_id, request))

    return await admit_task(db, engine, user_id, project_id, KIND, request, precheck)


async def _accepted_binding(
    db: AsyncSession, *, user_id: int, project_id: int, ai_config_id: int | None, allow_private: bool,
    admission: project_ai_config.ProjectModelPreflight, accepted_model: project_ai_config.ResolvedProjectModel,
) -> project_ai_config.ResolvedProjectModel:
    """核对中央接受的模型绑定，并按其 grant 在 认证 → 发送写门禁 → 作品 屏障内重验 ai.use（同大纲任务）。"""
    admission = project_ai_config.require_original_preflight(admission)
    if (type(accepted_model) is not project_ai_config.ResolvedProjectModel or accepted_model != admission.binding
            or admission.actor.id != user_id or admission.binding.project_id != project_id
            or admission.binding.model_type != "text" or admission.allow_private != allow_private):
        raise _binding_changed()
    if project_ai_config.is_original_model_preflight(admission):
        # 原重试凭据由中央在当前事务内唯一消费；此处只接受它返回的同一绑定对象。
        if accepted_model is not admission.binding or accepted_model.config_id != ai_config_id:
            raise _binding_changed()
    elif admission.requested_config_id != ai_config_id:
        raise _binding_changed()
    with db.no_autoflush:
        project = await project_access.require_project(
            db, user_id, project_id, "ai.use", for_update=True, visibility_write=True,
            grant_identity=accepted_model.grant_identity,
        )
    if project.user_id != accepted_model.project_owner_id:
        raise _binding_changed()
    return accepted_model


def _task_input(
    request: dict[str, Any], chapter_ids: list[int], scope: dict[str, Any],
    binding: project_ai_config.ResolvedProjectModel,
) -> dict[str, Any]:
    return {
        "request": request,
        "chapter_ids": chapter_ids,
        "scope": scope,
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
        raise _binding_changed()
    request = normalize_request(project_id, params)
    binding = await _accepted_binding(
        db, user_id=user_id, project_id=project_id, ai_config_id=request["ai_config_id"],
        allow_private=ctx.settings.allow_private_ai_endpoints, admission=admission, accepted_model=accepted_model,
    )
    plan = await plan_check(db, project_id, request)
    _require_startable(plan)
    # 同作品互斥最终由 resource_key 的部分唯一索引保证；这里先给出更明确的提示。
    await require_free(db, project_id)
    return project_id, plan.title, _task_input(request, plan.chapter_ids, plan.scope, binding)


async def retry_binding(
    db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task, kind: str,
    admission: project_ai_config.ProjectModelPreflight, accepted_model: project_ai_config.ResolvedProjectModel,
) -> tuple[dict[str, Any], project_ai_config.ResolvedProjectModel]:
    """重试共用：核对原任务形状与原模型绑定（须与中央接受的绑定逐字一致），返回 (原请求, 绑定)。"""
    data = task.input
    if (task.user_id != user_id or task.kind != kind or type(task.project_id) is not int
            or task.chapter_id is not None or type(data) is not dict or type(data.get("request")) is not dict
            or type(accepted_model) is not project_ai_config.ResolvedProjectModel
            or accepted_model.project_id != task.project_id):
        raise _retry_changed()
    request = data["request"]
    binding = project_ai_config.deserialize_project_model(
        data.get("_project_model"), actor_id=user_id, project_id=task.project_id,
        project_owner_id=accepted_model.project_owner_id,
    )
    if (binding is None or binding != accepted_model or binding.model_type != "text"
            or type(data.get("ai_config_id")) is not int or data["ai_config_id"] != binding.config_id
            or type(request.get("project_id")) is not int or request["project_id"] != task.project_id
            or (request.get("ai_config_id") is not None
                and (type(request["ai_config_id"]) is not int or request["ai_config_id"] != binding.config_id))):
        raise _retry_changed()
    current = await _accepted_binding(
        db, user_id=user_id, project_id=task.project_id, ai_config_id=binding.config_id,
        allow_private=ctx.settings.allow_private_ai_endpoints, admission=admission, accepted_model=accepted_model,
    )
    if current != binding:
        raise ConflictError("原任务模型配置已变化，请重新创建任务")
    return request, binding


async def validate_retry(
    db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task,
    admission: project_ai_config.ProjectModelPreflight, accepted_model: project_ai_config.ResolvedProjectModel,
) -> tuple[int, str, dict[str, Any]]:
    """重试：沿用原范围（chapter_ids）与原模型绑定，不重新求值范围；已抽取且仍新鲜的台账在运行时直接复用。"""
    data = task.input
    chapter_ids = data.get("chapter_ids") if type(data) is dict else None
    scope = data.get("scope") if type(data) is dict else None
    if not _valid_chapter_ids(chapter_ids) or type(scope) is not dict:
        raise _retry_changed()
    request, binding = await retry_binding(
        db, ctx, user_id=user_id, task=task, kind=KIND, admission=admission, accepted_model=accepted_model,
    )
    await require_free(db, task.project_id)
    return task.project_id, task.title, _task_input(request, list(chapter_ids), dict(scope), binding)


def _valid_chapter_ids(value: Any) -> bool:
    return (type(value) is list and bool(value) and all(type(item) is int and item > 0 for item in value)
            and len(set(value)) == len(value))


# ---------------- 执行 ----------------


@dataclass
class CheckJob:
    project_id: int
    actor_id: int
    task_id: int
    run_id: int
    chapter_ids: list[int]
    target: ModelTarget = field(repr=False)
    guard: collaboration_ai_guard.SendGuard = field(repr=False)
    execution: TaskExecutionIdentity = field(repr=False)
    char_budget: int = ledger.DEFAULT_CHAR_BUDGET
    # 组装 job 时按真实操作者取出的提示词覆盖（任务运行中途修改模板不影响本次任务）
    extract_override: PromptOverride | None = None
    compare_override: PromptOverride | None = None
    # 每章结果，与 chapter_ids 一一对应：{chapter_id, state, error}
    items: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    phase: str = "extract"
    # 抽取阶段下一章的下标（只写入运行记录，供查看；不写 Task 断点）
    cursor: int = 0
    progress: int = 1
    # 已发布的进度日志全文（事件核验用）；模型原文不进入任务输出
    log: str = ""
    cancelled: bool = False
    # 合并事务提交后才有值；done 事件与终态核验逐字比对
    result: dict[str, Any] | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _warn(job: CheckJob, text: str) -> None:
    if len(job.warnings) < MAX_WARNINGS:
        job.warnings.append(text[:WARNING_LIMIT])


def run_stats(
    job: CheckJob, *, merge: issues.MergeStats | None = None, batches: int = 0, failed_batches: int = 0,
    stale_outside: int = 0,
) -> dict[str, Any]:
    """运行记录的统计（RunStats 的落库形状，另含 phase / cursor 两项进度字段）。只存章节 id，不存章号。"""
    counts = Counter(item["state"] for item in job.items)
    return {
        "total": len(job.items),
        "extracted": counts["extracted"],
        "reused": counts["reused"],
        "skipped": counts["skipped_changed"] + counts["skipped_missing"],
        "failed": counts["failed"],
        "created": merge.created if merge is not None else 0,
        "updated": merge.updated if merge is not None else 0,
        "reappeared": merge.reappeared if merge is not None else 0,
        "not_reproduced": merge.not_reproduced if merge is not None else 0,
        "stale_outside": stale_outside,
        "batches": batches,
        "failed_batches": failed_batches,
        "skipped_chapters": [
            {"chapter_id": item["chapter_id"], "state": item["state"], "error": item["error"]}
            for item in job.items if item["state"] in ("skipped_changed", "skipped_missing", "failed")
        ],
        "warnings": list(job.warnings),
        "phase": job.phase,
        "cursor": job.cursor,
    }


async def _save_run(db: AsyncSession, job: CheckJob, stats: dict[str, Any], *, status: str | None = None) -> None:
    """在调用方事务内更新运行记录（不 commit）；只更新仍为 running 的本次记录。"""
    values: dict[str, Any] = {"stats": stats}
    if status is not None:
        values.update(status=status, finished_at=_now())
    result = await db.execute(
        update(ConsistencyRun)
        .where(ConsistencyRun.id == job.run_id, ConsistencyRun.project_id == job.project_id,
               ConsistencyRun.status == "running")
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise ConflictError("检查运行记录已变化，本次结果未保存")


async def close_run(
    sessionmaker: async_sessionmaker[AsyncSession], task: Task, status: str, stats: dict[str, Any] | None = None,
    *, worker_key: str | None,
) -> None:
    """任务取消 / 失败 / 中断时把仍为 running 的运行记录置为终态（幂等）。已保存的台账与问题不动。

    运行记录只用于展示，不是资源屏障：这里不要求当前授权，也不向上抛错（读取端会再按任务终态校正）。
    """
    values: dict[str, Any] = {"status": status, "finished_at": _now()}
    if stats is not None:
        values["stats"] = stats
    try:
        async with sessionmaker() as db:
            if task.project_id is None or task.statistics_key is None:
                return
            await project_lifecycle.lock_record(db, task.project_id)
            found = await db.scalar(select(Task.id).where(
                Task.id == task.id, Task.statistics_key == task.statistics_key, Task.project_id == task.project_id,
                Task.user_id == task.user_id, Task.kind == KIND, Task.queued_at == task.queued_at,
                Task.execution_key == task.execution_key, Task.status == task.status, Task.worker_key == worker_key,
            ).with_for_update())
            if found is None:
                return
            await require_live_worker(db, worker_key)
            await db.execute(
                update(ConsistencyRun)
                .where(ConsistencyRun.task_id == task.id, ConsistencyRun.project_id == task.project_id,
                       ConsistencyRun.status == "running")
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
    except Exception:  # noqa: BLE001  不输出可能含统计内容的 SQL 参数
        logger.error("一致性检查运行记录收尾失败 task=%s", task.id)


def _char_budget(bound: collaboration_ai_guard.BoundProvider, default: int) -> int:
    # 与章节分析同一口径：按模型上下文大小推算，未设置时用系统默认。
    snapshot = AIConfig(context_window_k=bound.context_window_k, max_output_k=bound.max_output_k)
    return ai_config_service.context_char_budget(snapshot, default)


@dataclass
class BoundExecution:
    """一次执行绑定好的出站与落库凭据：发送守卫、受控模型、字符预算与按真实操作者读取的提示词覆盖。"""

    guard: collaboration_ai_guard.SendGuard = field(repr=False)
    target: ModelTarget = field(repr=False)
    char_budget: int
    overrides: dict[str, PromptOverride | None]


async def bind_execution(
    ctx: TaskContext, task: Task, execution: TaskExecutionIdentity, *, kind: str, prompt_keys: tuple[str, ...],
) -> BoundExecution:
    """运行开始时按原领取身份重验并绑定出站门禁的 provider（检查与修改影响评估共用）。"""
    if (type(execution) is not TaskExecutionIdentity or not matches_running(execution, task)
            or execution.kind != kind or type(execution.project_id) is not int or execution.chapter_id is not None):
        raise ConflictError("任务执行身份已变化")
    data = execution.input
    request = data.get("request")
    if (type(request) is not dict or type(request.get("project_id")) is not int
            or request["project_id"] != execution.project_id):
        raise ConflictError("任务执行身份已变化")
    # 构建前按原领取身份锁内重验（令牌；协作者另含 accepted editor、原邀请轮次、作者模型授权），随即结束该写事务：
    # 后续发送与落库各自在 lease / 短事务内重验，不能持写锁跨越它们（SQLite 会自锁）。
    async with ctx.sessionmaker() as barrier:
        await require_write_execution(barrier, execution)
        await barrier.rollback()
    allow_private = ctx.settings.allow_private_ai_endpoints
    async with ctx.sessionmaker() as db:
        project = await get_execution_project(db, execution.actor_id, execution.project_id, execution)
        binding = project_ai_config.deserialize_project_model(
            data.get("_project_model"), actor_id=execution.actor_id, project_id=execution.project_id,
            project_owner_id=project.user_id,
        )
        if (binding is None or binding.model_type != "text" or binding.grant_identity != execution.grant_identity
                or type(data.get("ai_config_id")) is not int or data["ai_config_id"] != binding.config_id
                or (request.get("ai_config_id") is not None
                    and (type(request["ai_config_id"]) is not int or request["ai_config_id"] != binding.config_id))):
            raise ConflictError("任务模型身份无法验证")
        stamp = await read_visibility_stamp(db, project.id)
        guard = collaboration_ai_guard.for_task(
            execution, binding, stamp, cipher=ctx.cipher, allow_private=allow_private,
        )
        # 提示词覆盖按真实操作者读取：协作者不使用作者的私人模板。
        overrides = {key: await prompt_service.get_override(db, execution.actor_id, key) for key in prompt_keys}
        maker = visibility_sessionmaker(db)
        await db.rollback()
    bound = await guard.prepare_provider(maker, ctx.factory, request_timeout=ctx.settings.ai_request_timeout)
    target = ModelTarget(
        provider=bound.provider,
        model=bound.model,
        max_tokens=bound.max_output_k * ai_config_service.TOKENS_PER_K if bound.max_output_k else None,
        thinking=bound.thinking_level,  # type: ignore[arg-type]
    )
    return BoundExecution(
        guard=guard, target=target, char_budget=_char_budget(bound, ctx.settings.context_char_budget),
        overrides=overrides,
    )


async def build_check_job(ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity) -> CheckJob:
    """运行开始时组装：按原领取身份重验、绑定出站门禁的 provider、建立运行记录。"""
    data = execution.input if type(execution) is TaskExecutionIdentity and type(execution.input) is dict else {}
    chapter_ids, scope = data.get("chapter_ids"), data.get("scope")
    if not _valid_chapter_ids(chapter_ids) or type(scope) is not dict:
        raise ConflictError("任务执行身份已变化")
    bound = await bind_execution(ctx, task, execution, kind=KIND, prompt_keys=(EXTRACT_KEY, COMPARE_KEY))
    async with ctx.sessionmaker() as db:
        # 运行记录是本任务的第一笔写入：与之后每次落库一样，在同一事务内按原 grant 与模型绑定重验。
        await bound.guard.require_task_write(db)
        run = ConsistencyRun(
            project_id=execution.project_id,
            task_id=execution.task_id,
            actor_id=execution.actor_id,
            scope={**scope, "chapter_ids": list(chapter_ids)},
            status="running",
            stats={},
            started_at=_now(),
        )
        db.add(run)
        await db.commit()
        run_id = run.id
    return CheckJob(
        project_id=execution.project_id,
        actor_id=execution.actor_id,
        task_id=execution.task_id,
        run_id=run_id,
        chapter_ids=list(chapter_ids),
        target=bound.target,
        guard=bound.guard,
        execution=execution,
        char_budget=bound.char_budget,
        extract_override=bound.overrides[EXTRACT_KEY],
        compare_override=bound.overrides[COMPARE_KEY],
        items=[{"chapter_id": chapter_id, "state": "pending", "error": ""} for chapter_id in chapter_ids],
    )


@dataclass
class _Step:
    state: str
    error: str = ""
    facts: int = 0
    # 需要调用模型时的抽取输入；None = 无需调用（复用 / 跳过）
    source: ledger.ChapterSource | None = None


async def _inspect_chapter(
    job: CheckJob, sessionmaker: async_sessionmaker[AsyncSession], chapter_id: int,
) -> _Step:
    """读取章节与当前指纹：章节已删、已变空或台账新鲜时无需调用模型。只读。"""
    async with sessionmaker() as db:
        try:
            source = await ledger.load_source(db, job.project_id, chapter_id, char_budget=job.char_budget)
        except VisibilityPolicyError:
            raise
        except ConflictError:
            # 读取期间章节正被改动（或区块结构无效）：不能证明输入对应当前正文
            return _Step("skipped_changed", ledger.CHANGED)
        if source is None:
            return _Step("skipped_missing")
        if not source.content.strip():
            return _Step("skipped_changed", ledger.EMPTY_CONTENT)
        existing = (await ledger.ledgers_by_chapter(db, job.project_id, [chapter_id])).get(chapter_id)
        if ledger.is_fresh(existing, source.source_hash):
            return _Step("reused", facts=len(existing.facts or []))
        return _Step("pending", source=source)


async def _extract_chapter(
    job: CheckJob, sessionmaker: async_sessionmaker[AsyncSession], source: ledger.ChapterSource,
) -> _Step:
    """调用模型并落库。出站经 guard 的发送 lease 重验；落库事务内再按原 grant 与模型绑定重验，
    并由 save_ledger 重验章节仍存在且指纹未变。授权失效（VisibilityPolicyError）向上抛出，终止整次检查。"""
    try:
        text = await complete(job.target, ledger.build_extract_messages(source, job.extract_override))
        if not text.strip():
            raise AIProviderError(EMPTY_OUTPUT)
        facts, warnings = ledger.parse_extraction(text, source)
    except AIProviderError as exc:
        return _Step("failed", str(exc))
    except JSONExtractError as exc:
        return _Step("failed", f"AI 输出解析失败：{exc}")
    async with sessionmaker() as db:
        await job.guard.require_task_write(db)
        saved = await ledger.save_ledger(db, source, facts, model=job.target.model, task_id=job.task_id)
        step = _Step("extracted", facts=len(facts)) if saved is not None else _Step("skipped_changed", ledger.CHANGED)
        _record(job, source.chapter_id, step)
        if saved is not None:
            for warning in warnings:
                _warn(job, f"第 {source.chapter_no} 章：{warning}")
        # 每章台账与进度记录同一事务单独提交：中断后最多重做当前章。
        await _save_run(db, job, run_stats(job))
        await db.commit()
    return step


def _record(job: CheckJob, chapter_id: int, step: _Step) -> None:
    index = job.chapter_ids.index(chapter_id)
    job.items[index] = {"chapter_id": chapter_id, "state": step.state, "error": step.error[:WARNING_LIMIT]}
    job.cursor = max(job.cursor, index + 1)


def _line(position: int, total: int, step: _Step) -> str:
    text = {
        "extracted": f"抽取 {step.facts} 条事实",
        "reused": "台账未变，直接复用",
        "skipped_changed": "正文在检查期间发生变化，本次结果未保存",
        "skipped_missing": "章节已删除，跳过",
        "failed": "抽取失败",
    }[step.state]
    return f"第 {position}/{total} 章：{text}\n"


def _log(job: CheckJob, text: str) -> Event:
    job.log += text
    return "delta", {"text": text}


async def run_check_job(job: CheckJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    """事件：stage（阶段文案）、delta（进度日志，不含模型原文）、done（概要）、error。"""
    total = len(job.chapter_ids)
    try:
        attempted = 0
        first_failure = ""
        for index, chapter_id in enumerate(job.chapter_ids):
            step = await _inspect_chapter(job, sessionmaker, chapter_id)
            if step.source is not None:
                yield "stage", {"stage": f"第 {index + 1}/{total} 章 · 抽取事实"}
                attempted += 1
                step = await _extract_chapter(job, sessionmaker, step.source)
                if step.state == "failed":
                    first_failure = first_failure or step.error
            _record(job, chapter_id, step)
            job.progress = max(job.progress, min(EXTRACT_SHARE, EXTRACT_SHARE * (index + 1) // total))
            yield _log(job, _line(index + 1, total, step))
        failed = sum(1 for item in job.items if item["state"] == "failed")
        if attempted and failed == attempted:
            # 需要调用模型的章节全部失败（多为模型不可用）：不再比对，保留已有台账，重试时从这些章节继续。
            raise AIProviderError(f"事实抽取全部失败：{first_failure}")

        job.phase = "compare"
        yield "stage", {"stage": "比对准备中"}
        async with sessionmaker() as db:
            data = await issues.load_compare_input(db, job.project_id, job.chapter_ids)
        for item in job.items:
            # 抽取之后、比对之前正文又被修改（或章节被删除）：台账已过期，不参与比对，在概要中列出（C13）。
            if item["state"] in ("extracted", "reused") and item["chapter_id"] not in data.covered_ids:
                item.update(state="skipped_changed", error=ledger.CHANGED)
        batches = issues.plan_batches(data, issues.group_facts(data), char_budget=job.char_budget)
        # 被拆批的大组、未能发送的伏笔记录与已有问题摘要：告知作者，不静默截断。
        for notice in issues.batch_notices(batches):
            _warn(job, notice)
        incomplete = issues.has_partial(batches)
        findings: list[issues.Finding] = []
        failed_batches = 0
        batch_failure = ""
        for number, batch in enumerate(batches, start=1):
            yield "stage", {"stage": f"比对 {number}/{len(batches)} 批"}
            try:
                text = await complete(job.target, issues.build_compare_messages(batch, job.compare_override))
                if not text.strip():
                    raise AIProviderError(EMPTY_OUTPUT)
                found, warnings = issues.parse_comparison(text, batch)
            except AIProviderError as exc:
                failed_batches += 1
                batch_failure = batch_failure or str(exc)
                _warn(job, f"第 {number} 批比对失败：{exc}")
                outcome = "失败"
            except JSONExtractError as exc:
                failed_batches += 1
                batch_failure = batch_failure or f"AI 输出解析失败：{exc}"
                _warn(job, f"第 {number} 批比对失败：AI 输出解析失败：{exc}")
                outcome = "失败"
            else:
                findings.extend(found)
                for warning in warnings:
                    _warn(job, f"第 {number} 批：{warning}")
                outcome = "完成"
            job.progress = max(job.progress, EXTRACT_SHARE + COMPARE_SHARE * number // len(batches))
            yield _log(job, f"比对 {number}/{len(batches)} 批：{outcome}\n")
        if batches and failed_batches == len(batches):
            raise AIProviderError(f"跨章比对全部失败：{batch_failure}")

        yield "stage", {"stage": "合并问题"}
        async with sessionmaker() as db:
            # require_task_write 持有作品行锁直到提交：合并与作者的状态 / 备注 CAS 串行。
            await job.guard.require_task_write(db)
            if await issues.compare_dates_changed(db, data):
                findings.clear()
                incomplete = True
                _warn(job, issues.STORY_TIME_CHANGED)
            merged = await issues.merge_findings(
                db,
                project_id=job.project_id,
                run_id=job.run_id,
                findings=findings,
                # 有批次失败或大组被拆批时比对并未覆盖全部跨章组合：不标记「本次未复现」
                covered_chapter_ids=frozenset() if failed_batches or incomplete else data.covered_ids,
                scope_chapter_ids=data.scope_ids,
            )
            job.phase = "done"
            stats = run_stats(
                job, merge=merged, batches=len(batches), failed_batches=failed_batches,
                stale_outside=data.stale_outside,
            )
            await _save_run(db, job, stats, status="succeeded")
            await db.commit()
        job.progress = EXTRACT_SHARE + COMPARE_SHARE
        job.result = {"run_id": job.run_id, **stats}
        yield "done", job.result
    except asyncio.CancelledError:
        job.cancelled = True
        raise
    except JSONExtractError as exc:
        yield "error", {"detail": f"AI 输出解析失败：{exc}，请重试"}
    except AIProviderError as exc:
        yield "error", {"detail": str(exc)}
    except VisibilityPolicyError:
        yield "error", {"detail": STOPPED}
    except AppError as exc:
        yield "error", {"detail": exc.detail}
    except Exception:  # noqa: BLE001
        logger.error("一致性检查失败 project=%s task=%s", job.project_id, job.task_id)
        yield "error", {"detail": "一致性检查失败：服务器内部错误"}
