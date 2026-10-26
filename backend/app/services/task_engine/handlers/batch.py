"""chapter.batch：批量顺序生成（包装 batch_service）。

一个任务内逐章生成 + 分析；断点存 step_cursor / checkpoint。审核点产出 paused（任务转为待审核），
继续由 task_service.resume_task 触发（revalidate_resume），重试沿用原范围与断点（validate_retry）。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.schemas.batch import BatchGenerateRequest
from app.services import batch_service
from app.services.chapter_service import count_words
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register

logger = logging.getLogger(__name__)


class BatchHandler:
    kinds = (batch_service.KIND,)

    def __init__(self) -> None:
        # 运行中任务的执行对象（progress 只拿得到 task 与文本，借此读当前章下标与目标字数）
        self._jobs: dict[int, batch_service.BatchJob] = {}

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any]
    ) -> TaskSpec:
        project_id = int(params["project_id"])
        req = BatchGenerateRequest.model_validate(params)
        fields = await batch_service.validate_start(db, ctx, user_id=user_id, project_id=project_id, req=req)
        return TaskSpec(
            project_id=fields["project_id"],
            chapter_id=None,
            resource_key=batch_service.resource_key(fields["project_id"]),
            title=fields["title"],
            input=fields["input"],
        )

    async def validate_retry(self, db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task) -> TaskSpec:
        """重试：沿用原范围（chapter_ids）与断点，不重新求值范围，也不要求还有未写章节。"""
        ai_config_id = await batch_service.revalidate(db, ctx, task)
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

    async def build_job(self, ctx: TaskContext, task: Task) -> batch_service.BatchJob:
        return await batch_service.build_batch_job(ctx, task)

    async def run(self, job: batch_service.BatchJob, ctx: TaskContext) -> AsyncIterator[Event]:
        self._jobs[job.task_id] = job
        try:
            async with aclosing(batch_service.run_batch_job(job, ctx)) as events:
                async for event in events:
                    yield event
        finally:
            self._jobs.pop(job.task_id, None)

    def progress(self, task: Task, text: str) -> int:
        job = self._jobs.get(task.id)
        if job is None:
            return 1
        return batch_service.progress(job, count_words(text))

    async def on_abort(self, ctx: TaskContext, task: Task) -> None:
        """失败 / 取消 / 中断：断点中的当前章仍处于 generating 时还原（已完成的章节保留）。"""
        try:
            await batch_service.restore_current(ctx, task.id)
        except Exception:  # noqa: BLE001
            logger.exception("任务 %s 回退当前章节状态失败", task.id)


register(BatchHandler())
