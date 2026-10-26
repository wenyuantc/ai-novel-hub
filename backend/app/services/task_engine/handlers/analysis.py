"""chapter.analyze：包装 analysis_service。同项目串行（serial_key = analysis:{project_id}），不写生成记录。"""

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.schemas.analysis import AnalyzeRequest
from app.services import analysis_service
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register


class AnalysisHandler:
    kinds = ("chapter.analyze",)

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any]
    ) -> TaskSpec:
        req = AnalyzeRequest.model_validate(params)
        chapter, ai_config_id = await analysis_service.validate_analyze(
            db,
            user_id=user_id,
            chapter_id=int(params["chapter_id"]),
            ai_config_id=req.ai_config_id,
            cipher=ctx.cipher,
            factory=ctx.factory,
            ai_timeout=ctx.settings.ai_request_timeout,
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
                "ai_config_id": ai_config_id,
            },
        )

    async def build_job(self, ctx: TaskContext, task: Task) -> analysis_service.AnalysisJob:
        params = dict(task.input.get("request") or {})
        async with ctx.sessionmaker() as db:
            return await analysis_service.build_analysis_job(
                db,
                user_id=task.user_id,
                chapter_id=int(task.input["chapter_id"]),
                # 未指定 = 运行时的默认配置（自动分析也走这里；未配置可用模型时任务失败并给出提示）
                ai_config_id=params.get("ai_config_id"),
                cipher=ctx.cipher,
                factory=ctx.factory,
                ai_timeout=ctx.settings.ai_request_timeout,
                allow_private=ctx.settings.allow_private_ai_endpoints,
                task_id=task.id,
            )

    def run(self, job: analysis_service.AnalysisJob, ctx: TaskContext) -> AsyncIterator[Event]:
        return analysis_service.run_analysis_job(job, ctx.sessionmaker)

    def progress(self, task: Task, text: str) -> int:
        return min(90, 10 + len(text) // 40)

    async def on_abort(self, ctx: TaskContext, task: Task) -> None:
        # 分析只在成功时一次性落库，失败 / 取消没有需要还原的状态
        return None


register(AnalysisHandler())
