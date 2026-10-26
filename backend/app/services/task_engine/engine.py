"""TaskEngine：进程内调度协程 + 任务执行。

- tasks 表是唯一事实来源（数据库即队列）。调度器按 id 升序扫描 queued 任务，受全局 / 每用户并发上限约束，
  用条件 UPDATE（status='queued' → 'running'）领取，影响行数为 1 才算领到，然后 asyncio.create_task 执行。
- 前提：单进程部署（Dockerfile 中 uvicorn 不带 --workers）。start() 时的恢复逻辑会把所有 running 任务
  标记为 interrupted；多实例部署会误伤其他进程的任务，届时需改为 FOR UPDATE SKIP LOCKED + 心跳租约。
- serial_key：同键任务排队串行。调度时同键已有运行中（或本轮已登记）的任务就跳过，留到下一轮；
  同键内按 id 先进先出（前一个同键任务因上限被跳过时，后面的同键任务也不越过它）。与全局 / 每用户上限同时生效。
- on_success（可选）：任务写入 succeeded 后调用，异常只记日志（如章节任务成功后自动排队分析）。
- 取消运行中任务：对 asyncio.Task 调用一次 cancel()；CancelledError 传入 runner 的 finally，
  由其回退章节状态并写 cancelled 生成记录；引擎随后把任务置为 cancelled（关停时为 interrupted）。
"""

import asyncio
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from typing import Any

from sqlalchemy import func, select, update

import app.services.task_engine.handlers  # noqa: F401  导入即注册内置任务类型
from app.core.errors import AppError
from app.models import Task
from app.models.entities import ACTIVE_TASK_STATUSES
from app.services import task_service
from app.services.task_engine.hub import TaskHub
from app.services.task_engine.registry import Event, TaskContext, TaskHandler, get_handler

logger = logging.getLogger(__name__)

INTERRUPTED_MESSAGE = "服务重启，任务已中断"
CANCELLED_MESSAGE = "任务已取消"
TASK_GONE_MESSAGE = "任务不存在"
# SSE 心跳帧标记（app.api.sse 渲染为注释帧 ": ping"）
HEARTBEAT_EVENT = ""


class TaskEngine:
    # output_text / 进度节流刷库间隔（秒）
    flush_interval = 2.0
    # 订阅流空闲时的心跳间隔（秒）
    heartbeat_seconds = 15.0
    # 调度器兜底轮询间隔（秒）；正常由 notify() 唤醒
    poll_interval = 5.0
    # 取消运行中任务时等待其收尾的上限（秒）
    cancel_wait_seconds = 10.0

    def __init__(self, ctx: TaskContext):
        self.ctx = ctx
        self.hubs: dict[int, TaskHub] = {}
        self._running: dict[int, asyncio.Task[None]] = {}
        self._running_users: dict[int, int] = {}
        self._running_serial: dict[int, str] = {}
        self._cancel_requested: set[int] = set()
        self._shutdown: set[int] = set()
        self._finalizing: set[int] = set()
        self._wake: asyncio.Event | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._stopping = False

    # ---------------- 生命周期 ----------------

    @property
    def running(self) -> bool:
        return self._loop_task is not None

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        self._wake = asyncio.Event()
        self._stopping = False
        await self.recover()
        self._loop_task = asyncio.create_task(self._scheduler(), name="task-engine-scheduler")
        self.notify()

    async def stop(self) -> None:
        """停止调度；运行中的任务标记为关停中断后取消（→ interrupted），等待其收尾。"""
        loop_task, self._loop_task = self._loop_task, None
        if loop_task is not None:
            # 让调度器在两次领取之间退出，避免领取（已提交 running）后来不及启动执行
            self._stopping = True
            self.notify()
            try:
                await asyncio.wait_for(loop_task, timeout=5)
            except TimeoutError:
                logger.warning("任务调度器未能按时退出，强制取消")
                with suppress(asyncio.CancelledError):
                    await loop_task
        running = list(self._running.items())
        for task_id, t in running:
            self._shutdown.add(task_id)
            if task_id not in self._cancel_requested and task_id not in self._finalizing:
                t.cancel()
        if running:
            await asyncio.gather(*(t for _, t in running), return_exceptions=True)
        self._shutdown.clear()

    def notify(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def recover(self) -> None:
        """把上次进程遗留的 running 任务置为 interrupted，并还原其占用的资源；queued 任务保持不变。"""
        sm = self.ctx.sessionmaker
        async with sm() as db:
            stale = list(await db.scalars(select(Task).where(Task.status == "running").order_by(Task.id)))
        for task in stale:
            if task.id in self._running:
                continue
            handler = _try_handler(task.kind)
            if handler is not None:
                await handler.on_abort(self.ctx, task)
            async with sm() as db:
                await db.execute(
                    update(Task)
                    .where(Task.id == task.id, Task.status == "running")
                    .values(
                        status="interrupted",
                        error=INTERRUPTED_MESSAGE,
                        stage=task_service.STAGE_LABELS["interrupted"],
                        finished_at=func.now(),
                    )
                )
                await db.commit()
                row = await db.get(Task, task.id)
                out = task_service.out_json(row) if row is not None else None
            hub = self.hubs.pop(task.id, None)
            if hub is not None:
                hub.close("error", {"task": out, "detail": INTERRUPTED_MESSAGE})

    # ---------------- hub ----------------

    def ensure_hub(
        self, task_id: int, *, status: str = "queued", text: str = "", progress: int = 0, stage: str = ""
    ) -> tuple[TaskHub, bool]:
        """返回 (hub, created)。"""
        hub = self.hubs.get(task_id)
        if hub is not None:
            return hub, False
        hub = TaskHub(task_id, status=status, text=text, progress=progress, stage=stage)
        self.hubs[task_id] = hub
        return hub, True

    def close_hub(self, task_id: int, event: str, data: dict[str, Any]) -> None:
        hub = self.hubs.pop(task_id, None)
        if hub is not None:
            hub.close(event, data)

    # ---------------- 调度 ----------------

    async def _scheduler(self) -> None:
        assert self._wake is not None
        while not self._stopping:
            with suppress(TimeoutError):
                async with asyncio.timeout(self.poll_interval):
                    await self._wake.wait()
            self._wake.clear()
            if self._stopping:
                return
            try:
                await self._dispatch()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("任务调度失败")

    async def _dispatch(self) -> None:
        settings = self.ctx.settings
        sm = self.ctx.sessionmaker
        async with sm() as db:
            rows = await db.execute(
                select(Task.id, Task.user_id, Task.serial_key).where(Task.status == "queued").order_by(Task.id)
            )
            queued = list(rows.all())
        per_user = Counter(self._running_users.values())
        # 运行中占用的串行键；本轮被跳过的串行键也加入，保证同键内先进先出
        busy_serial = set(self._running_serial.values())
        for task_id, user_id, serial_key in queued:
            if self._stopping or len(self._running) >= settings.task_max_concurrency:
                break
            if task_id in self._running:
                continue
            if serial_key is not None and serial_key in busy_serial:
                continue
            if per_user[user_id] >= settings.task_max_per_user:
                if serial_key is not None:
                    busy_serial.add(serial_key)
                continue
            per_user[user_id] += 1
            # 先登记 asyncio.Task 再领取：库里出现 running 时内存里一定有可取消的执行体
            self._running_users[task_id] = user_id
            if serial_key is not None:
                busy_serial.add(serial_key)
                self._running_serial[task_id] = serial_key
            t = asyncio.create_task(self._claim_and_run(task_id), name=f"task-{task_id}")
            self._running[task_id] = t
            t.add_done_callback(lambda _t, tid=task_id: self._on_done(tid))
        await self._broadcast_queue_positions()

    async def _broadcast_queue_positions(self) -> None:
        if not any(h.status == "queued" for h in self.hubs.values()):
            return
        async with self.ctx.sessionmaker() as db:
            ids = list(await db.scalars(select(Task.id).where(Task.status == "queued").order_by(Task.id)))
        # 已登记、正在领取的任务不算排队
        ids = [i for i in ids if i not in self._running]
        for position, task_id in enumerate(ids):
            hub = self.hubs.get(task_id)
            if hub is not None and hub.status == "queued":
                hub.set_queue_position(position)

    def _on_done(self, task_id: int) -> None:
        self._running.pop(task_id, None)
        self._running_users.pop(task_id, None)
        self._running_serial.pop(task_id, None)
        self._cancel_requested.discard(task_id)
        self.notify()

    # ---------------- 执行 ----------------

    async def _claim_and_run(self, task_id: int) -> None:
        """条件 UPDATE 领取（queued → running），影响行数为 1 才执行；否则（已被取消）静默退出。"""
        if task_id in self._cancel_requested:
            return
        running_stage = task_service.STAGE_LABELS["running"]
        async with self.ctx.sessionmaker() as db:
            result = await db.execute(
                update(Task)
                .where(Task.id == task_id, Task.status == "queued")
                .values(status="running", started_at=func.now(), stage=running_stage)
            )
            await db.commit()
        if result.rowcount != 1:
            return
        hub, _ = self.ensure_hub(task_id, status="running")
        hub.set_queue_position(None)
        hub.set_progress(status="running", stage=running_stage)
        await self._run(task_id, hub)

    async def _run(self, task_id: int, hub: TaskHub) -> None:
        status, error, result = "failed", "", None
        task: Task | None = None
        handler: TaskHandler | None = None
        try:
            async with self.ctx.sessionmaker() as db:
                task = await db.get(Task, task_id)
            if task is None:
                # 任务行已随用户 / 项目删除
                self.close_hub(task_id, "error", {"task": None, "detail": TASK_GONE_MESSAGE})
                return
            handler = get_handler(task.kind)
            if task_id in self._cancel_requested:
                # 领取与取消请求几乎同时发生：不再执行
                status, error = "cancelled", CANCELLED_MESSAGE
            else:
                try:
                    job = await handler.build_job(self.ctx, task)
                except AppError as exc:
                    error = exc.detail
                else:
                    error = await self._consume(task, handler, hub, job)
                    if not error:
                        status, result = "succeeded", hub.result
        except asyncio.CancelledError:
            # 只会被 cancel() 一次；吞掉取消让收尾正常执行（runner 的 finally 已回退状态并写 cancelled 记录）
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            # done 已产出（结果已落库）后才收到取消：仍按成功处理
            if hub.result is not None:
                status, result, error = "succeeded", hub.result, ""
            elif task_id in self._shutdown:
                status, error = "interrupted", INTERRUPTED_MESSAGE
            else:
                status, error = "cancelled", CANCELLED_MESSAGE
        except Exception:  # noqa: BLE001
            logger.exception("任务 %s 执行失败", task_id)
            if hub.result is not None:
                status, result, error = "succeeded", hub.result, ""
            else:
                status, error = "failed", "任务执行失败：服务器内部错误"
        await self._finalize(task_id, task, handler, hub, status=status, result=result, error=error)

    async def _consume(self, task: Task, handler: TaskHandler, hub: TaskHub, job: Any) -> str:
        """消费 runner 事件；返回错误信息（成功时为空串，结果存于 hub.result）。"""
        last_flush = time.monotonic()
        error = ""
        async with aclosing(handler.run(job, self.ctx)) as events:
            async for event, data in events:
                if event == "delta":
                    hub.append(str(data.get("text") or ""))
                    hub.set_progress(progress=handler.progress(task, hub.text))
                elif event == "continue":
                    hub.publish("continue", data)
                    hub.set_progress(stage=f"字数不足，自动续写第 {data.get('round')} 轮")
                elif event == "done":
                    hub.result = data
                elif event == "error":
                    error = str(data.get("detail") or "生成失败")
                else:
                    hub.publish(event, data)
                if hub.dirty and time.monotonic() - last_flush >= self.flush_interval:
                    await self._flush(hub)
                    last_flush = time.monotonic()
        if not error and hub.result is None:
            error = "任务意外结束，未产出结果"
        return error

    async def _flush(self, hub: TaskHub) -> None:
        hub.dirty = False
        try:
            async with self.ctx.sessionmaker() as db:
                await db.execute(
                    update(Task)
                    .where(Task.id == hub.task_id)
                    .values(output_text=hub.text, progress=hub.progress, stage=hub.stage[:100])
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("任务 %s 刷新输出失败", hub.task_id)

    async def _finalize(
        self,
        task_id: int,
        task: Task | None,
        handler: TaskHandler | None,
        hub: TaskHub,
        *,
        status: str,
        result: dict[str, Any] | None,
        error: str,
    ) -> None:
        self._finalizing.add(task_id)
        out: dict[str, Any] | None = None
        try:
            if status != "succeeded" and handler is not None and task is not None:
                await handler.on_abort(self.ctx, task)
            try:
                async with self.ctx.sessionmaker() as db:
                    row = await db.get(Task, task_id)
                    if row is not None:
                        row.status = status
                        row.result = result
                        row.error = error
                        row.output_text = hub.text
                        row.progress = 100 if status == "succeeded" else hub.progress
                        row.stage = task_service.STAGE_LABELS[status]
                        row.finished_at = func.now()
                        await db.commit()
                        await db.refresh(row)
                        out = task_service.out_json(row)
                        await task_service.prune_finished(db, row.user_id)
            except Exception:  # noqa: BLE001
                logger.exception("任务 %s 结束状态写入失败", task_id)
            if status == "succeeded" and handler is not None and task is not None:
                await self._on_success(handler, task)
        finally:
            if self.hubs.get(task_id) is hub:
                self.hubs.pop(task_id, None)
            if status == "succeeded":
                hub.close("done", {"task": out, "result": result})
            else:
                hub.close("error", {"task": out, "detail": error})
            self._finalizing.discard(task_id)

    async def _on_success(self, handler: TaskHandler, task: Task) -> None:
        hook = getattr(handler, "on_success", None)
        if hook is None:
            return
        try:
            await hook(self.ctx, task)
        except Exception:  # noqa: BLE001
            logger.exception("任务 %s 成功回调失败", task.id)

    # ---------------- 取消 ----------------

    async def cancel_running(self, task_id: int) -> None:
        """取消运行中的任务并等待其收尾（最多 cancel_wait_seconds 秒）。"""
        t = self._running.get(task_id)
        if t is None:
            # 已经收尾完毕（库里的 running 是读取时的旧值）
            return
        if task_id not in self._cancel_requested and task_id not in self._finalizing:
            self._cancel_requested.add(task_id)
            t.cancel()
        await asyncio.wait({t}, timeout=self.cancel_wait_seconds)

    # ---------------- 订阅 ----------------

    async def subscribe(self, task_id: int) -> AsyncIterator[Event]:
        """快照 + 增量；任务结束（done / error）后结束。断开只移除该订阅者，不影响任务执行。"""
        sm = self.ctx.sessionmaker
        hub = self.hubs.get(task_id)
        if hub is None:
            async with sm() as db:
                row = await db.get(Task, task_id)
            if row is None:
                yield "error", {"task": None, "detail": TASK_GONE_MESSAGE}
                return
            if row.status in ACTIVE_TASK_STATUSES:
                hub, created = self.ensure_hub(
                    task_id, status=row.status, text=row.output_text, progress=row.progress, stage=row.stage
                )
                if created:
                    # 防止读库与建 hub 之间任务恰好结束：复查，已结束则丢弃刚建的 hub
                    async with sm() as db:
                        row = await db.get(Task, task_id)
                    if row is None or row.status not in ACTIVE_TASK_STATUSES:
                        if self.hubs.get(task_id) is hub and task_id not in self._running:
                            self.hubs.pop(task_id, None)
                        hub = None
            else:
                hub = None
            if hub is None:
                if row is None:
                    yield "error", {"task": None, "detail": TASK_GONE_MESSAGE}
                    return
                out = task_service.out_json(row)
                yield "snapshot", {"task": out, "text": row.output_text}
                if row.status == "succeeded":
                    yield "done", {"task": out, "result": row.result}
                else:
                    yield "error", {"task": out, "detail": row.error}
                return

        text, sub = hub.subscribe()
        try:
            async with sm() as db:
                row = await db.get(Task, task_id)
                out = await task_service.out_json_live(db, self, row) if row is not None else None
            yield "snapshot", {"task": out, "text": text}
            while True:
                if sub.overflowed and sub.queue.empty():
                    return
                try:
                    async with asyncio.timeout(self.heartbeat_seconds):
                        event, data = await sub.queue.get()
                except TimeoutError:
                    if await self._task_gone(task_id):
                        # 排队中的任务随用户 / 项目被级联删除：没有执行体会再关闭 hub，这里代为收尾
                        if task_id not in self._running:
                            self.close_hub(task_id, "error", {"task": None, "detail": TASK_GONE_MESSAGE})
                        yield "error", {"task": None, "detail": TASK_GONE_MESSAGE}
                        return
                    yield HEARTBEAT_EVENT, {}
                    continue
                yield event, data
                if event in ("done", "error"):
                    return
        finally:
            hub.unsubscribe(sub)

    async def _task_gone(self, task_id: int) -> bool:
        try:
            async with self.ctx.sessionmaker() as db:
                return await db.scalar(select(Task.id).where(Task.id == task_id)) is None
        except Exception:  # noqa: BLE001
            logger.exception("任务 %s 存在性检查失败", task_id)
            return False


def _try_handler(kind: str) -> TaskHandler | None:
    try:
        return get_handler(kind)
    except AppError:
        return None
