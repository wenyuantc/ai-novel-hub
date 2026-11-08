"""引擎内的持久取消意图 drain：单进程 single-flight，独立于 HTTP 生存。"""

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.models import Task
from app.models.entities import OPEN_TASK_STATUSES
from app.services import task_service
from app.services.task_engine.registry import get_handler

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine

logger = logging.getLogger(__name__)


class AdminCancellationDrain:
    def __init__(self, engine: "TaskEngine"):
        self.engine = engine
        self.work: dict[tuple[str, str], asyncio.Task] = {}
        # 启动遗留running即使第一次abort失败，后续仍按原重启中断语义收尾。
        self.recovering: set[str] = set()
        # 防止旧项目级失败标记让兼容路径在已释放资源后重放管理员旧abort。
        self.completed: dict[str, datetime | None] = {}
        # 已产出done/paused但终态事务暂时失败的本进程知识；重启仍按既有interrupted规则。
        self.winners: dict[str, tuple[datetime | None, dict]] = {}

    def request(self, task_key: str, operation_id: str) -> None:
        identity = (task_key, operation_id)
        if identity in self.work:
            return
        work = asyncio.create_task(self._drain(task_key, operation_id), name=f"task-cancel-{task_key}")
        self.work[identity] = work
        work.add_done_callback(lambda t: self.work.pop(identity, None))

    async def scan(self) -> None:
        completed, winners = dict(self.completed), dict(self.winners)
        cached_keys = set(completed) | set(winners) | self.recovering
        async with self.engine.ctx.sessionmaker() as db:
            metadata = select(
                Task.statistics_key, Task.admin_cancel_operation_id, Task.queued_at, Task.status
            )
            rows = list((await db.execute(metadata.where(Task.admin_cancel_operation_id.is_not(None)))).all())
            # 全站保留量没有固定上限；缓存核验不能构造超出数据库绑定参数上限的IN，
            # 更不能因此阻断持久取消意图的核心扫描。
            cached_keys = list(cached_keys - {key for key, _, _, _ in rows})
            for offset in range(0, len(cached_keys), 500):
                rows.extend((await db.execute(metadata.where(
                    Task.statistics_key.in_(cached_keys[offset:offset + 500])
                ))).all())
        facts = {key: (queued_at.replace(tzinfo=None) if queued_at is not None else None, status)
                 for key, _, queued_at, status in rows}
        # 缓存跟随现存Task的当前轮次，删除/裁剪/恢复入队后不无限保留结果或UUID。
        for key, round_at in completed.items():
            if (key not in facts or facts[key][0] != round_at) and self.completed.get(key) == round_at:
                self.completed.pop(key, None)
        for key, winner in winners.items():
            if (key not in facts or facts[key][0] != winner[0] or facts[key][1] != "running") and (
                self.winners.get(key) is winner
            ):
                self.winners.pop(key, None)
        self.recovering.intersection_update(key for key, (_, status) in facts.items() if status == "running")
        for key, operation_id, _, _ in rows:
            if key is not None and operation_id is not None:
                self.request(key, operation_id)

    @staticmethod
    def round_at(task: Task) -> datetime | None:
        return task.queued_at.replace(tzinfo=None) if task.queued_at is not None else None

    def remember_completed(self, task: Task) -> None:
        if task.statistics_key is not None:
            self.completed[task.statistics_key] = self.round_at(task)

    def completed_round(self, task: Task) -> bool:
        return task.statistics_key in self.completed and self.completed[task.statistics_key] == self.round_at(task)

    async def wait(self) -> None:
        while self.work:
            await asyncio.gather(*list(self.work.values()), return_exceptions=True)

    async def _load(self, task_key: str, operation_id: str) -> Task | None:
        async with self.engine.ctx.sessionmaker() as db:
            return await db.scalar(select(Task).where(
                Task.statistics_key == task_key, Task.admin_cancel_operation_id == operation_id
            ))

    async def _drain(self, task_key: str, operation_id: str) -> None:
        engine = self.engine
        try:
            task = await self._load(task_key, operation_id)
            if task is None:
                return
            with engine.cleanup_scope(task):
                if task.status not in OPEN_TASK_STATUSES:
                    # 非正常遗留组合不能盲abort succeeded，更不能擦除恢复依据。
                    logger.error("任务取消标记与终态不一致 task_key=%s operation=%s", task_key, operation_id)
                    return
                execution = engine._running.get(task.id)
                if task.status == "running" and execution is not None and not execution.done():
                    if engine._running_statistics_keys.get(task.id) != task_key:
                        return
                    await engine.cancel_running(task.id, statistics_key=task_key)
                    # 等待窗口结束不代表完成；只要执行体仍在，绝不由外部重复abort。
                    if not execution.done():
                        return
                    task = await self._load(task_key, operation_id)
                    if task is None or task.status != "running":
                        return
                winner = self.winners.get(task_key)
                round_at = task.queued_at.replace(tzinfo=None) if task.queued_at is not None else None
                if task.status == "running" and winner is not None and winner[0] == round_at:
                    row, committed = await engine.complete_state(
                        task, expected_status="running", abort_ok=True, operation_id=operation_id, **winner[1]
                    )
                    if committed:
                        self.winners.pop(task_key, None)
                        if row.status == "succeeded":
                            await engine._on_success(get_handler(task.kind), task)
                        await self._after_completion(row)
                    return
                # queued保留资源索引和领取屏障；paused不能回滚后来任务的资源。
                if task.status == "paused" and task.kind == "chapter.batch":
                    if (task.checkpoint or {}).get("current"):
                        logger.error("暂停任务断点资源不安全 task_key=%s operation=%s", task_key, operation_id)
                        return
                released_review = ((task.checkpoint or {}).get("rich_review") or {}).get("phase") == "awaiting_review"
                if (task.status == "paused" and not released_review
                        and task.kind not in ("chapter.batch", "book.import", "agent.turn")):
                    logger.error("未知暂停任务禁止资源回滚 task_key=%s operation=%s", task_key, operation_id)
                    return
                # 未知kind不能猜测如何安全释放资源，保留pending供后续修复。
                handler = get_handler(task.kind)
                if not released_review:
                    await handler.on_abort(engine.ctx, task)
                interrupted = task.status == "running" and task_key in self.recovering and not released_review
                status = "interrupted" if interrupted else "cancelled"
                error = "服务重启，任务已中断" if interrupted else "任务已取消"
                row, committed = await engine.complete_state(
                    task, expected_status=task.status, status=status, result=None, error=error,
                    text=task.output_text, progress=task.progress, abort_ok=True, operation_id=operation_id,
                )
                if committed:
                    self.recovering.discard(task_key)
                    await self._after_completion(row)
        except Exception:
            # 不输出SQL参数、私有插件异常或上游错误；marker是唯一重试依据。
            logger.error("管理员取消安全收尾未完成 task_key=%s operation=%s", task_key, operation_id)

    async def _after_completion(self, row: Task) -> None:
        """真实提交后恢复正常历史裁剪和所有者事件；不再进行任何旧资源abort。"""
        engine = self.engine
        out = task_service.out_json(row)
        event = "done" if row.status == "succeeded" else "paused" if row.status == "paused" else "error"
        payload = {"task": out, "result": row.result} if event != "error" else {"task": out, "detail": row.error}
        engine.close_hub(row.id, event, payload, statistics_key=row.statistics_key)
        engine.notify()
        async with engine.ctx.sessionmaker() as db:
            await task_service.prune_finished(db, row.user_id)
