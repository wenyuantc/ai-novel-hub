"""引擎内的持久取消意图 drain：按原轮次和操作号认领，独立于受理 HTTP 请求。"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import select, update

from app.models import Task, TaskWorkRecord
from app.models.entities import OPEN_TASK_STATUSES
from app.services import chapter_claim, project_lifecycle, task_service, task_statistics_service
from app.services.task_engine import admin_cleanup, cleanup_work, workers
from app.services.task_engine.registry import get_handler

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine

logger = logging.getLogger(__name__)

_CHAPTER_KINDS = frozenset({"chapter.generate", "chapter.polish", "chapter.revise"})
_CHAPTER_INTENT = "_admin_chapter_cancel"
_CHAPTER_RECEIPT = "_admin_chapter_receipt"


def _round_text(value: datetime) -> str:
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)).isoformat()


def _uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def chapter_cancel_intent(task: Task, operation_id: str) -> dict | None:
    """受理时冻结原 Task/轮次/执行/章节 claim，不把后来的章抢占当作原请求。"""
    if task.kind not in _CHAPTER_KINDS or task.status not in ("queued", "running"):
        return None
    from app.services.task_engine.handlers import chapter as chapter_handler

    try:
        claim = (chapter_handler.capture_queued_claim(task) if task.status == "queued"
                 else chapter_handler.capture_running_claim(task))
    except Exception:
        return None
    return {
        "operation_id": operation_id, "task_id": claim.task_id, "task_key": claim.task_key,
        "actor_id": claim.actor_id, "project_id": claim.project_id, "queued_at": _round_text(claim.queued_at),
        "execution_key": claim.execution_key, "chapter_id": claim.chapter_id, "claim_key": claim.claim_key,
        "prev_status": claim.prev_status, "phase": claim.phase,
    }


def _chapter_receipt(task: Task, operation_id: str) -> dict | None:
    payload = task.input if isinstance(task.input, dict) else {}
    value = payload.get(_CHAPTER_RECEIPT)
    if not isinstance(value, dict) or set(value) != {
        "operation_id", "task_id", "task_key", "actor_id", "project_id", "queued_at", "execution_key",
        "chapter_id", "claim_key", "prev_status", "phase", "status", "completion_key",
    }:
        return None
    if (task.kind not in _CHAPTER_KINDS or task.status not in ("cancelled", "interrupted")
            or task.admin_cancel_operation_id != operation_id or value["operation_id"] != operation_id
            or value["task_id"] != task.id or value["task_key"] != task.statistics_key
            or value["actor_id"] != task.user_id or value["project_id"] != task.project_id
            or value["chapter_id"] != task.chapter_id or task.queued_at is None
            or value["queued_at"] != _round_text(task.queued_at) or value["status"] != task.status
            or value["completion_key"] != task.execution_key or not _uuid(value["completion_key"])
            or not _uuid(value["task_key"]) or not _uuid(value["claim_key"])
            or value["phase"] not in ("queued", "running") or value["prev_status"] not in ("draft", "done")
            or "_chapter_claim" in payload or _CHAPTER_INTENT in payload):
        return None
    if ((value["phase"] == "queued" and value["execution_key"] is not None)
            or (value["phase"] == "running" and value["execution_key"] != value["completion_key"])):
        return None
    return value


class AdminCancellationDrain:
    def __init__(self, engine: "TaskEngine"):
        self.engine = engine
        self.attempts: set[str] = set()
        self.pending_releases: dict[str, TaskWorkRecord] = {}
        self.uncertain_claims: set[tuple[str, str]] = set()
        self.release_task: asyncio.Task | None = None
        self.work: dict[tuple[str, str], asyncio.Task] = {}
        # 启动遗留running即使第一次abort失败，后续仍按原重启中断语义收尾。
        self.recovering: dict[str, tuple[datetime | None, str | None]] = {}
        # 防止旧项目级失败标记让兼容路径在已释放资源后重放管理员旧abort。
        self.completed: dict[str, tuple[datetime | None, str | None]] = {}
        # 已产出done/paused但终态事务暂时失败的本进程知识；重启仍按既有interrupted规则。
        self.winners: dict[str, tuple] = {}

    def request(self, task_key: str, operation_id: str) -> None:
        identity = (task_key, operation_id)
        if identity in self.work:
            return
        work = asyncio.create_task(self._drain(task_key, operation_id), name=f"task-cancel-{task_key}")
        self.work[identity] = work
        work.add_done_callback(lambda t: self.work.pop(identity, None))

    async def commit_claim(self, db, task_key: str, operation_id: str) -> None:
        try:
            await db.commit()
        except (Exception, asyncio.CancelledError):
            self.uncertain_claims.add((task_key, operation_id))
            raise

    def schedule_confirmations(self) -> None:
        for key in self.uncertain_claims.copy():
            if key not in self.work:
                self.request(*key)
        if self.pending_releases and (self.release_task is None or self.release_task.done()):
            self.release_task = asyncio.create_task(self.retry_releases(), name="admin-cleanup-exit-confirmation")

    async def retry_releases(self) -> None:
        """HTTP worker 保持心跳期间，即使调度已停止仍确认归还已退出的清理。"""
        for attempt, record in list(self.pending_releases.items()):
            try:
                await admin_cleanup.relinquish(self.engine, record)
            except Exception:  # noqa: BLE001  精确 attempt 继续保留，绝不凭更新回执猜测已归还。
                logger.warning("管理员取消清理退出确认待重试 operation=%s", record.operation_id)
            else:
                self.pending_releases.pop(attempt, None)

    async def scan(self) -> None:
        completed, winners = dict(self.completed), dict(self.winners)
        cached_keys = set(completed) | set(winners) | set(self.recovering)
        async with self.engine.ctx.sessionmaker() as db:
            await admin_cleanup.reap_finished(db)
            metadata = select(
                Task.statistics_key, Task.admin_cancel_operation_id, Task.queued_at, Task.status, Task.execution_key
            )
            rows = list((await db.execute(metadata.where(Task.admin_cancel_operation_id.is_not(None)))).all())
            # 全站保留量没有固定上限；缓存核验不能构造超出数据库绑定参数上限的IN，
            # 更不能因此阻断持久取消意图的核心扫描。
            cached_keys = list(cached_keys - {key for key, _, _, _, _ in rows})
            for offset in range(0, len(cached_keys), 500):
                rows.extend((await db.execute(metadata.where(
                    Task.statistics_key.in_(cached_keys[offset:offset + 500])
                ))).all())
        facts = {key: (queued_at.replace(tzinfo=None) if queued_at is not None else None, status, execution_key)
                 for key, _, queued_at, status, execution_key in rows}
        # 缓存跟随现存Task的当前轮次，删除/裁剪/恢复入队后不无限保留结果或UUID。
        for key, identity in completed.items():
            if (key not in facts or (facts[key][0], facts[key][2]) != identity) \
                    and self.completed.get(key) == identity:
                self.completed.pop(key, None)
        for key, winner in winners.items():
            if (key not in facts or facts[key][0] != winner[0] or facts[key][1] != "running"
                    or facts[key][2] != (winner[2] if len(winner) > 2 else None)) and (
                self.winners.get(key) is winner
            ):
                self.winners.pop(key, None)
        for key, identity in list(self.recovering.items()):
            if key not in facts or facts[key][1] != "running" or (facts[key][0], facts[key][2]) != identity:
                self.recovering.pop(key, None)
        for key, operation_id, _, _, _ in rows:
            if key is not None and operation_id is not None:
                self.request(key, operation_id)

    @staticmethod
    def round_at(task: Task) -> datetime | None:
        return task.queued_at.replace(tzinfo=None) if task.queued_at is not None else None

    def remember_completed(self, task: Task) -> None:
        if task.statistics_key is not None:
            self.completed[task.statistics_key] = (self.round_at(task), task.execution_key)

    def completed_round(self, task: Task) -> bool:
        return (task.statistics_key in self.completed
                and self.completed[task.statistics_key] == (self.round_at(task), task.execution_key))

    async def wait(self) -> None:
        while self.work:
            await asyncio.gather(*list(self.work.values()), return_exceptions=True)

    async def _load(self, task_key: str, operation_id: str) -> Task | None:
        async with self.engine.ctx.sessionmaker() as db:
            row = await db.scalar(select(Task).where(
                Task.statistics_key == task_key, Task.admin_cancel_operation_id == operation_id
            ))
            # 非本进程的 running 意图留给其执行者，或等失联后由成功认领者收尾。
            if row is not None and row.status == "running" and row.worker_key is not None:
                if not any(lease.valid and lease.worker_key == row.worker_key
                           for lease in (self.engine.lease, self.engine.request_worker)):
                    return None
                if (row.id, task_key, row.execution_key) in self.engine._adopted:
                    # C1 正在恢复该轮，等它核对完收据并移交，不能同时调用回退。
                    return None
            return row

    async def _clear_chapter_receipt(self, task: Task, operation_id: str, receipt: dict) -> Task | None:
        """只清除已证明释放成功的同一操作收据；不会再次触碰章节。"""
        async with self.engine.ctx.sessionmaker() as db:
            await project_lifecycle.lock_record(db, task.project_id)
            row = await db.scalar(select(Task).where(
                Task.id == task.id, Task.statistics_key == task.statistics_key, Task.queued_at == task.queued_at,
                Task.execution_key == receipt["completion_key"], Task.status == receipt["status"],
                Task.worker_key == task.worker_key, Task.admin_cancel_operation_id == operation_id,
            ).execution_options(populate_existing=True))
            if row is None or _chapter_receipt(row, operation_id) != receipt:
                return None
            payload = dict(row.input)
            payload.pop(_CHAPTER_RECEIPT)
            changed = await db.execute(update(Task).where(
                Task.id == task.id, Task.statistics_key == task.statistics_key, Task.queued_at == task.queued_at,
                Task.execution_key == receipt["completion_key"], Task.status == receipt["status"],
                Task.worker_key == task.worker_key, Task.admin_cancel_operation_id == operation_id,
                Task.input[_CHAPTER_RECEIPT]["operation_id"].as_string() == operation_id,
                Task.input[_CHAPTER_RECEIPT]["claim_key"].as_string() == receipt["claim_key"],
            ).values(input=payload, execution_key=None, admin_cancel_operation_id=None,
                     admin_cancel_requested_at=None, cancel_requested_at=None)
                .execution_options(synchronize_session="fetch"))
            if changed.rowcount != 1:
                await db.rollback()
                return None
            await cleanup_work.clear_chapter_failure(db, task, receipt)
            await admin_cleanup.finish(db, task, operation_id)
            try:
                await db.commit()
            except Exception:
                await db.rollback()
                async with self.engine.ctx.sessionmaker() as proof:
                    row = await proof.scalar(select(Task).where(
                        Task.id == task.id, Task.statistics_key == task.statistics_key,
                        Task.queued_at == task.queued_at, Task.status == receipt["status"],
                        Task.worker_key == task.worker_key, Task.execution_key.is_(None),
                        Task.admin_cancel_operation_id.is_(None),
                    ))
                if row is None or not isinstance(row.input, dict) or _CHAPTER_RECEIPT in row.input:
                    raise
            self.remember_completed(row)
            return row

    async def _complete_chapter(self, task: Task, operation_id: str, *, status: str) -> Task | None:
        """Task 终态、原章节释放和可恢复收据同事务提交，之后再清除屏障。"""
        from app.services.task_engine.handlers import chapter as chapter_handler

        intent = chapter_cancel_intent(task, operation_id)
        if (intent is None or not isinstance(task.input, dict) or task.input.get(_CHAPTER_INTENT) != intent
                or _CHAPTER_RECEIPT in task.input):
            return None
        claim = (chapter_handler.capture_queued_claim(task) if task.status == "queued"
                 else chapter_handler.capture_running_claim(task))
        marker = claim.execution_key or str(uuid4())
        while marker in (claim.task_key, claim.claim_key):
            marker = str(uuid4())
        receipt = {**intent, "status": status, "completion_key": marker}
        stamp = task_statistics_service.now_utc()
        async with self.engine.ctx.sessionmaker() as db:
            await project_lifecycle.lock_record(db, claim.project_id)
            changed = await db.execute(update(Task).where(
                Task.id == claim.task_id, Task.statistics_key == claim.task_key, Task.user_id == claim.actor_id,
                Task.project_id == claim.project_id, Task.queued_at == claim.queued_at,
                Task.execution_key == claim.execution_key, Task.status == task.status,
                Task.worker_key == task.worker_key,
                Task.admin_cancel_operation_id == operation_id,
                Task.input[_CHAPTER_INTENT]["claim_key"].as_string() == claim.claim_key,
            ).values(status=status, cancel_requested_at=None,
                     error="服务重启，任务已中断" if status == "interrupted" else "任务已取消",
                     stage=task_service.STAGE_LABELS[status], finished_at=stamp)
                .execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                await db.rollback()
                return None
            await admin_cleanup.check_owner(db)
            if task.status == "running" and task.worker_key is not None:
                if not await workers.lock_live_worker(db, task.worker_key):
                    return None
            if not await chapter_claim.release_chapter_claim(db, claim):
                await db.rollback()
                return None
            row = await db.scalar(select(Task).where(
                Task.id == claim.task_id, Task.statistics_key == claim.task_key,
            ).execution_options(populate_existing=True))
            if row is None or not isinstance(row.input, dict) or row.input.get(_CHAPTER_INTENT) != intent:
                await db.rollback()
                return None
            payload = dict(row.input)
            payload.pop(_CHAPTER_INTENT)
            payload[_CHAPTER_RECEIPT] = receipt
            stored = await db.execute(update(Task).where(
                Task.id == claim.task_id, Task.statistics_key == claim.task_key, Task.queued_at == claim.queued_at,
                Task.execution_key == claim.execution_key, Task.status == status, Task.worker_key == task.worker_key,
                Task.admin_cancel_operation_id == operation_id,
            ).values(input=payload, execution_key=marker).execution_options(synchronize_session=False))
            if stored.rowcount != 1:
                await db.rollback()
                return None
            await task_statistics_service.record_state_change(db, row, status, finished_at=stamp)
            try:
                await db.commit()
            except Exception:
                await db.rollback()
                async with self.engine.ctx.sessionmaker() as proof:
                    row = await proof.scalar(select(Task).where(
                        Task.id == claim.task_id, Task.statistics_key == claim.task_key,
                        Task.queued_at == claim.queued_at, Task.execution_key == marker,
                        Task.worker_key == task.worker_key, Task.status == status,
                        Task.admin_cancel_operation_id == operation_id,
                    ))
                if row is None or _chapter_receipt(row, operation_id) != receipt:
                    raise
            else:
                async with self.engine.ctx.sessionmaker() as proof:
                    row = await proof.scalar(select(Task).where(
                        Task.id == claim.task_id, Task.statistics_key == claim.task_key,
                        Task.queued_at == claim.queued_at, Task.execution_key == marker,
                        Task.worker_key == task.worker_key, Task.status == status,
                        Task.admin_cancel_operation_id == operation_id,
                    ))
                if row is None or _chapter_receipt(row, operation_id) != receipt:
                    return None
        return await self._clear_chapter_receipt(row, operation_id, receipt)

    async def _drain(self, task_key: str, operation_id: str) -> None:
        engine = self.engine
        attempt = str(uuid4())
        record = None
        token = None
        self.attempts.add(attempt)
        try:
            task = await self._load(task_key, operation_id)
            if task is None:
                self.uncertain_claims.discard((task_key, operation_id))
                return
            with engine.cleanup_scope(task):
                if task.status not in OPEN_TASK_STATUSES:
                    receipt = _chapter_receipt(task, operation_id)
                    if receipt is not None:
                        row = await self._clear_chapter_receipt(task, operation_id, receipt)
                        if row is not None:
                            await self._after_completion(row)
                        return
                    # 非正常遗留组合不能盲abort succeeded，更不能擦除恢复依据。
                    logger.error("任务取消标记与终态不一致 task_key=%s operation=%s", task_key, operation_id)
                    return
                if task.status == "queued" and task.execution_key is not None:
                    # 本人先保留原资源；只接续已证明退出的请求，不并发执行同一 abort。
                    async with engine.ctx.sessionmaker() as db:
                        await task_service.retry_project_cleanup_failures(
                            db, engine, task.project_id,
                            queued_admin=(task.id, task_key, task.queued_at, task.execution_key, operation_id),
                        )
                    return
                execution = engine._running.get(task.id)
                if task.status == "running" and execution is not None and not execution.done():
                    if engine._running_statistics_keys.get(task.id) != task_key:
                        return
                    identity = (self.round_at(task), task.execution_key, task.worker_key)
                    await engine.cancel_running(task.id, statistics_key=task_key,
                                                expected_queued_at=task.queued_at,
                                                expected_execution_key=task.execution_key)
                    # 等待窗口结束不代表完成；只要执行体仍在，绝不由外部重复abort。
                    if not execution.done():
                        return
                    task = await self._load(task_key, operation_id)
                    if (task is None or task.status != "running"
                            or (self.round_at(task), task.execution_key, task.worker_key) != identity):
                        return
                if (task.kind in _CHAPTER_KINDS | {"chapter.analyze", "chapter.batch"} and task.status == "running"
                        and (task.id in engine._claiming or task.id in engine._finalizing
                             or task.id in engine._running_execution_keys)):
                    # 登记与退出的窗口不能只凭 _running 映射缺失推断执行体已经停止。
                    return
                round_at = self.round_at(task)
                recovering = (task.status == "running"
                              and self.recovering.get(task_key) == (round_at, task.execution_key))
                if task.status == "running" and task.worker_key is None and task.recovered_at is None:
                    # 在线受理的旧无主执行通过 C1 取得处理权，另一次启动已接管的行不能在这里再领。
                    owner = engine.lease.worker_key if engine.lease.valid else engine.request_worker.worker_key
                    if owner is None:
                        return
                    async with engine.ctx.sessionmaker() as db:
                        await workers.prepare_orphan_scan(db, engine.lease_seconds)
                        if not await workers.adopt(db, task.id, task_key, task.execution_key, None, owner):
                            return
                        await admin_cleanup.reserve_adopted(db, task.id, recovery=recovering)
                        await self.commit_claim(db, task_key, operation_id)
                    task = await self._load(task_key, operation_id)
                    if task is None:
                        return
                winner = self.winners.get(task_key)
                record = await admin_cleanup.claim(engine, task, operation_id, attempt, recovery=recovering)
                self.uncertain_claims.discard((task_key, operation_id))
                if record is None:
                    return
                token = admin_cleanup.current.set(record)
                recovering = record.evidence["recovery"]
                if (task.status == "running" and winner is not None and winner[0] == round_at
                        and (winner[2] if len(winner) > 2 else None) == task.execution_key):
                    winner_task = winner[3] if len(winner) > 3 else task
                    if winner_task.worker_key is None and winner[1].get("execution_identity") is None:
                        winner_task.worker_key = task.worker_key
                    row, committed = await engine.complete_state(
                        winner_task, expected_status="running", abort_ok=True, operation_id=operation_id, **winner[1]
                    )
                    if committed:
                        self.winners.pop(task_key, None)
                        if row.status == "succeeded":
                            callback_ok = await engine._on_success(get_handler(task.kind), winner_task,
                                                                   execution=winner[1].get("execution_identity"))
                            if not callback_ok and task.kind in _CHAPTER_KINDS:
                                warning = "正文已保存，自动分析状态未确认，请查看分析任务"
                                if await engine._mark_followup_warning(winner_task, warning):
                                    row.error = warning
                            await engine.clear_finished_execution(winner_task)
                        await self._after_completion(row)
                    return
                if task.status == "running" and task.kind == "chapter.analyze":
                    # 即使没有 pending，历史执行也可能已提交后丢失确认；仅完整收据可恢复成功。
                    from app.services import analysis_service

                    try:
                        original = analysis_service.capture_analysis_completion_for_recovery(task)
                        async with engine.ctx.sessionmaker() as proof:
                            verified = await analysis_service.completed_analysis_receipt(proof, original)
                        if verified is not None:
                            row, committed = await engine.complete_state(
                                task, expected_status="running", status="succeeded", result=verified[1], error="",
                                text=task.output_text, progress=task.progress, abort_ok=True,
                                operation_id=operation_id,
                            )
                            if committed:
                                self.recovering.pop(task_key, None)
                                await engine._on_success(get_handler(task.kind), task)
                                await engine.clear_finished_execution(task)
                                await self._after_completion(row)
                                return
                    except Exception:  # noqa: BLE001  收据或存储不可核验时保留原执行和意图。
                        pass
                    if task.project_id is not None:
                        await engine.record_abort_failure(task.project_id, None, task=task)
                    engine.remember_uncertain_execution(task)
                    logger.error("管理员取消的分析执行事实待核对 task=%s", task.id)
                    return
                if task.kind.startswith("wizard.") and (
                    task.status == "running" or task_service._has_wizard_receipt(task)
                ):
                    # 原向导可能已发送或提交，孤立执行的缺证状态不能当作安全 abort。
                    if task.project_id is not None:
                        await engine.record_abort_failure(task.project_id, None, task=task)
                    engine.remember_uncertain_execution(task)
                    logger.error("管理员取消的向导执行事实待核对 task=%s", task.id)
                    return
                if task.status == "running" and task.kind == "chapter.batch":
                    # 提案需先证明原轮次。断点损坏、正文/分析收据未知时不能执行通用 abort。
                    try:
                        pending = await engine._durable_rich_review(task)
                        if pending is not None:
                            current, identity = pending
                            row, committed = await engine.complete_state(
                                current, expected_status="running", status="cancelled", result=None,
                                error="任务已取消，候选未采纳", text=current.output_text, progress=current.progress,
                                abort_ok=True, operation_id=operation_id, review_proof=identity,
                            )
                            if committed:
                                self.recovering.pop(task_key, None)
                                await self._after_completion(row)
                                return
                        elif isinstance(task.checkpoint, dict) and task.checkpoint.get("phase") == "generate":
                            from app.services import batch_service

                            # 旧执行及其独立清理均已停止后，只凭锁内原 attempt 处理。
                            current_work = asyncio.current_task()
                            if task.project_id is not None and any(
                                work is not current_work and not work.done()
                                for work in engine._cleanup_work.get(task.project_id, {})
                            ):
                                return
                            snapshot = batch_service.capture_abort_snapshot(task, recovery=True)
                            if snapshot.claim is not None:
                                interrupted = recovering
                                status = "interrupted" if interrupted else "cancelled"
                                row, committed = await engine.complete_state(
                                    task, expected_status="running", status=status, result=None,
                                    error="批量执行结果尚未确认，请核对后处理", text=task.output_text,
                                    progress=task.progress, abort_ok=True, operation_id=operation_id,
                                    batch_snapshot=snapshot, batch_uncertain_attempt=True,
                                )
                                if committed:
                                    self.recovering.pop(task_key, None)
                                    await self._after_completion(row)
                                    return
                    except Exception:  # noqa: BLE001  未证实的提案/章节收据都继续持有屏障。
                        pass
                    if task.project_id is not None:
                        await engine.record_abort_failure(task.project_id, None, task=task)
                    engine.remember_uncertain_execution(task)
                    logger.error("管理员取消的批量执行事实待核对 task=%s", task.id)
                    return
                if task.kind in _CHAPTER_KINDS and task.status in ("queued", "running"):
                    # 孤立 running 仅在单进程中确认原执行体及其他项目清理都已停止后接管。
                    if task.status == "running" and task.project_id is not None:
                        current = asyncio.current_task()
                        if any(work is not current and not work.done()
                               for work in engine._cleanup_work.get(task.project_id, {})):
                            return
                    interrupted = (task.status == "running"
                                   and recovering)
                    row = await self._complete_chapter(
                        task, operation_id, status="interrupted" if interrupted else "cancelled",
                    )
                    if row is not None:
                        self.recovering.pop(task_key, None)
                        self._forget_released_chapter_failure(task)
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
                if task.status == "running" and task.recovered_at is not None:
                    return
                if not released_review and not await admin_cleanup.abort_once(engine, task, record, handler):
                    return
                interrupted = (task.status == "running"
                               and recovering and not released_review)
                status = "interrupted" if interrupted else "cancelled"
                error = "服务重启，任务已中断" if interrupted else "任务已取消"
                row, committed = await engine.complete_state(
                    task, expected_status=task.status, status=status, result=None, error=error,
                    text=task.output_text, progress=task.progress, abort_ok=True, operation_id=operation_id,
                )
                if committed:
                    self.recovering.pop(task_key, None)
                    await self._after_completion(row)
        except Exception:
            # 不输出SQL参数、私有插件异常或上游错误；marker是唯一重试依据。
            logger.error("管理员取消安全收尾未完成 task_key=%s operation=%s", task_key, operation_id)
        finally:
            if token is not None:
                admin_cleanup.current.reset(token)
            self.attempts.discard(attempt)
            if record is not None:
                self.pending_releases[attempt] = record
                await self.retry_releases()

    def _forget_released_chapter_failure(self, task: Task) -> None:
        """本轮已按同一 claim 原子释放；仅精确同一轮次的失败快照可遗忘，不推断其他代次。"""
        engine = self.engine
        snapshot = engine._failed_cleanups.get(task.id)
        intent = task.input.get(_CHAPTER_INTENT) if isinstance(task.input, dict) else None
        if (snapshot is None or not isinstance(intent, dict) or snapshot.task_key != task.statistics_key
                or snapshot.kind != task.kind or snapshot.execution_key != task.execution_key
                or task.queued_at is None
                or chapter_claim._utc(snapshot.queued_at) != chapter_claim._utc(task.queued_at)
                or getattr(snapshot.claim, "claim_key", None) is None
                or getattr(snapshot.claim, "claim_key", None) != intent.get("claim_key")):
            return
        engine.forget_abort_failure(snapshot)

    async def _after_completion(self, row: Task) -> None:
        """真实提交后恢复正常历史裁剪和所有者事件；不再进行任何旧资源abort。"""
        engine = self.engine
        out = task_service.out_json(row)
        event = "done" if row.status == "succeeded" else "paused" if row.status == "paused" else "error"
        payload = {"task": out, "result": row.result} if event != "error" else {"task": out, "detail": row.error}
        engine.close_hub(row.id, event, payload, statistics_key=row.statistics_key, queued_at=row.queued_at)
        engine.notify()
        async with engine.ctx.sessionmaker() as db:
            await task_service.prune_finished(db, row.user_id)
