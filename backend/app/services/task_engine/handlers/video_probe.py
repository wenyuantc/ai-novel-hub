"""个人视频模型探针，外部任务和本地任务在同一事务受理。"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers import AIProviderError
from app.core.errors import AppError, BadRequestError, ConflictError
from app.models import Task
from app.models.ai_external_jobs import AiExternalJob
from app.services import ai_config_service
from app.services.task_engine.execution_identity import TaskExecutionIdentity, matches_running, same_execution
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register

KIND = "ai.video_probe"


def _job_id(payload: dict[str, Any]) -> str:
    ids = payload.get("external_job_ids")
    if not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str):
        raise ConflictError("视频任务缺少原始厂商任务记录")
    return ids[0]


class VideoProbeHandler:
    kinds = (KIND,)
    execution_identity_aware = True
    resume_after_interrupt = True
    abort_status_aware = True

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any]
    ) -> TaskSpec:
        from app.ai import external_jobs
        from app.ai.providers.video import probe_defaults

        cfg = await ai_config_service.resolve_config(db, user_id, params["config_id"], "video")
        try:
            options = probe_defaults(cfg.provider, cfg.model, with_image=bool(params["image_ids"]))
        except (AIProviderError, ValueError) as exc:
            raise BadRequestError(str(exc)) from exc
        request = {**options, "prompt": "清晨的阳光照过安静的花园，镜头缓缓向前。", "image_ids": params["image_ids"]}
        jid = await external_jobs.create_job(db, ctx, actor_id=user_id, config=cfg, request=request)
        return TaskSpec(None, None, f"ai.video_probe:{cfg.instance_key}", "视频测试生成 · " + cfg.name,
                        {"request": params, "external_job_ids": [jid], "config_instance_key": cfg.instance_key})

    async def validate_retry(self, db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task) -> TaskSpec:
        job = await db.scalar(select(AiExternalJob).where(
            AiExternalJob.id == _job_id(task.input), AiExternalJob.actor_id == user_id,
            AiExternalJob.project_id.is_(None), AiExternalJob.config_instance_key == task.input["config_instance_key"],
        ).with_for_update())
        if job is None or job.state not in {"created", "submitted", "succeeded"}:
            raise ConflictError("这次视频测试已结束或提交结果未知，请核对后重新测试")
        cfg = await ai_config_service.resolve_config(db, user_id, job.config_id, "video")
        if cfg.instance_key != job.config_instance_key:
            raise ConflictError("原视频模型配置已变更")
        return TaskSpec(None, None, task.resource_key, task.title, dict(task.input))

    async def build_job(
        self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity,
    ) -> TaskExecutionIdentity:
        if not matches_running(execution, task) or task.project_id is not None:
            raise ConflictError("视频任务执行身份已变化")
        _job_id(execution.input)
        return execution

    async def run(self, job: TaskExecutionIdentity, ctx: TaskContext) -> AsyncIterator[Event]:
        from app.ai import external_jobs

        messages: asyncio.Queue[Event | None] = asyncio.Queue()

        async def emit(stage: str, progress: int):
            messages.put_nowait(("stage", {"stage": stage, "progress": progress}))

        async def generate():
            try:
                return await external_jobs.drive(ctx, [_job_id(job.input)], execution=job, emit=emit)
            finally:
                messages.put_nowait(None)

        worker = asyncio.create_task(generate())
        try:
            while (message := await messages.get()) is not None:
                yield message
            try:
                results = await worker
            except AppError as exc:
                yield "error", {"detail": exc.detail}
            else:
                yield "done", {"result_object_id": results[0]}
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    def progress(self, task: Task, text: str) -> int:
        return task.progress

    async def verify_event(self, db, task, *, job, execution, current_execution, event, data):
        if not same_execution(job, current_execution) or not matches_running(current_execution, task):
            raise ConflictError("视频任务执行身份已变化")
        if event == "done":
            await self._require_result(db, job, data)

    async def verify_finalization(self, db, task, *, job, execution, current_execution, status, result, text):
        if job is None or not same_execution(job, current_execution) or not matches_running(current_execution, task):
            raise ConflictError("视频任务执行身份已变化")
        await self._require_result(db, job, result)

    async def _require_result(self, db, job, result):
        expected = await db.scalar(select(AiExternalJob.result_object_id).where(
            AiExternalJob.id == _job_id(job.input), AiExternalJob.state == "succeeded",
            AiExternalJob.actor_id == job.actor_id, AiExternalJob.project_id.is_(None),
        ))
        if not expected or result != {"result_object_id": expected}:
            raise ConflictError("视频结果尚未保存")

    async def on_abort(
        self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity | None = None,
        terminal_status: str | None = None,
    ):
        from app.ai import external_jobs

        # 崩溃接管与普通生成失败也可能进入清理回调。只有已受理的用户/管理员取消才通知厂商。
        if terminal_status != "cancelled" and not (
            terminal_status is None and (task.status == "queued" or task.cancel_requested_at is not None
                                         or task.admin_cancel_operation_id is not None)
        ):
            return
        await external_jobs.cancel_jobs(
            ctx, [_job_id(task.input)], actor_id=task.user_id, task=task, execution=execution,
        )


register(VideoProbeHandler())
