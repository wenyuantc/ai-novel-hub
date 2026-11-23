"""把一次真实工具请求绑定到原应用登记；与提案状态在同一事务登记、收尾。"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentChange, AgentConversation, Task, TaskWorker, TaskWorkRecord
from app.services.task_engine import workers

logger = logging.getLogger(__name__)


async def drain(coro):
    """异常处理期间等独立收尾真正退出；调用方随后报告原错误或取消。"""
    task = asyncio.create_task(coro)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


@dataclass(frozen=True, slots=True)
class ExternalActivity:
    claim_id: str
    worker_key: str
    project_id: int
    task_id: int
    task_key: str
    queued_at: datetime
    execution_key: str | None
    change_key: str

    def conditions(self):
        return (
            TaskWorkRecord.work_key == self.claim_id, TaskWorkRecord.kind == "external",
            TaskWorkRecord.worker_key == self.worker_key, TaskWorkRecord.project_id == self.project_id,
            TaskWorkRecord.task_id == self.task_id, TaskWorkRecord.task_key == self.task_key,
            TaskWorkRecord.queued_at == self.queued_at, TaskWorkRecord.execution_key == self.execution_key,
            TaskWorkRecord.operation_id == self.change_key,
        )

    def register(self, db: AsyncSession) -> None:
        db.add(TaskWorkRecord(
            work_key=self.claim_id, kind="external", state="active", worker_key=self.worker_key,
            project_id=self.project_id, task_id=self.task_id, task_key=self.task_key, queued_at=self.queued_at,
            execution_key=self.execution_key, operation_id=self.change_key,
        ))

    def active(self):
        return exists(select(TaskWorkRecord.work_key).where(*self.conditions(), TaskWorkRecord.state == "active"))

    async def finish(self, db: AsyncSession) -> None:
        await db.execute(delete(TaskWorkRecord).where(*self.conditions()))

    async def record_exit(self, factory) -> bool:
        """网络工作及业务事务均已退出，若写回失败只标记退出，由原 claim 的恢复流程判定结果。"""
        async with factory.begin() as db:
            if not await workers.lock_live_worker(db, self.worker_key):
                return False
            await db.execute(update(TaskWorkRecord).where(
                *self.conditions(), TaskWorkRecord.state == "active",
            ).values(state="exited"))
        return True

    async def confirm_exit(self, lease: workers.WorkerLease) -> None:
        delay = 0.1
        while lease.worker_key == self.worker_key and lease.valid:
            try:
                if await self.record_exit(lease.engine.ctx.sessionmaker):
                    return
            except Exception:  # noqa: BLE001  未提交或丢回执都可按同一身份重试，不能因临时故障丢掉退出证明。
                if delay == 0.1:
                    logger.warning("外部调用退出尚未确认")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)

async def has_active_call(db: AsyncSession, project_id: int) -> bool:
    """删除方的等待提示；最终仍须重查未决提案及原业务证据。"""
    query = select(TaskWorkRecord.work_key).join(
        AgentChange, AgentChange.change_key == TaskWorkRecord.operation_id,
    ).join(TaskWorker, TaskWorker.worker_key == TaskWorkRecord.worker_key).join(
        Task, Task.id == TaskWorkRecord.task_id,
    ).join(AgentConversation, AgentConversation.id == AgentChange.conversation_id).where(
        TaskWorkRecord.kind == "external", TaskWorkRecord.state == "active",
        TaskWorkRecord.project_id == project_id, TaskWorker.stopped_at.is_(None), TaskWorker.fenced_at.is_(None),
        AgentChange.project_id == project_id, AgentChange.kind == "external", AgentChange.status == "executing",
        AgentChange.external_worker_key == TaskWorkRecord.worker_key,
        AgentChange.turn_task_id == TaskWorkRecord.task_id,
        AgentChange.turn_task_key == TaskWorkRecord.task_key, AgentConversation.project_id == project_id,
        AgentConversation.user_id == Task.user_id, Task.project_id == project_id, Task.kind == "agent.turn",
        Task.statistics_key == TaskWorkRecord.task_key, Task.queued_at == TaskWorkRecord.queued_at,
        Task.execution_key.is_not_distinct_from(TaskWorkRecord.execution_key),
        AgentChange.result["_external_claim_id"].as_string() == TaskWorkRecord.work_key,
        AgentChange.result["_external_task_key"].as_string() == TaskWorkRecord.task_key,
    )
    return bool(await db.scalar(select(query.exists())))
