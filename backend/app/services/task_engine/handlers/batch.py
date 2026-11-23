"""chapter.batch：批量顺序生成（包装 batch_service）。

一个任务内逐章生成 + 分析；断点存 step_cursor / checkpoint。审核点产出 paused（任务转为待审核），
继续由 task_service.resume_task 触发（revalidate_resume），重试沿用原范围与断点（validate_retry）。
"""

import copy
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Task
from app.schemas.batch import BatchGenerateRequest
from app.services import (
    ai_dispatch,
    analysis_service,
    batch_service,
    chapter_claim,
    collaboration_ai_guard,
    project_ai_config,
)
from app.services.chapter_service import count_words
from app.services.task_engine.execution_identity import (
    TaskExecutionIdentity,
    matches_running,
    matches_running_base,
    same_execution,
)
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register

logger = logging.getLogger(__name__)


def _changed() -> ConflictError:
    return ConflictError("批量输出身份已变化", extra={"code": "chapter_claim_changed"})


def _cached(
    job: batch_service.BatchJob, slot: str, inputs: tuple[object, ...],
) -> collaboration_ai_guard.SendGuard | None:
    """缓存只在全部绑定输入（按对象身份）未变时有效；否则视为不存在。"""
    cached = job.publication_guards.get(slot)
    if (isinstance(cached, tuple) and len(cached) == 2 and type(cached[0]) is tuple
            and len(cached[0]) == len(inputs)
            and all(old is new for old, new in zip(cached[0], inputs, strict=True))
            and type(cached[1]) is collaboration_ai_guard.SendGuard):
        return cached[1]
    return None


def _attempt_inputs(job: batch_service.BatchJob, execution: TaskExecutionIdentity) -> tuple[object, ...]:
    """本章 attempt 的发布绑定：原执行、模型/向量、占用与 attempt；构建后另含本章 ChapterJob 与场景戳。"""
    claim, attempt, active = job.active_claim, job.active_attempt, job.active_chapter_job
    if (claim is None or type(attempt) is not collaboration_ai_guard.BatchAttemptIdentity or attempt.claim != claim
            or job.execution_identity != execution):
        raise _changed()
    if active is not None and (active.chapter_claim != claim or active.batch_attempt != attempt
                               or active.execution != execution or active.context_stamp is None):
        raise _changed()
    stamp = active.context_stamp if active is not None else None
    return (execution, job.model_binding, job.visibility_stamp, stamp, job.model_cipher, job.allow_private,
            claim, attempt, job.embedding_selection, active)


def _attempt_guard(job: batch_service.BatchJob, execution: TaskExecutionIdentity) -> collaboration_ai_guard.SendGuard:
    """本章 attempt 固定同一 SendGuard：发布核验与受控任务流帧来源须是同一对象（E5-c2，同 E5-c1 章节）。

    构建前（只为发布来源，无场景戳）与构建后（带本章场景戳）是两组不同输入；任一输入变化即重建，
    不跨章节或 attempt 复用。
    """
    inputs = _attempt_inputs(job, execution)
    cached = _cached(job, "attempt", inputs)
    if cached is not None:
        return cached
    active = job.active_chapter_job
    guard = collaboration_ai_guard.for_task(
        execution, job.model_binding,
        active.context_stamp.visibility if active is not None else job.visibility_stamp,
        cipher=job.model_cipher, allow_private=job.allow_private,
        scene=active.context_stamp if active is not None else None, chapter=job.active_claim,
        batch_attempt=job.active_attempt, extra_bindings=(job.embedding_selection,),
    )
    job.publication_guards["attempt"] = (inputs, guard)
    return guard


def _analysis_guard(
    job: batch_service.BatchJob, execution: TaskExecutionIdentity,
) -> collaboration_ai_guard.SendGuard:
    """本章逐章分析中的发布 guard：绑定分析原执行与收据引用（按 E5-c1 分析规则只读核对）。"""
    analysis, reference = job.active_analysis
    if (type(analysis) is not analysis_service.AnalysisExecution
            or type(reference) is not analysis_service.AnalysisReference
            or analysis.task_identity != execution or job.execution_identity != execution
            or analysis.batch_cursor != job.cursor or job.active_claim is not None):
        raise _changed()
    inputs = (execution, job.model_binding, job.visibility_stamp, job.model_cipher, job.allow_private,
              analysis, reference, job.embedding_selection)
    cached = _cached(job, "analysis", inputs)
    if cached is not None:
        return cached
    guard = collaboration_ai_guard.for_task(
        execution, job.model_binding, job.visibility_stamp, cipher=job.model_cipher, allow_private=job.allow_private,
        analysis_execution=analysis, analysis_reference=reference, extra_bindings=(job.embedding_selection,),
    )
    job.publication_guards["analysis"] = (inputs, guard)
    return guard


def _between_guard(
    job: batch_service.BatchJob, current: TaskExecutionIdentity,
) -> collaboration_ai_guard.SendGuard:
    """无当前章（章与章之间、审核点、完成）：仅发布用 guard，原执行即本执行最近一次确认提交的断点。"""
    if (job.active_claim is not None or job.active_analysis is not None
            or current is not job.task_execution_identity):
        raise _changed()
    inputs = (current, job.model_binding, job.visibility_stamp, job.model_cipher, job.allow_private,
              job.embedding_selection)
    cached = _cached(job, "between", inputs)
    if cached is not None:
        return cached
    guard = collaboration_ai_guard.for_task(
        current, job.model_binding, job.visibility_stamp, cipher=job.model_cipher, allow_private=job.allow_private,
        extra_bindings=(job.embedding_selection,), publication_only=True,
    )
    job.publication_guards["between"] = (inputs, guard)
    return guard


class BatchHandler:
    kinds = (batch_service.KIND,)
    execution_identity_aware = True
    # E4-g2：发起走锁外预检 + 中央受控受理（作者 Agent 的批量也只能经签名提案确认的 execute_admitted）；
    # accepted editor 的批量按原 grant 在领取、构建、每章断点/占用、每次发送、每章保存与终态前重验，
    # 撤权/降级/重邀/停用/关闭作者模型授权时失败关闭：已提交章节保留，未完成章节按原 claim/attempt 回退。
    admission_preflight_aware = True
    collaboration_execution_ready = True

    def __init__(self) -> None:
        # 运行中任务的执行对象（progress 只拿得到 task 与文本，借此读当前章下标与目标字数）
        self._jobs: dict[tuple[Any, ...], batch_service.BatchJob] = {}

    @staticmethod
    def _identity(task_id: int, task_key: str | None, queued_at: Any, execution_key: str | None) -> tuple[Any, ...]:
        stamp = chapter_claim._utc(queued_at) if queued_at is not None else None
        return task_id, task_key, stamp, execution_key

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any],
        admission: project_ai_config.ProjectModelPreflight,
        accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        if (type(accepted_model) is not project_ai_config.ResolvedProjectModel
                or accepted_model != project_ai_config.require_original_preflight(admission).binding
                or type(params.get("project_id")) is not int):
            raise batch_service._admission_changed()
        project_id = params["project_id"]
        req = BatchGenerateRequest.model_validate(params)
        fields = await batch_service.validate_start(
            db, ctx, user_id=user_id, project_id=project_id, req=req, accepted_model=accepted_model,
        )
        return TaskSpec(
            project_id=fields["project_id"],
            chapter_id=None,
            resource_key=batch_service.resource_key(fields["project_id"]),
            title=fields["title"],
            input=fields["input"],
        )

    async def validate_retry(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task,
        admission: project_ai_config.ProjectModelPreflight,
        accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        """重试：沿用原范围（chapter_ids）与断点，不重新求值范围，也不要求还有未写章节。

        中央已消费原任务来源的受控预检；原模型绑定（含原邀请轮次）须与接受的绑定逐字一致。
        """
        if (type(accepted_model) is not project_ai_config.ResolvedProjectModel
                or accepted_model != project_ai_config.require_original_preflight(admission).binding
                or task.user_id != user_id):
            raise batch_service._admission_changed()
        ai_config_id = await batch_service.revalidate(db, ctx, task, accepted_model=accepted_model)
        return TaskSpec(
            project_id=task.project_id,
            chapter_id=None,
            resource_key=batch_service.resource_key(int(task.project_id or 0)),
            title=task.title,
            input={**task.input, "ai_config_id": ai_config_id},
        )

    async def revalidate_resume(self, db: AsyncSession, ctx: TaskContext, task: Task) -> None:
        await batch_service.revalidate(db, ctx, task)

    def finish_result(self, task: Task) -> dict[str, Any]:
        return batch_service.finish_result(task)

    async def build_job(
        self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity,
    ) -> batch_service.BatchJob:
        job = await batch_service.build_batch_job(ctx, task, execution=execution)
        # 仅本执行体的 detached Task 保留内存凭据；运行中不得从数据库新 current 重建旧 claim。
        task._batch_job = job
        return job

    async def run(self, job: batch_service.BatchJob, ctx: TaskContext) -> AsyncIterator[Event]:
        key = self._identity(job.task_id, job.task_key, job.queued_at, job.execution_key)
        self._jobs[key] = job
        try:
            async with aclosing(batch_service.run_batch_job(job, ctx)) as events:
                async for event in events:
                    yield event
        finally:
            if self._jobs.get(key) is job:
                self._jobs.pop(key, None)

    async def verify_event(
        self, db: AsyncSession, task: Task, *, job: batch_service.BatchJob,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, event: str, data: dict[str, Any],
        cancellation_operation: str | None = None,
    ) -> None:
        """引擎持原认证和任务锁；输出只能来自已验证的批量轮次及本章 attempt。"""
        if (type(job) is not batch_service.BatchJob or job.execution_identity != execution
                or job.task_execution_identity != current_execution or not same_execution(execution, current_execution)
                or not matches_running(current_execution, task, cancellation_operation=cancellation_operation)
                or not isinstance(data, dict)):
            raise ConflictError("批量输出身份已变化", extra={"code": "chapter_claim_changed"})
        if event in {"delta", "continue"} and job.active_claim is None:
            raise ConflictError("批量正文缺少当前章节占用", extra={"code": "chapter_claim_changed"})
        await ai_dispatch.require_visibility_stamp(db, job.visibility_stamp)
        if job.active_claim is not None:
            active, attempt = job.active_chapter_job, job.active_attempt
            if attempt is None or (active is None and event != "stage"
                                   and not (event == "flush" and data.get("text") == job.verified_text == "")):
                # 构建前只允许本 attempt 的阶段刷新及对应空正文落库；仍走下方原 guard，不允许任何正文。
                raise ConflictError("批量章节输出身份已变化", extra={"code": "chapter_claim_changed"})
            if active is not None and (active.chapter_claim != job.active_claim
                                       or active.batch_attempt != attempt or active.execution != execution
                                       or active.context_stamp is None):
                raise ConflictError("批量章节输出身份已变化", extra={"code": "chapter_claim_changed"})
            # 与受控任务流发布来源共用同一 guard（按全部绑定输入缓存；输入变化即重建）。
            guard = _attempt_guard(job, execution)
            await guard.require_task_write(db)
        else:
            if isinstance(task.checkpoint, dict) and task.checkpoint.get("current") is not None:
                raise ConflictError("批量章节输出占用已变化", extra={"code": "chapter_claim_changed"})
            await project_ai_config.revalidate_project_model(
                db, job.model_binding, cipher=job.model_cipher, allow_private=job.allow_private, for_update=True,
            )
            await project_ai_config.revalidate_shared_embedding(
                db, job.embedding_selection, cipher=job.model_cipher, allow_private=job.allow_private, for_update=True,
            )
        if event == "paused" and data.get("reason") == "rich_document_review":
            if job.pending_review_claim is None:
                raise ConflictError("批量提案身份无法验证", extra={"code": "rich_review_task_changed"})
            await batch_service.verify_batch_pending_event(db, task, job.pending_review_claim, data)
        elif event in {"done", "paused"}:
            if ((event == "done" and current_execution.step_cursor != job.total)
                    or not batch_service._same_json(
                        data, batch_service.summarize(job.persisted_checkpoint, job.persisted_cursor, job.total),
                    )):
                raise ConflictError("批量结果身份已变化", extra={"code": "chapter_claim_changed"})
        elif event == "reset":
            job.verified_text = ""
        elif event == "delta":
            job.verified_text += str(data.get("text") or "")
        elif event == "flush" and data.get("text") != job.verified_text:
            raise ConflictError("批量输出内容已变化", extra={"code": "chapter_claim_changed"})

    def stream_guard(
        self, job: batch_service.BatchJob, *, execution: TaskExecutionIdentity,
        current_execution: TaskExecutionIdentity, event: str, data: dict[str, Any],
    ) -> collaboration_ai_guard.SendGuard:
        """把本次事件对应的批量发布 guard 交给中央 ASGI 帧租约（E5-c2）；不重建本章已核验的 guard。

        - 有当前章：verify_event 刚用过的同一 attempt guard（绑定 attempt 与本章占用，章切换即换）；
        - 逐章分析中：绑定分析原执行与收据引用的 guard；
        - 无当前章：仅发布用 guard，原执行即本执行最近一次确认提交的断点。
        """
        if (type(job) is not batch_service.BatchJob or job.execution_identity != execution
                or job.task_execution_identity != current_execution
                or not same_execution(execution, current_execution)):
            raise _changed()
        if job.active_claim is not None:
            guard = _cached(job, "attempt", _attempt_inputs(job, execution))
            if guard is None:
                raise _changed()
            return guard
        if job.active_analysis is not None:
            return _analysis_guard(job, execution)
        return _between_guard(job, current_execution)

    async def verify_finalization(
        self, db: AsyncSession, task: Task, *, job: batch_service.BatchJob,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, status: str,
        result: dict[str, Any] | None, text: str,
    ) -> None:
        if status not in {"succeeded", "paused"} or not isinstance(result, dict) or text != job.verified_text:
            raise ConflictError("批量结果身份已变化", extra={"code": "chapter_claim_changed"})
        await self.verify_event(db, task, job=job, execution=execution, current_execution=current_execution,
                                event="done" if status == "succeeded" else "paused", data=result,
                                cancellation_operation=task.admin_cancel_operation_id)

    def progress(self, task: Task, text: str) -> int:
        job = self._jobs.get(self._identity(task.id, task.statistics_key, task.queued_at, task.execution_key))
        if job is None:
            return 1
        return batch_service.progress(job, count_words(text))

    def checkpoint_out(self, task: Task) -> dict[str, Any] | None:
        def public(value):
            if isinstance(value, dict):
                return {key: public(item) for key, item in value.items()
                        if key not in {"_chapter_claim", "_batch_completion", "_batch_attempt", "rich_analysis",
                                       "_batch_outcome_unknown"}}
            if isinstance(value, list):
                return [public(item) for item in value]
            return value

        return public(task.checkpoint)

    async def on_abort(
        self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity | None = None, recovery: bool = False,
    ) -> None:
        """运行时只清理原 attempt；进程排空后的恢复才读取持久化快照。"""
        try:
            job = getattr(task, "_batch_job", None)
            if job is not None:
                if (execution is None or not same_execution(execution, job.task_execution_identity)
                        or not matches_running_base(execution, task)):
                    raise ConflictError("批量清理执行身份已变化", extra={"code": "chapter_claim_changed"})
                claim = job.active_claim
            elif recovery:
                claim = batch_service.recovery_claim(task)
            else:
                current = task.checkpoint.get("current") if isinstance(task.checkpoint, dict) else None
                if current is not None:
                    raise ConflictError("批量章节清理缺少原执行凭据", extra={"code": "chapter_claim_changed"})
                claim = None
            if claim is not None:
                if (not isinstance(claim, chapter_claim.ChapterClaim) or claim.task_id != task.id
                        or claim.task_key != task.statistics_key or claim.actor_id != task.user_id
                        or claim.project_id != task.project_id or task.queued_at is None
                        or chapter_claim._utc(claim.queued_at) != chapter_claim._utc(task.queued_at)
                        or claim.execution_key != task.execution_key):
                    raise ConflictError("批量章节清理凭据已变化", extra={"code": "chapter_claim_changed"})
                await batch_service.restore_current(ctx, claim)
                if job is not None:
                    await batch_service._sync_released(ctx, job, claim, cleanup=True)
                elif recovery and isinstance(task.checkpoint, dict):
                    checkpoint = copy.deepcopy(task.checkpoint)
                    checkpoint["current"] = None
                    task.checkpoint = checkpoint
            elif not recovery:
                await batch_service.require_idle_snapshot(ctx, task, job)
        except Exception:  # noqa: BLE001
            logger.error("任务回退当前章节状态失败 task=%s", task.id)
            raise


register(BatchHandler())
