"""kb.index：知识库 / 剧情记忆建索引（包装 kb_index_service）。

同项目串行（serial_key = index:{project_id}），不写生成记录。
"""

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.services import kb_index_service
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register


class KbIndexHandler:
    kinds = (kb_index_service.KIND,)

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any]
    ) -> TaskSpec:
        spec = await kb_index_service.validate_index_task(db, user_id=user_id, params=params)
        return TaskSpec(
            project_id=spec["project_id"],
            chapter_id=None,
            resource_key=spec["resource_key"],
            serial_key=kb_index_service.serial_key(spec["project_id"]),
            title=spec["title"],
            input=spec["input"],
        )

    async def build_job(self, ctx: TaskContext, task: Task) -> kb_index_service.IndexJob:
        async with ctx.sessionmaker() as db:
            return await kb_index_service.build_index_job(
                db,
                task=task,
                cipher=ctx.cipher,
                factory=ctx.factory,
                ai_timeout=ctx.settings.ai_request_timeout,
                allow_private=ctx.settings.allow_private_ai_endpoints,
            )

    def run(self, job: kb_index_service.IndexJob, ctx: TaskContext) -> AsyncIterator[Event]:
        return kb_index_service.run_index_job(job, ctx.sessionmaker)

    def progress(self, task: Task, text: str) -> int:
        return kb_index_service.progress(text)

    async def on_abort(self, ctx: TaskContext, task: Task) -> None:
        await kb_index_service.on_abort(ctx.sessionmaker, task)


register(KbIndexHandler())
