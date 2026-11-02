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
- 暂停（多步任务的审核点）：runner 产出 paused 后结束，任务写为 paused（待审核）——不调用 on_abort / on_success，
  hub 以 paused 事件关闭。paused 不是活动状态：不被调度、不占并发名额与资源键；由 task_service.resume_task
  改回 queued 后同一任务从断点继续。取消落在 paused 产出之后时仍按 paused 收尾（与 done 已产出时相同）。
- 通用事件：reset 清空 hub 文本（多步任务每步只保留当前步的实时输出）；stage 只更新阶段文案。
"""

import asyncio
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import aclosing, contextmanager, suppress
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.task_engine.handlers  # noqa: F401  导入即注册内置任务类型
from app.core.errors import AppError
from app.models import Project, Task
from app.models.entities import ACTIVE_TASK_STATUSES
from app.services import authorization_service as authz
from app.services import quota_service, task_service, task_statistics_service
from app.services.task_engine.cancellation import AdminCancellationDrain
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
        self.ctx.task_engine = self
        self.hubs: dict[int, TaskHub] = {}
        self._running: dict[int, asyncio.Task[None]] = {}
        self._running_statistics_keys: dict[int, str | None] = {}
        self._running_projects: dict[int, int | None] = {}
        self._cleanup_work: dict[int, dict[asyncio.Task, int]] = {}
        self._abort_failures: set[int] = set()
        self._running_users: dict[int, int] = {}
        self._running_rounds: dict[int, Any] = {}
        self._admitted: dict[int, tuple[str | None, Any, int]] = {}
        self._claiming: set[int] = set()
        self.runtime = authz.runtime_for_factory(ctx.sessionmaker)
        self._running_serial: dict[int, str] = {}
        self._cancel_requested: set[int] = set()
        self._shutdown: set[int] = set()
        self._finalizing: set[int] = set()
        self._wake: asyncio.Event | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._stopping = False
        self.admin_cancellations = AdminCancellationDrain(self)

    # ---------------- 生命周期 ----------------

    @property
    def running(self) -> bool:
        return self._loop_task is not None

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        self._wake = asyncio.Event()
        self._stopping = False
        await task_statistics_service.initialize_tracking(self.ctx.sessionmaker)
        await self.recover()
        await self.scan_admin_cancellations()
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
            if (task_id not in self._cancel_requested and task_id not in self._finalizing
                    and task_id not in self._claiming):
                t.cancel()
        if running:
            await asyncio.gather(*(t for _, t in running), return_exceptions=True)
        # 不cancel独立清理或SQLite活游标；真正退出后才允许lifespan dispose数据库。
        await self.wait_admin_cancellations()
        cleanup = {work for scope in self._cleanup_work.values() for work in scope if not work.done()}
        if cleanup:
            await asyncio.gather(*cleanup, return_exceptions=True)
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
            if task.statistics_key is not None:
                self.admin_cancellations.recovering.add(task.statistics_key)
            handler = _try_handler(task.kind)
            abort_ok = handler is not None
            if handler is not None:
                try:
                    await handler.on_abort(self.ctx, task)
                except Exception:
                    abort_ok = False
            try:
                row, committed = await self.complete_state(
                    task, expected_status="running", status="interrupted", result=None, error=INTERRUPTED_MESSAGE,
                    text=task.output_text, progress=task.progress, abort_ok=abort_ok,
                )
            except Exception:
                # 意图仍持久存在，后续scan可重试；日志不输出带任务输入的SQL异常参数。
                logger.error("任务启动恢复尚未完成 task_key=%s", task.statistics_key)
                continue
            if committed and task.statistics_key is not None:
                self.admin_cancellations.recovering.discard(task.statistics_key)
            out = task_service.out_json(row) if row is not None and handler is not None else None
            self.close_hub(
                task.id, "error", {"task": out, "detail": INTERRUPTED_MESSAGE}, statistics_key=task.statistics_key
            )
        # 上次进程在确认外部（MCP）调用期间退出：提案停在 executing，结果未知，判为失败（不重新调用）
        from app.services.agent.service import recover_external

        async with sm() as db:
            if await recover_external(db, stale_only=False):
                await db.commit()

    # ---------------- hub ----------------

    def ensure_hub(
        self, task_id: int, *, statistics_key: str | None = None,
        status: str = "queued", text: str = "", progress: int = 0, stage: str = ""
    ) -> tuple[TaskHub, bool]:
        """返回 (hub, created)。"""
        hub = self.hubs.get(task_id)
        if hub is not None and statistics_key is not None and hub.statistics_key != statistics_key:
            self.hubs.pop(task_id, None)
            hub.close("error", {"task": None, "detail": TASK_GONE_MESSAGE})
            hub = None
        if hub is not None:
            return hub, False
        hub = TaskHub(
            task_id, statistics_key=statistics_key, status=status, text=text, progress=progress, stage=stage
        )
        self.hubs[task_id] = hub
        return hub, True

    def close_hub(
        self, task_id: int, event: str, data: dict[str, Any], *, statistics_key: str | None = None
    ) -> None:
        hub = self.hubs.get(task_id)
        if hub is not None and hub.statistics_key == statistics_key:
            self.hubs.pop(task_id, None)
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
                await self.scan_admin_cancellations()
                await self._dispatch()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("任务调度失败")

    async def _dispatch(self) -> None:
        if not self.runtime.settings_confirmed:
            return
        settings = self.ctx.settings
        sm = self.ctx.sessionmaker
        async with sm() as db:
            rows = await db.execute(
                select(Task.id, Task.statistics_key, Task.user_id, Task.serial_key, Task.project_id, Task.queued_at)
                .outerjoin(Project, Project.id == Task.project_id)
                .where(Task.status == "queued", Task.admin_cancel_operation_id.is_(None),
                       task_service.visible_project_condition())
                .order_by(Task.id)
            )
            queued = list(rows.all())
            limits = await quota_service.task_limits(db, (row.user_id for row in queued), settings)
        per_user = Counter(self._running_users.values())
        # 运行中占用的串行键；本轮被跳过的串行键也加入，保证同键内先进先出
        busy_serial = set(self._running_serial.values())
        for task_id, statistics_key, user_id, serial_key, project_id, queued_at in queued:
            if self._stopping or len(self._running) >= settings.task_max_concurrency:
                break
            if task_id in self._running:
                continue
            if serial_key is not None and serial_key in busy_serial:
                continue
            limit = limits[user_id]
            if limit is not None and per_user[user_id] >= limit:
                if serial_key is not None:
                    busy_serial.add(serial_key)
                continue
            per_user[user_id] += 1
            # 先登记 asyncio.Task 再领取：库里出现 running 时内存里一定有可取消的执行体
            self._running_projects[task_id] = project_id
            self._running_users[task_id] = user_id
            self._running_rounds[task_id] = queued_at
            self._running_statistics_keys[task_id] = statistics_key
            if serial_key is not None:
                busy_serial.add(serial_key)
                self._running_serial[task_id] = serial_key
            t = asyncio.create_task(self._claim_and_run(task_id, statistics_key), name=f"task-{task_id}")
            self._running[task_id] = t
            t.add_done_callback(lambda _t, tid=task_id: self._on_done(tid))
        await self._broadcast_queue_positions()

    async def _broadcast_queue_positions(self) -> None:
        if not any(h.status == "queued" for h in self.hubs.values()):
            return
        async with self.ctx.sessionmaker() as db:
            rows = list((await db.execute(
                select(Task.id, Task.statistics_key).where(Task.status == "queued").order_by(Task.id)
            )).all())
        # 已登记、正在领取的任务不算排队
        rows = [(task_id, key) for task_id, key in rows if task_id not in self._running]
        for position, (task_id, key) in enumerate(rows):
            hub = self.hubs.get(task_id)
            if hub is not None and hub.statistics_key == key and hub.status == "queued":
                hub.set_queue_position(position)

    def _on_done(self, task_id: int) -> None:
        task = self._running.pop(task_id, None)
        project_id = self._running_projects.pop(task_id, None)
        if task is not None and not task.cancelled() and task.exception() is not None:
            if project_id is not None:
                self._abort_failures.add(project_id)
            logger.error("任务执行体异常结束 task=%s", task_id)
        self._running_users.pop(task_id, None)
        self._running_rounds.pop(task_id, None)
        self._admitted.pop(task_id, None)
        self._claiming.discard(task_id)
        self._running_statistics_keys.pop(task_id, None)
        self._running_serial.pop(task_id, None)
        self._cancel_requested.discard(task_id)
        self.notify()

    # ---------------- 执行 ----------------

    def admitted_count(self, user_id: int | None = None) -> int:
        return sum(1 for _key, _round, owner in self._admitted.values() if user_id is None or owner == user_id)

    async def _claim_and_run(self, task_id: int, statistics_key: str | None) -> None:
        """真正领取在共享发布临界段复核额度；候选登记不算admitted。"""
        if task_id in self._cancel_requested:
            return
        running_stage = task_service.STAGE_LABELS["running"]
        try:
            async with self.runtime.hold(), self.ctx.sessionmaker() as db:
                if not self.runtime.settings_confirmed:
                    return
                await authz.gate(db)
                candidate = (await db.execute(select(Task.user_id, Task.queued_at).where(
                    Task.id == task_id, Task.statistics_key == statistics_key, Task.status == "queued",
                ))).first()
                if candidate is None:
                    return
                expected_round = self._running_rounds.get(task_id, candidate.queued_at)
                if candidate.queued_at != expected_round:
                    return
                await authz.lock_users(db, candidate.user_id)
                limit = (await quota_service.task_limits(db, [candidate.user_id], self.ctx.settings))[candidate.user_id]
                if self._stopping or self.admitted_count() >= self.ctx.settings.task_max_concurrency or (
                    limit is not None and self.admitted_count(candidate.user_id) >= limit
                ) or task_id in self._cancel_requested:
                    return
                # 活驱动CAS/提交确认期间只登记取消意图，不cancel游标。
                self._claiming.add(task_id)
                result = await db.execute(
                    update(Task)
                    .where(
                        Task.id == task_id, Task.statistics_key == statistics_key, Task.queued_at == expected_round,
                        Task.status == "queued", task_service.visible_task_project_exists(),
                        Task.admin_cancel_operation_id.is_(None),
                    )
                    .values(status="running", started_at=func.now(), stage=running_stage)
                )
                if result.rowcount != 1:
                    return
                row = await db.get(Task, task_id)
                await task_statistics_service.record_state_change(db, row, "running")
                self._running_statistics_keys[task_id] = row.statistics_key
                try:
                    await db.commit()
                except Exception:
                    # 提交确认丢失：不重放CAS，不在事实不明时开始外部执行。
                    await db.rollback()
                    while True:
                        try:
                            async with self.ctx.sessionmaker() as verify:
                                persisted = (await verify.execute(select(Task.status).where(
                                    Task.id == task_id, Task.statistics_key == statistics_key,
                                    Task.queued_at == expected_round,
                                ))).scalar_one_or_none()
                            break
                        except Exception:
                            if self._stopping:
                                return
                            await asyncio.sleep(0.1)
                    if persisted != "running":
                        return
                # 外层runtime尚未释放，覆盖DB已提交但commit await尚未返回的窗口。
                self._admitted[task_id] = (statistics_key, expected_round, candidate.user_id)
        finally:
            # 会话退出也可能await；取消保护须无缝延续到_run的收尾try。
            self._claiming.discard(task_id)
        hub, _ = self.ensure_hub(task_id, statistics_key=statistics_key, status="running")
        hub.set_queue_position(None)
        hub.set_progress(status="running", stage=running_stage)
        await self._run(task_id, hub)

    async def _run(self, task_id: int, hub: TaskHub) -> None:
        status, error, result = "failed", "", None
        task: Task | None = None
        handler: TaskHandler | None = None
        try:
            if hub.statistics_key is not None:
                async with self.ctx.sessionmaker() as db:
                    task = await db.scalar(select(Task).where(
                        Task.id == task_id, Task.statistics_key == hub.statistics_key
                    ))
            if task is None:
                # 任务行已随用户 / 项目删除
                if self.hubs.get(task_id) is hub:
                    self.hubs.pop(task_id, None)
                hub.close("error", {"task": None, "detail": TASK_GONE_MESSAGE})
                return
            handler = get_handler(task.kind)
            if task_id in self._shutdown:
                status, error = "interrupted", INTERRUPTED_MESSAGE
            elif task_id in self._cancel_requested:
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
                        if hub.paused is not None:
                            status, result = "paused", hub.paused
                        else:
                            status, result = "succeeded", hub.result
        except asyncio.CancelledError:
            # 只会被 cancel() 一次；吞掉取消让收尾正常执行（runner 的 finally 已回退状态并写 cancelled 记录）
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            # done / paused 已产出（结果 / 断点已落库）后才收到取消：仍按成功 / 暂停处理
            if hub.result is not None:
                status, result, error = "succeeded", hub.result, ""
            elif hub.paused is not None:
                status, result, error = "paused", hub.paused, ""
            elif task_id in self._shutdown:
                status, error = "interrupted", INTERRUPTED_MESSAGE
            else:
                status, error = "cancelled", CANCELLED_MESSAGE
        except Exception:  # noqa: BLE001
            logger.exception("任务 %s 执行失败", task_id)
            if hub.result is not None:
                status, result, error = "succeeded", hub.result, ""
            elif hub.paused is not None:
                status, result, error = "paused", hub.paused, ""
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
                elif event == "paused":
                    hub.paused = data
                elif event == "reset":
                    hub.reset()
                elif event == "stage":
                    hub.set_progress(stage=str(data.get("stage") or ""))
                elif event == "error":
                    error = str(data.get("detail") or "生成失败")
                else:
                    hub.publish(event, data)
                if hub.dirty and time.monotonic() - last_flush >= self.flush_interval:
                    await self._flush(hub)
                    last_flush = time.monotonic()
        if not error and hub.result is None and hub.paused is None:
            error = "任务意外结束，未产出结果"
        return error

    async def _flush(self, hub: TaskHub) -> None:
        hub.dirty = False
        if hub.statistics_key is None:
            return
        try:
            async with self.ctx.sessionmaker() as db:
                await db.execute(
                    update(Task)
                    .where(
                        Task.id == hub.task_id, Task.statistics_key == hub.statistics_key, Task.status == "running"
                    )
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
        committed = False
        abort_ok = status in ("succeeded", "paused") or (handler is not None and task is not None)
        try:
            # 暂停时资源已在当前步结束时释放，断点保留，不回退
            if status not in ("succeeded", "paused") and handler is not None and task is not None:
                try:
                    await handler.on_abort(self.ctx, task)
                except Exception:
                    abort_ok = False
                    if task.project_id is not None:
                        self._abort_failures.add(task.project_id)
                    error = "任务收尾失败，请稍后重试"
                    logger.error("任务取消收尾失败 task=%s", task_id)
            try:
                key = task.statistics_key if task is not None else None
                if key is not None and status in ("succeeded", "paused"):
                    self.admin_cancellations.winners[key] = (
                        task.queued_at.replace(tzinfo=None) if task.queued_at is not None else None,
                        dict(status=status, result=result, error=error, text=hub.text, progress=hub.progress),
                    )
                row, committed = await self.complete_state(
                    task, expected_status="running", status=status, result=result, error=error,
                    text=hub.text, progress=hub.progress, abort_ok=abort_ok,
                )
                if row is not None:
                    if key is not None and row.status != "running":
                        self.admin_cancellations.winners.pop(key, None)
                    out = task_service.out_json(row)
                    status, result, error = row.status, row.result, row.error
                    async with self.ctx.sessionmaker() as db:
                        await task_service.prune_finished(db, row.user_id)
                else:
                    if key is not None:
                        self.admin_cancellations.winners.pop(key, None)
                    status, result, error = "interrupted", None, TASK_GONE_MESSAGE
            except Exception:  # noqa: BLE001
                logger.error("任务结束状态写入失败 task=%s", task_id)
                # commit可能已提交而确认丢失；只重读原UUID，不把真实赢家变成待abort的孤立running。
                fact = None
                if task is not None and task.statistics_key is not None:
                    try:
                        async with self.ctx.sessionmaker() as db:
                            fact = await db.scalar(select(Task).where(
                                Task.id == task_id, Task.statistics_key == task.statistics_key
                            ))
                        if fact is not None and fact.status != "running" and fact.admin_cancel_operation_id is None:
                            status, result, error = fact.status, fact.result, fact.error
                            out = task_service.out_json(fact)
                            committed = True
                            self.admin_cancellations.winners.pop(task.statistics_key, None)
                            self.admin_cancellations.remember_completed(task)
                    except Exception:
                        logger.error("任务结束事实尚无法确认 task_key=%s", task.statistics_key)
                if not committed:
                    if task is not None and task.project_id is not None:
                        self._abort_failures.add(task.project_id)
                    status, result, error = "failed", None, "任务结束状态保存失败，请稍后重试"
            if committed and status == "succeeded" and handler is not None and task is not None:
                await self._on_success(handler, task)
        finally:
            if self.hubs.get(task_id) is hub:
                self.hubs.pop(task_id, None)
            if status == "succeeded":
                hub.close("done", {"task": out, "result": result})
            elif status == "paused":
                hub.close("paused", {"task": out, "result": result})
            else:
                hub.close("error", {"task": out, "detail": error})
            self._finalizing.discard(task_id)

    async def complete_state(
        self, task: Task | None, *, expected_status: str, status: str, result: dict[str, Any] | None,
        error: str, text: str, progress: int, abort_ok: bool, operation_id: str | None = None,
    ) -> tuple[Task | None, bool]:
        """状态/统计/清标记同一次CAS提交；接受夹在读写之间时重读同UUID，而不是忽略新意图。"""
        if task is None or task.statistics_key is None:
            return None, False
        async with self.ctx.sessionmaker() as db:
            while True:
                row = await db.scalar(select(Task).where(
                    Task.id == task.id, Task.statistics_key == task.statistics_key
                ).execution_options(populate_existing=True))
                if row is None or row.status != expected_status:
                    return row, False
                observed = row.admin_cancel_operation_id
                if operation_id is not None and observed != operation_id:
                    return row, False
                if observed is not None and not abort_ok:
                    # 收尾失败绝不释放管理员意图持有的资源屏障。
                    return row, False
                stamp = None if status == "paused" else task_statistics_service.now_utc()
                changed = await db.execute(update(Task).where(
                    Task.id == task.id, Task.statistics_key == task.statistics_key, Task.status == expected_status,
                    Task.admin_cancel_operation_id == observed if observed is not None else (
                        Task.admin_cancel_operation_id.is_(None)
                    ),
                ).values(status=status, result=result, error=error, output_text=text,
                         progress=100 if status == "succeeded" else progress,
                         stage=task_service.STAGE_LABELS[status], finished_at=stamp,
                         admin_cancel_operation_id=None, admin_cancel_requested_at=None))
                if changed.rowcount != 1:
                    await db.rollback()
                    continue
                await task_statistics_service.record_state_change(db, row, status, finished_at=stamp)
                try:
                    await db.commit()
                except Exception:
                    # finalize/recover/drain共用这一提交边界。确认丢失时只回放原UUID、
                    # 原入队轮次已落库的目标状态，不能漏登记完成代次或留下未关闭的Hub。
                    await db.rollback()
                    async with self.ctx.sessionmaker() as proof_db:
                        fact = await proof_db.scalar(select(Task).where(
                            Task.id == task.id, Task.statistics_key == task.statistics_key
                        ))
                    if (fact is None or fact.status != status or fact.admin_cancel_operation_id is not None
                            or self.admin_cancellations.round_at(fact) != self.admin_cancellations.round_at(task)):
                        raise
                    row = fact
                if observed is not None:
                    self.admin_cancellations.remember_completed(task)
                return row, True

    async def _on_success(self, handler: TaskHandler, task: Task) -> None:
        hook = getattr(handler, "on_success", None)
        if hook is None:
            return
        try:
            await hook(self.ctx, task)
        except Exception:  # noqa: BLE001
            logger.exception("任务 %s 成功回调失败", task.id)

    # ---------------- 取消 ----------------

    def request_admin_cancel(self, task_key: str, operation_id: str) -> None:
        self.admin_cancellations.request(task_key, operation_id)

    async def scan_admin_cancellations(self) -> None:
        await self.admin_cancellations.scan()

    async def wait_admin_cancellations(self) -> None:
        await self.admin_cancellations.wait()

    async def cancel_running(self, task_id: int, *, statistics_key: str | None = None) -> None:
        """取消运行中的任务并等待其收尾（最多 cancel_wait_seconds 秒）。"""
        if self._running_statistics_keys.get(task_id) != statistics_key:
            return
        t = self._running.get(task_id)
        if t is None:
            # 已经收尾完毕（库里的 running 是读取时的旧值）
            return
        if task_id not in self._cancel_requested and task_id not in self._finalizing:
            self._cancel_requested.add(task_id)
            if task_id not in self._claiming:
                t.cancel()
        await asyncio.wait({t}, timeout=self.cancel_wait_seconds)

    @contextmanager
    def cleanup_scope(self, task: Task):
        """登记请求侧 queued/paused 取消清理，即使 Task 已先提交终态也必须等待。"""
        current = asyncio.current_task()
        project_id = task.project_id
        if current is None or project_id is None:
            yield
            return
        work = self._cleanup_work.setdefault(project_id, {})
        work[current] = work.get(current, 0) + 1
        try:
            yield
        finally:
            work[current] -= 1
            if not work[current]:
                work.pop(current)
            if not work:
                self._cleanup_work.pop(project_id, None)

    def project_work(self, project_id: int) -> set[asyncio.Task]:
        current = asyncio.current_task()
        running = {t for tid, t in self._running.items() if self._running_projects.get(tid) == project_id}
        cleanup = set(self._cleanup_work.get(project_id, {}))
        return {t for t in running | cleanup if t is not current and not t.done()}

    def forget_project_hubs(
        self, task_ids: list[int], *, statistics_keys: dict[int, str | None] | None = None
    ) -> None:
        for task_id in task_ids:
            key = statistics_keys.get(task_id) if statistics_keys is not None else None
            self.close_hub(task_id, "error", {"task": None, "detail": TASK_GONE_MESSAGE}, statistics_key=key)

    # ---------------- 订阅 ----------------

    async def subscribe(self, task_id: int, *, statistics_key: str | None = None) -> AsyncIterator[Event]:
        """快照 + 增量；任务结束（done / error）或暂停（paused）后结束。断开只移除该订阅者，不影响任务执行。"""
        sm = self.ctx.sessionmaker
        # HTTP入口传入鉴权时捕获的UUID；内部直接调用则在首次读库时绑定。
        async with sm() as db:
            row = await self._visible_task(db, task_id, statistics_key=statistics_key)
        if row is None or row.statistics_key is None:
            yield "error", {"task": None, "detail": TASK_GONE_MESSAGE}
            return
        statistics_key = row.statistics_key
        hub = self.hubs.get(task_id)
        # 数据库终态优先：即使内存残留排队 hub，也必须回放结果，不能只发送心跳。
        if row.status not in ACTIVE_TASK_STATUSES or hub is None or hub.statistics_key != statistics_key:
            if row.status in ACTIVE_TASK_STATUSES:
                hub, created = self.ensure_hub(
                    task_id, statistics_key=statistics_key,
                    status=row.status, text=row.output_text, progress=row.progress, stage=row.stage,
                )
                if created:
                    # 防止读库与建 hub 之间任务恰好结束：复查，已结束则丢弃刚建的 hub
                    async with sm() as db:
                        row = await self._visible_task(db, task_id, statistics_key=statistics_key)
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
                for event in _finished_events(row):
                    yield event
                return

        text, sub = hub.subscribe()
        try:
            async with sm() as db:
                row = await self._visible_task(db, task_id, statistics_key=statistics_key)
                out = await task_service.out_json_live(db, self, row) if row is not None else None
            if row is None:
                yield "error", {"task": None, "detail": TASK_GONE_MESSAGE}
                return
            # 注册订阅前后可能恰好完成；以复查的持久化状态收尾，不再等待已错过的广播。
            if row.status not in ACTIVE_TASK_STATUSES:
                for event in _finished_events(row):
                    yield event
                return
            yield "snapshot", {"task": out, "text": text}
            while True:
                if sub.overflowed and sub.queue.empty():
                    return
                try:
                    async with asyncio.timeout(self.heartbeat_seconds):
                        event, data = await sub.queue.get()
                except TimeoutError:
                    if await self._task_gone(task_id, statistics_key=statistics_key):
                        # 排队中的任务随用户 / 项目被级联删除：没有执行体会再关闭 hub，这里代为收尾
                        if task_id not in self._running and self.hubs.get(task_id) is hub:
                            self.close_hub(
                                task_id, "error", {"task": None, "detail": TASK_GONE_MESSAGE},
                                statistics_key=statistics_key,
                            )
                        yield "error", {"task": None, "detail": TASK_GONE_MESSAGE}
                        return
                    yield HEARTBEAT_EVENT, {}
                    continue
                yield event, data
                if event in ("done", "error", "paused"):
                    return
        finally:
            hub.unsubscribe(sub)

    async def _visible_task(
        self, db: AsyncSession, task_id: int, *, statistics_key: str | None = None
    ) -> Task | None:
        statement = select(Task).where(
            Task.id == task_id, task_service.visible_task_project_exists()
        )
        if statistics_key is not None:
            statement = statement.where(Task.statistics_key == statistics_key)
        return await db.scalar(statement)

    async def _task_gone(self, task_id: int, *, statistics_key: str | None = None) -> bool:
        try:
            async with self.ctx.sessionmaker() as db:
                return await self._visible_task(db, task_id, statistics_key=statistics_key) is None
        except Exception:  # noqa: BLE001
            logger.exception("任务 %s 存在性检查失败", task_id)
            return False


def _finished_events(task: Task) -> tuple[Event, Event]:
    """从持久化状态回放完整快照与终止事件，不受残留 hub 影响。"""
    out = task_service.out_json(task)
    if task.status == "succeeded":
        end = "done", {"task": out, "result": task.result}
    elif task.status == "paused":
        end = "paused", {"task": out, "result": task.result}
    else:
        end = "error", {"task": out, "detail": task.error}
    return ("snapshot", {"task": out, "text": task.output_text}), end


def _try_handler(kind: str) -> TaskHandler | None:
    try:
        return get_handler(kind)
    except AppError:
        return None
