"""TaskEngine：进程内调度协程 + 任务执行。

- tasks 表是唯一事实来源（数据库即队列）。调度器按 id 升序扫描 queued 任务，受全局 / 每用户并发上限约束，
  用条件 UPDATE（status='queued' → 'running'）领取，影响行数为 1 才算领到，然后 asyncio.create_task 执行。
- 每次启动登记新的执行者租约，领取时记录归属；恢复只接管已失去执行者的任务。
- 正常领取与并发名额一起提交。当前部署仍使用单进程，跨进程取消和实时流转发另行处理。
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
import json
import logging
import sqlite3
import time
from collections import Counter, deque
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, aclosing, contextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import String, cast, func, select, update
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.task_engine.handlers  # noqa: F401  导入即注册内置任务类型
from app.core.errors import AppError, ConflictError
from app.models import Chapter, Project, ProjectCollaborator, Task, TaskExecutionSlot, User
from app.models.entities import ACTIVE_TASK_STATUSES, FINISHED_TASK_STATUSES
from app.plugins import state as plugin_state
from app.plugins.registry import EMPTY, use_registry
from app.services import (
    analysis_service,
    chapter_claim,
    generation_service,
    kb_index_service,
    project_lifecycle,
    quota_service,
    task_service,
    task_statistics_service,
)
from app.services import authorization_service as authz
from app.services.ai_dispatch import (
    VisibilityChangedError,
    VisibilityPolicyError,
    is_sqlite_busy,
    lock_visibility_write,
)
from app.services.collaboration_task_access import (
    GRANT_KEY,
    task_grant_identity,
    task_requires_owner_ai,
    task_visible_to_actor,
)
from app.services.system_settings_snapshot import effective_settings
from app.services.task_engine import (
    admin_cleanup,
    cleanup_work,
    execution_slots,
    execution_work,
    owner_cancellation,
    workers,
)
from app.services.task_engine.cancellation import AdminCancellationDrain
from app.services.task_engine.execution_identity import (
    TaskExecutionIdentity,
    from_queued_task,
    frozen_project_owner_id,
    matches_running,
    require_live_worker,
    require_write_execution,
    same_execution,
)
from app.services.task_engine.hub import Subscriber, TaskHub
from app.services.task_engine.output_flush import OutputFlushTimer, OutputSnapshot
from app.services.task_engine.peer_handoff import ClaimHandoff, HandoffUnavailable, PeerHandoffs
from app.services.task_engine.peer_transport import PeerTransport
from app.services.task_engine.peer_wake import PeerWakes
from app.services.task_engine.registry import Event, TaskContext, TaskHandler, get_handler
from app.services.task_engine.request_worker import RequestWorker

logger = logging.getLogger(__name__)

INTERRUPTED_MESSAGE = "服务重启，任务已中断"
# 受控策略代码只映射到服务端固定提示；其余输出门禁失败一律不透传异常正文。
_POLICY_MESSAGES = {
    "entity_source_changed": "设定来源已变化，请重新发起",
    "scene_context_changed": "场景或相关设定已变化，请核对后重新生成",
    # ai_dispatch 的固定文案受控错误；可见性收紧需要明确反馈（setting-ai-visibility 错误矩阵）。
    "ai_visibility_changed": "AI 可见设置已变更，已停止发送，请按当前设置重试",
    "ai_visibility_busy": "AI 请求正在发送，本次操作未完成，请稍后重试",
    "ai_history_sealed": "AI 可见设置已收紧，旧会话只可查看，请新开会话继续",
    # collaboration_ai_guard.CollaborationSendBlocked：发送或保存前锁内重验原执行/邀请轮次/模型授权失败。
    "permission_denied": "权限或模型授权已变化，已停止发送",
    # 事件发布/输出刷新前的锁内核验未通过（原执行身份、作品授权或邀请轮次已变化）；不透传底层异常。
    "task_output_changed": "任务输出授权已变化，已停止",
    # analysis_service.AnalysisExecutionChanged：分析写回/派生前锁内重验原执行、邀请轮次或模型来源失败（E4-g1）。
    "analysis_execution_changed": "分析任务执行身份或授权已变化，已停止",
}


def _receipt_storage_failure(exc: Exception) -> bool:
    """已知的业务凭据缺损可保留待核对，存储异常交给原接管者重试。"""
    if isinstance(exc, AppError):
        return exc.status_code >= 500 or isinstance(exc.__context__, DBAPIError)
    return not isinstance(exc, (TypeError, ValueError, RecursionError, analysis_service.AnalysisCommitUncertain,
                                generation_service.ChapterCommitUncertain))


def _policy_code(exc: BaseException) -> str | None:
    # 只读取受控 code，并输出上表的固定文案；不透传异常正文。
    from app.services.agent.history import HistorySealedError

    code = (exc.extra or {}).get("code") if isinstance(exc, VisibilityPolicyError | HistorySealedError) else None
    return code if code in _POLICY_MESSAGES else None
CANCELLED_MESSAGE = "任务已取消"
TASK_GONE_MESSAGE = "任务不存在"
TASK_ACCESS_LOST_MESSAGE = "任务访问权限已失效"
_COLLABORATION_CHECK_EVENT = "__collaboration_check__"
# 协作授权失效时系统按原任务 UUID/入队轮次收尾的固定文案（不含成员、模型或正文信息）。
# 单章保存事务已提交（持久完成收据可证）、但完成事件/终态核验因授权或设置变化未通过时的受限系统收尾：
# 只把原轮次 Task 记为成功并释放活动资源；不发布结果、不派生后续任务、不写正文或向外发送。
RECEIPT_FINALIZED_NOTE = "正文已保存；执行授权或设置已变化，结果未发布，也未派生后续任务"
RECEIPT_FINALIZED_DETAIL = "正文已保存，请刷新后查看"
# 章节分析的同类受限收尾（E4-g1）：分析/设定回写/剧情记忆已在同一事务提交，结果按收据逐字重建后保存。
ANALYSIS_RECEIPT_FINALIZED_NOTE = "分析已保存；执行授权或设置已变化，未派生后续任务"
ANALYSIS_RECEIPT_FINALIZED_DETAIL = "分析已保存，请刷新后查看"
# 批量的同类受限收尾（E4-g2）：所有章节步骤已由本轮执行按原断点 CAS 持久化，但完成事件/终态核验因授权或
# 设置变化未通过；结果由持久断点逐字重建保存，不发布、不派生。
BATCH_RECEIPT_FINALIZED_NOTE = "批量已完成；执行授权或设置已变化，结果未发布"
BATCH_RECEIPT_FINALIZED_DETAIL = "批量已完成，请刷新后查看"
# 开书向导的同类受限收尾（E4-g3c）：向导直接写库，内容替换与完成收据同一事务提交；完成事件/终态核验因撤权、降级、
# 关作者模型授权或改密失败时，结果按收据逐字保存为成功（不发布、不派生、不写内容、不向外发送）。
WIZARD_RECEIPT_FINALIZED_NOTE = "向导结果已保存；执行授权或设置已变化，结果未发布"
WIZARD_RECEIPT_FINALIZED_DETAIL = "向导结果已保存，请刷新后查看"
_WIZARD_KINDS = frozenset({"wizard.outline", "wizard.characters", "wizard.world", "wizard.careers"})
# 收据受限收尾（E5-d）的成功终态帧：done 携带任务快照，但 result 固定为空并附「结果未发布」说明。
# 只按本类型的固定 NOTE 识别（NOTE 只由对应受限收尾函数写入），实时收尾与回放共用。
_RECEIPT_UNPUBLISHED = (
    (frozenset({"chapter.generate", "chapter.polish", "chapter.revise"}), RECEIPT_FINALIZED_NOTE,
     RECEIPT_FINALIZED_DETAIL),
    (frozenset({"chapter.analyze"}), ANALYSIS_RECEIPT_FINALIZED_NOTE, ANALYSIS_RECEIPT_FINALIZED_DETAIL),
    (frozenset({"chapter.batch"}), BATCH_RECEIPT_FINALIZED_NOTE, BATCH_RECEIPT_FINALIZED_DETAIL),
    (_WIZARD_KINDS, WIZARD_RECEIPT_FINALIZED_NOTE, WIZARD_RECEIPT_FINALIZED_DETAIL),
)


def receipt_unpublished_detail(task: Task) -> str | None:
    """已提交的收据收尾成功行（status=succeeded 且 error 为本类型固定 NOTE）→ 对外说明；其余 None。"""
    if task.status != "succeeded":
        return None
    for kinds, note, detail in _RECEIPT_UNPUBLISHED:
        if task.kind in kinds and task.error == note:
            return detail
    return None


COLLABORATION_FAILURE_MESSAGES = {
    "collaboration_revoked": "协作授权已失效，任务已停止",
    "collaboration_model_revoked": "作者模型授权已关闭，任务已停止",
    "collaboration_account_unavailable": "账号状态已变化，任务已停止",
}
# 排队中协作任务失败关闭时可原子收尾的类型；其余（拆书）保持原屏障并记录待核对。
# agent.turn（E4-i 裁决 11）：与终态同事务作废本轮待确认提案（agent.service.void_revoked_turn_changes）。
_COLLABORATION_QUEUED_CLEANUP_KINDS = frozenset({
    "chapter.generate", "chapter.polish", "chapter.revise", "chapter.batch", "chapter.analyze", "kb.index",
    "outline.continue", "outline.expand", "character.generate", "organization.generate", "career.generate",
    "relations.parse", "relations.generate", "wizard.outline", "wizard.characters", "wizard.world", "wizard.careers",
    "agent.turn", "consistency.check", "consistency.impact", "scene.restructure",
})
# 待审核协作任务在降级/撤权/轮次失效后可由系统受限收尾的类型（E5-a；Agent 待确认轮次 E4-i 裁决 11）：
# 知识库索引不会暂停，拆书不在白名单。
_COLLABORATION_PAUSED_CLEANUP_KINDS = _COLLABORATION_QUEUED_CLEANUP_KINDS - {"kb.index"}
_QUEUED_RECEIPT_INPUT_KEYS = ("_admin_chapter_cancel", "_admin_chapter_receipt", "_external_execution",
                              "_external_snapshot_mac", "_batch_outcome_unknown", "_wizard_pending",
                              "_wizard_completion")
_QUEUED_RECEIPT_CHECKPOINT_KEYS = ("_analysis_pending", "_analysis_completion", "_chapter_claim",
                                   "_batch_completion", "rich_analysis", "_external_execution",
                                   "_external_snapshot_mac", "_batch_outcome_unknown", "_wizard_pending",
                                   "_wizard_completion")
_REDACTED_EVENT = "__private_event_redacted__"
_UNSET_BINDING = object()
_SINGLE_CHAPTER_KINDS = frozenset({"chapter.generate", "chapter.polish", "chapter.revise"})
# 只产出草稿的任务：唯一持久化是 Task 自身的终态与 result（在终态事务内锁内重验后写入），没有已提交的业务副作用。
# 成功终态核验失败（撤权/降级/关模型授权/改密/可见性变化）时可按原执行改记无结果失败并释放活动资源（E4-g3a 大纲；
# E4-g3b 实体 AI：五类设定草稿同样只写 Task 终态，人工确认后才经 content.edit 接口保存）。
# consistency.check 同样登记：台账与问题在各自事务内锁内重验后提交、无需证明也不回退，Task 终态只保存概要；
# 终态核验失败时改记无结果失败并释放 consistency:{project}，已保存的台账与问题保留。
# consistency.impact（修改影响评估）同理：台账与影响记录在各自事务内锁内重验后提交，Task 终态只保存概要。
_DRAFT_ONLY_KINDS = frozenset({
    "outline.continue", "outline.expand", "character.generate", "organization.generate", "career.generate",
    "relations.parse", "relations.generate", "consistency.check", "consistency.impact", "scene.restructure",
})
# SSE 心跳帧标记（app.api.sse 渲染为注释帧 ": ping"）
HEARTBEAT_EVENT = ""

# 这些 build_job 只从原始请求/当前数据库组装上下文。仅准备阶段可重建一次，
# 绝不重跑已开始的 runner，也不重播 Agent 或已生成的多轮副本。
_REBUILDABLE_VISIBILITY_JOBS = frozenset({
    "chapter.generate", "chapter.revise", "chapter.analyze", "outline.continue", "outline.expand",
    "character.generate", "organization.generate", "career.generate", "relations.parse", "relations.generate",
    "wizard.outline", "wizard.characters", "wizard.world", "wizard.careers",
})


@dataclass(frozen=True, slots=True, repr=False)
class AbortSnapshot:
    """收尾失败时冻结的原始身份；不能用后来同 ID 的任务替换。"""

    task_id: int
    task_key: str
    actor_id: int
    project_id: int | None
    kind: str
    chapter_id: int | None
    resource_key: str | None
    queued_at: datetime
    execution_key: str | None
    status: str
    input_json: str
    checkpoint_json: str
    step_cursor: int
    claim: Any
    worker_key: str | None

    def task(self) -> Task:
        return Task(id=self.task_id, statistics_key=self.task_key, user_id=self.actor_id, project_id=self.project_id,
                    kind=self.kind, chapter_id=self.chapter_id, resource_key=self.resource_key,
                    queued_at=self.queued_at, execution_key=self.execution_key, worker_key=self.worker_key,
                    status=self.status,
                    input=json.loads(self.input_json), checkpoint=json.loads(self.checkpoint_json),
                    step_cursor=self.step_cursor)


async def _finish_before_cancel(coro) -> Any:
    """build_job、事件核验与输出刷新含持锁数据库事务：取消（用户取消、协作撤权复查）不能打断在途游标。

    aiosqlite 等驱动被取消的语句仍在线程中执行，连接若随取消被丢弃会遗留写事务，
    使随后按原 claim 的受限回退永久拿不到锁。先等本步结束（其结果或异常被丢弃，不发布未完成的核验、
    不使用刚组装的 job），再把取消原样传递给 _run 收尾。
    """
    step = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(step)
    except asyncio.CancelledError:
        with suppress(Exception):
            await step
        raise


class _UnprovenRecoveryResource(Exception):
    """原始业务凭据与锁内资源不符，清理事务须全部回滚。"""


class TaskEngine:
    # output_text / 进度节流刷库间隔（秒）
    flush_interval = 2.0
    # 订阅流空闲时的心跳间隔（秒）
    heartbeat_seconds = 15.0
    # 调度器兜底轮询间隔（秒）；正常由 notify() 唤醒
    poll_interval = 5.0
    heartbeat_interval = 5.0
    lease_seconds = 30.0
    lease_safety_seconds = 5.0
    # 取消运行中任务时等待其收尾的上限（秒）
    cancel_wait_seconds = 10.0
    # 待审核协作任务授权失效的数据库全量复查间隔（秒）；本进程撤权通知会立即复查对应成员。
    collaboration_scan_interval: float | None = None  # 默认随调度器绝对轮询期限运行
    # 全量复查的键集分页大小（每批一个短只读会话）。
    collaboration_scan_batch = 100
    # 受控订阅恰逢领取提交、引擎尚未登记领取来源时，等待该来源的上限（秒，E5-e）；超时失败关闭。
    claim_source_wait_seconds = 5.0

    def __init__(self, ctx: TaskContext):
        self.ctx = ctx
        self.feature_plugins = ctx.feature_plugins or EMPTY
        self.peer_wakes = PeerWakes(self)
        self.ctx.task_engine = self
        self.hubs: dict[int, TaskHub] = {}
        self.peer_handoffs = PeerHandoffs()
        self._running: dict[int, asyncio.Task[None]] = {}
        self._running_statistics_keys: dict[int, str | None] = {}
        self._running_execution_keys: dict[int, str] = {}
        self._running_projects: dict[int, int | None] = {}
        self._cleanup_work: dict[int, dict[asyncio.Task, int]] = {}
        self._abort_failures: set[int] = set()
        self._durable_abort_failures: dict[str, tuple[int, AbortSnapshot | None]] = {}
        self._failed_cleanups: dict[int, AbortSnapshot] = {}
        self._unproven_abort_failures: set[int] = set()
        self._uncertain_executions: set[tuple[int, str | None, datetime | None, str | None]] = set()
        self._running_users: dict[int, int] = {}
        self._running_rounds: dict[int, Any] = {}
        self._claiming: set[int] = set()
        self.runtime = authz.runtime_for_factory(ctx.sessionmaker)
        self._running_serial: dict[int, str] = {}
        self._cancel_requested: set[int] = set()
        self._shutdown: set[int] = set()
        self._finalizing: set[int] = set()
        self._wake: asyncio.Event | None = None
        self._legacy_unproven_logged: set[tuple[int, str | None]] = set()
        # 领取在锁内确认无法前进（旧任务不可证明、handler 未就绪等）的排队任务：task_id → (UUID, 入队轮次)。
        # 本进程不因自身执行体结束而立即重排它们（避免 _on_done → notify 热循环反复取写锁）；
        # 外部 notify() 或兜底轮询超时会清空，届时按数据库事实重新领取，不掩盖已变为可处理的情形。
        self._stalled: dict[int, tuple[str | None, Any]] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self.lease = workers.WorkerLease(self)
        self.peer = PeerTransport(self)
        self.peer.on_wake = self.peer_wakes.receive
        self.request_worker = RequestWorker(self)
        self._project_executions: dict[int, TaskExecutionIdentity] = {}
        self._exit_confirmations: set[asyncio.Task] = set()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._heartbeat_stop = asyncio.Event()
        self._lease_lost = False
        self._lifecycle_lock = asyncio.Lock()
        self._reap_lock = asyncio.Lock()
        self._adopted: set[tuple[int, str | None, str | None]] = set()
        self._stopping = False
        self.admin_cancellations = AdminCancellationDrain(self)
        self._collaboration_subscribers: dict[tuple[int, int], set[Subscriber]] = {}
        # 数据库复查确认协作授权失效后登记的运行体（task_id → 原 UUID 与执行键），取消后按失败收尾。
        self._collaboration_revoked: dict[int, tuple[str, str, str]] = {}
        self._collaboration_checks: set[asyncio.Task] = set()
        # 待审核协作任务的全量复查（E5-a）：上次启动时刻、在途扫描、串行锁与不可证明任务的去重日志。
        self._paused_scan_at: float | None = None
        self._paused_scans: set[asyncio.Task] = set()
        self._pending_paused_scans: set[tuple[int | None, int | None]] = set()
        self._pending_run_checks: dict[tuple[int, int], set[int]] = {}
        self._paused_scan_lock = asyncio.Lock()
        self._paused_unproven_logged: set[tuple[int, str | None, Any]] = set()
        # 本进程原执行体产出的单章 done 载荷（task_id → (执行键, 载荷)），仅供收据收尾比对 result_hash。
        self._done_candidates: dict[int, tuple[str | None, dict[str, Any]]] = {}

    # ---------------- 生命周期 ----------------

    @property
    def running(self) -> bool:
        return self._loop_task is not None

    async def start(self) -> None:
        with use_registry(self.feature_plugins):
            await self._start()

    async def _start(self) -> None:
        async with self._lifecycle_lock:
            if self._loop_task is not None:
                return
            self._wake = asyncio.Event()
            self._stopping = self._lease_lost = False
            self._paused_scan_at = None
            # 正在发送的 SQLite 实例可能持有主库写锁直到模型返回响应头。
            # 在现有请求超时范围内用新事务重新登记，不中断另一实例。
            deadline = time.monotonic() + self.ctx.settings.ai_request_timeout
            while True:
                self._heartbeat_stop = asyncio.Event()
                try:
                    await task_statistics_service.initialize_tracking(self.ctx.sessionmaker)
                    await self.lease.register()
                    self._heartbeat_task = asyncio.create_task(self._heartbeat(), name="task-engine-heartbeat")
                    await self.recover()
                    await self.scan_admin_cancellations()
                    break
                except (OperationalError, sqlite3.OperationalError) as exc:
                    await self._stop_worker()
                    remaining = deadline - time.monotonic()
                    if not is_sqlite_busy(exc) or remaining <= 0:
                        raise
                    await asyncio.sleep(min(1.0, remaining))
                except BaseException:
                    await self._stop_worker()
                    raise
            self._loop_task = asyncio.create_task(self._scheduler(), name="task-engine-scheduler")
            self.notify()

    def _mark_lease_lost(self) -> None:
        if self._lease_lost:
            return
        self._lease_lost = True
        self.lease.invalidate()
        self.peer_handoffs.clear()
        logger.warning("执行者租约已失效，停止领取与执行")
        for task_id, task in self._running.items():
            # 活游标或正在退出的执行体由其已有写屏障退出；再次 cancel 会打断驱动清理。
            if task_id not in self._claiming and task_id not in self._finalizing and not task.cancelling():
                task.cancel()
        self.notify()

    async def _heartbeat(self) -> None:
        while not self._heartbeat_stop.is_set():
            try:
                async with asyncio.timeout(self.heartbeat_interval):
                    await self._heartbeat_stop.wait()
                return
            except TimeoutError:
                pass
            if self._lease_lost:
                continue
            key = self.lease.worker_key
            try:
                if (not await self.lease.heartbeat() and key == self.lease.worker_key
                        and not self._heartbeat_stop.is_set()):
                    self._mark_lease_lost()
            except Exception:  # noqa: BLE001  事务已回滚，下个周期重新连接。
                logger.warning("执行者心跳未确认")
                if key == self.lease.worker_key and not self._heartbeat_stop.is_set() and not self.lease.valid:
                    self._mark_lease_lost()

    async def _replace_lost_worker(self) -> None:
        self._mark_lease_lost()
        active = list(self._running.values())
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        for hub in self.hubs.values():
            hub.close("error", {"task": None, "detail": INTERRUPTED_MESSAGE})
        self.hubs.clear()
        self._running_execution_keys.clear()
        self._adopted.clear()
        self.admin_cancellations.recovering.clear()
        self._done_candidates.clear()
        if self._stopping:
            return
        await self.lease.stop()
        if self._stopping:
            return
        await self.lease.register()
        self._lease_lost = False
        self.notify()

    async def _stop_worker(self) -> None:
        # 本地执行与收尾均已排空。让在途心跳结束后再持久标记停止，避免晚到的应答影响下一代。
        self.peer_handoffs.clear()
        self._heartbeat_stop.set()
        if self._heartbeat_task is not None:
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
        if self._exit_confirmations:
            await asyncio.gather(*self._exit_confirmations)
        await self.lease.stop()

    async def stop(self) -> None:
        """停止调度；运行中的任务标记为关停中断后取消（→ interrupted），等待其收尾。"""
        async with self._lifecycle_lock:
            await self._stop_tasks()
            await self._stop_worker()

    async def _stop_tasks(self) -> None:
        loop_task, self._loop_task = self._loop_task, None
        self._stopping = True
        if loop_task is not None:
            # 让调度器在两次领取之间退出，避免领取（已提交 running）后来不及启动执行
            self.notify()
            try:
                await asyncio.wait_for(loop_task, timeout=5)
            except TimeoutError:
                logger.warning("任务调度器未能按时退出，强制取消")
                with suppress(asyncio.CancelledError):
                    await loop_task
        checks = [work for work in self._collaboration_checks if not work.done()]
        for work in checks:
            work.cancel()
        if checks:
            await asyncio.gather(*checks, return_exceptions=True)
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
        self._pending_paused_scans.clear()
        self._pending_run_checks.clear()

    def notify(self) -> None:
        # 外部事件可能让此前无法前进的排队任务变得可处理：清空本进程退避记录后重新领取。
        self._stalled.clear()
        if self._wake is not None:
            self._wake.set()

    def notify_queue_changed(self) -> None:
        """业务队列变更已提交；唤醒本机并提示对端重查。"""
        self.notify()
        self.peer_wakes.schedule()

    def _mark_stalled(self, task_id: int, statistics_key: str | None, queued_at: Any) -> None:
        self._stalled[task_id] = (statistics_key, queued_at)

    def capture_abort_snapshot(
        self, task: Task, *, claim: Any = None, recovery: bool = False,
    ) -> AbortSnapshot | None:
        """捕获原 Task 和具体资源凭据；格式缺损时仅保留项目级未证明屏障。"""
        try:
            if (type(task.id) is not int or type(task.user_id) is not int or (type(task.project_id) is not int
                    and not (recovery and task.project_id is None and task.status == "queued" and task.execution_key))
                    or not isinstance(task.statistics_key, str) or str(UUID(task.statistics_key)) != task.statistics_key
                    or not isinstance(task.queued_at, datetime)):
                return None
            if task.kind in _SINGLE_CHAPTER_KINDS:
                from app.services.task_engine.handlers import chapter as chapter_handler

                actual = (chapter_handler.capture_queued_claim(task) if task.status == "queued"
                          else chapter_handler.capture_running_claim(task))
                if claim is not None and claim != actual:
                    return None
                claim = actual
            elif task.kind == "chapter.batch":
                from app.services import batch_service

                claim = batch_service.capture_abort_snapshot(task, recovery=recovery)
            elif task.kind == "kb.index" and task_service._request_dict(task).get("mode") == "document":
                actual = kb_index_service.running_document_claim(task) if task.execution_key is not None else claim
                if (not isinstance(actual, kb_index_service.DocumentIndexClaim)
                        or (claim is not None and claim != actual)):
                    return None
                claim = actual
            queued = (task.queued_at.replace(tzinfo=UTC) if task.queued_at.tzinfo is None
                      else task.queued_at.astimezone(UTC))
            original_input = claim.input if task.kind == "chapter.batch" else task.input
            original_checkpoint = claim.checkpoint if task.kind == "chapter.batch" else task.checkpoint
            original_cursor = claim.cursor if task.kind == "chapter.batch" else task.step_cursor
            return AbortSnapshot(
                task.id, task.statistics_key, task.user_id, task.project_id, task.kind, task.chapter_id,
                task.resource_key, queued, task.execution_key, task.status,
                json.dumps(original_input, sort_keys=True, separators=(",", ":")),
                json.dumps(original_checkpoint, sort_keys=True, separators=(",", ":")), original_cursor, claim,
                task.worker_key,
            )
        except Exception:  # noqa: BLE001  旧任务不完整时不得根据状态推断资源归属。
            return None

    def remember_abort_failure(self, project_id: int, snapshot: AbortSnapshot | None) -> None:
        self._abort_failures.add(project_id)
        if snapshot is not None:
            previous = self._failed_cleanups.get(snapshot.task_id)
            if previous is None or previous == snapshot:
                self._failed_cleanups[snapshot.task_id] = snapshot
            else:
                self._unproven_abort_failures.add(project_id)
        else:
            self._unproven_abort_failures.add(project_id)

    @staticmethod
    def _same_accepted_cancellation(execution: TaskExecutionIdentity, task: Task) -> bool:
        operation = task.admin_cancel_operation_id
        if operation is None:
            return False
        if task.kind in _SINGLE_CHAPTER_KINDS:
            from app.services.task_engine.cancellation import _CHAPTER_INTENT, chapter_cancel_intent

            intent = chapter_cancel_intent(task, operation)
            if intent is None or not isinstance(task.input, dict) or task.input.get(_CHAPTER_INTENT) != intent:
                return False
            expected = {**execution.input, _CHAPTER_INTENT: intent}
            execution = replace(execution, input_json=json.dumps(expected, ensure_ascii=False, sort_keys=True,
                                                                 separators=(",", ":"), allow_nan=False))
        return matches_running(execution, task, cancellation_operation=operation)

    async def _record_uncertain_claim(self, execution: TaskExecutionIdentity) -> None:
        original = Task(id=execution.task_id, project_id=execution.project_id, statistics_key=execution.task_key,
                        queued_at=execution.queued_at, execution_key=execution.execution_key,
                        worker_key=execution.worker_key)
        await self.record_abort_failure(execution.project_id, None, task=original)

    async def record_abort_failure(
        self, project_id: int | None, snapshot: AbortSnapshot | None, *, task=None, registration_rolled_back=False,
    ) -> None:
        if await cleanup_work.record_failure(
            self, project_id, snapshot, task=task, registration_rolled_back=registration_rolled_back,
        ) and project_id is not None:
            self.remember_abort_failure(project_id, snapshot)

    async def clear_abort_failure(self, snapshot: AbortSnapshot | None) -> None:
        if snapshot is not None and self._failed_cleanups.get(snapshot.task_id) == snapshot:
            async with self.ctx.sessionmaker() as db:
                await cleanup_work.clear_proven_failure(db, snapshot)
                await db.commit()
            self.forget_abort_failure(snapshot)

    def has_cleanup_failure(self, project_id: int) -> bool:
        """三类证据独立判断；任何单个缓存意外清除都不能放开项目删除。"""
        return (project_id in self._abort_failures or project_id in self._unproven_abort_failures
                or any(item.project_id == project_id for item in self._failed_cleanups.values()))

    def remember_uncertain_execution(self, task: Task) -> None:
        self._uncertain_executions.add((
            task.id, task.statistics_key, self.admin_cancellations.round_at(task), task.execution_key,
        ))

    async def record_uncertain_execution(self, task: Task) -> None:
        self.remember_uncertain_execution(task)
        await self.record_abort_failure(task.project_id, None, task=task)

    def execution_is_uncertain(self, task: Task) -> bool:
        return task.recovered_at is not None or (
            task.id, task.statistics_key, self.admin_cancellations.round_at(task), task.execution_key,
        ) in self._uncertain_executions

    def live_output_hub(
        self, task_id: int, *, task_key: str | None, status: str, queued_at: datetime | None,
        execution_key: str | None, worker_key: str | None, batch_cursor: int | None = None,
    ) -> TaskHub | None:
        """只为已确认的本地执行轮次提供实时文本与进度。入队或订阅也能创建 hub。"""
        if (status != "running" or task_key is None or execution_key is None or worker_key is None
                or worker_key != self.lease.worker_key or not self.lease.valid
                or self._running_statistics_keys.get(task_id) != task_key
                or self._running_rounds.get(task_id) != queued_at
                or self._running_execution_keys.get(task_id) != execution_key):
            return None
        runner, hub = self._running.get(task_id), self.hubs.get(task_id)
        if (runner is None or runner.done() or hub is None or hub.closed or hub.status != "running"
                or hub.statistics_key != task_key
                or hub.queued_at != queued_at or hub.execution_key != execution_key):
            return None
        current = getattr(hub.source, "execution", hub.source)
        if (not isinstance(current, TaskExecutionIdentity) or current.task_id != task_id
                or current.task_key != task_key or current.execution_key != execution_key
                or current.worker_key != worker_key):
            return None
        if batch_cursor is not None:
            for source in (hub.source, hub.snapshot_source()):
                execution = getattr(source, "execution", None)
                if execution is None or execution.step_cursor != batch_cursor:
                    return None
        return hub

    def forget_abort_failure(self, snapshot: AbortSnapshot | None) -> None:
        if snapshot is None or self._failed_cleanups.get(snapshot.task_id) != snapshot:
            return
        self._failed_cleanups.pop(snapshot.task_id, None)
        if (snapshot.project_id not in self._unproven_abort_failures
                and not any(item.project_id == snapshot.project_id for item in self._failed_cleanups.values())):
            self._abort_failures.discard(snapshot.project_id)

    async def _durable_rich_review(self, task: Task | None) -> tuple[Task, dict[str, Any]] | None:
        """仅认同一个不可复用 task UUID 与事务内已落库的提案/轮次。"""
        if task is None or task.statistics_key is None:
            return None
        from app.services.rich_document_review import recover_pending_identity

        async with self.ctx.sessionmaker() as db:
            row = await db.scalar(select(Task).where(
                Task.id == task.id, Task.statistics_key == task.statistics_key,
                Task.queued_at == task.queued_at, Task.execution_key == task.execution_key,
                Task.worker_key == task.worker_key,
            ))
            if row is None or row.status != "running":
                return None
            identity = await recover_pending_identity(db, row)
            return (row, identity) if identity is not None else None

    async def recover(self) -> None:
        """启动时立即处理孤立任务，随后扫描超过阈值的外部调用。"""
        await self._reap_orphans()

    async def recover_requested_round(
        self, task_id: int, task_key: str, queued_at: datetime | None, execution_key: str | None, worker: str,
    ) -> bool | Exception:
        """没有后台调度器时，由请求执行者接管一条已获准取消的原轮次。"""
        same_round = (Task.id == task_id, Task.statistics_key == task_key, Task.queued_at == queued_at)
        original = (*same_round, Task.execution_key == execution_key, Task.status == "running",
                    Task.recovered_at.is_(None), Task.cancel_requested_at.is_not(None))
        admin_handoff = select(Task.admin_cancel_operation_id).where(*same_round, Task.worker_key == worker)
        token = (task_id, task_key, execution_key)
        async with self._reap_lock:
            if not self.request_worker.valid or self.request_worker.worker_key != worker:
                return False
            async with self.ctx.sessionmaker() as db:
                row = (await db.execute(select(Task.worker_key).where(*original))).one_or_none()
                if row is None:
                    operation = await db.scalar(admin_handoff)
                    if operation is not None:
                        # 管理员章节收据可能已更换执行键，操作本身仍需排空。
                        self.request_admin_cancel(task_key, operation)
                    return operation is None
                previous = row.worker_key
                if previous not in (None, worker):
                    orphan = await workers.prepare_orphan_scan(db, self.lease_seconds, worker_key=previous)
                    candidate = await db.scalar(select(Task.id).where(*original, Task.worker_key == previous, orphan))
                    await db.commit()
                    if candidate is None:
                        return False
            self._adopted.add(token)
            try:
                if previous != worker:
                    async with self.ctx.sessionmaker() as db:
                        # 原轮次核对与 C1 的认领在同一事务，覆盖扫描后重新入队的窗口。
                        if await db.scalar(select(Task.id).where(*original, Task.worker_key == previous)
                                           .with_for_update(skip_locked=True)) is None:
                            return False
                        if not await workers.adopt(db, task_id, task_key, execution_key, previous, worker):
                            return False
                        await admin_cleanup.reserve_adopted(db, task_id, recovery=True)
                        await db.commit()
                async with self.ctx.sessionmaker() as db:
                    owned = await db.scalar(select(Task).where(*original, Task.worker_key == worker))
                    if owned is not None and not await workers.lock_live_worker(db, worker, read_only=True):
                        return False
                if owned is None:
                    return True
                try:
                    if not await self._recover_adopted(owned):
                        return False
                except Exception as exc:
                    # 此时已重新证明本 worker 持有原轮次；让仍等待的请求核对持久状态后获知业务收尾错误。
                    return exc
            finally:
                self._adopted.discard(token)
        async with self.ctx.sessionmaker() as db:
            operation = await db.scalar(admin_handoff)
        if operation is not None:
            # 旧 drain 可能在接管期间看到 _adopted 而退出；归还后唤醒并保留定向重查。
            self.request_admin_cancel(task_key, operation)
            return False
        return True

    async def _reap_orphans(self) -> None:
        async with self._reap_lock:
            me = self.lease.worker_key
            if me is None or not self.lease.valid:
                return
            sm = self.ctx.sessionmaker
            async with sm() as db:
                orphan = await workers.prepare_orphan_scan(db, self.lease_seconds)
                candidates = (await db.execute(select(
                    Task.id, Task.statistics_key, Task.execution_key, Task.worker_key,
                ).where(orphan).order_by(Task.id))).all()
                await db.commit()
            from app.services.recycle_bin_service import recover_pending_deletions

            await recover_pending_deletions(sm, workers_checked=True)
            for task_id, task_key, execution_key, previous in candidates:
                if self.lease.worker_key != me or not self.lease.valid:
                    return
                try:
                    async with sm() as db:
                        if not await workers.adopt(db, task_id, task_key, execution_key, previous, me):
                            continue
                        await admin_cleanup.reserve_adopted(db, task_id, recovery=True)
                        # 即便提交确认丢失，也会在下面按原轮次查回本次认领。
                        self._adopted.add((task_id, task_key, execution_key))
                        await db.commit()
                except Exception:  # noqa: BLE001  下次重新查库；一条记录的暂错不能阻塞其他任务的收尾。
                    logger.error("孤立任务认领暂未完成 task_key=%s", task_key)
            for token in sorted(self._adopted.copy(), key=lambda t: t[0]):
                if self.lease.worker_key != me or not self.lease.valid:
                    return
                task_id, task_key, execution_key = token
                try:
                    async with sm() as db:
                        task = await db.scalar(select(Task).where(
                            Task.id == task_id, Task.statistics_key == task_key,
                            Task.execution_key == execution_key, Task.worker_key == me,
                            Task.status == "running", Task.recovered_at.is_(None),
                        ))
                    if task is None or await self._recover_adopted(task):
                        self._adopted.discard(token)
                except Exception:  # noqa: BLE001  存储暂不可用时保留认领、下个周期重试；SQL 参数可能包含正文。
                    logger.error("任务接管暂未完成 task_key=%s", task_key)
            await self._recover_external()

    async def _recover_external(self) -> None:
        from app.services.agent.service import recover_external

        async with self.ctx.sessionmaker() as db:
            if await recover_external(db, stale_only=True, engine=self):
                await db.commit()

    async def _mark_recovery_uncertain(self, task: Task) -> bool:
        async with self.ctx.sessionmaker() as db:
            conditions = (
                Task.id == task.id, Task.statistics_key == task.statistics_key,
                Task.worker_key == task.worker_key, Task.queued_at == task.queued_at,
                Task.execution_key == task.execution_key, Task.status == "running",
                Task.admin_cancel_operation_id == task.admin_cancel_operation_id,
                Task.recovered_at.is_(None),
            )
            if await db.scalar(select(Task.id).where(*conditions).with_for_update()) is None:
                return False
            await require_live_worker(db, task.worker_key)
            changed = await db.execute(update(Task).where(*conditions).values(
                recovered_at=func.now(), worker_key=None, cancel_requested_at=None,
            ))
            if changed.rowcount != 1:
                return False
            await db.commit()
        await self.record_uncertain_execution(task)
        logger.error("任务执行事实待核对 task=%s", task.id)
        return True

    async def _recover_adopted(self, task: Task) -> bool:
        """只消费本次认领的快照。True 表示已收尾或交由管理员 drain，False 留待下一轮。"""
        from app.services.rich_document_review import ReviewRecoveryUncertain

        if task.statistics_key is not None:
            self.admin_cancellations.recovering[task.statistics_key] = (
                self.admin_cancellations.round_at(task), task.execution_key,
            )
        if task.admin_cancel_operation_id is not None and task.statistics_key is not None:
            self.request_admin_cancel(task.statistics_key, task.admin_cancel_operation_id)
            return True
        complete_receipt = None
        detail = None
        checkpoint = task.checkpoint if isinstance(task.checkpoint, dict) else {}
        if task.admin_cancel_operation_id is None:
            if task.kind == "chapter.analyze" and "_analysis_completion" in checkpoint:
                complete_receipt = self._complete_from_analysis_receipt
                detail = ANALYSIS_RECEIPT_FINALIZED_DETAIL
            elif task.kind in _WIZARD_KINDS and "_wizard_completion" in checkpoint:
                complete_receipt = self._complete_from_wizard_receipt
                detail = WIZARD_RECEIPT_FINALIZED_DETAIL
            elif task.kind in _SINGLE_CHAPTER_KINDS and "_chapter_claim" in checkpoint:
                complete_receipt = self._complete_from_chapter_receipt
                detail = RECEIPT_FINALIZED_DETAIL
        if complete_receipt is not None and await complete_receipt(task) is not None:
            self.admin_cancellations.recovering.pop(task.statistics_key, None)
            self.close_hub(task.id, "error", {"task": None, "detail": detail}, statistics_key=task.statistics_key)
            return True

        wizard_pending = (task.kind in _WIZARD_KINDS and task.admin_cancel_operation_id is None
                          and "_wizard_pending" in checkpoint and "_wizard_completion" not in checkpoint)
        if task.kind == "chapter.analyze" or (task.kind.startswith("wizard.") and not wizard_pending):
            return await self._mark_recovery_uncertain(task)

        try:
            pending = await self._durable_rich_review(task)
        except ReviewRecoveryUncertain:
            return await self._mark_recovery_uncertain(task)
        if pending is not None:
            current, identity = pending
            if current.admin_cancel_operation_id is not None:
                return True
            row, committed = await self.complete_state(
                current, expected_status="running", status="paused", result=identity, error="",
                text=current.output_text, progress=current.progress, abort_ok=True, review_proof=identity,
            )
            if committed:
                self.admin_cancellations.recovering.pop(task.statistics_key, None)
                self.close_hub(task.id, "paused", {"task": task_service.out_json(row), "result": identity},
                               statistics_key=task.statistics_key)
            return committed

        if (task.kind in _SINGLE_CHAPTER_KINDS and "_chapter_claim" in checkpoint
                or task.kind == "chapter.batch" and (
                    checkpoint.get("phase") != "generate" or "_batch_completion" in checkpoint
                )):
            if (task.kind == "chapter.batch" and "_batch_completion" in checkpoint
                    and task.admin_cancel_operation_id is None):
                try:
                    row, committed = await self.complete_state(
                        task, expected_status="running", status="interrupted", result=None,
                        error=INTERRUPTED_MESSAGE, text=task.output_text, progress=task.progress,
                        abort_ok=True, batch_receipt_proof=True,
                    )
                except ConflictError:
                    committed = False
                if committed:
                    self.admin_cancellations.recovering.pop(task.statistics_key, None)
                    self.close_hub(task.id, "error", {"task": task_service.out_json(row),
                                                      "detail": INTERRUPTED_MESSAGE},
                                   statistics_key=task.statistics_key)
                    return True
            return await self._mark_recovery_uncertain(task)

        if task.kind in _SINGLE_CHAPTER_KINDS and task.admin_cancel_operation_id is not None:
            return True
        async with self.ctx.sessionmaker() as db:
            uncertain = await owner_cancellation.requested_result_unproven(db, task)
        if uncertain:
            # 本人受理以后原执行才进入待核对；已有完成/审核收据在上方处理。
            return await self._mark_recovery_uncertain(task)
        handler = _try_handler(task.kind)
        abort_ok = handler is not None
        snapshot = self.capture_abort_snapshot(task, recovery=True)
        chapter_resource = None
        if task.kind in _SINGLE_CHAPTER_KINDS:
            # 缺损的历史占用不能仅凭章节状态推断为清理成功。
            chapter_resource = snapshot.claim if snapshot is not None else None
            abort_ok = chapter_resource is not None
        elif handler is not None and not wizard_pending:
            try:
                if task.kind == "chapter.batch":
                    await handler.on_abort(self.ctx, task, recovery=True)
                else:
                    await handler.on_abort(self.ctx, task)
            except AppError:
                abort_ok = False
        try:
            row, committed = await self.complete_state(
                task, expected_status="running", status="interrupted", result=None, error=INTERRUPTED_MESSAGE,
                text=task.output_text, progress=task.progress, abort_ok=abort_ok,
                recovery_claim=chapter_resource, wizard_pending=wizard_pending, owner_recovery=True,
            )
        except _UnprovenRecoveryResource:
            if wizard_pending:
                return await self._mark_recovery_uncertain(task)
            abort_ok = False
            row, committed = await self.complete_state(
                task, expected_status="running", status="interrupted", result=None, error=INTERRUPTED_MESSAGE,
                text=task.output_text, progress=task.progress, abort_ok=False, owner_recovery=True,
            )
        if committed:
            if not abort_ok and handler is not None and task.project_id is not None:
                await self.record_abort_failure(task.project_id, snapshot)
            self.admin_cancellations.recovering.pop(task.statistics_key, None)
            out = task_service.out_json(row) if row is not None and handler is not None else None
            detail = row.error if row is not None else INTERRUPTED_MESSAGE
            self.close_hub(task.id, "error", {"task": out, "detail": detail},
                           statistics_key=task.statistics_key)
        return committed

    # ---------------- hub ----------------

    def ensure_hub(
        self, task_id: int, *, statistics_key: str | None = None,
        status: str = "queued", text: str = "", progress: int = 0, stage: str = "",
        queued_at: Any = None, execution_key: str | None = None,
    ) -> tuple[TaskHub, bool]:
        """返回 (hub, created)。"""
        hub = self.hubs.get(task_id)
        if hub is not None and statistics_key is not None and hub.statistics_key != statistics_key:
            self.hubs.pop(task_id, None)
            hub.close("error", {"task": None, "detail": TASK_GONE_MESSAGE})
            hub = None
        if hub is not None and queued_at is not None and hub.queued_at is not None and hub.queued_at != queued_at:
            self.hubs.pop(task_id, None)
            hub.close("error", {"task": None, "detail": TASK_GONE_MESSAGE})
            hub = None
        if (hub is not None and hub.execution_key is not None and execution_key is not None
                and hub.execution_key != execution_key):
            self.hubs.pop(task_id, None)
            hub.close("error", {"task": None, "detail": TASK_GONE_MESSAGE})
            hub = None
        if hub is not None:
            if queued_at is not None:
                hub.queued_at = queued_at
            if execution_key is not None:
                hub.execution_key = execution_key
            return hub, False
        hub = TaskHub(
            task_id, statistics_key=statistics_key, status=status, text=text, progress=progress, stage=stage,
            queued_at=queued_at, execution_key=execution_key,
        )
        self.hubs[task_id] = hub
        return hub, True

    def close_hub(
        self, task_id: int, event: str, data: dict[str, Any], *, statistics_key: str | None = None,
        queued_at: Any = _UNSET_BINDING, execution_key: Any = _UNSET_BINDING,
    ) -> None:
        hub = self.hubs.get(task_id)
        if (hub is not None and hub.statistics_key == statistics_key
                and (queued_at is _UNSET_BINDING or hub.queued_at in (None, queued_at))
                and (execution_key is _UNSET_BINDING or hub.execution_key in (None, execution_key))):
            self.hubs.pop(task_id, None)
            hub.close(event, data)

    # ---------------- 调度 ----------------

    async def _scheduler(self) -> None:
        assert self._wake is not None
        next_reap = time.monotonic() + self.poll_interval
        while not self._stopping:
            try:
                async with asyncio.timeout(max(0.0, next_reap - time.monotonic())):
                    await self._wake.wait()
            except TimeoutError:
                pass
            self._wake.clear()
            if self._stopping:
                return
            try:
                if not self.lease.valid or self._lease_lost:
                    await self._replace_lost_worker()
                    next_reap = time.monotonic()
                if self._stopping:
                    return
                periodic = time.monotonic() >= next_reap
                if periodic:
                    self._recheck_active_members()
                    # 即使无关完成事件持续唤醒，也按定时期限重试排队任务。
                    self._stalled.clear()
                    next_reap = time.monotonic() + self.poll_interval
                    await self._reap_orphans()
                await self.scan_admin_cancellations()
                await owner_cancellation.scan(self)
                if periodic or self._paused_scan_at is None or self.collaboration_scan_interval is not None:
                    self._maybe_scan_collaboration_paused_soon()
                await self._dispatch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # 登记失败需退避。普通唤醒中的调度故障不能推迟已经排定的失联扫描。
                if not self.lease.valid or self._lease_lost:
                    next_reap = time.monotonic() + self.poll_interval
                logger.error("任务调度失败，下个周期重试 error_type=%s", type(exc).__name__)

    async def _dispatch(self) -> None:
        if self._lease_lost or not self.lease.valid or not self.runtime.settings_confirmed:
            return
        settings = self.ctx.settings
        sm = self.ctx.sessionmaker
        async with sm() as db:
            rows = await db.execute(
                select(Task.id, Task.statistics_key, Task.user_id, Task.serial_key, Task.project_id, Task.queued_at)
                .where(*execution_slots.dispatchable_tasks())
                .order_by(Task.id)
            )
            queued = list(rows.all())
            limits = await quota_service.task_limits(db, (row.user_id for row in queued), settings)
            slots = (await db.execute(select(
                TaskExecutionSlot.task_id, TaskExecutionSlot.user_id, TaskExecutionSlot.serial_key,
            ))).all() if queued else []
        occupied = {row.task_id: (row.user_id, row.serial_key) for row in slots}
        # 主库已登记的正常领取和本地未提交候选只预留一次；最终裁决在领取事务里。
        for task_id, user_id in self._running_users.items():
            occupied.setdefault(task_id, (user_id, self._running_serial.get(task_id)))
        per_user = Counter(user_id for user_id, _ in occupied.values())
        busy_serial = {key for _, key in occupied.values() if key is not None}
        current = {row.id: (row.statistics_key, row.queued_at) for row in queued}
        for stalled_id, identity in tuple(self._stalled.items()):
            if current.get(stalled_id) != identity:
                # 已不再排队或已换入队轮次：不再退避。
                self._stalled.pop(stalled_id, None)
        for task_id, statistics_key, user_id, serial_key, project_id, queued_at in queued:
            if self._stopping or len(occupied) >= settings.task_max_concurrency:
                break
            if task_id in occupied or task_id in self._running:
                if serial_key is not None:
                    busy_serial.add(serial_key)
                continue
            if serial_key is not None and serial_key in busy_serial:
                continue
            if self._stalled.get(task_id) == (statistics_key, queued_at):
                # 退避中的任务仍占据其串行键队首，同键后续任务不得越过它。
                if serial_key is not None:
                    busy_serial.add(serial_key)
                continue
            limit = limits[user_id]
            if limit is not None and per_user[user_id] >= limit:
                if serial_key is not None:
                    busy_serial.add(serial_key)
                continue
            occupied[task_id] = (user_id, serial_key)
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
        for position, (task_id, key) in enumerate(rows):
            hub = self.hubs.get(task_id)
            if hub is not None and hub.statistics_key == key and hub.status == "queued":
                if hub.source is None and hub.execution_key is None:
                    # E5-e：排队位次帧的来源（只证明同一入队轮次的排队 progress）；不影响非受控订阅。
                    from app.services.task_engine.stream_evidence import issue_queued

                    try:
                        hub.source = issue_queued(hub)
                    except ValueError:
                        pass
                hub.set_queue_position(position)

    def _on_done(self, task_id: int) -> None:
        task = self._running.pop(task_id, None)
        project_id = self._running_projects.pop(task_id, None)
        execution = self._project_executions.pop(task_id, None)
        failed_project = None
        if task is not None and not task.cancelled() and task.exception() is not None:
            execution_key = self._running_execution_keys.get(task_id)
            if execution_key is not None:
                # 此执行体已退出，仍然有效的本进程租约会把它排除在孤立扫描之外。
                # 用已领取的轮次登记恢复；恢复事务在锁内重新核实 worker、执行键与 running 状态。
                self._adopted.add((task_id, self._running_statistics_keys.get(task_id), execution_key))
            elif project_id is not None:
                self.remember_abort_failure(project_id, None)
                failed_project = project_id
            logger.error("任务执行体异常结束 task=%s", task_id)
        if execution is not None or failed_project is not None:
            confirmation = asyncio.create_task(self._confirm_project_exit(execution, failed_project))
            self._exit_confirmations.add(confirmation)
            confirmation.add_done_callback(self._exit_confirmations.discard)
        self._running_users.pop(task_id, None)
        self._running_rounds.pop(task_id, None)
        self._claiming.discard(task_id)
        self._running_statistics_keys.pop(task_id, None)
        if self._running_execution_keys.pop(task_id, None) is not None:
            # 真实执行已经收尾，因配额/串行键被挡的队首可立即重试。
            self._stalled.clear()
        self._running_serial.pop(task_id, None)
        self._cancel_requested.discard(task_id)
        self._collaboration_revoked.pop(task_id, None)
        # 未领取的候选只唤醒，不重置退避，避免反复争用同一堵塞任务。
        if self._wake is not None:
            self._wake.set()

    # ---------------- 执行 ----------------

    async def _fail_reset_queued_chapter(
        self, claim: chapter_claim.ChapterClaim, *, worker_key: str | None = None,
    ) -> None:
        """章被人工复位或其他抢占取代时，只结束原 queued Task，绝不回退当前章节。"""
        from app.services.task_engine.handlers import chapter as chapter_handler

        async with self.ctx.sessionmaker() as db:
            # 系统收尾不要求原 actor 仍可登录或仍是成员：只锁同一 gate/User → 门禁 → 项目，
            # 再按原 Task UUID/轮次与 claim 逐字 CAS；绝不触碰当前章节。
            original = await project_lifecycle.load_record(db, claim.project_id)
            if original is None:
                return
            await authz.gate(db)
            await authz.lock_users(db, *sorted({claim.actor_id, original.user_id}), missing_ok=True)
            try:
                await lock_visibility_write(db, claim.project_id)
                project = await project_lifecycle.lock_active(db, claim.project_id)
            except AppError:
                return
            if (project.user_id == claim.actor_id) != (claim.grant_identity is None):
                # 作者任务不应带协作轮次，协作任务也不能属于作者；身份无法证明时不收尾。
                return
            task = await db.scalar(select(Task).where(
                Task.id == claim.task_id, Task.statistics_key == claim.task_key, Task.user_id == claim.actor_id,
                Task.project_id == claim.project_id, Task.chapter_id == claim.chapter_id,
                Task.status == "queued", Task.queued_at == claim.queued_at, Task.execution_key.is_(None),
                Task.admin_cancel_operation_id.is_(None),
            ).with_for_update())
            if task is None or chapter_handler.capture_queued_claim(task) != claim:
                return
            if worker_key is not None and not await workers.lock_live_worker(db, worker_key):
                return
            chapter = await db.scalar(select(Chapter).where(
                Chapter.id == claim.chapter_id, Chapter.project_id == claim.project_id,
            ).with_for_update())
            if (chapter is not None and chapter.status == "generating"
                    and chapter.generation_claim_key == claim.claim_key
                    and chapter.generation_claim_task_key == claim.task_key
                    and chapter.generation_claim_prev_status == claim.prev_status):
                return
            payload = dict(task.input)
            payload.pop("_chapter_claim", None)
            stamp = task_statistics_service.now_utc()
            changed = await db.execute(update(Task).where(
                Task.id == claim.task_id, Task.statistics_key == claim.task_key, Task.status == "queued",
                Task.queued_at == claim.queued_at, Task.execution_key.is_(None),
                Task.admin_cancel_operation_id.is_(None),
            ).values(status="failed", input=payload, error="章节生成状态已被复位，任务未执行",
                     stage=task_service.STAGE_LABELS["failed"], finished_at=stamp))
            if changed.rowcount != 1:
                return
            await task_statistics_service.record_state_change(db, task, "failed", finished_at=stamp)
            await db.commit()
            self.close_hub(claim.task_id, "error", {"task": task_service.out_json(task), "detail": task.error},
                           statistics_key=claim.task_key, queued_at=claim.queued_at)

    @staticmethod
    async def _collaboration_claim_failure(db: AsyncSession, task: Task) -> str | None:
        """调用方已持 gate、actor/作者 User 锁与作品父行锁；返回失效原因，仍有效时为 None。"""
        payload = task.input if isinstance(task.input, dict) else {}
        with db.no_autoflush:
            user = (await db.execute(select(User.is_active, User.token_version).where(
                User.id == task.user_id,
            ).execution_options(populate_existing=True))).one_or_none()
            token = payload.get("_task_actor_token_version")
            if (user is None or user.is_active is not True
                    or (type(token) is int and token != user.token_version)):
                return "collaboration_account_unavailable"
            grant = task_grant_identity(payload)
            owner_id = await db.scalar(select(Project.user_id).where(Project.id == task.project_id))
            if grant is None or owner_id is None or owner_id == task.user_id:
                return "collaboration_revoked"
            member = (await db.execute(select(
                ProjectCollaborator.status, ProjectCollaborator.role, ProjectCollaborator.invitation_id,
                ProjectCollaborator.revision, ProjectCollaborator.allow_owner_ai,
            ).where(
                ProjectCollaborator.project_id == task.project_id, ProjectCollaborator.user_id == task.user_id,
            ))).one_or_none()
        if (member is None or member.status != "accepted" or member.role != "editor"
                or (member.invitation_id, member.revision) != grant):
            return "collaboration_revoked"
        if task_requires_owner_ai(payload) and member.allow_owner_ai is not True:
            return "collaboration_model_revoked"
        return None

    async def _fail_collaboration_queued(
        self, db: AsyncSession, task: Task, code: str,
        chapter_queued: chapter_claim.ChapterClaim | None,
        document_queued: kb_index_service.DocumentIndexClaim | None,
    ) -> bool:
        """受限系统清理：同一锁内按原 UUID/入队轮次把排队或待审核任务置为 failed 并回退本轮占用。

        只结束任务与回退占用，不写正文、设定或模型产出；无法证明资源归属时保持原屏障。
        待审核（paused，E5-a）任务没有执行体与活动占用：只按原轮次 CAS 终态并丢弃待审核提案，
        已提交的章节与断点保留；仍有章节占用、未结批量章节或待核对收据时保持原屏障。
        """
        message = COLLABORATION_FAILURE_MESSAGES[code]
        task_id, task_key, queued_at = task.id, task.statistics_key, task.queued_at
        paused = task.status == "paused"
        cleanup_kinds = _COLLABORATION_PAUSED_CLEANUP_KINDS if paused else _COLLABORATION_QUEUED_CLEANUP_KINDS
        if (task.status not in ("queued", "paused") or task.execution_key is not None
                or task.admin_cancel_operation_id is not None
                or task.admin_cancel_requested_at is not None or task_key is None or queued_at is None
                or task.kind not in cleanup_kinds or not isinstance(task.input, dict)
                or (task.checkpoint is not None and not isinstance(task.checkpoint, dict))):
            logger.error("协作任务授权已失效，但无法证明可安全收尾 task_key=%s", task_key)
            return False
        if any(key in task.input for key in _QUEUED_RECEIPT_INPUT_KEYS) or any(
            key in (task.checkpoint or {}) for key in _QUEUED_RECEIPT_CHECKPOINT_KEYS
        ):
            logger.error("协作任务存在待核对凭据，保留原屏障 task_key=%s", task_key)
            return False
        if paused:
            # 待审核时章节抢占已在暂停前释放；任何残留占用或进行中的批量章节都不能由系统猜测回退。
            if chapter_queued is not None or document_queued is not None or "_chapter_claim" in task.input:
                return False
            if task.kind == "chapter.batch" and (task.checkpoint or {}).get("current") is not None:
                return False
            held = await db.scalar(select(Chapter.id).where(
                Chapter.project_id == task.project_id, Chapter.generation_claim_task_key == task_key,
            ).limit(1))
            if held is not None:
                logger.error("待审核协作任务仍持有章节占用，保留原屏障 task_key=%s", task_key)
                return False
        else:
            if task.kind in _SINGLE_CHAPTER_KINDS and chapter_queued is None:
                return False
            if task.kind == "kb.index" and task_service._request_dict(task).get("mode") == "document" \
                    and document_queued is None:
                return False
            if task.kind not in _SINGLE_CHAPTER_KINDS and "_chapter_claim" in task.input:
                return False
            if task.kind == "chapter.batch" and task.checkpoint is not None:
                review = task.checkpoint.get("rich_review")
                if (task.checkpoint.get("current") is not None or task.checkpoint.get("phase") == "awaiting_review"
                        or (isinstance(review, dict) and review.get("phase") == "awaiting_review")):
                    return False
        stamp = task_statistics_service.now_utc()
        # 待审核任务保留暂停时已持久的结果摘要（与本人取消待审核任务一致）；排队任务尚无结果。
        values: dict[str, Any] = {"status": "failed", "error": message, "stage": task_service.STAGE_LABELS["failed"],
                                  "finished_at": stamp}
        if not paused:
            values["result"] = None
        try:
            changed = await db.execute(update(Task).where(
                Task.id == task_id, Task.statistics_key == task_key, Task.user_id == task.user_id,
                Task.project_id == task.project_id, Task.kind == task.kind,
                Task.status == ("paused" if paused else "queued"), Task.queued_at == queued_at,
                Task.execution_key.is_(None),
                Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
            ).values(**values))
            if changed.rowcount != 1:
                await db.rollback()
                return False
            if paused:
                await cleanup_work.register(db, self, task, settled=True)
                from app.services.rich_document_review import discard_pending

                # 与终态同事务丢弃待审核提案；断点中的审核阶段一并复位，不写正文。
                await discard_pending(db, task)
            if task.kind == "agent.turn":
                from app.services.agent import service as agent_service

                # E4-i 裁决 11：同事务作废本轮仍待确认的 Agent 提案（不执行、会话内可见）；executing / 带外部
                # 未知结果标记的行不动，外部调用未知屏障保留。
                await agent_service.void_revoked_turn_changes(
                    db, task_id=task_id, task_key=task_key, project_id=task.project_id,
                )
            if chapter_queued is not None and not await chapter_claim.release_chapter_claim(db, chapter_queued):
                # 章节已被人工复位或换成其他轮次：只摘除原任务指针，绝不回退当前章节。
                held = await db.scalar(select(Chapter.id).where(
                    Chapter.id == chapter_queued.chapter_id, Chapter.project_id == chapter_queued.project_id,
                    Chapter.generation_claim_key == chapter_queued.claim_key,
                ))
                if held is not None:
                    await db.rollback()
                    logger.error("协作任务章节占用无法按原轮次释放 task_key=%s", task_key)
                    return False
                payload = dict(task.input)
                payload.pop("_chapter_claim", None)
                popped = await db.execute(update(Task).where(
                    Task.id == task_id, Task.statistics_key == task_key, Task.status == "failed",
                    Task.queued_at == queued_at, Task.execution_key.is_(None),
                ).values(input=payload))
                if popped.rowcount != 1:
                    await db.rollback()
                    return False
            if document_queued is not None \
                    and not await kb_index_service.release_document_claim(db, document_queued):
                await db.rollback()
                logger.error("协作任务知识库占用无法按原轮次释放 task_key=%s", task_key)
                return False
            await task_statistics_service.record_state_change(db, task, "failed", finished_at=stamp)
            try:
                await db.commit()
            except Exception:
                # 提交确认丢失：只按原 UUID/轮次的持久事实确认，不重放 CAS。
                await db.rollback()
                async with self.ctx.sessionmaker() as proof:
                    fact = await proof.scalar(select(Task.id).where(
                        Task.id == task_id, Task.statistics_key == task_key, Task.queued_at == queued_at,
                        Task.status == "failed", Task.error == message, Task.execution_key.is_(None),
                    ))
                if fact is None:
                    raise
        except Exception:  # noqa: BLE001  不输出可能含任务输入的 SQL 参数；下一轮调度按数据库事实重试。
            logger.error("协作任务失效收尾未完成 task_key=%s", task_key)
            return False
        async with self.ctx.sessionmaker() as fresh:
            row = await fresh.scalar(select(Task).where(Task.id == task_id, Task.statistics_key == task_key))
            if row is not None:
                self.close_hub(task_id, "error", {"task": task_service.out_json(row), "detail": message},
                               statistics_key=task_key, queued_at=queued_at)
                with suppress(Exception):
                    await task_service.prune_finished(fresh, row.user_id)
        return True

    @staticmethod
    def _paused_collaboration_candidates(
        db: AsyncSession, *, project_id: int | None = None, actor_id: int | None = None, after_id: int = 0,
        limit: int | None = None,
    ):
        """待审核且带协作轮次的任务（不含作者任务、管理员取消意图或删除中的作品）；未验证方言不扫描。

        只取可受限收尾的类型：白名单外的失效任务永远无法收尾，若纳入候选会在每轮全量复查时反复取写锁。
        按 ix_tasks_status_id 键集分页（status='paused' AND id > after_id），单批读取的 input 有上限。
        """
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            has_grant = func.json_type(Task.input, f'$."{GRANT_KEY}"').is_not(None)
        elif dialect == "postgresql":
            has_grant = func.json_typeof(Task.input[GRANT_KEY]).is_not(None)
        else:
            return None
        statement = select(
            Task.id, Task.statistics_key, Task.user_id, Task.project_id, Task.queued_at, Task.input,
        ).join(Project, Project.id == Task.project_id).where(
            Task.status == "paused", Task.project_id.is_not(None), Task.statistics_key.is_not(None),
            Task.queued_at.is_not(None), Task.execution_key.is_(None), Task.admin_cancel_operation_id.is_(None),
            Task.admin_cancel_requested_at.is_(None), Project.user_id != Task.user_id,
            Task.kind.in_(sorted(_COLLABORATION_PAUSED_CLEANUP_KINDS)), Task.id > after_id,
            *project_lifecycle.active_conditions(), has_grant,
        ).order_by(Task.id)
        if project_id is not None:
            statement = statement.where(Task.project_id == project_id)
        if actor_id is not None:
            statement = statement.where(Task.user_id == actor_id)
        if limit is not None:
            statement = statement.limit(limit)
        return statement

    @staticmethod
    async def _paused_grant_lost(db: AsyncSession, row: Any) -> bool:
        """无锁预筛：原轮次仍有效（含作者模型授权）时不取任何写锁。最终判定在锁内重做。"""
        payload = row.input if isinstance(row.input, dict) else {}
        token = payload.get("_task_actor_token_version")
        visible = await db.scalar(select(Task.id).where(
            Task.id == row.id, Task.statistics_key == row.statistics_key,
            task_visible_to_actor(db, row.user_id, active_grant_required=True,
                                  token_version=token if type(token) is int else None),
        ))
        if visible is None:
            return True
        if task_requires_owner_ai(payload):
            allowed = await db.scalar(select(ProjectCollaborator.allow_owner_ai).where(
                ProjectCollaborator.project_id == row.project_id, ProjectCollaborator.user_id == row.user_id,
                ProjectCollaborator.status == "accepted",
            ))
            return allowed is not True
        return False

    async def scan_collaboration_paused(self, *, project_id: int | None = None, actor_id: int | None = None) -> int:
        """按数据库事实收尾授权已失效的待审核协作任务（E5-a）；返回本次收尾数。

        触发：撤权/降级等协作变更通知（限定作品与成员），以及调度器按 collaboration_scan_interval 的全量复查
        （覆盖进程重启与跨实例未收到通知）。通知只是唤醒，是否收尾以锁内重读为准。
        """
        async with self._paused_scan_lock:
            finished, after_id = 0, 0
            while not self._stopping:
                # 每批独立的短只读会话：无锁预筛，仍有效者不取任何写锁；SQLite 下不长时间持有读快照。
                async with self.ctx.sessionmaker() as db:
                    statement = self._paused_collaboration_candidates(
                        db, project_id=project_id, actor_id=actor_id, after_id=after_id,
                        limit=self.collaboration_scan_batch,
                    )
                    if statement is None:
                        return 0
                    rows = list((await db.execute(statement)).all())
                    lost = [row for row in rows if await self._paused_grant_lost(db, row)]
                for row in lost:
                    if self._stopping:
                        break
                    grant = task_grant_identity(row.input)
                    if grant is None:
                        continue
                    if await self._fail_collaboration_paused(
                        row.id, row.statistics_key, row.queued_at, row.user_id, row.project_id, grant,
                    ):
                        finished += 1
                if len(rows) < self.collaboration_scan_batch:
                    break
                after_id = rows[-1].id
            return finished

    async def _fail_collaboration_paused(
        self, task_id: int, task_key: str, queued_at: Any, actor_id: int, project_id: int, grant: Any,
    ) -> bool:
        """与领取相同的锁序（gate → actor/作者 User → 发送写门禁 → 作品）内复核并受限收尾单个待审核任务。"""
        if self._stopping:
            return False
        async with self.ctx.sessionmaker() as db:
            async with cleanup_work.request_scope(db, self):
                try:
                    owner_id = await db.scalar(select(Project.user_id).where(Project.id == project_id))
                    if owner_id is None or owner_id == actor_id:
                        return False
                    await authz.gate(db)
                    await authz.lock_users(db, *sorted({actor_id, owner_id}), missing_ok=True)
                    await lock_visibility_write(db, project_id)
                    project = await project_lifecycle.lock_active(db, project_id)
                    if project.user_id != owner_id:
                        await db.rollback()
                        return False
                    task = await db.scalar(select(Task).where(
                        Task.id == task_id, Task.statistics_key == task_key, Task.user_id == actor_id,
                        Task.project_id == project_id, Task.status == "paused", Task.queued_at == queued_at,
                        Task.execution_key.is_(None), Task.admin_cancel_operation_id.is_(None),
                        Task.admin_cancel_requested_at.is_(None),
                    ).with_for_update().execution_options(populate_existing=True))
                    if task is None or task_grant_identity(task.input) != grant:
                        # 已继续、已结束、管理员已介入或轮次已变化：不是本次复查的对象。
                        await db.rollback()
                        return False
                    failure = await self._collaboration_claim_failure(db, task)
                    if failure is None:
                        await db.rollback()
                        return False
                    done = await self._fail_collaboration_queued(db, task, failure, None, None)
                except AppError:
                    with suppress(Exception):
                        await db.rollback()
                    return False
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001  不输出可能含任务输入的 SQL 参数；下一轮复查按数据库事实重试。
                    with suppress(Exception):
                        await db.rollback()
                    logger.error("待审核协作任务失效收尾未完成 task_key=%s", task_key)
                    return False
                if not done:
                    with suppress(Exception):
                        await db.rollback()
                    marker = (task_id, task_key, queued_at)
                    if marker not in self._paused_unproven_logged:
                        self._paused_unproven_logged.add(marker)
                        logger.error("待审核协作任务授权已失效但无法证明可安全收尾 task_key=%s", task_key)
                return done

    def _maybe_scan_collaboration_paused_soon(self) -> None:
        """调度循环内按间隔发起全量复查；进程启动后的第一轮立即复查（覆盖重启前遗留的待审核任务）。"""
        now = time.monotonic()
        if (self.collaboration_scan_interval is not None and self._paused_scan_at is not None
                and now - self._paused_scan_at < self.collaboration_scan_interval):
            return
        self._paused_scan_at = now
        self._spawn_paused_scan(None, None)

    def _spawn_paused_scan(self, project_id: int | None, actor_id: int | None) -> None:
        # 全量扫描已经覆盖期间积压的成员键；提交在扫描进行中时仍安排一轮复查。
        key = (project_id, actor_id)
        pending = self._pending_paused_scans
        if key == (None, None) or len(pending) >= self.collaboration_scan_batch:
            pending.clear()
            pending.add((None, None))
        elif (None, None) not in pending:
            pending.add(key)
        if any(not work.done() for work in self._paused_scans):
            return

        async def run() -> None:
            while pending:
                project, actor = pending.pop()
                try:
                    await self.scan_collaboration_paused(project_id=project, actor_id=actor)
                except Exception:
                    logger.warning("待审核协作任务复查失败 project=%s", project)

        work = asyncio.create_task(run(), name="collaboration-paused")
        self._collaboration_checks.add(work)
        work.add_done_callback(self._collaboration_checks.discard)
        self._paused_scans.add(work)
        work.add_done_callback(self._paused_scans.discard)

    def _log_legacy_document_unproven(self, task_id: int, statistics_key: str | None) -> None:
        # 无法证明时任务仍排队并会被反复扫描；同一任务只记一次，避免日志刷屏。
        if (task_id, statistics_key) not in self._legacy_unproven_logged:
            self._legacy_unproven_logged.add((task_id, statistics_key))
            logger.error("旧知识库索引任务缺少占用，且无法证明可安全清理 task_key=%s", statistics_key)

    async def _fail_legacy_document_queued(
        self, db: AsyncSession, legacy: kb_index_service.LegacyDocumentTask,
    ) -> bool:
        """领取事务内（gate → User → 作品 → Task 锁已持有）清理旧无占用文档任务为 failed。"""
        status = "failed"
        try:
            cleaned = await kb_index_service.cleanup_legacy_unclaimed_document_task(db, legacy, status=status)
            if cleaned is None:
                await db.rollback()
                return False
            try:
                await db.commit()
            except Exception:
                # 提交确认丢失：只按原 UUID/轮次的持久事实确认，不重放 CAS。
                await db.rollback()
                async with self.ctx.sessionmaker() as proof:
                    if not await kb_index_service.legacy_cleanup_committed(proof, legacy, status=status):
                        raise
        except Exception:  # noqa: BLE001  不输出可能含任务输入的 SQL 参数；下一轮调度按数据库事实重试。
            with suppress(Exception):
                await db.rollback()
            logger.error("旧知识库索引任务清理未完成 task_key=%s", legacy.task_key)
            return False
        message = kb_index_service.LEGACY_TASK_MESSAGES[status]
        async with self.ctx.sessionmaker() as fresh:
            row = await fresh.scalar(select(Task).where(
                Task.id == legacy.task_id, Task.statistics_key == legacy.task_key,
            ))
            if row is not None:
                self.close_hub(legacy.task_id, "error", {"task": task_service.out_json(row), "detail": message},
                               statistics_key=legacy.task_key, queued_at=legacy.queued_at)
                with suppress(Exception):
                    await task_service.prune_finished(fresh, row.user_id)
        return True

    async def _release_slot(self, execution: TaskExecutionIdentity) -> None:
        # 在途归还也属于收尾。重复的取消/关停不得打断活游标，停机先等原执行体退出。
        self._finalizing.add(execution.task_id)
        try:
            delay = 0.1
            while True:
                try:
                    async with self.ctx.sessionmaker() as db:
                        await execution_slots.release(db, execution)
                    return
                except Exception:  # noqa: BLE001  提交回执可能丢失，新事务按原轮次幂等归还。
                    if delay == 0.1:
                        logger.warning("任务名额归还尚未确认 task=%s", execution.task_id)
                    if self._stopping or self._lease_lost or not self.lease.valid:
                        # stop 或失联确认和这个 worker 的遗留名额清理在主库同事务提交。
                        return
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 5.0)
        finally:
            self._finalizing.discard(execution.task_id)

    async def _confirm_project_exit(
        self, execution: TaskExecutionIdentity | None, failed_project: int | None = None,
    ) -> None:
        delay = 0.1
        while True:
            try:
                if failed_project is not None:
                    await self.record_abort_failure(failed_project, None)
                if execution is not None:
                    async with self.ctx.sessionmaker() as db:
                        await execution_work.confirm_exit(db, execution)
                return
            except Exception:  # noqa: BLE001  回执丢失时用新事务按原身份重试。
                if delay == 0.1:
                    logger.warning("作品执行体退出尚未确认")
                if self._stopping or self._lease_lost or not self.lease.valid:
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)

    async def _claim_and_run(self, task_id: int, statistics_key: str | None) -> None:
        # 确认提交、启动模型、成功回调和异常退出共用一处释放出口。
        async with AsyncExitStack() as finish:
            await self._claim_and_execute(task_id, statistics_key, finish)

    async def _claim_and_execute(self, task_id: int, statistics_key: str | None, finish: AsyncExitStack) -> None:
        """在领取事务的授权门内复核额度、串行键和同键队首。"""
        worker_key = self.lease.worker_key
        if worker_key is None or not self.lease.valid or self._lease_lost or task_id in self._cancel_requested:
            return
        running_stage = task_service.STAGE_LABELS["running"]
        try:
            async with self.runtime.hold(), self.ctx.sessionmaker() as db:
                if not self.runtime.settings_confirmed:
                    return
                await authz.gate(db)
                candidate = (await db.execute(select(Task.user_id, Task.queued_at, Task.project_id, Task.kind).where(
                    Task.id == task_id, Task.statistics_key == statistics_key, Task.status == "queued",
                ))).first()
                if candidate is None:
                    return
                expected_round = self._running_rounds.get(task_id, candidate.queued_at)
                if candidate.queued_at != expected_round:
                    return
                owner_id = None
                if candidate.project_id is not None:
                    owner_id = await db.scalar(select(Project.user_id).where(Project.id == candidate.project_id))
                    if owner_id is None:
                        return
                await authz.lock_users(db, *sorted({candidate.user_id, *([owner_id] if owner_id is not None else [])}))
                actor_unavailable = False
                if candidate.project_id is not None:
                    try:
                        if candidate.kind in _SINGLE_CHAPTER_KINDS:
                            # 与章节抢占/内容写相同的顺序：User → 发送写门禁 → 项目。
                            try:
                                await authz.require_authenticated(db, candidate.user_id)
                            except AppError:
                                # 作者任务维持原行为；协作任务稍后在全部锁内按原轮次失败关闭。
                                actor_unavailable = True
                            await lock_visibility_write(db, candidate.project_id)
                        await project_lifecycle.lock_active(db, candidate.project_id)
                    except AppError:
                        return
                queued_select = select(Task).where(
                    Task.id == task_id, Task.statistics_key == statistics_key, Task.queued_at == expected_round,
                    Task.user_id == candidate.user_id, Task.project_id == candidate.project_id,
                    Task.kind == candidate.kind, Task.status == "queued", Task.execution_key.is_(None),
                )
                queued_select = queued_select.with_for_update()
                queued_task = await db.scalar(queued_select)
                if queued_task is None or not await workers.lock_live_worker(db, worker_key):
                    return
                owner = self.feature_plugins.plugin_of_kind(queued_task.kind)
                if owner and not await plugin_state.is_enabled(db, owner):
                    operation = plugin_state.mark_for_cancellation(queued_task, datetime.now(UTC))
                    await db.commit()
                    self.request_admin_cancel(statistics_key, operation)
                    return
                queued_had_text = bool(queued_task.output_text)
                queued_claim = None
                chapter_queued_claim = None
                if queued_task.kind in _SINGLE_CHAPTER_KINDS:
                    from app.services.task_engine.handlers import chapter as chapter_handler

                    try:
                        chapter_queued_claim = chapter_handler.capture_queued_claim(queued_task)
                    except AppError:
                        # 没有原 claim 的旧排队记录不得猜测为可运行的新章。
                        self._mark_stalled(task_id, statistics_key, expected_round)
                        return
                if queued_task.kind == "kb.index" and task_service._request_dict(queued_task).get("mode") == "document":
                    queued_claim = await kb_index_service.queued_document_claim(db, queued_task)
                    if queued_claim is None:
                        # 0045 前入队的旧文档任务没有占用：不补造身份、不执行；锁内可证明无执行体时
                        # 受限清理为 failed（E4-e），否则保持原屏障。关停中或已登记取消意图时不动手。
                        if not self._stopping and task_id not in self._cancel_requested:
                            legacy = kb_index_service.capture_legacy_document_task(
                                queued_task, task_key=statistics_key, queued_at=expected_round,
                            )
                            if legacy is None or not await self._fail_legacy_document_queued(db, legacy):
                                self._log_legacy_document_unproven(task_id, statistics_key)
                                self._mark_stalled(task_id, statistics_key, expected_round)
                        return
                if (candidate.project_id is not None and isinstance(queued_task.input, dict)
                        and GRANT_KEY in queued_task.input):
                    # 协作任务按锁内最新数据库状态复核；失效时在本事务内受限收尾，不再留作永久排队。
                    failure = await self._collaboration_claim_failure(db, queued_task)
                    if failure is not None:
                        if not self._stopping and task_id not in self._cancel_requested \
                                and not await self._fail_collaboration_queued(
                                    db, queued_task, failure, chapter_queued_claim, queued_claim,
                                ):
                            self._mark_stalled(task_id, statistics_key, expected_round)
                        return
                elif actor_unavailable:
                    self._mark_stalled(task_id, statistics_key, expected_round)
                    return
                try:
                    settings = await effective_settings(db)
                except AppError:
                    # 当前无法核验上限，等下一次设置通知或轮询再试。
                    self._mark_stalled(task_id, statistics_key, expected_round)
                    return
                limit = (await quota_service.task_limits(db, [candidate.user_id], settings))[candidate.user_id]
                if not await execution_slots.can_claim(db, queued_task, settings.task_max_concurrency, limit):
                    self._mark_stalled(task_id, statistics_key, expected_round)
                    return
                if self._stopping or not self.lease.valid or task_id in self._cancel_requested:
                    return
                # 活驱动CAS/提交确认期间只登记取消意图，不cancel游标。
                self._claiming.add(task_id)
                execution_key = str(uuid4())
                try:
                    execution = from_queued_task(queued_task, execution_key, worker_key=worker_key)
                    actor = (authz.AuthenticatedActor(execution.actor_id, execution.actor_token_version)
                             if execution.actor_token_version is not None else execution.actor_id)
                    await authz.require_authenticated(db, actor)
                    queued_handler = get_handler(execution.kind)
                    if (execution.actor_token_version is None
                            or (execution.grant_identity is not None
                                and (getattr(queued_handler, "collaboration_execution_ready", False) is not True
                                     or getattr(queued_handler, "execution_identity_aware", False) is not True))):
                        self._mark_stalled(task_id, statistics_key, expected_round)
                        return
                except AppError:
                    # 历史身份或原认证已失效：不得从当前用户补签。
                    self._mark_stalled(task_id, statistics_key, expected_round)
                    return
                result = await db.execute(
                    update(Task)
                    .where(
                        Task.id == task_id, Task.statistics_key == statistics_key, Task.queued_at == expected_round,
                        Task.user_id == candidate.user_id, Task.project_id == candidate.project_id,
                        Task.kind == candidate.kind, Task.status == "queued", Task.execution_key.is_(None),
                        task_service.visible_task_project_exists(),
                        workers.registered_worker_exists(worker_key),
                        Task.admin_cancel_operation_id.is_(None),
                    )
                    .values(status="running", execution_key=execution_key, worker_key=worker_key,
                            recovered_at=None, started_at=func.now(), stage=running_stage)
                )
                if result.rowcount != 1:
                    return
                row = await db.get(Task, task_id)
                running_claim = None
                running_chapter_claim = None
                if queued_claim is not None:
                    running_claim = await kb_index_service.activate_document_claim(
                        db, queued_claim, execution_key=execution_key, worker_key=worker_key,
                    )
                if chapter_queued_claim is not None:
                    try:
                        running_chapter_claim = await chapter_claim.activate_queued_chapter(
                            db, chapter_queued_claim, execution_key=execution_key, worker_key=worker_key,
                        )
                    except ConflictError as exc:
                        if (exc.extra or {}).get("code") != "chapter_claim_changed":
                            raise
                        # activate 失败已回滚 running CAS；新事务确认原占用确实已变化后才关闭旧任务。
                        await self._fail_reset_queued_chapter(chapter_queued_claim, worker_key=worker_key)
                        return
                await task_statistics_service.record_state_change(db, row, "running")
                if self._lease_lost or not self.lease.valid:
                    return
                self._running_statistics_keys[task_id] = row.statistics_key
                execution_slots.reserve(db, execution)
                if execution.project_id is not None:
                    execution_work.register(db, execution)
                    if self._running.get(task_id) is asyncio.current_task():
                        self._project_executions[task_id] = execution
                    else:
                        # 直接驱动一次领取时没有调度器的完成回调，此作用域即原执行体。
                        finish.push_async_callback(self._confirm_project_exit, execution)
                # 从此处起即使提交确认失败或执行前返回，也按这个不可变身份归还。
                finish.push_async_callback(self._release_slot, execution)
                try:
                    await db.commit()
                except Exception:
                    # 提交确认丢失：不重放CAS，不在事实不明时开始外部执行。
                    await db.rollback()
                    while True:
                        try:
                            async with self.ctx.sessionmaker() as verify:
                                persisted_task = await verify.scalar(select(Task).where(
                                    Task.id == task_id, Task.statistics_key == statistics_key,
                                    Task.queued_at == expected_round, Task.execution_key == execution_key,
                                    Task.worker_key == worker_key, workers.registered_worker_exists(worker_key),
                                    select(TaskExecutionSlot.execution_key)
                                    .where(*execution_slots.same_slot(execution)).exists(),
                                ))
                                persisted = "running" if matches_running(execution, persisted_task) else None
                                if persisted == "running" and queued_claim is not None:
                                    if running_claim is None or not await kb_index_service.document_claim_is_current(
                                        verify, running_claim,
                                    ):
                                        persisted = None
                                if persisted == "running" and chapter_queued_claim is not None:
                                    try:
                                        if running_chapter_claim is None:
                                            persisted = None
                                        else:
                                            await chapter_claim.require_running_chapter(verify, running_chapter_claim)
                                    except chapter_claim.ChapterClaimStorageError:
                                        raise
                                    except AppError:
                                        persisted = None
                            break
                        except Exception:
                            if self._stopping or not self.lease.valid:
                                return
                            await asyncio.sleep(0.1)
                    if persisted != "running":
                        # 本轮领取未获证实。单个候选不能因完成通知反复占住最后一个名额。
                        self._mark_stalled(task_id, statistics_key, expected_round)
                        return
                # 外层runtime尚未释放，覆盖DB已提交但commit await尚未返回的窗口。
                if self._lease_lost or not self.lease.valid or self.lease.worker_key != worker_key:
                    return
                self._running_execution_keys[task_id] = execution_key
        finally:
            # 会话退出也可能await；取消保护须无缝延续到_run的收尾try。
            self._claiming.discard(task_id)
        hub, _ = self.ensure_hub(task_id, statistics_key=statistics_key, status="running",
                                 queued_at=expected_round, execution_key=execution_key)
        hub.source = self._claim_source(execution, hub)
        peer_active = self.peer.url is not None
        if peer_active:
            self.peer_handoffs.begin(hub, worker_key)
        if hub.text or (peer_active and queued_had_text):
            # 续跑的旧缓存属于上一执行体；新轮必须从它自己经验证的输出开始。
            hub.reset()
        hub.set_queue_position(None)
        hub.set_progress(status="running", stage=running_stage)
        if running_chapter_claim is not None:
            await self._run(task_id, hub, execution, claim=running_chapter_claim)
        else:
            await self._run(task_id, hub, execution)

    @staticmethod
    def _claim_source(execution: TaskExecutionIdentity, hub: TaskHub) -> Any:
        """领取提交确认后的 hub 来源（E5-e）。

        仅受控流可证明的任务类型（实现 stream_guard）签发无输出的领取来源：只证明领取 progress / reset、正文
        为空的快照，以及按原 hub 终态证明的 error；首个已核验事件会换成 PublicationSource。其余类型维持原
        执行身份（受控流对其失败关闭）。只是同步登记，不读写数据库、不改变作者订阅帧序列。
        """
        if getattr(_try_handler(execution.kind), "stream_guard", None) is None:
            return execution
        from app.services.task_engine.stream_evidence import issue_claim

        try:
            return issue_claim(execution, hub=hub)
        except ValueError:
            return execution

    async def _run(
        self, task_id: int, hub: TaskHub, execution: TaskExecutionIdentity | None = None, *,
        claim: chapter_claim.ChapterClaim | None = None,
    ) -> None:
        status, error, result = "failed", "", None
        task: Task | None = None
        handler: TaskHandler | None = None
        job: Any = None
        unconfirmed = False
        try:
            if hub.statistics_key is not None:
                async with self.ctx.sessionmaker() as db:
                    task = await db.scalar(select(Task).where(
                        Task.id == task_id, Task.statistics_key == hub.statistics_key, Task.status == "running",
                        Task.queued_at == hub.queued_at, Task.execution_key == hub.execution_key,
                    ))
            if (execution is None or task is None or not matches_running(execution, task)
                    or task.admin_cancel_operation_id is not None):
                # 不从新读到的行补原 actor/project/kind/input 或领取断点。
                if (execution is not None and (task is None or (task.worker_key == execution.worker_key
                        and not self._same_accepted_cancellation(execution, task)))):
                    await self._record_uncertain_claim(execution)
                if self.hubs.get(task_id) is hub:
                    self.hubs.pop(task_id, None)
                hub.close("error", {"task": None, "detail": TASK_GONE_MESSAGE})
                return
            handler = get_handler(task.kind)
            aware = getattr(handler, "execution_identity_aware", False) is True
            if ((aware and execution.actor_token_version is None)
                    or (execution.grant_identity is not None
                        and (getattr(handler, "collaboration_execution_ready", False) is not True or not aware))):
                # 已按原领取身份逐字证明是本轮执行（上面的 matches_running）：不再留下孤立 running，
                # 走普通收尾（on_abort + CAS）失败关闭。缺令牌版本时不能用执行身份补签认证，
                # 只以不带身份的原 UUID/轮次/execution_key CAS 收尾。
                status, error = "failed", "任务执行身份尚未确认"
                unconfirmed = True
            elif task_id in self._shutdown:
                status, error = "interrupted", INTERRUPTED_MESSAGE
            elif task.cancel_requested_at is not None or task_id in self._cancel_requested:
                # 领取与取消请求几乎同时发生：不再执行
                revoked = self._collaboration_revoked.get(task_id)
                if revoked is not None and revoked[:2] == (execution.task_key, execution.execution_key):
                    status, error = "failed", COLLABORATION_FAILURE_MESSAGES[revoked[2]]
                else:
                    status, error = "cancelled", CANCELLED_MESSAGE
            else:
                try:
                    try:
                        options = {"execution": execution} if aware else {}
                        if task.kind in _SINGLE_CHAPTER_KINDS:
                            options["claim"] = claim
                        job = await _finish_before_cancel(handler.build_job(self.ctx, task, **options))
                    except VisibilityChangedError:
                        if task.kind not in _REBUILDABLE_VISIBILITY_JOBS:
                            raise
                        job = await _finish_before_cancel(handler.build_job(self.ctx, task, **options))
                except AppError as exc:
                    error = exc.detail
                else:
                    error = await self._consume(task, handler, hub, job, execution=execution)
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
            elif (revoked := self._collaboration_revoked.get(task_id)) is not None and execution is not None \
                    and revoked[:2] == (execution.task_key, execution.execution_key):
                # 数据库复查已确认协作授权失效：与用户取消同一停止路径，但以失败终态记录原因。
                status, error = "failed", COLLABORATION_FAILURE_MESSAGES[revoked[2]]
            else:
                status, error = "cancelled", CANCELLED_MESSAGE
        except (generation_service.ChapterCommitUncertain, analysis_service.AnalysisCommitUncertain):
            # 原事务可能已经提交正文；不能触发 on_abort、清除 execution 或开放重试。
            if task is not None:
                await self.record_uncertain_execution(task)
            if self.hubs.get(task_id) is hub:
                self.hubs.pop(task_id, None)
            hub.close("error", {"task": None, "detail": "任务保存结果尚未确认，请稍后核对"})
            return
        except VisibilityPolicyError as exc:
            # 只将受控策略代码映射到固定提示，不能透传异常正文或未验证的文本。
            hub.text, hub.result, hub.paused, hub.dirty = "", None, None, False
            status = "failed"
            code = _policy_code(exc)
            error = _POLICY_MESSAGES[code] if code is not None else "任务执行失败：服务器内部错误"
        except Exception:  # noqa: BLE001
            logger.error("任务 %s 执行失败", task_id)
            if hub.result is not None:
                status, result, error = "succeeded", hub.result, ""
            elif hub.paused is not None:
                status, result, error = "paused", hub.paused, ""
            else:
                status, error = "failed", "任务执行失败：服务器内部错误"
        try:
            current_execution = getattr(job, "task_execution_identity", execution) if job is not None else execution
            valid_execution = execution is None or same_execution(execution, current_execution)
        except Exception:  # noqa: BLE001  损坏的 job 快照不能作为本次执行依据。
            current_execution, valid_execution = None, False
        if not valid_execution:
            # job 快照与领取身份不一致：无法证明属于本轮，保留 running 屏障交由恢复/管理员排空。
            await self._record_uncertain_claim(execution)
            hub.close("error", {"task": None, "detail": "任务执行身份尚未确认"})
            return
        if unconfirmed and execution is not None and execution.actor_token_version is None:
            # 缺令牌版本时不能用执行身份补签认证；仅以原 UUID/轮次/execution_key 的普通 CAS 收尾。
            execution = current_execution = None
        await self._finalize(task_id, task, handler, hub, status=status, result=result, error=error, claim=claim,
                             execution=execution, current_execution=current_execution, job=job)

    async def _event_task(
        self, db: AsyncSession, execution: TaskExecutionIdentity, current: TaskExecutionIdentity, event: str,
    ) -> Task:
        if not same_execution(execution, current):
            raise VisibilityPolicyError("任务执行身份已变化")
        # E4i-b2（裁决 18）：协作执行的 verify_event 会再锁作者 User（guard.require_task_write、
        # 带 for_update 的模型重验等）。作者 User 必须与 actor 一起在发送写门禁之前按 ID 升序锁定，
        # 否则与作者发送 lease（先锁作者 User 再等共享门禁）交叉等待。作者执行为 None，行为不变。
        owner_id = frozen_project_owner_id(current)
        if execution.kind in _SINGLE_CHAPTER_KINDS and event in ("done", "paused"):
            # 单章提交后 input/claim/checkpoint 已合法变化；由专用原 claim/receipt 进一步核验。
            if execution.actor_token_version is None or execution.project_id is None:
                raise VisibilityPolicyError("任务执行身份已变化")
            await authz.require_authenticated(
                db, authz.AuthenticatedActor(execution.actor_id, execution.actor_token_version), write=True,
                targets=(owner_id,) if owner_id is not None else (),
            )
            await lock_visibility_write(db, execution.project_id)
            project = await project_lifecycle.lock_active(db, execution.project_id)
            if owner_id is not None and project.user_id != owner_id:
                raise VisibilityPolicyError("任务执行身份已变化")
            with db.no_autoflush:
                statement = select(Task).where(
                    Task.id == execution.task_id, Task.statistics_key == execution.task_key,
                    Task.user_id == execution.actor_id, Task.project_id == execution.project_id,
                    Task.chapter_id == execution.chapter_id, Task.kind == execution.kind,
                    Task.queued_at == execution.queued_at, Task.execution_key == execution.execution_key,
                    Task.status == "running", Task.worker_key == current.worker_key,
                )
                if event != "done":
                    statement = statement.where(Task.admin_cancel_operation_id.is_(None),
                                                Task.admin_cancel_requested_at.is_(None))
                row = await db.scalar(statement.with_for_update())
            if row is None:
                raise VisibilityPolicyError("任务执行身份已变化")
            await require_live_worker(db, current.worker_key)
            return row
        await require_write_execution(db, current, project_owner_id=owner_id)
        row = await db.get(Task, current.task_id)
        if row is None:
            raise VisibilityPolicyError("任务执行身份已变化")
        return row

    @staticmethod
    async def _callback_transaction(db: AsyncSession):
        transaction = db.get_transaction()
        if transaction is None or not transaction.is_active or db.get_nested_transaction() is not None:
            raise VisibilityPolicyError("任务验证事务已变化")
        connection = await db.connection()
        physical = connection.sync_connection
        if connection.closed or connection.invalidated or physical.get_transaction() is None:
            raise VisibilityPolicyError("任务验证事务已变化")
        return transaction.sync_transaction, physical, physical.get_transaction(), physical.connection.dbapi_connection

    @staticmethod
    async def _require_callback_transaction(db: AsyncSession, original) -> None:
        current = await TaskEngine._callback_transaction(db)
        if (any(left is not right for left, right in zip(current, original, strict=True))
                or db.new or db.deleted or any(db.is_modified(item) for item in db.dirty)):
            raise VisibilityPolicyError("任务验证事务已变化")

    async def _consume(
        self, task: Task, handler: TaskHandler, hub: TaskHub, job: Any, *,
        execution: TaskExecutionIdentity | None = None,
    ) -> str:
        """消费 runner 事件；返回错误信息（成功时为空串，结果存于 hub.result）。"""
        last_flush = time.monotonic()
        error = ""

        def publish(event: str, data: dict[str, Any]) -> None:
            nonlocal error
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

        plugin_task = self.feature_plugins.plugin_of_kind(task.kind) is not None

        async def check_plugin_cancellation(db: AsyncSession) -> None:
            # 停用后立即重开仍须停止原轮次；持久取消意图比当前插件开关更精确。
            operation = await db.scalar(select(Task.admin_cancel_operation_id).where(
                Task.id == task.id, Task.statistics_key == task.statistics_key, Task.queued_at == task.queued_at,
                Task.execution_key == task.execution_key, Task.worker_key == task.worker_key, Task.status == "running",
            ))
            if operation is not None:
                raise asyncio.CancelledError

        async def checked(event: str, data: dict[str, Any]) -> None:
            await timer.settle()
            if getattr(handler, "execution_identity_aware", False) is True:
                try:
                    verify = getattr(handler, "verify_event", None)
                    current = getattr(job, "task_execution_identity", execution)
                    if verify is None or execution is None or current is None:
                        raise VisibilityPolicyError("任务输出身份无法验证")
                    async with self.ctx.sessionmaker() as db:
                        if plugin_task:
                            await authz.gate(db)
                            await check_plugin_cancellation(db)
                        row = await self._event_task(db, execution, current, event)
                        transaction = await self._callback_transaction(db)
                        await verify(db, row, job=job, execution=execution, current_execution=current,
                                     event=event, data=data)
                        await self._require_callback_transaction(db, transaction)
                        stream_guard = getattr(handler, "stream_guard", None)
                        if stream_guard is not None:
                            from app.services.task_engine.stream_evidence import (
                                PublicationSource,
                                issue_publication,
                            )

                            guard = stream_guard(job, execution=execution, current_execution=current,
                                                 event=event, data=data)
                            if not (type(hub.source) is PublicationSource and hub.source.execution == current
                                    and hub.source.guard is guard):
                                hub.source = issue_publication(current, guard, hub=hub)
                        else:
                            hub.source = current
                        publish(event, deepcopy(data))
                except Exception as exc:
                    # 不能发送尚未通过本次门禁的数据，也不把私有异常/文本写成失败输出。
                    hub.text, hub.result, hub.paused, hub.dirty = "", None, None, False
                    code = _policy_code(exc)
                    if code is not None:
                        raise VisibilityPolicyError(_POLICY_MESSAGES[code], extra={"code": code}) from None
                    raise VisibilityPolicyError("任务输出授权已变化", extra={"code": "task_output_changed"}) from None
            else:
                # 无专门事件验证的 handler 也须核验执行者，然后在同一事务内发布。
                async with self.ctx.sessionmaker() as db:
                    if (execution is None or execution.worker_key != task.worker_key
                            or not await self._owns_task_round(db, task, status="running")):
                        raise VisibilityPolicyError("任务执行身份已变化")
                    if plugin_task:
                        await check_plugin_cancellation(db)
                    publish(event, data)

        timer = OutputFlushTimer()

        async def flush_idle() -> bool:
            nonlocal last_flush
            persisted = await self._flush(hub, handler=handler, job=job, execution=execution, idle_snapshot=pending)
            last_flush = time.monotonic()
            return persisted is not False

        async with aclosing(handler.run(job, self.ctx)) as events, AsyncExitStack() as timers:
            timers.push_async_callback(timer.settle)
            async for event, data in events:
                if event == "done" and task.kind in _SINGLE_CHAPTER_KINDS and isinstance(data, dict):
                    self._done_candidates[task.id] = (hub.execution_key, deepcopy(data))
                if event in ("done", "paused"):
                    # runner 产出终止事件前已原子提交结果；此后的取消不能打断其身份核验，
                    # 否则已提交的正文/收据会被记成取消而永久停在待核对。核验完成后再传递取消，
                    # 由 _run 按「done 已产出后才收到取消」收尾。
                    step = asyncio.ensure_future(checked(event, data))
                    try:
                        await asyncio.shield(step)
                    except asyncio.CancelledError:
                        await step
                        raise
                else:
                    await _finish_before_cancel(checked(event, data))
                if hub.dirty and event not in ("done", "paused"):
                    remaining = last_flush + self.flush_interval - time.monotonic()
                    if remaining <= 0:
                        await _finish_before_cancel(self._flush(hub, handler=handler, job=job, execution=execution))
                        last_flush = time.monotonic()
                        remaining = max(0.05, self.flush_interval)
                    if hub.dirty:
                        pending = OutputSnapshot.capture(hub)
                        timer.arm(remaining, flush_idle, self.flush_interval)
        if not error and hub.result is None and hub.paused is None:
            error = "任务意外结束，未产出结果"
        return error

    async def _flush(
        self, hub: TaskHub, *, handler: TaskHandler | None = None, job: Any = None,
        execution: TaskExecutionIdentity | None = None, idle_snapshot: OutputSnapshot | None = None,
    ) -> bool:
        from app.services.task_engine.stream_evidence import PublicationSource

        snapshot = idle_snapshot or OutputSnapshot.capture(hub)
        origin = snapshot.source
        if hub.statistics_key is None:
            return True
        if (hub.text and isinstance(origin, PublicationSource) and origin.execution.kind == "chapter.batch"
                and isinstance(snapshot.text_source, PublicationSource)
                and snapshot.text_source.execution.step_cursor != origin.execution.step_cursor):
            # chapter_end 的持久断点已确认推进，而下一章尚未产出 reset；不能给新 cursor 写上一章正文。
            return True
        try:
            async with self.ctx.sessionmaker() as db:
                conditions = [
                    Task.id == hub.task_id, Task.statistics_key == hub.statistics_key, Task.status == "running",
                    Task.queued_at == hub.queued_at, Task.execution_key == hub.execution_key,
                    Task.worker_key == (execution.worker_key if execution is not None else None),
                ]
                if handler is not None and getattr(handler, "execution_identity_aware", False) is True:
                    current = getattr(job, "task_execution_identity", execution)
                    verify = getattr(handler, "verify_event", None)
                    if execution is None or current is None or verify is None:
                        raise VisibilityPolicyError("任务输出身份无法验证")
                    if idle_snapshot is not None and (OutputSnapshot.capture(hub) != snapshot or current != (
                        origin.execution if isinstance(origin, PublicationSource) else origin
                    )):
                        return True
                    row = await self._event_task(db, execution, current, "flush")
                    transaction = await self._callback_transaction(db)
                    await verify(db, row, job=job, execution=execution, current_execution=current,
                                 event="flush", data={"text": snapshot.text})
                    await self._require_callback_transaction(db, transaction)
                    if idle_snapshot is not None and isinstance(origin, PublicationSource):
                        guard = handler.stream_guard(job, execution=execution, current_execution=current,
                                                     event="flush", data={"text": snapshot.text})
                        if guard is not origin.guard or getattr(job, "task_execution_identity", execution) != current:
                            return True
                    raw_input, raw_checkpoint = (await db.execute(select(
                        cast(Task.input, String).label("input_raw"),
                        cast(Task.checkpoint, String).label("checkpoint_raw"),
                    ).where(Task.id == current.task_id, Task.statistics_key == current.task_key))).one()
                    conditions.extend((
                        Task.user_id == current.actor_id, Task.project_id == current.project_id,
                        Task.chapter_id == current.chapter_id, Task.kind == current.kind,
                        Task.step_cursor == current.step_cursor, Task.admin_cancel_operation_id.is_(None),
                        Task.admin_cancel_requested_at.is_(None),
                        cast(Task.input, String) == raw_input if raw_input is not None else Task.input.is_(None),
                        cast(Task.checkpoint, String) == raw_checkpoint if raw_checkpoint is not None
                        else Task.checkpoint.is_(None),
                    ))
                changed = await db.execute(
                    update(Task)
                    .where(*conditions)
                    .values(output_text=snapshot.text, progress=snapshot.progress, stage=snapshot.stage)
                )
                if changed.rowcount != 1 and execution is not None:
                    raise VisibilityPolicyError("任务输出身份已变化")
                if execution is not None and not getattr(handler, "execution_identity_aware", False):
                    await require_live_worker(db, execution.worker_key)
                await db.commit()
                if OutputSnapshot.capture(hub) == snapshot:
                    hub.dirty = False
                return True
        except Exception as exc:  # noqa: BLE001
            if idle_snapshot is not None:
                # 断点、占用、授权可能先于下一事件推进。该来源已失效；仅存储等暂时故障按窗口重试。
                if isinstance(exc, AppError) and exc.status_code < 500 and not isinstance(exc.__context__, DBAPIError):
                    logger.debug("任务 %s 的定时输出未通过原执行核验", hub.task_id)
                    return True
                logger.error("任务 %s 定时刷新输出失败", hub.task_id)
                return False
            logger.error("任务 %s 刷新输出失败", hub.task_id)
            if handler is not None and getattr(handler, "execution_identity_aware", False) is True:
                hub.text, hub.result, hub.paused = "", None, None
                code = _policy_code(exc)
                if code is not None:
                    raise VisibilityPolicyError(_POLICY_MESSAGES[code], extra={"code": code}) from None
                raise VisibilityPolicyError("任务输出授权已变化", extra={"code": "task_output_changed"}) from None
            return False

    @staticmethod
    async def _owns_task_round(db: AsyncSession, task: Task, *, status: str) -> bool:
        """核验原任务轮次和执行者，用于无授权回调的发布及收尾入口。"""
        conditions = (
            Task.id == task.id, Task.statistics_key == task.statistics_key, Task.queued_at == task.queued_at,
            Task.execution_key == task.execution_key, Task.worker_key == task.worker_key, Task.status == status,
        )
        if db.get_bind().dialect.name == "sqlite":
            # SQLite 的 SELECT 不持有行锁；先取得写事务，直到事件发布或检查返回。
            locked = await db.execute(update(Task).where(*conditions).values(updated_at=Task.updated_at))
            if locked.rowcount != 1:
                return False
        elif await db.scalar(select(Task.id).where(*conditions).with_for_update()) is None:
            return False
        try:
            await require_live_worker(db, task.worker_key)
        except ConflictError:
            return False
        return True

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
        claim: chapter_claim.ChapterClaim | None = None,
        execution: TaskExecutionIdentity | None = None,
        current_execution: TaskExecutionIdentity | None = None,
        job: Any = None,
    ) -> None:
        self._finalizing.add(task_id)
        out: dict[str, Any] | None = None
        committed = False
        winner_marker = None
        abort_ok = status in ("succeeded", "paused") or (handler is not None and task is not None)
        # 本次 on_abort 失败且已按原身份快照登记，可由管理员 drain / 项目排空按原身份重试。
        abort_failure_snapshotted = False
        abort_snapshot = None
        # 收据受限收尾（E5-d）提交后的行；终态帧由按原轮次重读的已提交行构造。
        receipt_row: Task | None = None
        try:
            if task is not None and execution is not None and task.worker_key != execution.worker_key:
                status, result, error = "interrupted", None, TASK_GONE_MESSAGE
                return
            if task is not None and task.worker_key is not None:
                async with self.ctx.sessionmaker() as db:
                    if not await self._owns_task_round(db, task, status="running"):
                        status, result, error = "interrupted", None, TASK_GONE_MESSAGE
                        return
            try:
                pending = await self._durable_rich_review(task)
            except Exception:  # noqa: BLE001
                if task is not None:
                    await self.record_uncertain_execution(task)
                status, result, error = "failed", None, "提案保存结果尚未确认，请稍后核对"
                return
            if pending is not None and status not in ("succeeded", "paused"):
                current, identity = pending
                # 一旦提案事务提交，后续异常/关机不能重发模型；显式取消只丢弃提案、不回滚后来抢占。
                if status != "cancelled" and current.admin_cancel_operation_id is None:
                    status, result, error = "paused", identity, ""
                abort_ok = True
            if (pending is None and status not in ("succeeded", "paused") and task is not None and claim is not None
                    and task.kind in _SINGLE_CHAPTER_KINDS):
                # 保存事务可能已提交（取消/核验失败落在提交与 done 之间）：原 claim 已在保存内释放，
                # on_abort 无从回退。凭持久完成收据受限收尾，避免以授权失效为由永久占用章节。
                if (receipt_row := await self._complete_from_chapter_receipt(
                    task, original_claim=claim, result=self._done_candidate(task_id, hub),
                )) is not None:
                    status, result, error = "receipt_completed", None, RECEIPT_FINALIZED_DETAIL
                    return
            if (pending is None and status not in ("succeeded", "paused") and task is not None
                    and execution is not None and task.kind == "chapter.analyze"):
                # 同上：分析写回事务可能已提交（撤权/改密落在提交与 done 核验之间），凭完成收据受限收尾，
                # 避免原任务永久占用 analysis:{project} 串行键。
                if (receipt_row := await self._complete_from_analysis_receipt(task, original=execution)) is not None:
                    status, result, error = "receipt_completed", None, ANALYSIS_RECEIPT_FINALIZED_DETAIL
                    return
            if (pending is None and status not in ("succeeded", "paused") and task is not None
                    and execution is not None and task.kind in _WIZARD_KINDS):
                # 向导内容与完成收据已同事务提交，但完成事件核验或取消（撤权复查/用户取消）落在提交之后：
                # 凭收据受限收尾，避免以授权失效为由永久占用 wizard:{project}:{kind}（E4-g3c）。
                if (receipt_row := await self._complete_from_wizard_receipt(task, original=execution)) is not None:
                    status, result, error = "receipt_completed", None, WIZARD_RECEIPT_FINALIZED_DETAIL
                    return
            if (pending is None and status == "failed" and task is not None and execution is not None
                    and task.kind == "chapter.batch" and job is not None
                    and getattr(job, "active_claim", True) is None
                    and getattr(job, "persisted_cursor", None) == getattr(job, "total", -1)
                    and isinstance(getattr(job, "persisted_checkpoint", None), dict)
                    and job.persisted_checkpoint.get("phase") == "generate"
                    and "_batch_completion" not in job.persisted_checkpoint):
                # 批量全部步骤已按原断点持久化，仅完成事件发布/核验因授权或设置变化失败：
                # 与单章/分析收据收尾一致，按持久断点重建结果记为成功（不发布、不派生）。
                if (receipt_row := await self._complete_from_batch_checkpoint(
                    task, original=execution, status="succeeded", error="",
                )) is not None:
                    status, result, error = "receipt_completed", None, BATCH_RECEIPT_FINALIZED_DETAIL
                    return
            # 执行体判定的原停止原因：on_abort 失败会改写 error，按持久断点收尾时仍记录原因。
            run_error = error
            # 暂停时资源已在当前步结束时释放，断点保留，不回退
            if status not in ("succeeded", "paused") and pending is None and handler is not None and task is not None:
                abort_snapshot = self.capture_abort_snapshot(task, claim=claim)
                try:
                    options = {"execution": execution} if getattr(handler, "execution_identity_aware", False) is True \
                        else {}
                    if task.kind in _SINGLE_CHAPTER_KINDS:
                        options["claim"] = claim
                    await handler.on_abort(self.ctx, task, **options)
                    if execution is not None and job is not None:
                        advanced = getattr(job, "task_execution_identity", current_execution)
                        if not same_execution(execution, advanced):
                            raise ConflictError("任务执行身份已变化")
                        current_execution = advanced
                    await self.clear_abort_failure(abort_snapshot)
                except Exception:
                    abort_ok = False
                    if task.project_id is not None:
                        await self.record_abort_failure(task.project_id, abort_snapshot)
                        abort_failure_snapshotted = abort_snapshot is not None
                    error = "任务收尾失败，请稍后重试"
                    logger.error("任务取消收尾失败 task=%s", task_id)
            receipt_result = result if status == "succeeded" and claim is not None else None
            try:
                key = task.statistics_key if task is not None else None
                if key is not None and status in ("succeeded", "paused"):
                    winner_marker = (
                        task.queued_at.replace(tzinfo=None) if task.queued_at is not None else None,
                        dict(status=status, result=result, error=error, text=hub.text, progress=hub.progress,
                             execution_identity=execution, current_execution_identity=current_execution, job=job),
                        task.execution_key,
                        task,
                    )
                    self.admin_cancellations.winners[key] = winner_marker
                intended_status = status
                row, committed = await self.complete_state(
                    task, expected_status="running", status=status, result=result, error=error,
                    text=hub.text, progress=hub.progress, abort_ok=abort_ok,
                    review_proof=pending[1] if pending is not None else None,
                    execution_identity=execution,
                    current_execution_identity=current_execution,
                    job=job,
                )
                if row is not None:
                    if key is not None and row.status != "running":
                        self.admin_cancellations.winners.pop(key, None)
                    out = task_service.out_json(row)
                    status, result, error = row.status, row.result, row.error
                    if not committed and row.status == "running":
                        if key is not None and self.admin_cancellations.winners.get(key) is winner_marker:
                            self.admin_cancellations.winners.pop(key, None)
                        if receipt_result is not None and (receipt_row := await self._complete_from_chapter_receipt(
                            task, original_claim=claim, result=receipt_result,
                        )) is not None:
                            # 成功终态核验在提交后因授权/设置变化未通过：按收据受限收尾，不再走 on_success。
                            out, status, result, error = None, "receipt_completed", None, RECEIPT_FINALIZED_DETAIL
                        elif (status == "succeeded" and task is not None and task.kind == "chapter.analyze"
                              and execution is not None
                              and (receipt_row := await self._complete_from_analysis_receipt(
                                  task, original=execution)) is not None):
                            out, status, result, error = (None, "receipt_completed", None,
                                                          ANALYSIS_RECEIPT_FINALIZED_DETAIL)
                        elif (status == "succeeded" and task is not None and task.kind in _WIZARD_KINDS
                              and execution is not None
                              and (receipt_row := await self._complete_from_wizard_receipt(
                                  task, original=execution)) is not None):
                            out, status, result, error = (None, "receipt_completed", None,
                                                          WIZARD_RECEIPT_FINALIZED_DETAIL)
                        elif (pending is None and task is not None and task.kind == "chapter.batch"
                              and execution is not None and row.admin_cancel_operation_id is None
                              and intended_status in ("succeeded", "failed", "cancelled", "interrupted")
                              and (receipt_row := await self._complete_from_batch_checkpoint(
                                  task, original=execution, status=intended_status, error=run_error,
                              )) is not None):
                            # 执行体快照无法证明（如取消落在某章保存提交之后），由持久断点证明本轮事实后收尾。
                            await self.clear_abort_failure(abort_snapshot)
                            out, result = None, None
                            status, error = "receipt_completed", (
                                BATCH_RECEIPT_FINALIZED_DETAIL if intended_status == "succeeded" else run_error
                            )
                        elif abort_failure_snapshotted and row.admin_cancel_operation_id is not None:
                            # complete_state 因管理员意图下收尾失败而故意保留屏障，并非提交结果未知；
                            # 原快照已登记，不能再记为无身份失败，否则项目永远无法按原身份重试排空。
                            status, result, error = "failed", None, "任务收尾失败，请稍后重试"
                        else:
                            if task is not None:
                                await self.record_uncertain_execution(task)
                            status, result, error = "failed", None, "任务保存结果尚未确认，请稍后核对"
                    async with self.ctx.sessionmaker() as db:
                        await task_service.prune_finished(db, row.user_id, source=task)
                else:
                    if key is not None:
                        self.admin_cancellations.winners.pop(key, None)
                    status, result, error = "interrupted", None, TASK_GONE_MESSAGE
            except Exception as finalize_exc:  # noqa: BLE001
                logger.error("任务结束状态写入失败 task=%s", task_id)
                # commit可能已提交而确认丢失；只重读原UUID，不把真实赢家变成待abort的孤立running。
                fact = None
                if task is not None and task.statistics_key is not None:
                    try:
                        async with self.ctx.sessionmaker() as db:
                            fact = await db.scalar(select(Task).where(
                                Task.id == task_id, Task.statistics_key == task.statistics_key,
                                Task.queued_at == task.queued_at, Task.worker_key == task.worker_key,
                                Task.execution_key == (task.execution_key if status == "succeeded" or not abort_ok
                                                       else None),
                            ))
                        if fact is not None and fact.status != "running" and fact.admin_cancel_operation_id is None:
                            status, result, error = fact.status, fact.result, fact.error
                            out = task_service.out_json(fact)
                            committed = True
                            self.admin_cancellations.winners.pop(task.statistics_key, None)
                            self.admin_cancellations.remember_completed(fact)
                    except Exception:
                        logger.error("任务结束事实尚无法确认 task_key=%s", task.statistics_key)
                if not committed and receipt_result is not None and task is not None:
                    if self.admin_cancellations.winners.get(task.statistics_key) is winner_marker:
                        self.admin_cancellations.winners.pop(task.statistics_key, None)
                    if (receipt_row := await self._complete_from_chapter_receipt(
                        task, original_claim=claim, result=receipt_result,
                    )) is not None:
                        out, status, result, error = None, "receipt_completed", None, RECEIPT_FINALIZED_DETAIL
                        return
                if (not committed and status == "succeeded" and task is not None and task.kind == "chapter.analyze"
                        and execution is not None):
                    if self.admin_cancellations.winners.get(task.statistics_key) is winner_marker:
                        self.admin_cancellations.winners.pop(task.statistics_key, None)
                    if (receipt_row := await self._complete_from_analysis_receipt(
                            task, original=execution)) is not None:
                        out, status, result, error = (None, "receipt_completed", None,
                                                      ANALYSIS_RECEIPT_FINALIZED_DETAIL)
                        return
                if (not committed and status == "succeeded" and task is not None and task.kind in _WIZARD_KINDS
                        and execution is not None):
                    # 终态核验（verify_finalization 按原 grant 重验、终态认证）在提交前失败：内容早已随收据提交，
                    # 按收据受限收尾为成功（不发布），而不是停在 running 并登记清理失败（E4-g3c）。
                    if self.admin_cancellations.winners.get(task.statistics_key) is winner_marker:
                        self.admin_cancellations.winners.pop(task.statistics_key, None)
                    if (receipt_row := await self._complete_from_wizard_receipt(
                            task, original=execution)) is not None:
                        out, status, result, error = (None, "receipt_completed", None,
                                                      WIZARD_RECEIPT_FINALIZED_DETAIL)
                        return
                if (not committed and pending is None and task is not None and task.kind == "chapter.batch"
                        and execution is not None):
                    # 终态核验在提交前因撤权/改密等失败（异常）：按持久断点受限收尾。待审核点无法发布时
                    # 以失败结束并保留断点（之后可重试从断点继续），不能留下永久 running。
                    if self.admin_cancellations.winners.get(task.statistics_key) is winner_marker:
                        self.admin_cancellations.winners.pop(task.statistics_key, None)
                    fallback_status, fallback_error = (
                        ("failed", _POLICY_MESSAGES["task_output_changed"]) if status == "paused"
                        else (status, run_error)
                    )
                    if (receipt_row := await self._complete_from_batch_checkpoint(
                        task, original=execution, status=fallback_status, error=fallback_error,
                    )) is not None:
                        await self.clear_abort_failure(abort_snapshot)
                        out, result = None, None
                        status, error = "receipt_completed", (
                            BATCH_RECEIPT_FINALIZED_DETAIL if fallback_status == "succeeded" else fallback_error
                        )
                        return
                if (not committed and status == "succeeded" and pending is None and task is not None
                        and execution is not None and task.kind in _DRAFT_ONLY_KINDS):
                    # 草稿任务的成功终态核验失败：没有已提交的业务副作用需要证明，按原 UUID/轮次/执行键改记
                    # 无结果失败（不发布草稿），避免以授权失效为由永久占用活动资源键。
                    if self.admin_cancellations.winners.get(task.statistics_key) is winner_marker:
                        self.admin_cancellations.winners.pop(task.statistics_key, None)
                    # 原提示词来源设定在发送后被修改时保留更准确的固定文案（与完成事件核验路径一致，
                    # 不透传异常正文）；其余授权/设置变化统一为任务输出授权已变化。
                    draft_code = ("entity_source_changed" if _policy_code(finalize_exc) == "entity_source_changed"
                                  else "task_output_changed")
                    try:
                        row, committed = await self.complete_state(
                            task, expected_status="running", status="failed", result=None,
                            error=_POLICY_MESSAGES[draft_code], text=hub.text, progress=hub.progress,
                            abort_ok=True, execution_identity=execution,
                            current_execution_identity=current_execution, job=job,
                        )
                    except Exception:  # noqa: BLE001
                        row = None
                        logger.error("草稿任务失败终态写入失败 task=%s", task_id)
                    if committed and row is not None and row.status != "running":
                        out = task_service.out_json(row)
                        status, result, error = row.status, row.result, row.error
                        # 与普通终态一致按用户上限裁剪已结束任务；裁剪失败不影响已提交的终态。
                        try:
                            async with self.ctx.sessionmaker() as db:
                                await task_service.prune_finished(db, row.user_id, source=task)
                        except Exception:  # noqa: BLE001
                            logger.error("草稿任务终态后裁剪失败 task=%s", task_id)
                        return
                if (not committed and status == "paused" and pending is None and task is not None
                        and task.kind == "agent.turn" and execution is not None
                        and execution.grant_identity is not None and handler is not None
                        and (finalize := getattr(handler, "finalize_revoked_pause", None)) is not None):
                    # E4-i 裁决 11：断点（待确认提案）已提交、暂停终态核验因协作授权失效而失败。锁内复核失效并作废
                    # 本轮提案后按原 UUID/轮次/执行键 CAS 为 failed，不能停在 running 占用会话直到重启。
                    # 授权仍有效（其他核验失败）时保持原屏障。
                    try:
                        code = await finalize(self.ctx, task, execution=execution)
                    except Exception:  # noqa: BLE001  不输出可能含任务输入的 SQL 参数。
                        code = None
                        logger.error("协作轮次暂停核验失败后的受限收尾未完成 task_key=%s", task.statistics_key)
                    if code is not None:
                        if self.admin_cancellations.winners.get(task.statistics_key) is winner_marker:
                            self.admin_cancellations.winners.pop(task.statistics_key, None)
                        try:
                            row, committed = await self.complete_state(
                                task, expected_status="running", status="failed", result=None,
                                error=COLLABORATION_FAILURE_MESSAGES[code], text=hub.text, progress=hub.progress,
                                abort_ok=True, execution_identity=execution,
                                current_execution_identity=current_execution, job=job,
                            )
                        except Exception:  # noqa: BLE001
                            row = None
                            logger.error("协作轮次失败终态写入失败 task=%s", task_id)
                        if committed and row is not None and row.status != "running":
                            out = task_service.out_json(row)
                            status, result, error = row.status, row.result, row.error
                            try:
                                async with self.ctx.sessionmaker() as db:
                                    await task_service.prune_finished(db, row.user_id, source=task)
                            except Exception:  # noqa: BLE001
                                logger.error("协作轮次终态后裁剪失败 task=%s", task_id)
                            return
                if not committed:
                    if (task is not None and task.kind in _SINGLE_CHAPTER_KINDS | {"chapter.analyze"}
                            and task.statistics_key is not None
                            and self.admin_cancellations.winners.get(task.statistics_key) is winner_marker):
                        self.admin_cancellations.winners.pop(task.statistics_key, None)
                    if task is not None:
                        await self.record_uncertain_execution(task)
                    status, result, error = "failed", None, "任务结束状态保存失败，请稍后重试"
            if committed and status == "succeeded" and handler is not None and task is not None:
                callback_ok = await self._on_success(handler, task, execution=execution)
                if not callback_ok and task.kind in _SINGLE_CHAPTER_KINDS:
                    warning = "正文已保存，自动分析状态未确认，请查看分析任务"
                    if await self._mark_followup_warning(task, warning):
                        if out is not None:
                            out = {**out, "error": warning}
                await self.clear_finished_execution(task)
                async with self.ctx.sessionmaker() as db:
                    await task_service.prune_finished(db, task.user_id, source=task)
            if committed and task is not None and execution is not None and status != "running":
                try:
                    from app.services.task_engine.stream_evidence import capture_task_view, issue_terminal

                    async with self.ctx.sessionmaker() as db:
                        fact = await db.scalar(select(Task).where(
                            Task.id == execution.task_id, Task.statistics_key == execution.task_key,
                            Task.user_id == execution.actor_id, Task.project_id == execution.project_id,
                            Task.kind == execution.kind, Task.queued_at == execution.queued_at, Task.status == status,
                            Task.worker_key == execution.worker_key,
                        ))
                        if (fact is not None and json.dumps(fact.result, sort_keys=True, separators=(",", ":"))
                                == json.dumps(result, sort_keys=True, separators=(",", ":"))
                                and fact.output_text == hub.text):
                            hub.terminal_source = issue_terminal(execution, capture_task_view(fact))
                except Exception:  # noqa: BLE001  无法证明已提交终态时不为排队帧补签。
                    hub.terminal_source = None
        finally:
            self._done_candidates.pop(task_id, None)
            receipt_frame, recancel = None, None
            if status == "receipt_completed" and receipt_row is not None:
                try:
                    receipt_frame = await asyncio.shield(self._receipt_terminal_frame(receipt_row))
                except asyncio.CancelledError as exc:  # 仍须关闭 hub；取消在收尾后原样传递。
                    recancel = exc
                except Exception:  # noqa: BLE001  无法证明已提交终态时退回不含快照的固定 error 帧。
                    logger.error("收据收尾终态帧重读失败 task=%s", task_id)
            if self.hubs.get(task_id) is hub:
                self.hubs.pop(task_id, None)
            if receipt_frame is not None:
                # 帧来源是已提交行的持久视图（E5-b 回放证明），不签发 PublicationSource/TerminalEvidence。
                hub.source = receipt_frame[2]
                hub.close(receipt_frame[0], receipt_frame[1])
            elif status == "succeeded":
                hub.close("done", {"task": out, "result": result})
            elif status == "paused":
                hub.close("paused", {"task": out, "result": result})
            else:
                hub.close("error", {"task": out, "detail": error})
            self._finalizing.discard(task_id)
            if recancel is not None:
                raise recancel

    async def _receipt_terminal_frame(self, row: Task) -> tuple[str, dict[str, Any], Any] | None:
        """收据受限收尾（E5-d）的终态帧：按原 UUID/入队轮次重读已提交的终态行，与持久回放同一映射。

        成功行（固定 NOTE）→ `done {task, result: None, unpublished: True, detail}`；批量按断点收尾的
        失败/取消/中断行 → `error {task, detail}`。来源是该行的持久视图，受控订阅按 E5-b 持久回放证明在发送时
        锁内重算并按订阅原绑定复核授权；不依赖当前可见性戳或模型绑定。无法重读时返回 None（调用方退回旧帧）。
        """
        from app.services.task_engine.stream_evidence import capture_task_view, persisted_replay_events

        if row.statistics_key is None or row.status not in FINISHED_TASK_STATUSES:
            return None
        async with self.ctx.sessionmaker() as db:
            fact = await db.scalar(select(Task).where(
                Task.id == row.id, Task.statistics_key == row.statistics_key, Task.user_id == row.user_id,
                Task.project_id == row.project_id, Task.kind == row.kind, Task.queued_at == row.queued_at,
                Task.status == row.status, Task.execution_key.is_(None), Task.worker_key == row.worker_key,
            ).execution_options(populate_existing=True))
            if fact is None or (fact.status == "succeeded" and receipt_unpublished_detail(fact) is None):
                return None
            event, data = persisted_replay_events(fact)[1]
            return event, data, capture_task_view(fact)

    def _done_candidate(self, task_id: int, hub: TaskHub) -> dict[str, Any] | None:
        stored = self._done_candidates.get(task_id)
        if stored is None or stored[0] != hub.execution_key:
            return None
        return stored[1]

    async def _complete_from_chapter_receipt(
        self, task: Task, *, original_claim: chapter_claim.ChapterClaim | None = None,
        result: dict[str, Any] | None = None,
    ) -> Task | None:
        """单章受限系统收尾：保存事务已提交且持久完成收据逐字可证时，把原轮次 Task 记为成功。

        不要求当前授权/令牌/可见性（这正是撤权、改密、改模型落在提交与 done 核验之间的情形），
        但只认：原 UUID/入队轮次/执行键仍为 running、无管理员意图（drain 负责），以及
        `completed_chapter_receipt` 对收据、输入摘要、版本摘要与（协作）模型/日志来源的完整核验。
        只更新 Task 终态并释放活动资源；result 仅在与收据 result_hash 一致时保存，否则置空。
        不写正文/版本/日志、不派生后续任务、不向外发送。无法证明时返回 None，由调用方保留原屏障。
        """
        if (task.kind not in _SINGLE_CHAPTER_KINDS or task.statistics_key is None or task.project_id is None
                or task.execution_key is None or task.chapter_id is None or not isinstance(task.queued_at, datetime)):
            return None
        worker_key = original_claim.worker_key if original_claim is not None else task.worker_key
        if task.worker_key != worker_key:
            return None
        conditions = (
            Task.worker_key == worker_key,
            Task.id == task.id, Task.statistics_key == task.statistics_key, Task.user_id == task.user_id,
            Task.project_id == task.project_id, Task.chapter_id == task.chapter_id, Task.kind == task.kind,
            Task.queued_at == task.queued_at, Task.execution_key == task.execution_key, Task.status == "running",
            Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
        )
        try:
            async with self.ctx.sessionmaker() as db:
                probe = (await db.execute(select(Task.input, Task.checkpoint).where(*conditions))).one_or_none()
                await db.rollback()
                if (probe is None or not isinstance(probe.input, dict) or not isinstance(probe.checkpoint, dict)
                        or not isinstance(probe.checkpoint.get("_chapter_claim"), dict)):
                    return None
                pending = probe.checkpoint["_chapter_claim"]
                claim = chapter_claim.ChapterClaim(
                    task_id=task.id, task_key=task.statistics_key, actor_id=task.user_id, project_id=task.project_id,
                    chapter_id=task.chapter_id, queued_at=chapter_claim._utc(task.queued_at),
                    execution_key=task.execution_key, claim_key=pending.get("claim_key"),
                    prev_status=pending.get("prev_status"), grant_identity=chapter_claim._grant(probe.input),
                    phase="running", kind="single", worker_key=worker_key,
                )
                if original_claim is not None and original_claim != claim:
                    return None
                receipt = await generation_service.completed_chapter_receipt(db, claim)
                if receipt is None:
                    await db.rollback()
                    return None
                await require_live_worker(db, worker_key)
                verified = (result if isinstance(result, dict)
                            and generation_service._receipt_digest(result) == receipt.get("result_hash") else None)
                stamp = task_statistics_service.now_utc()
                # completed_chapter_receipt 已在本事务内锁作品与 Task 行；条件 UPDATE 仍按原轮次逐字 CAS。
                changed = await db.execute(update(Task).where(*conditions).values(
                    cancel_requested_at=None,
                    status="succeeded", result=verified, error=RECEIPT_FINALIZED_NOTE, progress=100,
                    stage=task_service.STAGE_LABELS["succeeded"], finished_at=stamp, execution_key=None,
                ).execution_options(synchronize_session="fetch"))
                if changed.rowcount != 1:
                    await db.rollback()
                    return None
                row = await db.get(Task, task.id)
                await task_statistics_service.record_state_change(db, row, "succeeded", finished_at=stamp)
                try:
                    await db.commit()
                except Exception:
                    await db.rollback()
                    async with self.ctx.sessionmaker() as proof:
                        row = await proof.scalar(select(Task).where(
                            Task.id == task.id, Task.statistics_key == task.statistics_key,
                            Task.queued_at == task.queued_at, Task.status == "succeeded",
                            Task.worker_key == worker_key, Task.execution_key.is_(None),
                            Task.error == RECEIPT_FINALIZED_NOTE,
                        ))
                    if row is None:
                        if original_claim is None:
                            raise
                        return None
            async with self.ctx.sessionmaker() as db:
                await task_service.prune_finished(db, row.user_id, source=task)
            logger.warning("单章任务按完成收据受限收尾 task_key=%s", task.statistics_key)
            return row
        except Exception as exc:  # noqa: BLE001  无法证明时保留原屏障，不输出含任务输入的异常参数。
            if original_claim is None and _receipt_storage_failure(exc):
                # 暂时存储故障需由原接管者在下个周期重试，不能转成永久待核对。
                raise
            logger.error("单章完成收据收尾未完成 task_key=%s", task.statistics_key)
            return None

    async def _complete_from_analysis_receipt(
        self, task: Task, *, original: TaskExecutionIdentity | None = None,
    ) -> Task | None:
        """章节分析受限系统收尾：写回事务已提交且持久完成收据逐字可证时，把原轮次 Task 记为成功。

        与 `_complete_from_chapter_receipt` 同一边界：不要求当前授权/令牌/可见性，只认原 UUID/入队轮次/
        执行键仍为 running、无管理员意图，以及 `completed_analysis_receipt` 对原输入摘要、模型/向量绑定、
        授权轮次、分析行归属与结果/分析摘要的完整核验（锁作品 → Task → 分析行）。result 由收据逐字重建
        （终态删除/裁剪证明要求与收据一致）。不写分析/设定/记忆、不派生记忆索引、不向外发送。
        无法证明时返回 None，由调用方保留原屏障。
        """
        if (task.kind != analysis_service.KIND or task.statistics_key is None or task.project_id is None
                or task.execution_key is None or task.chapter_id is None or not isinstance(task.queued_at, datetime)):
            return None
        worker_key = original.worker_key if original is not None else task.worker_key
        if task.worker_key != worker_key:
            return None
        conditions = (
            Task.worker_key == worker_key,
            Task.id == task.id, Task.statistics_key == task.statistics_key, Task.user_id == task.user_id,
            Task.project_id == task.project_id, Task.chapter_id == task.chapter_id, Task.kind == task.kind,
            Task.queued_at == task.queued_at, Task.execution_key == task.execution_key, Task.status == "running",
            Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
        )

        def _identity(value: Any) -> tuple[Any, ...]:
            return (value.task_id, value.task_key, value.actor_id, value.project_id, value.execution_key,
                    value.grant_identity, value.task_chapter_id, value.input_hash, value.actor_token_version,
                    value.model_binding, value.embedding_selection)

        try:
            async with self.ctx.sessionmaker() as db:
                row = await db.scalar(select(Task).where(*conditions).execution_options(populate_existing=True))
                if row is None or not isinstance(row.checkpoint, dict) or "_analysis_completion" not in row.checkpoint:
                    await db.rollback()
                    return None
                execution = analysis_service.capture_analysis_execution(row)
                if original is not None and _identity(
                    analysis_service.analysis_execution_from_claim(original),
                ) != _identity(execution):
                    await db.rollback()
                    return None
                await db.rollback()
                proof = await analysis_service.completed_analysis_receipt(db, execution, status="running")
                if proof is None:
                    await db.rollback()
                    return None
                await require_live_worker(db, worker_key)
                stamp = task_statistics_service.now_utc()
                changed = await db.execute(update(Task).where(*conditions).values(
                    cancel_requested_at=None,
                    status="succeeded", result=proof[1], error=ANALYSIS_RECEIPT_FINALIZED_NOTE, progress=100,
                    stage=task_service.STAGE_LABELS["succeeded"], finished_at=stamp, execution_key=None,
                ).execution_options(synchronize_session="fetch"))
                if changed.rowcount != 1:
                    await db.rollback()
                    return None
                # 前面的回滚已使探查读到的实例过期：重新载入，避免在同步上下文触发隐式加载。
                done = await db.get(Task, task.id, populate_existing=True)
                await task_statistics_service.record_state_change(db, done, "succeeded", finished_at=stamp)
                try:
                    await db.commit()
                except Exception:
                    await db.rollback()
                    async with self.ctx.sessionmaker() as verify:
                        done = await verify.scalar(select(Task).where(
                            Task.id == task.id, Task.statistics_key == task.statistics_key,
                            Task.queued_at == task.queued_at, Task.status == "succeeded",
                            Task.worker_key == worker_key, Task.execution_key.is_(None),
                            Task.error == ANALYSIS_RECEIPT_FINALIZED_NOTE,
                        ))
                    if done is None:
                        if original is None:
                            raise
                        return None
            async with self.ctx.sessionmaker() as db:
                await task_service.prune_finished(db, done.user_id, source=task)
            logger.warning("章节分析按完成收据受限收尾 task_key=%s", task.statistics_key)
            return done
        except Exception as exc:  # noqa: BLE001  无法证明时保留原屏障，不输出含任务输入的异常参数。
            if original is None and _receipt_storage_failure(exc):
                # 暂时存储故障需由原接管者在下个周期重试，不能转成永久待核对。
                raise
            logger.error("章节分析完成收据收尾未完成 task_key=%s", task.statistics_key)
            return None

    async def _complete_from_wizard_receipt(
        self, task: Task, *, original: TaskExecutionIdentity | None = None,
    ) -> Task | None:
        """开书向导受限系统收尾（E4-g3c）：内容替换与完成收据已同事务提交且收据逐字可证时，把原轮次 Task 记为成功。

        与单章/分析收据收尾同一边界：不要求当前授权/令牌/可见性（撤权、降级、关作者模型授权、改密恰好落在提交与
        完成事件/终态核验之间），只认原 UUID/入队轮次/执行键仍为 running、无管理员意图，以及
        `wizard_service.completed_wizard_receipt` 在 作品 → Task 锁内对收据、原输入摘要、模型绑定与原 grant、
        结果摘要与输出全文摘要的核验。result 取自收据（终态删除/裁剪证明要求与之逐字一致）。只更新本 Task 终态并释放
        `wizard:{project}:{kind}`；不写作品内容、不发布结果、不派生、不向外发送。
        无法证明时返回 None，由调用方保留原屏障。
        """
        from app.services import wizard_service

        if (task.kind not in _WIZARD_KINDS or task.statistics_key is None or task.project_id is None
                or task.execution_key is None or not isinstance(task.queued_at, datetime)):
            return None
        if original is not None and (
            original.task_id, original.task_key, original.actor_id, original.project_id, original.kind,
            original.execution_key,
        ) != (task.id, task.statistics_key, task.user_id, task.project_id, task.kind, task.execution_key):
            return None
        worker_key = original.worker_key if original is not None else task.worker_key
        if task.worker_key != worker_key:
            return None
        conditions = (
            Task.worker_key == worker_key,
            Task.id == task.id, Task.statistics_key == task.statistics_key, Task.user_id == task.user_id,
            Task.project_id == task.project_id, Task.kind == task.kind, Task.queued_at == task.queued_at,
            Task.execution_key == task.execution_key, Task.status == "running",
            Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
        )
        try:
            async with self.ctx.sessionmaker() as db:
                # 先无锁探查：普通失败（未保存）不必锁作品。
                probe = await db.scalar(select(Task.checkpoint).where(*conditions))
                await db.rollback()
                if not isinstance(probe, dict) or "_wizard_completion" not in probe:
                    return None
                verified = await wizard_service.completed_wizard_receipt(db, task)
                if verified is None:
                    await db.rollback()
                    return None
                await require_live_worker(db, worker_key)
                stamp = task_statistics_service.now_utc()
                changed = await db.execute(update(Task).where(*conditions).values(
                    cancel_requested_at=None,
                    status="succeeded", result=verified, error=WIZARD_RECEIPT_FINALIZED_NOTE, progress=100,
                    stage=task_service.STAGE_LABELS["succeeded"], finished_at=stamp, execution_key=None,
                ).execution_options(synchronize_session="fetch"))
                if changed.rowcount != 1:
                    await db.rollback()
                    return None
                done = await db.get(Task, task.id, populate_existing=True)
                await task_statistics_service.record_state_change(db, done, "succeeded", finished_at=stamp)
                try:
                    await db.commit()
                except Exception:
                    await db.rollback()
                    async with self.ctx.sessionmaker() as verify:
                        done = await verify.scalar(select(Task).where(
                            Task.id == task.id, Task.statistics_key == task.statistics_key,
                            Task.queued_at == task.queued_at, Task.status == "succeeded",
                            Task.worker_key == worker_key, Task.execution_key.is_(None),
                            Task.error == WIZARD_RECEIPT_FINALIZED_NOTE,
                        ))
                    if done is None:
                        if original is None:
                            raise
                        return None
            async with self.ctx.sessionmaker() as db:
                await task_service.prune_finished(db, done.user_id, source=task)
            logger.warning("向导任务按完成收据受限收尾 task_key=%s", task.statistics_key)
            return done
        except Exception as exc:  # noqa: BLE001  无法证明时保留原屏障，不输出含任务输入的异常参数。
            if original is None and _receipt_storage_failure(exc):
                # 暂时存储故障需由原接管者在下个周期重试，不能转成永久待核对。
                raise
            logger.error("向导完成收据收尾未完成 task_key=%s", task.statistics_key)
            return None

    async def _complete_from_batch_checkpoint(
        self, task: Task, *, original: TaskExecutionIdentity, status: str, error: str,
    ) -> Task | None:
        """批量受限系统收尾（E4-g2）：执行体内存快照无法证明、但持久断点可逐字证明本轮事实时结束原轮次。

        典型窗口：取消（撤权复查/用户取消）落在某章保存提交之后、执行体同步断点之前，on_abort 按旧快照无法证明；
        或全部章节已完成、完成事件/终态核验因撤权、改密、关闭作者模型授权而失败。与单章/分析收据收尾同一边界：
        不要求当前授权，只认原 UUID/入队轮次/执行键仍为 running、无管理员意图、原输入逐字一致、断点无活动
        章节占用（current 为空且没有章节仍持本任务占用），存在章节完成收据时经 `_require_resume_receipt`
        锁内核验其属于本轮执行与对应版本。succeeded 仅在全部步骤已持久（cursor == 总数、无待分析收据）时
        按持久断点重建结果；其余以 `_run` 已判定的停止终态（failed/cancelled/interrupted）结束、保留断点供重试。
        只改原 Task 终态并释放 batch 资源键；不写正文/设定/记忆、不派生、不向外发送。无法证明时返回 None。
        """
        from app.services import batch_service
        from app.services.task_engine.execution_identity import _json

        if (task.kind != batch_service.KIND or type(original) is not TaskExecutionIdentity
                or original.kind != batch_service.KIND or original.worker_key != task.worker_key
                or status not in ("succeeded", "failed", "cancelled", "interrupted")
                or task.statistics_key is None or task.project_id is None or task.execution_key is None
                or not isinstance(task.queued_at, datetime)
                or (task.id, task.statistics_key, task.user_id, task.project_id, task.execution_key)
                != (original.task_id, original.task_key, original.actor_id, original.project_id,
                    original.execution_key)
                or chapter_claim._utc(task.queued_at) != original.queued_at):
            return None
        conditions = (
            Task.worker_key == original.worker_key,
            Task.id == task.id, Task.statistics_key == task.statistics_key, Task.user_id == task.user_id,
            Task.project_id == task.project_id, Task.kind == task.kind, Task.queued_at == task.queued_at,
            Task.execution_key == task.execution_key, Task.status == "running",
            Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
        )
        try:
            async with self.ctx.sessionmaker() as db:
                with db.no_autoflush:
                    await project_lifecycle.lock_record(db, task.project_id)
                    found = (await db.execute(select(Task).add_columns(
                        cast(Task.checkpoint, String).label("checkpoint_raw"),
                        cast(Task.input, String).label("input_raw"),
                    ).where(*conditions).with_for_update().execution_options(populate_existing=True))).one_or_none()
                    if found is None:
                        await db.rollback()
                        return None
                    await require_live_worker(db, original.worker_key)
                    row, raw_checkpoint, raw_input = found
                    checkpoint = row.checkpoint
                    original_input = original.input
                    ids = original_input.get("chapter_ids")
                    if (not isinstance(row.input, dict) or _json(row.input) != original.input_json
                            or not isinstance(checkpoint, dict) or checkpoint.get("current") is not None
                            or checkpoint.get("phase") not in ("generate", "analyze")
                            or "_batch_outcome_unknown" in checkpoint or "_batch_outcome_unknown" in row.input
                            or not isinstance(ids, list) or not isinstance(checkpoint.get("items"), list)
                            or len(checkpoint["items"]) != len(ids)
                            or type(row.step_cursor) is not int or not 0 <= row.step_cursor <= len(ids)):
                        await db.rollback()
                        return None
                    held = await db.scalar(select(Chapter.id).where(
                        Chapter.project_id == task.project_id, Chapter.generation_claim_task_key == task.statistics_key,
                    ).limit(1))
                    if held is not None:
                        await db.rollback()
                        return None
                    if "_batch_completion" in checkpoint:
                        receipt = checkpoint.get("_batch_completion")
                        if (not isinstance(receipt, dict) or receipt.get("execution_key") != original.execution_key
                                or receipt.get("task_key") != original.task_key):
                            await db.rollback()
                            return None
                        # 锁内核验收据、原 Task（running 时必须是本执行键）、章节内容版本与成功日志。
                        await batch_service._require_resume_receipt(db, row, checkpoint)
                    result = None
                    if status == "succeeded":
                        if (row.step_cursor != len(ids) or checkpoint.get("phase") != "generate"
                                or "_batch_completion" in checkpoint or "rich_analysis" in checkpoint):
                            await db.rollback()
                            return None
                        result = batch_service.summarize(checkpoint, row.step_cursor, len(ids))
                    stamp = task_statistics_service.now_utc()
                    changed = await db.execute(update(Task).where(
                        *conditions, Task.step_cursor == row.step_cursor,
                        cast(Task.checkpoint, String) == raw_checkpoint,
                        cast(Task.input, String) == raw_input,
                    ).values(
                        cancel_requested_at=None, status=status, result=result,
                        error=BATCH_RECEIPT_FINALIZED_NOTE if status == "succeeded" else error,
                        output_text="", progress=100 if status == "succeeded" else row.progress,
                        stage=task_service.STAGE_LABELS[status], finished_at=stamp, execution_key=None,
                    ).execution_options(synchronize_session="fetch"))
                    if changed.rowcount != 1:
                        await db.rollback()
                        return None
                done = await db.get(Task, task.id, populate_existing=True)
                await task_statistics_service.record_state_change(db, done, status, finished_at=stamp)
                try:
                    await db.commit()
                except Exception:
                    await db.rollback()
                    async with self.ctx.sessionmaker() as verify:
                        done = await verify.scalar(select(Task).where(
                            Task.id == task.id, Task.statistics_key == task.statistics_key,
                            Task.queued_at == task.queued_at, Task.status == status, Task.execution_key.is_(None),
                            Task.worker_key == original.worker_key,
                        ))
                    if done is None:
                        return None
            async with self.ctx.sessionmaker() as db:
                await task_service.prune_finished(db, done.user_id, source=task)
            logger.warning("批量任务按持久断点受限收尾 task_key=%s", task.statistics_key)
            return done
        except Exception:  # noqa: BLE001  无法证明时保留原屏障，不输出含任务输入的异常参数。
            logger.error("批量断点收尾未完成 task_key=%s", task.statistics_key)
            return None

    async def complete_state(
        self, task: Task | None, *, expected_status: str, status: str, result: dict[str, Any] | None,
        error: str, text: str, progress: int, abort_ok: bool, operation_id: str | None = None,
        batch_snapshot: Any = None, abort_snapshot: AbortSnapshot | None = None, batch_receipt_proof: bool = False,
        batch_uncertain_attempt: bool = False,
        recovery_claim: chapter_claim.ChapterClaim | None = None, wizard_pending: bool = False,
        owner_recovery: bool = False,
        review_proof: dict[str, Any] | None = None,
        execution_identity: TaskExecutionIdentity | None = None,
        current_execution_identity: TaskExecutionIdentity | None = None,
        job: Any = None,
    ) -> tuple[Task | None, bool]:
        """状态/统计/清标记同一次CAS提交；接受夹在读写之间时重读同UUID，而不是忽略新意图。"""
        if task is None or task.statistics_key is None:
            return None, False
        if execution_identity is not None and not matches_running(execution_identity, task):
            return None, False
        if (current_execution_identity is not None
                and not same_execution(execution_identity, current_execution_identity)):
            return None, False
        worker_key = execution_identity.worker_key if execution_identity is not None else task.worker_key
        request_owns = self.request_worker.worker_key == worker_key and (
            operation_id is not None or (task.id, task.statistics_key, task.execution_key) in self._adopted
        )
        if expected_status == "running" and worker_key is not None and self.lease.worker_key != worker_key \
                and not request_owns:
            return None, False
        recovery_cleanup = recovery_claim is not None or wizard_pending
        if recovery_cleanup and (
            expected_status != "running" or status != "interrupted" or not abort_ok or worker_key is None
            or recovery_claim is not None and (task.kind not in _SINGLE_CHAPTER_KINDS or wizard_pending)
            or wizard_pending and task.kind not in _WIZARD_KINDS
        ):
            return None, False
        system_cleanup = bool(execution_identity is not None and not abort_ok and result is None
                              and status in ("failed", "cancelled", "interrupted"))
        if system_cleanup:
            # 精确原执行清理失败时只保存无正文终态并保留 execution 屏障；不借新 token 授权发送。
            text = ""
        analysis_success = task.kind == "chapter.analyze" and status == "succeeded"
        analysis_execution = None
        if analysis_success:
            if execution_identity is not None:
                analysis_execution = analysis_service.analysis_execution_from_claim(execution_identity)
            elif task.admin_cancel_operation_id is not None:
                if operation_id != task.admin_cancel_operation_id:
                    return None, False
                analysis_execution = analysis_service.capture_analysis_completion_for_recovery(task)
            else:
                analysis_execution = analysis_service.capture_analysis_execution(task)
        chapter_success = task.kind in _SINGLE_CHAPTER_KINDS and status == "succeeded"
        chapter_execution = None
        if chapter_success:
            from app.services.task_engine.handlers import chapter as chapter_handler

            chapter_execution = chapter_handler.capture_running_claim(task)
        batch_checkpoint = batch_cursor = None
        expected_input_json = None
        if abort_snapshot is not None:
            if (abort_snapshot.task_id, abort_snapshot.task_key, abort_snapshot.actor_id, abort_snapshot.project_id,
                    abort_snapshot.kind, chapter_claim._utc(abort_snapshot.queued_at),
                    abort_snapshot.execution_key, abort_snapshot.worker_key) != (
                    task.id, task.statistics_key, task.user_id, task.project_id, task.kind,
                    chapter_claim._utc(task.queued_at), task.execution_key, worker_key):
                return None, False
            expected_input_json = abort_snapshot.input_json
        if task.kind == "chapter.batch":
            if batch_snapshot is not None:
                from app.services import batch_service

                if (not isinstance(batch_snapshot, batch_service.BatchAbortSnapshot)
                        or (batch_snapshot.task_id, batch_snapshot.task_key, batch_snapshot.actor_id,
                            batch_snapshot.project_id, chapter_claim._utc(batch_snapshot.queued_at),
                            batch_snapshot.execution_key, batch_snapshot.worker_key) != (
                            task.id, task.statistics_key, task.user_id, task.project_id,
                            chapter_claim._utc(task.queued_at), task.execution_key, worker_key)
                        or json.dumps(batch_snapshot.input, sort_keys=True, separators=(",", ":"))
                        != json.dumps(task.input, sort_keys=True, separators=(",", ":"))):
                    return None, False
                batch_checkpoint, batch_cursor = batch_snapshot.checkpoint, batch_snapshot.cursor
            else:
                job = getattr(task, "_batch_job", None)
                batch_checkpoint = deepcopy(job.persisted_checkpoint if job is not None else task.checkpoint)
                batch_cursor = job.persisted_cursor if job is not None else task.step_cursor
            if expected_input_json is None:
                expected_input_json = json.dumps(task.input, sort_keys=True, separators=(",", ":"))
        elif abort_snapshot is not None:
            batch_checkpoint = json.loads(abort_snapshot.checkpoint_json)
            batch_cursor = abort_snapshot.step_cursor
        async with self.ctx.sessionmaker() as db:
            while True:
                if (review_proof is not None or analysis_success or chapter_success
                        or (execution_identity is not None and not system_cleanup)):
                    if ((task.project_id is None and execution_identity is None)
                            or (review_proof is not None and status not in ("paused", "cancelled"))):
                        return None, False
                    actor = (authz.AuthenticatedActor(execution_identity.actor_id,
                                                     execution_identity.actor_token_version)
                             if execution_identity is not None and execution_identity.actor_token_version is not None
                             else task.user_id)
                    if execution_identity is not None and execution_identity.actor_token_version is None:
                        return None, False
                    try:
                        # E4i-b2：协作执行的 verify_finalization（仅 succeeded / paused 调用）会再锁作者 User，
                        # 须与 actor 一起先于门禁升序锁定；其他终态不调用 handler，锁集合不变。
                        owner_id = (frozen_project_owner_id(execution_identity)
                                    if execution_identity is not None and not system_cleanup
                                    and status in ("succeeded", "paused")
                                    and getattr(get_handler(task.kind), "execution_identity_aware", False) is True
                                    else None)
                        await authz.require_authenticated(
                            db, actor, write=True, targets=(owner_id,) if owner_id is not None else (),
                        )
                        if task.project_id is not None:
                            await lock_visibility_write(db, task.project_id)
                    except AppError:
                        if (execution_identity is None or result is not None
                                or status not in ("failed", "cancelled", "interrupted")):
                            raise
                        # 撤权/删除后只允许按原执行与资源事实做无正文系统收尾。
                        await db.rollback()
                        system_cleanup, text = True, ""
                        error = {"failed": "任务执行已停止", "cancelled": CANCELLED_MESSAGE,
                                 "interrupted": INTERRUPTED_MESSAGE}[status]
                        continue
                if (task.kind in _SINGLE_CHAPTER_KINDS | {"chapter.batch"} or abort_snapshot is not None
                        or analysis_success or execution_identity is not None or recovery_cleanup) \
                        and task.project_id is not None:
                    # 每次回滚重试都重取 Project → Task 锁，不能沿用上一事务的门禁。
                    await project_lifecycle.lock_record(db, task.project_id)
                statement = select(Task).where(
                    Task.id == task.id, Task.statistics_key == task.statistics_key,
                    Task.user_id == task.user_id, Task.project_id == task.project_id, Task.kind == task.kind,
                    Task.queued_at == task.queued_at, Task.execution_key == task.execution_key,
                    Task.worker_key == worker_key,
                ).execution_options(populate_existing=True)
                raw_checkpoint = raw_input = None
                if (task.kind == "chapter.batch" or abort_snapshot is not None or analysis_success or chapter_success
                        or execution_identity is not None):
                    row, raw_checkpoint, raw_input = (await db.execute(statement.add_columns(
                        cast(Task.checkpoint, String).label("checkpoint_raw"),
                        cast(Task.input, String).label("input_raw"),
                    ).with_for_update())).one_or_none() or (None, None, None)
                    if row is not None and (task.kind == "chapter.batch" or abort_snapshot is not None) and (
                        row.step_cursor != batch_cursor or json.dumps(
                        row.checkpoint, sort_keys=True, separators=(",", ":"),
                    ) != json.dumps(batch_checkpoint, sort_keys=True, separators=(",", ":")) or json.dumps(
                        row.input, sort_keys=True, separators=(",", ":"),
                    ) != expected_input_json):
                        return row, False
                elif task.kind in _SINGLE_CHAPTER_KINDS or recovery_cleanup or worker_key is not None:
                    statement = statement.with_for_update()
                    row = await db.scalar(statement)
                else:
                    row = await db.scalar(statement)
                if row is None or row.status != expected_status:
                    return row, False
                if execution_identity is not None and task.kind not in _SINGLE_CHAPTER_KINDS | {"chapter.analyze"}:
                    # 原停止及已产出的批量结果可与迟到的管理员意图一起提交。仍需原轮次、
                    # 输入和完整断点，批量赢家还会在下方锁内重新核对模型、结果和输出。
                    proved_stop = status == "cancelled" and abort_ok and result is None
                    batch_winner = task.kind == "chapter.batch" and status in ("succeeded", "paused") and abort_ok
                    stopping_for = row.admin_cancel_operation_id if proved_stop or batch_winner else None
                    if not matches_running(current_execution_identity or execution_identity, row,
                                           cancellation_operation=stopping_for):
                        return row, False
                if expected_status == "running":
                    await require_live_worker(db, worker_key)
                if recovery_cleanup:
                    if row.admin_cancel_operation_id is not None or row.admin_cancel_requested_at is not None:
                        return row, False
                    if recovery_claim is not None:
                        released = await chapter_claim.release_chapter_claim(db, recovery_claim)
                    else:
                        from app.services import wizard_service

                        released = await wizard_service.release_uncommitted_pending(db, row)
                    if not released:
                        raise _UnprovenRecoveryResource()
                    # 两种释放都会修改 Task 的输入或断点。终态 CAS 只能使用本事务刚写完的值。
                    row, raw_checkpoint, raw_input = (await db.execute(statement.add_columns(
                        cast(Task.checkpoint, String).label("checkpoint_raw"),
                        cast(Task.input, String).label("input_raw"),
                    ).with_for_update())).one()
                if analysis_success:
                    verified = await analysis_service.completed_analysis_receipt(db, analysis_execution)
                    if verified is None or json.dumps(verified[1], sort_keys=True, separators=(",", ":")) \
                            != json.dumps(result, sort_keys=True, separators=(",", ":")):
                        return row, False
                if chapter_success:
                    receipt = await generation_service.completed_chapter_receipt(db, chapter_execution)
                    if receipt is None or not isinstance(result, dict) \
                            or receipt.get("result_hash") != generation_service._receipt_digest(result):
                        return row, False
                if review_proof is not None:
                    from app.services.rich_document_review import recover_pending_identity

                    actual = await recover_pending_identity(db, row)
                    if json.dumps(actual, sort_keys=True, separators=(",", ":")) != json.dumps(
                        review_proof, sort_keys=True, separators=(",", ":"),
                    ):
                        return row, False
                if (execution_identity is not None and status in ("succeeded", "paused")
                        and getattr(get_handler(task.kind), "execution_identity_aware", False) is True):
                    verify = getattr(get_handler(task.kind), "verify_finalization", None)
                    if verify is None:
                        return row, False
                    transaction = await self._callback_transaction(db)
                    await verify(db, row, job=job, execution=execution_identity,
                                 current_execution=current_execution_identity or execution_identity,
                                 status=status, result=result, text=text)
                    await self._require_callback_transaction(db, transaction)
                    result = deepcopy(result)
                if (row.kind == "chapter.analyze" and isinstance(row.checkpoint, dict)
                        and ("_analysis_pending" in row.checkpoint
                             or (status != "succeeded" and "_analysis_completion" in row.checkpoint))):
                    # 只有分析 service 清理已证实未提交的 pending 后才能进入普通失败/取消终态。
                    return row, False
                if (row.kind in _SINGLE_CHAPTER_KINDS and status != "succeeded"
                        and isinstance(row.checkpoint, dict) and "_chapter_claim" in row.checkpoint):
                    return row, False
                if (row.kind.startswith("wizard.") and status != "succeeded"
                        and isinstance(row.checkpoint, dict)
                        and any(key in row.checkpoint for key in ("_wizard_pending", "_wizard_completion"))):
                    return row, False
                observed = row.admin_cancel_operation_id
                if batch_receipt_proof:
                    if (task.kind != "chapter.batch" or expected_status != "running" or status != "interrupted"
                            or observed is not None or not isinstance(batch_checkpoint, dict)
                            or "_batch_completion" not in batch_checkpoint):
                        return row, False
                    from app.services import batch_service

                    await batch_service._require_resume_receipt(db, row, batch_checkpoint)
                if operation_id is not None and observed != operation_id:
                    return row, False
                if observed is not None and not abort_ok:
                    # 收尾失败绝不释放管理员意图持有的资源屏障。
                    return row, False
                if batch_uncertain_attempt:
                    if (task.kind != "chapter.batch" or expected_status != "running"
                            or status not in ("cancelled", "interrupted") or batch_snapshot is None
                            or operation_id is None or observed != operation_id):
                        return row, False
                    from app.services import batch_service

                    # 章节释放、未知结果标记、任务终态和统计共用本事务。
                    marked = await batch_service.release_uncertain_attempt(
                        db, batch_snapshot, expected_operation_id=operation_id,
                    )
                    row, raw_checkpoint, raw_input = (await db.execute(statement.add_columns(
                        cast(Task.checkpoint, String).label("checkpoint_raw"),
                        cast(Task.input, String).label("input_raw"),
                    ).with_for_update())).one_or_none() or (None, None, None)
                    if (row is None or json.dumps(row.checkpoint, sort_keys=True, separators=(",", ":"))
                            != json.dumps(marked, sort_keys=True, separators=(",", ":"))
                            or row.step_cursor != batch_cursor or json.dumps(
                                row.input, sort_keys=True, separators=(",", ":"),
                            ) != expected_input_json or row.admin_cancel_operation_id != operation_id):
                        return row, False
                next_input = None
                if observed is not None and row.kind in _SINGLE_CHAPTER_KINDS:
                    from app.services.task_engine.cancellation import _CHAPTER_INTENT, chapter_cancel_intent

                    has_intent = isinstance(row.input, dict) and _CHAPTER_INTENT in row.input
                    intent = row.input.get(_CHAPTER_INTENT) if has_intent else None
                    original_intent = chapter_cancel_intent(task, observed)
                    if not has_intent and status == "succeeded":
                        # 管理员可能在正文提交之后才受理；只有原执行体的完成收据能证明成功获胜。
                        from app.services.task_engine.handlers import chapter as chapter_handler

                        try:
                            original_claim = chapter_handler.capture_running_claim(task)
                            receipt = await generation_service.completed_chapter_receipt(db, original_claim)
                        except AppError:
                            receipt = None
                        if receipt is None:
                            return row, False
                    elif not has_intent or original_intent is None or intent != original_intent:
                        return row, False
                    else:
                        next_input = dict(row.input)
                        next_input.pop(_CHAPTER_INTENT)
                if status == "paused" and isinstance(result, dict) and result.get("reason") == "rich_document_review" \
                        and observed is not None:
                    status, result, error = "cancelled", None, "任务已取消，候选未采纳"
                if owner_recovery and observed is None and abort_ok and row.cancel_requested_at is not None:
                    # 回退已确认时在终态锁内裁决迟到的本人意图；凭据缺损仍沿用 C1 的结果与屏障。
                    status, error = "cancelled", CANCELLED_MESSAGE
                stamp = None if status == "paused" else task_statistics_service.now_utc()
                values = dict(status=status, result=result, error=error, output_text=text,
                              progress=100 if status == "succeeded" else progress,
                              stage=task_service.STAGE_LABELS[status], finished_at=stamp,
                              execution_key=task.execution_key if status == "succeeded" or not abort_ok else None,
                              cancel_requested_at=None, admin_cancel_operation_id=None, admin_cancel_requested_at=None)
                if next_input is not None:
                    values["input"] = next_input
                conditions = [
                    Task.id == task.id, Task.statistics_key == task.statistics_key, Task.status == expected_status,
                    Task.user_id == task.user_id, Task.project_id == task.project_id, Task.kind == task.kind,
                    Task.queued_at == task.queued_at, Task.execution_key == task.execution_key,
                    Task.worker_key == worker_key,
                    Task.admin_cancel_operation_id == observed if observed is not None else (
                        Task.admin_cancel_operation_id.is_(None)
                    ),
                ]
                if (task.kind == "chapter.batch" or abort_snapshot is not None or analysis_success or chapter_success
                        or execution_identity is not None or recovery_cleanup):
                    if task.kind == "chapter.batch" or abort_snapshot is not None:
                        conditions.append(Task.step_cursor == batch_cursor)
                    elif execution_identity is not None:
                        cursor = (current_execution_identity or execution_identity).step_cursor
                        conditions.append(Task.step_cursor == cursor)
                    conditions.extend((cast(Task.checkpoint, String) == raw_checkpoint if raw_checkpoint is not None
                                       else Task.checkpoint.is_(None),
                                       cast(Task.input, String) == raw_input if raw_input is not None
                                       else Task.input.is_(None)))
                changed = await db.execute(update(Task).where(*conditions).values(**values).execution_options(
                             synchronize_session="fetch",
                         ))
                if changed.rowcount != 1:
                    await db.rollback()
                    continue
                if status == "cancelled":
                    from app.services.rich_document_review import discard_pending

                    await discard_pending(db, row)
                await task_statistics_service.record_state_change(db, row, status, finished_at=stamp)
                await cleanup_work.settle(db, self)
                await cleanup_work.finish_retry(db, self)
                if observed is not None:
                    await admin_cleanup.finish(db, task, observed)
                try:
                    await db.commit()
                except Exception:
                    # finalize/recover/drain共用这一提交边界。确认丢失时只回放原UUID、
                    # 原入队轮次已落库的目标状态，不能漏登记完成代次或留下未关闭的Hub。
                    await db.rollback()
                    async with self.ctx.sessionmaker() as proof_db:
                        if batch_uncertain_attempt:
                            from app.services import batch_service

                            if not await batch_service.confirm_uncertain_attempt(
                                proof_db, batch_snapshot, status=status, expected_operation_id=operation_id,
                            ):
                                raise
                        fact = await proof_db.scalar(select(Task).where(
                            Task.id == task.id, Task.statistics_key == task.statistics_key,
                            Task.worker_key == worker_key, Task.queued_at == task.queued_at,
                            Task.execution_key == (
                                task.execution_key if status == "succeeded" or not abort_ok else None
                            ),
                        ))
                        if recovery_cleanup:
                            from app.models import TaskStatistic

                            statistic = await proof_db.get(TaskStatistic, task.statistics_key)
                            if (fact is None or statistic is None or statistic.status != status
                                    or statistic.finished_event_at is None or fact.finished_at is None
                                    or chapter_claim._utc(statistic.finished_event_at)
                                    != chapter_claim._utc(fact.finished_at)):
                                raise
                            if recovery_claim is not None:
                                held = await proof_db.scalar(select(Chapter.id).where(
                                    Chapter.id == recovery_claim.chapter_id,
                                    Chapter.generation_claim_task_key == recovery_claim.task_key,
                                    Chapter.generation_claim_key == recovery_claim.claim_key,
                                ))
                                if held is not None or "_chapter_claim" in (fact.input or {}):
                                    raise
                            elif "_wizard_pending" in (fact.checkpoint or {}):
                                raise
                    if (fact is None or fact.status != status or fact.admin_cancel_operation_id is not None
                            or self.admin_cancellations.round_at(fact) != self.admin_cancellations.round_at(task)):
                        raise
                    row = fact
                if observed is not None:
                    self.admin_cancellations.remember_completed(row)
                return row, True

    async def clear_finished_execution(self, task: Task) -> None:
        """回调完全结束后只清除原成功轮次的持久收尾标记。"""
        if task.statistics_key is None or task.execution_key is None:
            return
        async with self.ctx.sessionmaker() as db:
            changed = await db.execute(update(Task).where(
                Task.id == task.id, Task.statistics_key == task.statistics_key, Task.queued_at == task.queued_at,
                Task.execution_key == task.execution_key, Task.worker_key == task.worker_key,
                Task.status == "succeeded",
                Task.admin_cancel_operation_id.is_(None),
            ).values(execution_key=None))
            if changed.rowcount == 1:
                try:
                    await require_live_worker(db, task.worker_key)
                except ConflictError:
                    return
                await db.commit()

    async def _mark_followup_warning(self, task: Task, warning: str) -> bool:
        """不触碰结果收据；未知派生结果只提示核对，绝不自动重发。"""
        try:
            async with self.ctx.sessionmaker() as db:
                changed = await db.execute(update(Task).where(
                    Task.id == task.id, Task.statistics_key == task.statistics_key, Task.user_id == task.user_id,
                    Task.project_id == task.project_id, Task.kind == task.kind, Task.queued_at == task.queued_at,
                    Task.execution_key == task.execution_key, Task.status == "succeeded",
                    Task.admin_cancel_operation_id.is_(None),
                    Task.worker_key == task.worker_key,
                ).values(error=warning).execution_options(synchronize_session=False))
                if changed.rowcount != 1:
                    return False
                await require_live_worker(db, task.worker_key)
                await db.commit()
                return True
        except Exception:  # noqa: BLE001  提示写入未知时只以原轮次持久事实确认。
            pass
        try:
            async with self.ctx.sessionmaker() as proof:
                actual = await proof.scalar(select(Task.error).where(
                    Task.id == task.id, Task.statistics_key == task.statistics_key,
                    Task.user_id == task.user_id, Task.project_id == task.project_id, Task.kind == task.kind,
                    Task.queued_at == task.queued_at, Task.execution_key == task.execution_key,
                    Task.status == "succeeded", Task.admin_cancel_operation_id.is_(None),
                    Task.worker_key == task.worker_key,
                ))
                return actual == warning
        except Exception:
            return False

    async def _on_success(
        self, handler: TaskHandler, task: Task, *, execution: TaskExecutionIdentity | None = None,
    ) -> bool:
        hook = getattr(handler, "on_success", None)
        if hook is None:
            return True
        try:
            if execution is not None and execution.worker_key != task.worker_key:
                return False
            if task.worker_key is not None:
                async with self.ctx.sessionmaker() as db:
                    if not await self._owns_task_round(db, task, status="succeeded"):
                        return False
            if getattr(handler, "execution_identity_aware", False) is True:
                await hook(self.ctx, task, execution=execution)
            else:
                await hook(self.ctx, task)
            return True
        except Exception:  # noqa: BLE001
            logger.error("任务 %s 成功回调失败", task.id)
            return False

    # ---------------- 取消 ----------------

    def request_admin_cancel(self, task_key: str, operation_id: str) -> None:
        self.admin_cancellations.request(task_key, operation_id)

    async def scan_admin_cancellations(self) -> None:
        await self.admin_cancellations.scan()

    async def wait_admin_cancellations(self) -> None:
        await self.admin_cancellations.wait()

    def signal_running_cancel(
        self, task_id: int, *, statistics_key: str | None, expected_queued_at: Any,
        expected_execution_key: Any,
    ) -> asyncio.Task | None:
        if self._running_statistics_keys.get(task_id) != statistics_key:
            return None
        if (expected_queued_at is not _UNSET_BINDING
                and self._running_rounds.get(task_id, _UNSET_BINDING) != expected_queued_at):
            return None
        if (expected_execution_key is not _UNSET_BINDING
                and self._running_execution_keys.get(task_id) != expected_execution_key):
            return None
        t = self._running.get(task_id)
        if t is None:
            # 已经收尾完毕（库里的 running 是读取时的旧值）
            return None
        if task_id not in self._cancel_requested and task_id not in self._finalizing:
            self._cancel_requested.add(task_id)
            if task_id not in self._claiming:
                t.cancel()
        return t

    async def cancel_running(
        self, task_id: int, *, statistics_key: str | None = None, expected_queued_at: Any = _UNSET_BINDING,
        expected_execution_key: Any = _UNSET_BINDING,
    ) -> None:
        """取消运行中的任务并等待其收尾（最多 cancel_wait_seconds 秒）。"""
        t = self.signal_running_cancel(task_id, statistics_key=statistics_key, expected_queued_at=expected_queued_at,
                                       expected_execution_key=expected_execution_key)
        if t is not None:
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

    def notify_collaboration_change(self, project_id: int, actor_id: int) -> None:
        """业务变更完成后提醒本机和对端，授权只取决于库内状态。"""
        self.recheck_collaboration(project_id, actor_id)
        self.peer_wakes.collaboration(project_id, actor_id)

    def recheck_all_collaboration(self) -> None:
        """批量通知超过容量时，让所有本机活动订阅/执行各自核验原绑定。"""
        for project_id, actor_id in tuple(self._collaboration_subscribers):
            self._signal_collaboration_subscribers(project_id, actor_id)
        if self.running and not self._stopping:
            self._recheck_active_members()
            self._spawn_paused_scan(None, None)
            self.notify()

    def _signal_collaboration_subscribers(self, project_id: int, actor_id: int) -> None:
        self.peer_wakes.member_changed(project_id, actor_id)
        for subscriber in tuple(self._collaboration_subscribers.get((project_id, actor_id), ())):
            try:
                event = (_COLLABORATION_CHECK_EVENT, {}, None) if subscriber.guarded else (
                    _COLLABORATION_CHECK_EVENT, {}
                )
                subscriber.queue.put_nowait(event)
            except asyncio.QueueFull:
                # 已有待消费帧时，消费前同样会重验，不能因唤醒帧挤满而放行。
                pass

    def recheck_collaboration(self, project_id: int, actor_id: int) -> None:
        self._signal_collaboration_subscribers(project_id, actor_id)
        # 唤醒调度器：排队中的协作任务在领取锁内按数据库复核并受限收尾。
        self.notify()
        # 待审核（paused）协作任务没有执行体：按数据库复核后由系统受限收尾（E5-a）。
        if self._loop_task is not None and not self._stopping:
            self._spawn_paused_scan(project_id, actor_id)
        targets = [
            task_id for task_id, owner in tuple(self._running_users.items())
            if owner == actor_id and self._running_projects.get(task_id) == project_id
        ]
        self._request_run_check(project_id, actor_id, targets)

    def _recheck_active_members(self) -> None:
        # 只复查本机已领取且使用协作授权的执行；授权有效时只读，不取得业务写锁。
        groups: dict[tuple[int, int], list[int]] = {}
        for execution in self._project_executions.values():
            if execution.project_id is not None and execution.grant_identity is not None:
                groups.setdefault((execution.project_id, execution.actor_id), []).append(execution.task_id)
        for (project_id, actor_id), tasks in groups.items():
            self._request_run_check(project_id, actor_id, tasks)

    def _request_run_check(self, project_id: int, actor_id: int, tasks: list[int]) -> None:
        if not tasks or self._stopping:
            return
        key = (project_id, actor_id)
        if key in self._pending_run_checks:
            self._pending_run_checks[key].update(tasks)
            return
        pending = self._pending_run_checks[key] = set(tasks)

        async def run() -> None:
            try:
                while pending:
                    targets = list(pending)
                    pending.clear()
                    await self._check_collaboration_runs(project_id, actor_id, targets)
            finally:
                self._pending_run_checks.pop(key, None)

        work = asyncio.create_task(run(), name=f"collaboration-check-{project_id}-{actor_id}")
        self._collaboration_checks.add(work)
        work.add_done_callback(self._collaboration_checks.discard)

    async def _check_collaboration_runs(self, project_id: int, actor_id: int, task_ids: list[int]) -> None:
        """通知只触发复查：数据库确认原协作轮次失效后，才按原执行键取消本进程执行体。

        通知丢失时，调度器也会定期复查仍在运行的协作任务。
        """
        for task_id in task_ids:
            task_key = self._running_statistics_keys.get(task_id)
            execution_key = self._running_execution_keys.get(task_id)
            queued_at = self._running_rounds.get(task_id)
            if task_key is None or execution_key is None or task_id in self._finalizing:
                # 尚在领取：领取事务本身在锁内按数据库复核。
                continue
            try:
                async with self.ctx.sessionmaker() as db:
                    row = await db.scalar(select(Task).where(
                        Task.id == task_id, Task.statistics_key == task_key, Task.user_id == actor_id,
                        Task.project_id == project_id, Task.execution_key == execution_key, Task.status == "running",
                    ))
                    if row is None or not isinstance(row.input, dict) or GRANT_KEY not in row.input:
                        # 作者任务不受成员变化影响。
                        continue
                    token = row.input.get("_task_actor_token_version")
                    visible = await db.scalar(select(Task.id).where(
                        Task.id == task_id, Task.statistics_key == task_key,
                        task_visible_to_actor(db, actor_id, active_grant_required=True,
                                              token_version=token if type(token) is int else None),
                    ))
                    code = None
                    if visible is None:
                        code = "collaboration_revoked"
                    elif task_requires_owner_ai(row.input):
                        allowed = await db.scalar(select(ProjectCollaborator.allow_owner_ai).where(
                            ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == actor_id,
                            ProjectCollaborator.status == "accepted",
                        ))
                        if allowed is not True:
                            code = "collaboration_model_revoked"
            except Exception:  # noqa: BLE001  复查失败不放行也不猜测，交由执行体自身的检查点。
                logger.warning("协作授权复查失败 task_key=%s", task_key)
                continue
            if code is None:
                continue
            if (self._running_execution_keys.get(task_id) != execution_key
                    or self._running_statistics_keys.get(task_id) != task_key or task_id not in self._running):
                # 复查期间原执行体已收尾（_on_done 已清理）：不登记，避免残留标记误标同 ID 的后续轮次。
                continue
            self._collaboration_revoked[task_id] = (task_key, execution_key, code)
            await self.cancel_running(task_id, statistics_key=task_key, expected_queued_at=queued_at,
                                      expected_execution_key=execution_key)

    async def subscribe(
        self, task_id: int, *, statistics_key: str | None = None, actor_id: int | None = None,
        token_version: int | None = None, active_grant_required: bool = False,
        initial_project_id: Any = _UNSET_BINDING, initial_queued_at: Any = _UNSET_BINDING,
        initial_grant: Any = _UNSET_BINDING, guarded: bool = False,
        db_snapshot_only: bool = False, handoff: ClaimHandoff | None = None,
        follow_local_queue: bool = False, source_worker: str | None = None, source_execution: str | None = None,
        peer_owner_projection: bool = False,
    ) -> AsyncIterator[Event]:
        """快照 + 增量；任务结束（done / error）或暂停（paused）后结束。断开只移除该订阅者，不影响任务执行。"""
        from app.services.task_engine.peer_protocol import PeerUnavailable
        from app.services.task_engine.stream_evidence import QueuedSource

        sm = self.ctx.sessionmaker
        if ((guarded and actor_id is None)
                or (actor_id is not None and (token_version is None or statistics_key is None))):
            error = ("error", {"task": None, "detail": TASK_ACCESS_LOST_MESSAGE, "code": "permission_denied"})
            yield (*error, None) if guarded else error
            return
        task_project_id = None

        def matches_binding(task: Task | None) -> bool:
            if task is None:
                return False
            if initial_project_id is not _UNSET_BINDING and task.project_id != initial_project_id:
                return False
            if initial_queued_at is not _UNSET_BINDING and task.queued_at != initial_queued_at:
                return False
            if active_grant_required and initial_grant is not _UNSET_BINDING:
                if task_grant_identity(task.input) != initial_grant:
                    return False
            if task.status == "running" and (follow_local_queue or source_worker is not None) and (
                not self.lease.valid or task.worker_key != self.lease.worker_key
                or (source_worker is not None and task.worker_key != source_worker)
                or (source_execution is not None and task.execution_key != source_execution)
                or task.recovered_at is not None
            ):
                raise PeerUnavailable()
            if handoff is not None and (
                not self.lease.valid or self.lease.worker_key != handoff.worker_key
                or task.status == "queued" or task.worker_key not in (None, handoff.worker_key)
                or (task.status == "running" and (
                    task.worker_key != handoff.worker_key or task.execution_key != handoff.execution_key
                    or task.recovered_at is not None
                ))
                or (handoff.hub.closed and task.status not in ACTIVE_TASK_STATUSES and not handoff.matches_row(task))
            ):
                raise HandoffUnavailable()
            return True

        async def read_visible() -> Task | None:
            try:
                async with sm() as db:
                    row = await self._visible_task(
                        db, task_id, statistics_key=statistics_key, actor_id=actor_id,
                        token_version=token_version, active_grant_required=active_grant_required,
                    )
                    if not matches_binding(row):
                        return None
                    if active_grant_required and row.project_id is not None and actor_id is not None:
                        from app.services.collaboration_task_access import (
                            GRANT_KEY,
                            owner_ai_allowed,
                            task_requires_owner_ai,
                        )

                        if (isinstance(row.input, dict) and GRANT_KEY in row.input
                                and task_requires_owner_ai(row.input)
                                and not await owner_ai_allowed(db, row.project_id, actor_id)):
                            return None
                    return row
            except (HandoffUnavailable, PeerUnavailable):
                raise
            except Exception as exc:  # noqa: BLE001
                # 权限存储不可用时关闭流；不要把数据库异常/SQL 发给用户。
                logger.warning("任务流授权检查失败 error_type=%s", type(exc).__name__)
                return None

        async def access_error() -> Event:
            if actor_id is None:
                return "error", {"task": None, "detail": TASK_GONE_MESSAGE}
            # 保留作者发起删除时原有的“任务不存在”生命周期错误；不放行协作者或失效账号。
            if task_project_id is not None:
                try:
                    async with sm() as db:
                        owner_gone = await db.scalar(select(Project.id).join(User, User.id == actor_id).where(
                            Project.id == task_project_id, Project.user_id == actor_id,
                            User.is_active.is_(True), User.token_version == token_version,
                            (Project.deleted_at.is_not(None) | Project.deletion_started_at.is_not(None)),
                        ))
                    if owner_gone is not None:
                        return "error", {"task": None, "detail": TASK_GONE_MESSAGE}
                except Exception:  # noqa: BLE001
                    pass
            return "error", {"task": None, "detail": TASK_ACCESS_LOST_MESSAGE, "code": "permission_denied"}

        async def public_error():
            event = await access_error()
            return (*event, None) if guarded else event

        def persisted_source(task: Task) -> Any:
            if not guarded:
                return None
            from app.services.task_engine.stream_evidence import capture_task_view

            return capture_task_view(task)

        async def owner_deletion_cancel(event: Event, db: AsyncSession) -> Event | None:
            # 项目删除中的作者仍收到已提交的取消终态，但不回放断点、风格或私人引用。
            if actor_id is None or task_project_id is None or event[0] != "error" \
                    or event[1].get("detail") != "任务已取消":
                return None
            task = await db.scalar(select(Task).join(Project, Project.id == Task.project_id).join(
                User, User.id == actor_id,
            ).where(
                Task.id == task_id, Task.statistics_key == statistics_key, Task.user_id == actor_id,
                Task.status == "cancelled", Project.id == task_project_id, Project.user_id == actor_id,
                Project.deletion_started_at.is_not(None), User.is_active.is_(True), User.token_version == token_version,
            ))
            if not matches_binding(task):
                return None
            value = deletion_cancel_event(task)
            if guarded:
                from app.services.ai_dispatch import VisibilityStamp
                from app.services.task_engine.stream_evidence import capture_task_view, frame_evidence

                project = await db.get(Project, task_project_id)
                if project is None:
                    return None
                stamp = VisibilityStamp(project.id, project.ai_visibility_revision, project.ai_history_epoch,
                                        project.last_deleted_at)
                return *value, frame_evidence(value[0], value[1], capture_task_view(task), stamp)
            return value

        async def checked_frame(event: Event, source: Any = None) -> Any:
            if actor_id is None:
                return event
            try:
                async with sm() as db:
                    latest = await self._visible_task(
                        db, task_id, statistics_key=statistics_key, actor_id=actor_id,
                        token_version=token_version, active_grant_required=active_grant_required,
                    )
                    if not matches_binding(latest):
                        return await owner_deletion_cancel(event, db)
                    if latest.project_id is None:
                        if guarded:
                            from app.services.task_engine.stream_evidence import (
                                TaskViewEvidence,
                                capture_task_view,
                                frame_evidence,
                            )

                            if type(source) is TaskViewEvidence and capture_task_view(latest) != source:
                                return None
                            return (*event, frame_evidence(event[0], event[1], source, None)) if source is not None \
                                else None
                        return event
                    from app.services.personal_projection import project_payload

                    stamp = None
                    if guarded:
                        from app.services.ai_dispatch import read_visibility_stamp

                        stamp = await read_visibility_stamp(db, latest.project_id)

                    data = await project_payload(db, actor_id, latest.project_id, event[1])
                    public_change = None
                    if event[0] in ("proposal", "executed"):
                        # 只放行事件原本的提案身份和内容；不能以新读到的同整数 ID 替代旧帧。
                        from app.models import AgentChange
                        from app.schemas.agent import ChangeOut
                        from app.services.agent import service as agent_service
                        from app.services.agent.transactions import public_change_payload

                        original = event[1].get("change")
                        if not isinstance(original, dict) or type(original.get("id")) is not int \
                                or not isinstance(original.get("change_key"), str):
                            return _REDACTED_EVENT, {}
                        change = await db.scalar(select(AgentChange).where(
                            AgentChange.id == original["id"], AgentChange.change_key == original["change_key"],
                            AgentChange.project_id == latest.project_id, AgentChange.turn_task_id == task_id,
                            AgentChange.turn_task_key == statistics_key,
                        ).execution_options(populate_existing=True))
                        if change is None:
                            return _REDACTED_EVENT, {}
                        captured = public_change_payload(ChangeOut.model_validate(change).model_dump(mode="json"))
                        if any(original.get(key) != value for key, value in captured.items() if key != "view_version"):
                            return _REDACTED_EVENT, {}
                        public_change = (await agent_service.public_changes(
                            db, actor_id, latest.project_id, [change],
                        ))[0].model_dump(mode="json")
                    # 富文档提案凭据要与本人任务和当前 pending 提案在同一个最终 SQL 快照中验证。
                    from app.services.task_engine.stream_evidence import (
                        finalize_projected_frame,
                        rich_review_of,
                        rich_review_pending_conditions,
                        rich_review_shape_ok,
                    )

                    original_review = rich_review_of(event[0], event[1])
                    if original_review is not None:
                        from app.models import SceneReviewProposal
                        from app.services.collaboration_task_access import task_visible_to_actor

                        if not rich_review_shape_ok(original_review, statistics_key):
                            return _REDACTED_EVENT, {}
                        latest = await db.scalar(select(Task).join(
                            SceneReviewProposal, SceneReviewProposal.task_id == Task.id,
                        ).where(
                            *rich_review_pending_conditions(
                                task_id=task_id, task_key=statistics_key, project_id=task_project_id,
                                queued_at=initial_queued_at, review=original_review,
                            ),
                            task_visible_to_actor(
                                db, actor_id, active_grant_required=active_grant_required, token_version=token_version,
                            ),
                        ).execution_options(populate_existing=True))
                    else:
                        # 投影查询期间可能降级，但仍是 accepted；必须再验原活动邀请轮次。
                        latest = await self._visible_task(
                            db, task_id, statistics_key=statistics_key, actor_id=actor_id,
                            token_version=token_version, active_grant_required=active_grant_required,
                        )
                    if not matches_binding(latest):
                        return None
                    if guarded:
                        from app.services.ai_dispatch import read_visibility_stamp
                        from app.services.task_engine.stream_evidence import TaskViewEvidence, capture_task_view

                        if (stamp != await read_visibility_stamp(db, latest.project_id) or source is None
                                or (type(source) is TaskViewEvidence and capture_task_view(latest) != source)):
                            return None
                    if public_change is not None and isinstance(data, dict):
                        # 必须在泛化投影之后、最终原任务权限复核之后再注入已验证的私有操作凭据。
                        data["change"] = public_change
                    # 与中央回放证明共用：注入原 Task UUID 与已验证的提案凭据。
                    if not finalize_projected_frame(
                        event[0], data, task_id=task_id, task_key=statistics_key, original_review=original_review,
                    ):
                        return _REDACTED_EVENT, {}
                    if guarded and data is not None:
                        from app.services.task_engine.stream_evidence import frame_evidence

                        terminal = hub.terminal_source if hub is not None else None
                        return event[0], data, frame_evidence(
                            event[0], data, source, stamp, terminal,
                            original_projection=event[1] if source_worker is not None else None,
                        )
                    if peer_owner_projection and data is not None:
                        # 原件只交给本机发送者重核投影，内部通道只传公开帧与引用摘要。
                        return event[0], data, event[1]
                    return (event[0], data) if data is not None else (_REDACTED_EVENT, {})
            except (HandoffUnavailable, PeerUnavailable):
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("任务流授权或投影检查失败 error_type=%s", type(exc).__name__)
                return None

        # HTTP入口传入鉴权时捕获的UUID；内部直接调用则在首次读库时绑定。
        row = await read_visible()
        if row is None or row.statistics_key is None:
            yield await public_error()
            return
        statistics_key = row.statistics_key
        task_project_id = row.project_id
        if initial_project_id is _UNSET_BINDING:
            initial_project_id = row.project_id
        if initial_queued_at is _UNSET_BINDING:
            initial_queued_at = row.queued_at
        if active_grant_required and initial_grant is _UNSET_BINDING:
            initial_grant = task_grant_identity(row.input)
        hub = self.hubs.get(task_id)
        if db_snapshot_only:
            # 从库内读取一个当前视图，绝不为其他进程的执行创建本地订阅或借用其残留 hub。
            hub = None
            source = persisted_source(row)
            if row.status in ACTIVE_TASK_STATUSES:
                if guarded and row.status != "queued":
                    yield await public_error()
                    return
                async with sm() as db:
                    out = task_service.to_out(row, queue_position=await task_service.queue_position(db, row))
                    out = await task_service.project_task_out(
                        db, row.user_id, out, active_grant_required=active_grant_required, task_key=statistics_key,
                        expected_grant=task_grant_identity(row.input), expected_queued_at=row.queued_at,
                    )
                candidates = (("snapshot", {"task": out.model_dump(mode="json"), "text": row.output_text or ""}),)
            else:
                candidates = _finished_events(row)
            for candidate in candidates:
                safe = await checked_frame(candidate, source)
                if safe is None:
                    # 排队快照在投影期间被同轮领取，调用方将重新定位到当前执行者。
                    if guarded and row.status == "queued" and await read_visible() is not None:
                        return
                    yield await public_error()
                    return
                if safe[0] != _REDACTED_EVENT:
                    yield safe
            return
        sub = None
        if handoff is not None:
            hub = handoff.hub
            if row.status not in ACTIVE_TASK_STATUSES and not hub.closed:
                # 数据库刚结束，本地执行还在发布终态收据。只在锁外等这一轮的原 hub。
                try:
                    async with asyncio.timeout(self.claim_source_wait_seconds):
                        await hub.terminal_ready.wait()
                except TimeoutError:
                    raise HandoffUnavailable() from None
            if hub.closed:
                row = await read_visible()
                if row is None:
                    yield await public_error()
                    return
            elif (self.hubs.get(task_id) is not hub or task_id not in self._running
                  or self._running_execution_keys.get(task_id) != handoff.execution_key):
                raise HandoffUnavailable()
            sub = self.peer_handoffs.attach(handoff, row, worker_key=self.lease.worker_key, guarded=guarded)
        # 除了已经观察到 queued 的交接，数据库终态优先于所有本地缓存。
        elif (row.status not in ACTIVE_TASK_STATUSES or hub is None or hub.statistics_key != statistics_key
                or (hub.queued_at is not None and hub.queued_at != row.queued_at)
                or (row.execution_key is not None and hub.execution_key != row.execution_key)):
            if row.status in ACTIVE_TASK_STATUSES:
                hub, created = self.ensure_hub(
                    task_id, statistics_key=statistics_key,
                    status=row.status, text=row.output_text, progress=row.progress, stage=row.stage,
                    queued_at=row.queued_at, execution_key=row.execution_key,
                )
                if created:
                    # 防止读库与建 hub 之间任务恰好结束：复查，已结束则丢弃刚建的 hub
                    row = await read_visible()
                    if row is None or row.status not in ACTIVE_TASK_STATUSES:
                        if self.hubs.get(task_id) is hub and task_id not in self._running:
                            self.hubs.pop(task_id, None)
                        hub = None
            else:
                hub = None
            if hub is None:
                if row is None:
                    yield await public_error()
                    return
                source = persisted_source(row)
                for event in _finished_events(row):
                    safe = await checked_frame(event, source)
                    if safe is None:
                        yield await public_error()
                        return
                    if safe[0] != _REDACTED_EVENT:
                        yield safe
                return

        subscription_key = None

        def detach_sub() -> None:
            nonlocal sub, subscription_key
            if sub is not None:
                hub.unsubscribe(sub)
            if subscription_key is not None and sub is not None:
                subscribers = self._collaboration_subscribers.get(subscription_key)
                if subscribers is not None:
                    subscribers.discard(sub)
                    if not subscribers:
                        self._collaboration_subscribers.pop(subscription_key, None)
            sub = None
            subscription_key = None

        try:
            snapshot = None
            if sub is not None and row.project_id is not None and actor_id is not None:
                subscription_key = (row.project_id, actor_id)
                self._collaboration_subscribers.setdefault(subscription_key, set()).add(sub)
            # E5-e：受控订阅读到已提交的 running 行、而本进程领取者尚未登记领取来源（领取提交与登记之间）。
            awaiting_claim = False
            for attempt in range(5):
                if handoff is not None:
                    # 原排队快照已送出；队列从领取帧开始，后续走相同投影与帧验证。
                    break
                retry_snapshot = False
                queued_advance_view = None
                try:
                    async with sm() as db:
                        row = await self._visible_task(
                            db, task_id, statistics_key=statistics_key, actor_id=actor_id,
                            token_version=token_version, active_grant_required=active_grant_required,
                        )
                        if not matches_binding(row):
                            row = None
                        original_view = persisted_source(row) if guarded and row is not None else None
                        if guarded and row is not None and row.status == "queued":
                            # 排队快照只能来自同一持久行，不采信旧 hub 的进度或正文。
                            queued_out = task_service.to_out(
                                row, queue_position=await task_service.queue_position(db, row),
                            )
                            queued_out = await task_service.project_task_out(
                                db, row.user_id, queued_out, active_grant_required=True, task_key=row.statistics_key,
                                expected_grant=task_grant_identity(row.input), expected_queued_at=row.queued_at,
                            )
                            out = queued_out.model_dump(mode="json")
                        else:
                            out = await task_service.out_json_live(db, self, row) if row is not None else None
                        if row is not None and row.status in ACTIVE_TASK_STATUSES:
                            # 投影后再次读取授权，再同步注册订阅并立即发快照；订阅计数出现时快照已就绪。
                            row = await self._visible_task(
                                db, task_id, statistics_key=statistics_key, actor_id=actor_id,
                                token_version=token_version, active_grant_required=active_grant_required,
                            )
                            if not matches_binding(row):
                                row = None
                            elif guarded and persisted_source(row) != original_view:
                                # 受控订阅投影期间任务正常推进（领取/进度刷新/断点更新/终态）且原绑定仍有效：
                                # 活动态重读最新快照；已进入非活动态则保留最新 row 交由下方持久回放分支。
                                if row.status in ACTIVE_TASK_STATUSES:
                                    retry_snapshot = True
                            elif row.status in ACTIVE_TASK_STATUSES and not hub.closed:
                                if not guarded:
                                    # 投影等待期间领取 progress 可能已广播；最终 SELECT 返回前也可能完成领取。
                                    # 与正文快照/注册在同一无 await 段同步公开状态，不重新投影私人引用。
                                    live = task_service.to_out(row).model_dump(mode="json", include={
                                        "status", "progress", "stage", "started_at",
                                    })
                                    if (self.hubs.get(task_id) is hub and hub.statistics_key == row.statistics_key
                                            and hub.queued_at == row.queued_at and hub.status == "running"
                                            and hub.execution_key is not None
                                            and (hub.execution_key == row.execution_key
                                                 or (row.status == "queued" and row.execution_key is None))):
                                        live.update(hub.progress_payload())
                                    out.update(live)
                                    if out["status"] != "queued":
                                        out["queue_position"] = None
                                text, sub = hub.subscribe(guarded=guarded)
                                if row.project_id is not None and actor_id is not None:
                                    subscription_key = (row.project_id, actor_id)
                                    self._collaboration_subscribers.setdefault(subscription_key, set()).add(sub)
                                source = hub.snapshot_source() if guarded else None
                                unproven = guarded and (source is None or type(source) is QueuedSource)
                                if guarded and row.status == "queued" and not unproven:
                                    # 排队摘要刚投影完、hub 已挂上领取/发布来源：以新运行行重算快照，避免 status 失配。
                                    detach_sub()
                                    retry_snapshot = True
                                else:
                                    if unproven and row.status == "queued" and text == row.output_text:
                                        source = original_view
                                    elif unproven and row.status == "running" and not text:
                                        awaiting_claim = True
                                    if not awaiting_claim:
                                        safe = await checked_frame(("snapshot", {"task": out, "text": text}), source)
                                        if safe is None:
                                            if guarded and source is original_view:
                                                detach_sub()
                                                queued_advance_view = original_view
                                            else:
                                                row = None
                                        elif safe[0] != _REDACTED_EVENT:
                                            snapshot = safe
                except (HandoffUnavailable, PeerUnavailable):
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("任务流授权检查失败 error_type=%s", type(exc).__name__)
                    detach_sub()
                    row, out = None, None
                    retry_snapshot = False
                    queued_advance_view = None
                if queued_advance_view is not None:
                    # 排队快照在 checked_frame 期间恰逢任务推进（领取/终态）：在独立只读会话复核原绑定后重试/回放。
                    latest_row = await read_visible()
                    if latest_row is not None and persisted_source(latest_row) != queued_advance_view:
                        row = latest_row
                        if row.status in ACTIVE_TASK_STATUSES:
                            retry_snapshot = True
                    else:
                        row = None
                if not retry_snapshot:
                    break
                if attempt == 4:
                    row = None
            if awaiting_claim and row is not None:
                # 不持读取会话等待：领取者提交后会同步登记领取来源并发布领取帧（已注册的订阅队列不会错过）。
                # 等到可证明来源后按最新持久行重新投影无正文快照；超时或 hub 无来源关闭时失败关闭。
                claimed = await self._await_claim_source(hub)
                safe = None
                if claimed is not None:
                    try:
                        async with sm() as db:
                            latest = await self._visible_task(
                                db, task_id, statistics_key=statistics_key, actor_id=actor_id,
                                token_version=token_version, active_grant_required=active_grant_required,
                            )
                            out = (await task_service.out_json_live(db, self, latest)
                                   if matches_binding(latest) else None)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("任务流授权检查失败 error_type=%s", type(exc).__name__)
                        out = None
                    if out is not None:
                        safe = await checked_frame(("snapshot", {"task": out, "text": ""}), claimed)
                if safe is None:
                    row = None
                elif safe[0] != _REDACTED_EVENT:
                    snapshot = safe
            # 不带着读取会话挂起在 yield，避免发送租约等待自身锁。
            if snapshot is not None:
                yield snapshot
            if row is None:
                yield await public_error()
                return
            # 注册前可能恰好完成；以复查的持久化状态收尾，不再等待已错过的广播。
            if sub is None and row.status not in ACTIVE_TASK_STATUSES:
                source = persisted_source(row)
                for event in _finished_events(row):
                    safe = await checked_frame(event, source)
                    if safe is None:
                        yield await public_error()
                        return
                    if safe[0] != _REDACTED_EVENT:
                        yield safe
                return
            if sub is None:
                yield await public_error()
                return
            buffered: deque[tuple[str, dict, Any]] = deque()
            next_heartbeat = time.monotonic() + self.heartbeat_seconds
            watch_grant = actor_id is not None and active_grant_required and initial_grant is not None
            while True:
                if sub.overflowed and sub.queue.empty() and not buffered:
                    return
                try:
                    if buffered:
                        event, data, source = buffered.popleft()
                    else:
                        remaining = max(0.0, next_heartbeat - time.monotonic())
                        # 静默读者自行核验，调度停止及提醒丢失都不能推迟复查。
                        if follow_local_queue:
                            remaining = min(0.2, self.poll_interval, remaining)
                        elif watch_grant:
                            remaining = min(remaining, self.poll_interval)
                        async with asyncio.timeout(remaining):
                            queued = await sub.queue.get()
                        if guarded:
                            event, data, source = queued
                        else:
                            event, data = queued
                            source = None
                except TimeoutError:
                    row = await read_visible()
                    if row is None:
                        # 排队中的任务随用户 / 项目被级联删除：没有执行体会再关闭 hub，这里代为收尾
                        if actor_id is None and task_id not in self._running and self.hubs.get(task_id) is hub:
                            self.close_hub(
                                task_id, "error", {"task": None, "detail": TASK_GONE_MESSAGE},
                                statistics_key=statistics_key,
                            )
                        yield await public_error()
                        return
                    if follow_local_queue:
                        if (row.status == "running" and row.worker_key != self.lease.worker_key) or (
                            row.status not in ACTIVE_TASK_STATUSES and not hub.closed
                            and not (task_id in self._running and self._running_statistics_keys.get(task_id)
                                     == statistics_key and self._running_rounds.get(task_id) == row.queued_at)
                        ):
                            raise PeerUnavailable() from None
                        if row.status == "running" and row.worker_key == self.lease.worker_key:
                            follow_local_queue = False
                            source_worker, source_execution = row.worker_key, row.execution_key
                    if time.monotonic() >= next_heartbeat:
                        yield (HEARTBEAT_EVENT, {}, None) if guarded else (HEARTBEAT_EVENT, {})
                        next_heartbeat = time.monotonic() + self.heartbeat_seconds
                    continue
                if event == _COLLABORATION_CHECK_EVENT:
                    if await read_visible() is None:
                        yield await public_error()
                        return
                    continue
                if event == "delta" and set(data) == {"text"} and isinstance(data["text"], str):
                    # 只合并已经排队的内容：不跨状态/阶段变化、撤权唤醒或其他事件。
                    # 单次实际发送仍在合并后做完整身份和权限重验。
                    parts = [data["text"]]
                    size = len(data["text"])
                    for _ in range(127):
                        if size >= 16384:
                            break
                        try:
                            queued = sub.queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if guarded:
                            next_event, next_data, next_source = queued
                        else:
                            next_event, next_data = queued
                            next_source = None
                        if next_event == "delta" and set(next_data) == {"text"} \
                                and isinstance(next_data["text"], str) and next_source == source:
                            parts.append(next_data["text"])
                            size += len(next_data["text"])
                        else:
                            buffered.append((next_event, next_data, next_source))
                            break
                    data = {"text": "".join(parts)}
                safe = await checked_frame((event, data), source)
                if safe is None:
                    yield await public_error()
                    return
                if event == _COLLABORATION_CHECK_EVENT or safe[0] == _REDACTED_EVENT:
                    continue
                yield safe
                next_heartbeat = time.monotonic() + self.heartbeat_seconds
                if event in ("done", "error", "paused"):
                    return
        finally:
            if sub is not None:
                hub.unsubscribe(sub)
            if subscription_key is not None:
                subscribers = self._collaboration_subscribers.get(subscription_key)
                if subscribers is not None:
                    subscribers.discard(sub)
                    if not subscribers:
                        self._collaboration_subscribers.pop(subscription_key, None)

    async def _await_claim_source(self, hub: TaskHub) -> Any:
        """等待领取者登记本 hub 的领取来源（或其后的已发布来源），有上限（E5-e）。

        只读取 hub 上由引擎发布时冻结的来源，不从数据库补签；hub 关闭仍无来源或超时返回 None（调用方失败关闭）。
        """
        from app.services.task_engine.stream_evidence import QueuedSource

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.claim_source_wait_seconds
        while True:
            current = hub.source
            if current is not None and type(current) is not QueuedSource:
                return current
            remaining = deadline - loop.time()
            if hub.closed or remaining <= 0:
                return None
            waiters = [asyncio.ensure_future(hub.source_changed.wait()),
                       asyncio.ensure_future(hub.terminal_ready.wait())]
            try:
                await asyncio.wait(waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in waiters:
                    waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)

    async def _visible_task(
        self, db: AsyncSession, task_id: int, *, statistics_key: str | None = None, actor_id: int | None = None,
        token_version: int | None = None, active_grant_required: bool = False,
    ) -> Task | None:
        statement = select(Task).where(
            Task.id == task_id, task_service.visible_task_project_exists()
        )
        if statistics_key is not None:
            statement = statement.where(Task.statistics_key == statistics_key)
        if actor_id is not None:
            from app.services.collaboration_task_access import task_visible_to_actor

            statement = statement.where(task_visible_to_actor(
                db, actor_id, active_grant_required=active_grant_required, token_version=token_version,
            ))
        return await db.scalar(statement.execution_options(populate_existing=True))

    async def _task_gone(self, task_id: int, *, statistics_key: str | None = None) -> bool:
        try:
            async with self.ctx.sessionmaker() as db:
                return await self._visible_task(db, task_id, statistics_key=statistics_key) is None
        except Exception:  # noqa: BLE001
            logger.exception("任务 %s 存在性检查失败", task_id)
            return False


def deletion_cancel_event(task: Task) -> Event:
    """作品删除期间只允许原作者看取消摘要，去掉全部内容引用与断点。"""
    safe = task_service.to_out(task).model_copy(update={
        "checkpoint": None, "skill": None, "rich_review": None, "retry_of_id": None,
        "chapter_id": None, "scene_id": None, "analysis_id": None, "character_id": None,
        "outline_id": None,
    })
    return "error", {"task": safe.model_dump(mode="json"), "detail": "任务已取消"}


def _finished_events(task: Task) -> tuple[Event, Event]:
    """从持久化状态回放完整快照与终止事件，不受残留 hub 影响；与中央回放证明同源。"""
    from app.services.task_engine.stream_evidence import persisted_replay_events

    return persisted_replay_events(task)


def _try_handler(kind: str) -> TaskHandler | None:
    try:
        return get_handler(kind)
    except AppError:
        return None
