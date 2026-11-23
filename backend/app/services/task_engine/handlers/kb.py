"""kb.index：知识库 / 剧情记忆建索引（包装 kb_index_service）。

同项目串行（serial_key = index:{project_id}），不写生成记录。
"""

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Task
from app.services import kb_index_service
from app.services.collaboration_task_access import task_grant_identity
from app.services.task_engine.execution_identity import TaskExecutionIdentity, matches_running, same_execution
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register


class KbIndexHandler:
    kinds = (kb_index_service.KIND,)
    execution_identity_aware = True
    # 协作受理不依赖文本模型（E6-a）：handler 在锁内写入原邀请轮次与作者向量选择，中央受理按该轮次锁作品。
    shared_embedding_admission = True
    # E6-b：文档/重建（及作者旧记忆）的每批快照 → 受控发送 → 锁内重验 + 快照 CAS 写已接入，
    # 引擎可领取协作者任务；E6-c 起已授权编辑者的上传/重新索引/重建/记忆条目入口开放，订阅仍 404
    # （无 stream_guard，裁决 5）。
    collaboration_execution_ready = True

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any]
    ) -> TaskSpec:
        spec = await kb_index_service.validate_index_task(
            db, user_id=user_id, params=params, cipher=ctx.cipher,
            allow_private=ctx.settings.allow_private_ai_endpoints,
        )
        return TaskSpec(
            project_id=spec["project_id"],
            chapter_id=None,
            resource_key=spec["resource_key"],
            serial_key=kb_index_service.serial_key(spec["project_id"]),
            title=spec["title"],
            input=spec["input"],
        )

    async def validate_retry(self, db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task) -> TaskSpec:
        """原固定来源任务不能退回旧默认模型路径并重复未知的外部调用。"""
        payload = task.input if isinstance(task.input, dict) else {}
        # 与 TaskOut.retry_blocked_reason 共用同一判定（E6-f 裁决 19），错误文案保持原样。
        reason = kb_index_service.retry_blocked_reason(payload)
        if reason == "legacy_index":
            # 旧无占用任务只做过系统清理，不复用其请求；作者须经「重新索引」走新受理建立带占用的新任务。
            raise ConflictError("旧版索引任务不能重试，请在知识库中重新索引该文档",
                                extra={"code": "kb_legacy_task_not_retryable"})
        if reason == "request_invalid":
            raise ConflictError("原索引任务信息无效，请重新发起")
        if reason is not None:
            # 分析派生 / 人工记忆条目绑定任务的来源不可重放；破损的协作绑定与协作者非文档 / 重建任务同样拒绝。
            raise ConflictError("该索引任务的原执行需先核对，请重新发起安全重建")
        grant = task_grant_identity(payload)
        request = payload["request"]
        spec = await self.validate_and_claim(db, ctx, user_id=user_id, kind=task.kind, params=dict(request))
        # 协作者任务（E6-c）只能在原邀请轮次内重试：新受理在锁内重读的轮次须与原任务一致，
        # 向量选择按当前作者授权与配置重新冻结（不沿用旧选择补签）。作者任务不得变成协作受理。
        if task_grant_identity(spec.input) != grant:
            raise ConflictError("任务执行轮次已变化，请刷新后重试", extra={"code": "task_round_changed"})
        return spec

    def retry_blocked_reason(self, task: Task) -> kb_index_service.RetryBlockedReason | None:
        """TaskOut 只读字段：与 validate_retry 同一判定（E6-f 裁决 19）。"""
        return kb_index_service.retry_blocked_reason(task.input)

    async def build_job(
        self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity,
    ) -> kb_index_service.IndexJob:
        if not matches_running(execution, task):
            raise kb_index_service._claim_changed()
        async with ctx.sessionmaker() as db:
            return await kb_index_service.build_index_job(
                db,
                task=task,
                cipher=ctx.cipher,
                factory=ctx.factory,
                ai_timeout=ctx.settings.ai_request_timeout,
                allow_private=ctx.settings.allow_private_ai_endpoints,
                execution=execution,
            )

    def run(self, job: kb_index_service.IndexJob, ctx: TaskContext) -> AsyncIterator[Event]:
        return kb_index_service.run_index_job(job, ctx.sessionmaker)

    def progress(self, task: Task, text: str) -> int:
        return kb_index_service.progress(text)

    async def verify_event(
        self, db: AsyncSession, task: Task, *, job: kb_index_service.IndexJob,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, event: str, data: dict,
    ) -> None:
        if not same_execution(execution, current_execution):
            raise kb_index_service._claim_changed()
        if job.bound_memory:
            await kb_index_service.require_bound_index_job(db, job)
        else:
            # 引擎已在本事务按原执行身份重验认证、授权轮次与 Task 行（_event_task）；此处补文档占用与向量配置。
            await kb_index_service.require_index_event(db, job, event=event)

    async def verify_finalization(
        self, db: AsyncSession, task: Task, *, job: kb_index_service.IndexJob | None,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, status: str, result: Any, text: str,
    ) -> None:
        if not same_execution(execution, current_execution) or job is None:
            raise kb_index_service._claim_changed()
        if job.bound_memory:
            await kb_index_service.require_bound_index_job(db, job)
        else:
            await kb_index_service.require_index_finalization(db, job)

    async def on_abort(
        self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity | None = None,
    ) -> None:
        if execution is not None and (
            task.id != execution.task_id or task.statistics_key != execution.task_key
            or task.user_id != execution.actor_id or task.project_id != execution.project_id
            or task.queued_at is None or kb_index_service._utc(task.queued_at) != execution.queued_at
            or task.execution_key != execution.execution_key or task.worker_key != execution.worker_key
        ):
            raise kb_index_service._claim_changed()
        await kb_index_service.on_abort(ctx.sessionmaker, task)


register(KbIndexHandler())
