"""outline.continue / outline.expand：包装 outline_ai_service。只产出草稿，不写库。"""

from collections.abc import AsyncIterator
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.schemas.outline import OutlineContinueRequest, OutlineExpandRequest
from app.services import outline_ai_service
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register


class OutlineHandler:
    kinds = ("outline.continue", "outline.expand")

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any]
    ) -> TaskSpec:
        okind = cast(outline_ai_service.OutlineKind, kind)
        project_id = int(params["project_id"])
        ai_config_id, title, resource_key = await outline_ai_service.validate_outline_task(
            db,
            user_id=user_id,
            kind=okind,
            project_id=project_id,
            params=params,
            cipher=ctx.cipher,
            factory=ctx.factory,
            ai_timeout=ctx.settings.ai_request_timeout,
            allow_private=ctx.settings.allow_private_ai_endpoints,
        )
        if kind == "outline.expand":
            req: Any = OutlineExpandRequest.model_validate(params)
            request = {"project_id": project_id, "outline_id": int(params["outline_id"]), **req.model_dump(mode="json")}
        else:
            req = OutlineContinueRequest.model_validate(params)
            request = {"project_id": project_id, **req.model_dump(mode="json")}
        return TaskSpec(
            project_id=project_id,
            chapter_id=None,
            # 同项目同时只能有一个续写；同一条大纲同时只能有一个展开
            resource_key=resource_key,
            title=title,
            input={"request": request, "ai_config_id": ai_config_id},
        )

    async def build_job(self, ctx: TaskContext, task: Task) -> outline_ai_service.OutlineJob:
        params = dict(task.input["request"])
        async with ctx.sessionmaker() as db:
            return await outline_ai_service.build_outline_job(
                db,
                user_id=task.user_id,
                kind=cast(outline_ai_service.OutlineKind, task.kind),
                project_id=int(params["project_id"]),
                # 按原请求重新解析：未指定 = 运行时的默认配置
                params=params,
                cipher=ctx.cipher,
                factory=ctx.factory,
                ai_timeout=ctx.settings.ai_request_timeout,
                allow_private=ctx.settings.allow_private_ai_endpoints,
            )

    def run(self, job: outline_ai_service.OutlineJob, ctx: TaskContext) -> AsyncIterator[Event]:
        return outline_ai_service.run_outline_job(job, ctx.sessionmaker)

    def progress(self, task: Task, text: str) -> int:
        return min(90, 10 + len(text) // 40)

    async def on_abort(self, ctx: TaskContext, task: Task) -> None:
        # 只产出草稿，没有需要还原的状态
        return None


register(OutlineHandler())
