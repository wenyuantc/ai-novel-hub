"""consistency.check：一致性检查（包装 consistency.check 服务）；consistency.impact：修改影响评估（impact_task）。

两个种类共用同一套受理、核验与发布骨架（ImpactHandler 继承 ConsistencyHandler），并共用 resource_key 互斥。

作者与 accepted editor 共用，模板同大纲任务（E4-g3a）：受理走锁外预检 + 中央受控受理；协作者任务按原 grant 在
受理、领取（引擎复核）、构建前、每次出站（for_task 发送 lease）、每次落库事务、每个事件发布与终态写入前重验。

与草稿任务的区别：台账与问题在各自的事务内（锁内重验后）提交，任务取消 / 失败 / 撤权后这些已保存的数据保留，
没有需要回退或“待证明”的状态；Task 自身只保存进度日志与概要。运行期间不改写 Task 的 step_cursor / checkpoint
（发送守卫要求它们与领取时逐字一致），进度记录在 consistency_runs，续跑依靠台账新鲜度。
"""

import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.services import project_ai_config
from app.services.collaboration_ai_guard import CollaborationSendBlocked, SendGuard
from app.services.consistency import check, impact_task
from app.services.task_engine.execution_identity import TaskExecutionIdentity, matches_running
from app.services.task_engine.registry import Event, TaskContext, TaskSpec, register


def _same_json(first: Any, second: Any) -> bool:
    try:
        return json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(
            second, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return False


class ConsistencyHandler:
    kinds = (check.KIND,)
    # 本处理器的执行对象类型：须带 task_id / execution / guard / log / result / progress
    job_type: type = check.CheckJob
    execution_identity_aware = True
    admission_preflight_aware = True
    collaboration_execution_ready = True

    def __init__(self) -> None:
        # 运行中任务的执行对象（progress 只拿得到 task 与文本，借此读当前进度）
        self._jobs: dict[tuple[Any, ...], Any] = {}

    @staticmethod
    def _key(task_id: int, task_key: str | None, execution_key: str | None) -> tuple[Any, ...]:
        return task_id, task_key, execution_key

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any],
        admission: project_ai_config.ProjectModelPreflight,
        accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        project_id, title, task_input = await check.validate_start(
            db, ctx, user_id=user_id, params=params, admission=admission, accepted_model=accepted_model,
        )
        return self._spec(project_id, title, task_input)

    async def validate_retry(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task,
        admission: project_ai_config.ProjectModelPreflight,
        accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        """重试：沿用原范围与原模型绑定（含原邀请轮次）；仍新鲜的台账在运行时直接复用。"""
        project_id, title, task_input = await check.validate_retry(
            db, ctx, user_id=user_id, task=task, admission=admission, accepted_model=accepted_model,
        )
        return self._spec(project_id, title, task_input)

    @staticmethod
    def _spec(project_id: int, title: str, task_input: dict[str, Any]) -> TaskSpec:
        # 同作品同时只允许一个进行中的检查；只读正文，不与章节分析、批量生成互锁（无 serial_key）。
        return TaskSpec(
            project_id=project_id, chapter_id=None, resource_key=check.resource_key(project_id),
            title=title, input=task_input,
        )

    async def build_job(self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity) -> check.CheckJob:
        job = await check.build_check_job(ctx, task, execution=execution)
        # 仅本执行体的 detached Task 保留内存对象，供 on_abort 写回已完成的进度统计。
        task._consistency_job = job
        return job

    def _events(self, job: Any, ctx: TaskContext) -> AsyncIterator[Event]:
        return check.run_check_job(job, ctx.sessionmaker)

    async def run(self, job: Any, ctx: TaskContext) -> AsyncIterator[Event]:
        key = self._key(job.task_id, job.execution.task_key, job.execution.execution_key)
        self._jobs[key] = job
        try:
            async with aclosing(self._events(job, ctx)) as events:
                async for event in events:
                    yield event
        finally:
            if self._jobs.get(key) is job:
                self._jobs.pop(key, None)

    def _bound(self, job: Any, execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity) -> SendGuard:
        """领取时构造的同一 guard：执行身份在运行期间不推进，发布身份须与原执行逐字相等。"""
        if (type(job) is not self.job_type or type(job.guard) is not SendGuard or job.guard._task is not execution
                or job.execution is not execution or current_execution != execution):
            raise CollaborationSendBlocked()
        return job.guard

    async def verify_event(
        self, db: AsyncSession, task: Task, *, job: Any,
        execution: TaskExecutionIdentity, current_execution: TaskExecutionIdentity, event: str, data: dict[str, Any],
    ) -> None:
        guard = self._bound(job, execution, current_execution)
        if not matches_running(execution, task) or not isinstance(data, dict):
            raise CollaborationSendBlocked()
        await guard.require_task_write(db)
        # 输出只能是本执行产出的进度日志与概要：模型原文不进入任务输出。
        if event in ("delta", "flush"):
            text = data.get("text")
            if type(text) is not str or (event == "flush" and text != job.log) \
                    or (event == "delta" and not job.log.endswith(text)):
                raise CollaborationSendBlocked()
        elif event == "done" and (job.result is None or not _same_json(data, job.result)):
            raise CollaborationSendBlocked()

    def stream_guard(
        self, job: Any, *, execution: TaskExecutionIdentity,
        current_execution: TaskExecutionIdentity, event: str, data: dict[str, Any],
    ) -> SendGuard:
        """把领取时构造、本次事件已用其核验的原 guard 交给中央 ASGI 帧租约。"""
        return self._bound(job, execution, current_execution)

    async def verify_finalization(
        self, db: AsyncSession, task: Task, *, execution: TaskExecutionIdentity,
        current_execution: TaskExecutionIdentity, status: str, result: dict[str, Any] | None,
        job: Any | None, text: str,
    ) -> None:
        if job is None or status != "succeeded" or not isinstance(result, dict):
            raise CollaborationSendBlocked()
        guard = self._bound(job, execution, current_execution)
        if (not matches_running(execution, task) or job.result is None or not _same_json(result, job.result)
                or text != job.log):
            raise CollaborationSendBlocked()
        await guard.require_task_write(db)

    def progress(self, task: Task, text: str) -> int:
        job = self._jobs.get(self._key(task.id, task.statistics_key, task.execution_key))
        return min(95, job.progress) if job is not None else 1

    async def on_abort(
        self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity | None = None,
    ) -> None:
        """不占用章节状态，没有需要还原的业务数据；只把仍在运行的检查记录置为终态（幂等）。

        已保存的台账与问题保留。排队中被取消的任务尚无运行记录；进程重启后的恢复没有内存对象，记为中断。
        """
        job = getattr(task, "_consistency_job", None)
        if type(job) is check.CheckJob and job.task_id == task.id:
            status = "cancelled" if job.cancelled else "failed"
            await check.close_run(ctx.sessionmaker, task, status, check.run_stats(job),
                                  worker_key=job.execution.worker_key)
        else:
            owner = execution.worker_key if execution is not None else task.worker_key
            await check.close_run(ctx.sessionmaker, task, "interrupted", worker_key=owner)


class ImpactHandler(ConsistencyHandler):
    """consistency.impact：单章修改影响评估。没有运行记录；台账与影响记录在各自事务内锁内重验后提交。"""

    kinds = (impact_task.KIND,)
    job_type = impact_task.ImpactJob

    async def validate_and_claim(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, kind: str, params: dict[str, Any],
        admission: project_ai_config.ProjectModelPreflight,
        accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        project_id, title, task_input = await impact_task.validate_start(
            db, ctx, user_id=user_id, params=params, admission=admission, accepted_model=accepted_model,
        )
        return self._spec(project_id, title, task_input)

    async def validate_retry(
        self, db: AsyncSession, ctx: TaskContext, *, user_id: int, task: Task,
        admission: project_ai_config.ProjectModelPreflight,
        accepted_model: project_ai_config.ResolvedProjectModel,
    ) -> TaskSpec:
        project_id, title, task_input = await impact_task.validate_retry(
            db, ctx, user_id=user_id, task=task, admission=admission, accepted_model=accepted_model,
        )
        return self._spec(project_id, title, task_input)

    async def build_job(
        self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity,
    ) -> impact_task.ImpactJob:
        return await impact_task.build_impact_job(ctx, task, execution=execution)

    def _events(self, job: Any, ctx: TaskContext) -> AsyncIterator[Event]:
        return impact_task.run_impact_job(job, ctx.sessionmaker)

    async def on_abort(
        self, ctx: TaskContext, task: Task, *, execution: TaskExecutionIdentity | None = None,
    ) -> None:
        """不占用章节状态、没有运行记录：无需还原任何数据；已保存的台账（含上一版）保留，重试时直接比较。"""


register(ConsistencyHandler())
register(ImpactHandler())
