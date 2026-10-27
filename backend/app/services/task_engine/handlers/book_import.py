"""book.import：拆书反推（包装 book_import.reverse）。

多步任务：断点存 step_cursor / checkpoint。AI 调用重试后仍失败时产出 paused（pause_reason = failed），
继续由 task_service.resume_task 触发（revalidate_resume），跳过由 book_import.reverse.skip_paused 触发，
重试沿用原断点（validate_retry）。已写入的数据在失败 / 取消 / 中断时全部保留，on_abort 不需要回退。
"""

from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.services.book_import import reverse
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register


class BookImportHandler:
    kinds = (reverse.KIND,)

    def __init__(self) -> None:
        # 运行中任务的执行对象（progress 只拿得到 task 与文本，借此读当前阶段与批次）
        self._jobs: dict[int, reverse.ImportJob] = {}

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any]
    ) -> TaskSpec:
        fields = await reverse.validate_start(db, ctx, user_id=user_id, params=params)
        return TaskSpec(
            project_id=fields["project_id"],
            chapter_id=None,
            resource_key=reverse.resource_key(user_id),
            title=fields["title"],
            input=fields["input"],
        )

    async def validate_retry(self, db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task) -> TaskSpec:
        """重试：沿用原请求与断点（已完成的批次不再调用 AI）。"""
        ai_config_id = await reverse.revalidate(db, ctx, task)
        return TaskSpec(
            project_id=task.project_id,
            chapter_id=None,
            resource_key=reverse.resource_key(user_id),
            title=task.title,
            input={**task.input, "ai_config_id": ai_config_id},
        )

    async def revalidate_resume(self, db: AsyncSession, ctx: TaskContext, task: Task) -> None:
        await reverse.revalidate(db, ctx, task)

    def finish_result(self, task: Task) -> dict[str, Any]:
        return reverse.finish_result(task)

    def checkpoint_out(self, task: Task) -> dict[str, Any] | None:
        """TaskOut.checkpoint 只给摘要（findings / final 体积大，前端用不到）。"""
        return reverse.summarize(task.checkpoint) if task.checkpoint else None

    async def build_job(self, ctx: TaskContext, task: Task) -> reverse.ImportJob:
        return await reverse.build_import_job(ctx, task)

    async def run(self, job: reverse.ImportJob, ctx: TaskContext) -> AsyncIterator[Event]:
        self._jobs[job.task_id] = job
        try:
            async with aclosing(reverse.run_import_job(job, ctx)) as events:
                async for event in events:
                    yield event
        finally:
            self._jobs.pop(job.task_id, None)

    def progress(self, task: Task, text: str) -> int:
        job = self._jobs.get(task.id)
        return reverse.progress(job) if job is not None else 1

    async def on_abort(self, ctx: TaskContext, task: Task) -> None:
        # 已写入的项目、章节与设定全部保留；断点在每一步结束时已提交
        return None


register(BookImportHandler())
