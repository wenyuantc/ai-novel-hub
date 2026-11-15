"""chapter.analyze：包装 analysis_service。同项目串行（serial_key = analysis:{project_id}），不写生成记录。"""

import json
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.schemas.analysis import AnalyzeRequest
from app.services import ai_dispatch, analysis_service, project_ai_config
from app.services.collaboration_ai_guard import SendGuard, publication_execution_matches
from app.services.task_engine.execution_identity import TaskExecutionIdentity, matches_running, same_execution
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register


class AnalysisHandler:
    kinds = ("chapter.analyze",)
    execution_identity_aware = True
    admission_preflight_aware = True
    # E4-g1：accepted editor 的手动分析与单章成功后的自动派生走带 grant 的受理/领取复核/build_job/发送 lease/
    # 写回链路（写回含设定回写与剧情记忆，均在持锁写事务内按原 grant 与模型来源重验）。
    # 分析派生的记忆向量索引不对协作者开放（E6），只保留关键词检索单元。
    collaboration_execution_ready = True

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any],
        admission: project_ai_config.ProjectModelPreflight,
        accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        if (type(accepted_model) is not project_ai_config.ResolvedProjectModel
                or accepted_model != project_ai_config.require_original_preflight(admission).binding
                or type(params.get("chapter_id")) is not int):
            raise analysis_service._admission_changed()
        req = AnalyzeRequest.model_validate(params)
        project_id = params.get("project_id")
        chapter, selection = await analysis_service.validate_analysis_task(
            db,
            user_id=user_id,
            chapter_id=params["chapter_id"],
            project_id=project_id if type(project_id) is int else None,
            req=req,
            model=accepted_model,
            cipher=ctx.cipher,
            allow_private=ctx.settings.allow_private_ai_endpoints,
        )
        return TaskSpec(
            project_id=chapter.project_id,
            chapter_id=chapter.id,
            resource_key=None,
            serial_key=analysis_service.serial_key(chapter.project_id),
            title=analysis_service.task_title(chapter),
            input={
                "request": {"chapter_id": chapter.id, **req.model_dump(mode="json")},
                "chapter_id": chapter.id,
                "ai_config_id": accepted_model.config_id,
                "_project_model": project_ai_config.serialize_project_model(accepted_model),
                "_shared_embedding": project_ai_config.serialize_shared_embedding(selection),
            },
        )

    async def build_job(
        self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity,
    ) -> analysis_service.AnalysisJob:
        if not matches_running(execution, task) or task.admin_cancel_operation_id is not None:
            raise analysis_service.AnalysisExecutionChanged()
        original = execution.input
        params = original.get("request")
        if (not isinstance(params, dict) or type(original.get("chapter_id")) is not int
                or original["chapter_id"] != execution.chapter_id
                or type(params.get("chapter_id")) is not int or params["chapter_id"] != execution.chapter_id):
            raise analysis_service.AnalysisExecutionChanged()
        bound = analysis_service.analysis_execution_from_claim(execution)
        pending = await analysis_service.begin_analysis_pending(ctx.sessionmaker, bound)
        try:
            async with ctx.sessionmaker() as db:
                return await analysis_service.build_analysis_job(
                    db,
                    user_id=bound.actor_id,
                    chapter_id=bound.task_chapter_id,
                    # 未指定 = 运行时的默认配置；指定的配置已删除时任务失败。
                    ai_config_id=params.get("ai_config_id"),
                    cipher=ctx.cipher,
                    factory=ctx.factory,
                    ai_timeout=ctx.settings.ai_request_timeout,
                    allow_private=ctx.settings.allow_private_ai_endpoints,
                    task_id=bound.task_id,
                    task_key=bound.task_key,
                    execution=bound,
                    pending=pending,
                    context_budget=ctx.settings.context_char_budget,
                )
        except BaseException:
            # 本构建路径尚无模型或向量调用，明确没有结果提交，才能清除此轮 pending。
            await ai_dispatch._wait_send_cleanup(analysis_service.discard_analysis_pending(ctx.sessionmaker, pending))
            raise

    def run(self, job: analysis_service.AnalysisJob, ctx: TaskContext) -> AsyncIterator[Event]:
        return analysis_service.run_analysis_job(job, ctx.sessionmaker)

    def progress(self, task: Task, text: str) -> int:
        return min(90, 10 + len(text) // 40)

    async def _verify_output(
        self, db: AsyncSession, job: analysis_service.AnalysisJob, execution: TaskExecutionIdentity,
        current_execution: TaskExecutionIdentity, *, result: dict[str, Any] | None,
    ) -> None:
        if (job.execution != analysis_service.analysis_execution_from_claim(execution)
                or job.task_execution_identity != current_execution or not same_execution(execution, current_execution)
                or job.send_guard is None or job.cipher is None or job.visibility_stamp is None):
            raise analysis_service.AnalysisExecutionChanged()
        if result is None and current_execution != execution:
            # pending 尚有效时，原模型、可见性和收据必须在发布前共同持锁核验。
            await job.send_guard.require_task_write(db)
            return
        await analysis_service._analysis_write_barrier(db, job.execution)
        await ai_dispatch.require_visibility_stamp(db, job.visibility_stamp)
        await analysis_service._lock_analysis_config_owners(db, job.execution)
        await project_ai_config.revalidate_project_model(
            db, job.binding, cipher=job.cipher, allow_private=job.allow_private,
        )
        await project_ai_config.revalidate_shared_embedding(
            db, job.execution.embedding_selection, cipher=job.cipher, allow_private=job.allow_private,
        )
        if result is not None:
            proof = await analysis_service.completed_analysis_receipt(db, job.execution)
            if (proof is None or job.pending is None or proof[0].get("nonce") != job.pending.nonce
                    or json.dumps(proof[1], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    != json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))):
                raise analysis_service.AnalysisExecutionChanged()

    async def verify_event(
        self, db: AsyncSession, task: Task, *, job: analysis_service.AnalysisJob,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, event: str, data: dict[str, Any],
    ) -> None:
        await self._verify_output(db, job, execution, current_execution, result=data if event == "done" else None)

    def stream_guard(
        self, job: analysis_service.AnalysisJob, *, execution: TaskExecutionIdentity,
        current_execution: TaskExecutionIdentity, event: str, data: dict[str, Any],
    ) -> SendGuard:
        """把领取时构造、本次事件已用其核验的原 guard 交给中央 ASGI 帧租约（E5-c1）。

        发布身份可随 pending / 完成收据推进，但只接受由该 guard 的原断点与 pending 文档派生的身份。
        """
        guard = job.send_guard if type(job) is analysis_service.AnalysisJob else None
        if (type(guard) is not SendGuard or guard._task != execution or guard._analysis_execution is None
                or job.execution != analysis_service.analysis_execution_from_claim(execution)
                or guard._analysis_execution != job.execution or guard._analysis_pending is not job.pending
                or job.task_execution_identity != current_execution
                or not same_execution(execution, current_execution)
                or not publication_execution_matches(guard, current_execution)):
            raise analysis_service.AnalysisExecutionChanged()
        return guard

    async def verify_finalization(
        self, db: AsyncSession, task: Task, *, job: analysis_service.AnalysisJob,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, status: str,
        result: dict[str, Any] | None, text: str,
    ) -> None:
        if status == "succeeded" and not isinstance(result, dict):
            raise analysis_service.AnalysisExecutionChanged()
        await self._verify_output(
            db, job, execution, current_execution, result=result if status == "succeeded" else None,
        )

    async def on_abort(self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity | None = None) -> None:
        # 分析只在成功时一次性落库，失败 / 取消没有需要还原的状态
        return None

    async def on_success(self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity) -> None:
        # 本章记忆已与分析同一事务写入（关键词即可检索）；有向量模型时排队生成向量
        await analysis_service.enqueue_memory_for_execution(
            ctx.sessionmaker, analysis_service.analysis_execution_from_claim(execution), engine=ctx.task_engine,
            cipher=ctx.cipher, allow_private=ctx.settings.allow_private_ai_endpoints,
        )


register(AnalysisHandler())
