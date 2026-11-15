"""受控任务流的进程内帧来源；不序列化到响应或日志。"""

import asyncio
import hashlib
import json
import weakref
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any

from app.models import Project, Task
from app.services.ai_dispatch import VisibilityStamp
from app.services.task_engine.execution_identity import TaskExecutionIdentity, matches_running, same_execution


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)).isoformat()


@dataclass(frozen=True, slots=True, repr=False)
class TaskViewEvidence:
    task_id: int
    task_key: str
    queued_at: str | None
    status: str
    fingerprint: str


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class TerminalEvidence:
    execution: TaskExecutionIdentity
    view: TaskViewEvidence


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class PublicationSource:
    execution: TaskExecutionIdentity
    guard: object


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class ClaimSource:
    """领取事务提交确认后、首个已核验事件之前的无输出来源（E5-e）。

    只由引擎在本次领取（queued → running CAS）已确认提交后按同一执行身份签发；只能证明不含模型输出的帧
    （领取时的 progress / reset、正文为空的快照），以及按已登记终态证明的终态 error。发送时锁内要求持锁行
    仍是同一执行（或已由同一执行提交终态），不以持锁行补签任何输出。
    """

    execution: TaskExecutionIdentity


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class QueuedSource:
    """排队中 hub 的排队位次帧来源（E5-e）；只证明同一入队轮次的排队 progress，不证明任何正文。"""

    task_id: int
    task_key: str
    queued_at: str | None


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class TaskStreamEvidence:
    source: PublicationSource | TaskViewEvidence | ClaimSource | QueuedSource
    _terminal: TerminalEvidence | None
    visibility: VisibilityStamp | None
    event: str
    payload_hash: str

    @property
    def terminal(self) -> TerminalEvidence | None:
        """只解析发布该帧的原 hub 已登记的终态，不能从当前任务补造。"""
        if self._terminal is not None:
            return self._terminal
        hub = _publication_hub(self.source) or _claim_hub(self.source)
        return hub.terminal_source if hub is not None else None


class TerminalEvidencePending(Exception):
    """原执行已提交终态但其本进程收尾尚未登记；不得持帧锁等待。"""

    def __init__(self, hub: object) -> None:
        super().__init__("任务终态证明尚未就绪")
        self._hub = weakref.ref(hub)

    async def wait(self) -> None:
        hub = self._hub()
        if hub is None:
            return
        await hub.terminal_ready.wait()


class PublicationSourcePending(TerminalEvidencePending):
    """批量帧来源已落后于持锁行、而同一 hub 尚未发布对应的新来源（E5-c2），或设定类向导未决帧等待本执行
    保存后的完成来源（同执行戳后继，2026-11-13）；不得持帧锁等待。

    发送者退出整个租约后，等到该 hub 换下一个已发布来源或登记终态，再以原帧重新验证。
    """

    def __init__(self, hub: object, changed: asyncio.Event) -> None:
        super().__init__(hub)
        self._changed = changed

    async def wait(self) -> None:
        hub = self._hub()
        if hub is None:
            return
        waiters = [asyncio.ensure_future(hub.terminal_ready.wait()), asyncio.ensure_future(self._changed.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)


def _snapshot(value: object) -> tuple[Any, ...]:
    return tuple(getattr(value, field.name) for field in fields(value))


_PUBLICATIONS: weakref.WeakKeyDictionary[PublicationSource, tuple[object, ...]] = weakref.WeakKeyDictionary()
_TERMINALS: weakref.WeakKeyDictionary[TerminalEvidence, tuple[object, ...]] = weakref.WeakKeyDictionary()
_FRAMES: weakref.WeakKeyDictionary[TaskStreamEvidence, tuple[object, ...]] = weakref.WeakKeyDictionary()


def issue_publication(execution: TaskExecutionIdentity, guard: object, *, hub: object) -> PublicationSource:
    """仅 engine 成功验证输出事务后调用；冻结该帧原 guard 与执行体。"""
    from app.services.collaboration_ai_guard import SendGuard, publication_execution_matches

    if (type(execution) is not TaskExecutionIdentity or type(guard) is not SendGuard
            or not publication_execution_matches(guard, execution) or hub.task_id != execution.task_id
            or hub.statistics_key != execution.task_key
            or _time(hub.queued_at) != _time(execution.queued_at) or hub.execution_key != execution.execution_key):
        raise ValueError("任务帧来源无效")
    source = PublicationSource(execution, guard)
    _PUBLICATIONS[source] = (_snapshot(execution), guard, weakref.ref(hub))
    return source


def _publication_hub(source: object) -> object | None:
    if type(source) is not PublicationSource:
        return None
    state = _PUBLICATIONS.get(source)
    if state is None or state[0] != _snapshot(source.execution) or state[1] is not source.guard:
        return None
    hub = state[2]()
    if (hub is None or hub.task_id != source.execution.task_id or hub.statistics_key != source.execution.task_key
            or _time(hub.queued_at) != _time(source.execution.queued_at)
            or hub.execution_key != source.execution.execution_key):
        return None
    return hub


_CLAIMS: weakref.WeakKeyDictionary[ClaimSource, tuple[object, ...]] = weakref.WeakKeyDictionary()
_QUEUED: weakref.WeakKeyDictionary[QueuedSource, tuple[object, ...]] = weakref.WeakKeyDictionary()


def _hub_matches_execution(hub: object, execution: TaskExecutionIdentity) -> bool:
    return bool(hub.task_id == execution.task_id and hub.statistics_key == execution.task_key
                and _time(hub.queued_at) == _time(execution.queued_at)
                and hub.execution_key == execution.execution_key)


def issue_claim(execution: TaskExecutionIdentity, *, hub: object) -> ClaimSource:
    """仅 engine 在本次领取 CAS 已确认提交后、发布领取帧之前调用（E5-e）。

    execution 必须是领取者在原 queued 行锁内按本次 CAS 的 execution_key 构造的同一执行身份；hub 必须正是
    以该执行登记的 hub。签发不访问数据库、不授予发送/预检/写入能力。
    """
    if (type(execution) is not TaskExecutionIdentity or execution.actor_token_version is None
            or not isinstance(execution.task_key, str) or not isinstance(execution.execution_key, str)
            or getattr(hub, "closed", True) or not _hub_matches_execution(hub, execution)):
        raise ValueError("任务领取来源无效")
    source = ClaimSource(execution)
    _CLAIMS[source] = (_snapshot(execution), weakref.ref(hub))
    return source


def _claim_hub(source: object) -> object | None:
    if type(source) is not ClaimSource:
        return None
    state = _CLAIMS.get(source)
    if state is None or state[0] != _snapshot(source.execution):
        return None
    hub = state[1]()
    if hub is None or not _hub_matches_execution(hub, source.execution):
        return None
    return hub


def issue_queued(hub: object) -> QueuedSource:
    """仅 engine 为排队中、尚无执行键的 hub 发布排队位次前调用（E5-e）。"""
    if (getattr(hub, "closed", True) or hub.status != "queued" or hub.execution_key is not None
            or type(hub.task_id) is not int or not isinstance(hub.statistics_key, str)):
        raise ValueError("任务排队来源无效")
    source = QueuedSource(hub.task_id, hub.statistics_key, _time(hub.queued_at))
    _QUEUED[source] = (_snapshot(source), weakref.ref(hub))
    return source


def _queued_hub(source: object) -> object | None:
    if type(source) is not QueuedSource:
        return None
    state = _QUEUED.get(source)
    if state is None or state[0] != _snapshot(source):
        return None
    hub = state[1]()
    if (hub is None or hub.task_id != source.task_id or hub.statistics_key != source.task_key
            or _time(hub.queued_at) != source.queued_at):
        return None
    return hub


def _is_batch_guard(guard: object) -> bool:
    task = getattr(guard, "_task", None)
    return type(task) is TaskExecutionIdentity and task.kind == "chapter.batch"


def issue_terminal(execution: TaskExecutionIdentity, view: TaskViewEvidence) -> TerminalEvidence:
    """仅 engine 验证本次终态提交后调用；不从当前行补造原执行。"""
    terminal = TerminalEvidence(execution, view)
    _TERMINALS[terminal] = (_snapshot(execution), _snapshot(view))
    return terminal


def capture_task_view(task: Task) -> TaskViewEvidence:
    if not isinstance(task.statistics_key, str):
        raise ValueError("任务身份无效")
    values = {
        "id": task.id, "task_key": task.statistics_key, "actor": task.user_id, "project": task.project_id,
        "chapter": task.chapter_id, "kind": task.kind, "status": task.status, "queued": _time(task.queued_at),
        "execution": task.execution_key, "resource": task.resource_key, "serial": task.serial_key,
        "retry": task.retry_of_id, "cursor": task.step_cursor, "input": task.input, "checkpoint": task.checkpoint,
        "result": task.result, "output": task.output_text, "error": task.error, "stage": task.stage,
        "progress": task.progress, "admin": task.admin_cancel_operation_id,
        "admin_at": _time(task.admin_cancel_requested_at),
    }
    return TaskViewEvidence(task.id, task.statistics_key, _time(task.queued_at), task.status, _digest(values))


def frame_evidence(
    event: str, data: dict[str, Any], source: PublicationSource | TaskViewEvidence,
    visibility: VisibilityStamp | None, terminal: TerminalEvidence | None = None,
) -> TaskStreamEvidence:
    evidence = TaskStreamEvidence(source, terminal, visibility, event, _digest(data))
    _FRAMES[evidence] = (source, terminal, visibility, event, evidence.payload_hash)
    return evidence


_FRAME_TASK_EVENTS = ("snapshot", "done", "error", "paused")
_REVIEW_KEYS = frozenset({"reason", "token", "task_key", "run_key", "chapter_id"})
_REVIEW_VALUE_KEYS = ("reason", "token", "run_key", "chapter_id")


def persisted_replay_events(task: Task) -> tuple[tuple[str, dict[str, Any]], tuple[str, dict[str, Any]]]:
    """已结束 / 待审核任务的原始回放帧；引擎发送与中央证明共用，不得另写一套映射。"""
    from app.services import task_service
    from app.services.task_engine.engine import receipt_unpublished_detail

    out = task_service.out_json(task)
    unpublished = receipt_unpublished_detail(task)
    if unpublished is not None:
        # 收据受限收尾（E5-d）：成功但结果未发布。帧只带任务快照与固定说明，不带收据重建的 result。
        end = "done", {"task": out, "result": None, "unpublished": True, "detail": unpublished}
    elif task.status == "succeeded":
        end = "done", {"task": out, "result": task.result}
    elif task.status == "paused":
        end = "paused", {"task": out, "result": task.result}
    else:
        end = "error", {"task": out, "detail": task.error}
    return ("snapshot", {"task": out, "text": task.output_text}), end


def rich_review_of(event: str, data: object) -> dict[str, Any] | None:
    """待审核富文档帧的原提案凭据；其余帧为 None。"""
    review = data.get("result") if event == "paused" and isinstance(data, dict) else None
    return review if isinstance(review, dict) and review.get("reason") == "rich_document_review" else None


def rich_review_shape_ok(review: dict[str, Any], task_key: str | None) -> bool:
    return (set(review) == _REVIEW_KEYS and review.get("task_key") == task_key
            and type(review.get("chapter_id")) is int and isinstance(review.get("token"), str)
            and isinstance(review.get("run_key"), str))


def rich_review_pending_conditions(
    *, task_id: int, task_key: str, project_id: int | None, queued_at: Any, review: dict[str, Any],
) -> tuple[Any, ...]:
    """Task 断点与 pending 提案联表一致的条件；调用方另加本人可见性条件。"""
    from app.models import SceneReviewProposal

    checkpoint = Task.checkpoint["rich_review"]
    return (
        Task.id == task_id, Task.statistics_key == task_key, Task.project_id == project_id,
        Task.queued_at == queued_at, Task.status == "paused",
        checkpoint["phase"].as_string() == "awaiting_review",
        checkpoint["token"].as_string() == review["token"],
        checkpoint["run_key"].as_string() == review["run_key"],
        checkpoint["chapter_id"].as_integer() == review["chapter_id"],
        SceneReviewProposal.task_key == task_key, SceneReviewProposal.status == "pending",
        SceneReviewProposal.token == review["token"], SceneReviewProposal.run_key == review["run_key"],
        SceneReviewProposal.chapter_id == review["chapter_id"],
    )


def finalize_projected_frame(
    event: str, data: object, *, task_id: int, task_key: str, original_review: dict[str, Any] | None,
) -> bool:
    """作品投影之后、最终复核之后注入原 Task UUID 与已验证提案凭据；False = 必须拒绝该帧。"""
    if not isinstance(data, dict):
        return True
    if event in _FRAME_TASK_EVENTS:
        projected_task = data.get("task")
        if isinstance(projected_task, dict) and projected_task.get("id") == task_id:
            # 输出前用最终复核的本人原 Task UUID，不信任事件中嵌套的身份值。
            data["task"] = {**projected_task, "task_key": task_key}
    if original_review is not None:
        projected_review = data.get("result")
        if not isinstance(projected_review, dict) or any(
            projected_review.get(key) != original_review[key] for key in _REVIEW_VALUE_KEYS
        ):
            return False
        data["result"] = {**projected_review, "task_key": task_key}
    return True


async def _bound_task_visible(db, task: Task, project: Project | None, review: dict[str, Any] | None) -> bool:
    """同一帧租约内按订阅原绑定（本人、原令牌、原 queued_at、原 grant、活动态 editor）复核任务仍可见；
    待审核富文档帧另在同一句 SQL 中联表要求 Task 断点与当前 pending 提案一致。"""
    from sqlalchemy import select

    from app.models import SceneReviewProposal
    from app.services.collaboration_task_access import task_grant_identity, task_visible_to_actor
    from app.services.task_engine.stream_lease import task_stream_binding

    binding = await task_stream_binding(
        db, task.user_id, task.project_id, task_id=task.id, task_key=task.statistics_key,
    )
    visible = task_visible_to_actor(
        db, binding.actor.id, active_grant_required=binding.active_grant_required,
        token_version=binding.actor.token_version,
    )
    statement = select(Task.id, Task.input).where(
        Task.id == task.id, Task.statistics_key == binding.task_key, Task.user_id == binding.actor.id,
        Task.project_id == binding.project_id, Task.queued_at == binding.queued_at, visible,
    )
    if review is not None:
        # 与引擎同一联表条件：Task 断点与当前 pending 提案须在同一快照内一致。
        if project is None or not rich_review_shape_ok(review, task.statistics_key):
            return False
        statement = statement.join(SceneReviewProposal, SceneReviewProposal.task_id == Task.id).where(
            *rich_review_pending_conditions(
                task_id=task.id, task_key=task.statistics_key, project_id=project.id,
                queued_at=binding.queued_at, review=review,
            ),
        )
    row = (await db.execute(statement)).first()
    return row is not None and task_grant_identity(row.input) == binding.task_grant


async def _verify_persisted_replay(
    db, task: Task, project: Project | None, event: str, data: dict[str, Any], source: TaskViewEvidence,
) -> bool:
    """已结束或待审核任务的持久回放帧：不依赖执行体或发送时 SendGuard，只认同一持锁行的重算投影。

    帧租约已在同一根事务内按订阅原绑定（本人、原令牌、作品活跃、成员原轮次及活动态 editor）
    共享锁定 Project 与 Task；这里再要求持锁行与帧来源逐字段一致，并以与引擎 `checked_frame`
    相同的原始帧（`persisted_replay_events`）、作品投影与最终注入（`finalize_projected_frame`）重算后比较摘要。
    """
    from app.models.entities import FINISHED_TASK_STATUSES

    if task.status not in (*FINISHED_TASK_STATUSES, "paused") or capture_task_view(task) != source:
        return False
    raw = dict(persisted_replay_events(task)).get(event)
    if raw is None or not isinstance(data, dict) or set(data) != set(raw):
        return False
    if project is None:
        # 个人任务：引擎不经作品投影、不注入，原持久帧逐字发送；最终仍按原绑定复核本人可见。
        expected: object = raw
    else:
        from app.services.personal_projection import project_payload

        expected = await project_payload(db, task.user_id, project.id, raw)
    review = rich_review_of(event, raw) if project is not None else None
    if not await _bound_task_visible(db, task, project, review):
        return False
    if project is not None and not finalize_projected_frame(
        event, expected, task_id=task.id, task_key=task.statistics_key, original_review=review,
    ):
        return False
    return isinstance(expected, dict) and _digest(data) == _digest(expected)


def _wizard_successor_source(source: object) -> bool:
    if type(source) is not PublicationSource:
        return False
    from app.services.collaboration_ai_guard import wizard_successor_eligible

    return wizard_successor_eligible(source.guard)


def _project_at_stamp(project: Project | None, stamp: object) -> bool:
    return bool(isinstance(stamp, VisibilityStamp) and project is not None and project.id == stamp.project_id
                and project.ai_visibility_revision == stamp.revision and project.ai_history_epoch == stamp.epoch
                and _time(project.last_deleted_at) == _time(stamp.last_deleted_at))


async def _verify_wizard_successor(
    db, task: Task, evidence: TaskStreamEvidence, hub: object, guard: object, original: TaskExecutionIdentity,
) -> bool:
    """设定类向导「同执行戳后继」（2026-11-13）：保存前产出、在本任务收紧可见性的保存提交后才发出的帧。

    只接受同一 hub 已登记的最新来源，且它是同一执行、带由原未决凭据逐项派生的完成收据、可见性戳为本任务保存
    事务内读取的新戳的仅发布 guard（WizardHandler.stream_guard 的 done 来源）；帧投影戳须是构建戳或该新戳。
    当前授权与新戳仍成立由该 guard 在本帧租约锁内复核（运行中 require_task_read / 已结束 require_terminal_read）。
    不以持锁行补签，不跨执行；他人在保存后再收紧，或构建后保存前改戳（保存失败、不会有完成来源）均拒绝。
    完成来源尚未发布时退出整个租约等待下一来源或终态（受帧租约 frame_timeout 约束）。
    """
    from app.services.collaboration_ai_guard import wizard_publication_successor
    from app.services.task_engine.stream_lease import require_task_stream_lease

    if matches_running(original, task):
        # 原未决执行仍在运行而戳已变：变化来自他人，不是本任务保存。
        return False
    latest, changed = hub.source, hub.source_changed
    if (latest is not evidence.source and type(latest) is PublicationSource
            and _publication_hub(latest) is hub and hub.task_id == task.id
            and wizard_publication_successor(guard, original, latest.guard, latest.execution, evidence.visibility)):
        if matches_running(latest.execution, task):
            await latest.guard.require_task_read(db)
        else:
            terminal = evidence.terminal
            if terminal is None and not hub.closed:
                raise TerminalEvidencePending(hub)
            if (type(terminal) is not TerminalEvidence
                    or _TERMINALS.get(terminal) != (_snapshot(terminal.execution), _snapshot(terminal.view))
                    or not same_execution(latest.execution, terminal.execution)
                    or capture_task_view(task) != terminal.view):
                return False
            await latest.guard.require_terminal_read(db, terminal)
        await require_task_stream_lease(
            db, task.user_id, task.project_id, task_id=task.id, task_key=task.statistics_key,
        )
        return True
    if evidence.terminal is None and not hub.closed:
        raise PublicationSourcePending(hub, changed)
    return False


def _stream_kind(kind: object) -> bool:
    """按 `_stream_readable` 白名单已开放的范围：只有实现 stream_guard 的任务类型（单章、分析、批量、向导、
    大纲、实体）才可证明领取/排队帧；Agent、知识库、拆书等继续失败关闭。"""
    from app.services.task_engine.registry import get_handler

    if type(kind) is not str:
        return False
    try:
        handler = get_handler(kind)
    except Exception:  # noqa: BLE001
        return False
    return callable(getattr(handler, "stream_guard", None))


def stream_kind_open(kind: object) -> bool:
    """生产订阅入口（E5-f）判定协作者可走受控管线的任务类型；与领取/排队证明同一白名单。"""
    return _stream_kind(kind)


async def _owner_ai_current(db, task: Task) -> bool:
    """协作任务依赖作者模型授权时，帧租约锁内直接核对当前 `allow_owner_ai`（不只依赖成员轮次递增）。"""
    from app.services.collaboration_task_access import GRANT_KEY, owner_ai_allowed, task_requires_owner_ai

    if not isinstance(task.input, dict):
        return False
    if task.project_id is None or GRANT_KEY not in task.input or not task_requires_owner_ai(task.input):
        return True
    return await owner_ai_allowed(db, task.project_id, task.user_id)


def _bounded_progress(data: dict[str, Any]) -> bool:
    progress = data.get("progress")
    return type(progress) is int and 0 <= progress <= 100 and isinstance(data.get("stage"), str)


def _claim_payload_shape(event: str, data: dict[str, Any]) -> bool:
    """领取来源只覆盖不含模型输出的帧：领取 progress（含排队位次清空的过渡帧）、续跑 reset、正文为空的快照。"""
    if event == "reset":
        return data == {}
    if event == "progress":
        if set(data) == {"status", "progress", "stage"}:
            return data.get("status") == "running" and _bounded_progress(data)
        return (set(data) == {"status", "progress", "stage", "queue_position"} and data.get("status") == "queued"
                and data.get("queue_position") is None and _bounded_progress(data))
    if event == "snapshot":
        return set(data) == {"task", "text"} and data.get("text") == "" and isinstance(data.get("task"), dict)
    return False


def _claim_time_task(task: Task, execution: TaskExecutionIdentity) -> Task:
    """同一执行在领取提交时的行状态（断点/游标取自领取身份）；游离对象，只供重算投影，不入会话。"""
    from sqlalchemy import inspect as sa_inspect

    copy = Task()
    for attribute in sa_inspect(Task).column_attrs:
        setattr(copy, attribute.key, getattr(task, attribute.key))
    copy.checkpoint = execution.checkpoint
    copy.step_cursor = execution.step_cursor
    return copy


async def _claim_snapshot_matches(db, task: Task, shown: dict[str, Any], execution: TaskExecutionIdentity) -> bool:
    """运行中快照的任务摘要只含已提交行可证明的字段：按持锁行（或同一执行在领取提交时的断点/游标，二者都是
    已提交事实）重算订阅投影后逐字一致；progress/stage 是 hub 实时值（库里节流刷新），只校验类型。"""
    from app.services import task_service
    from app.services.collaboration_task_access import task_grant_identity

    if not _bounded_progress(shown):
        return False
    candidates = [task]
    if task.step_cursor != execution.step_cursor or task.checkpoint != execution.checkpoint:
        # 投影后、发送前，同一执行可能已合法推进断点（如分析写入 pending、批量建立断点）。
        candidates.append(_claim_time_task(task, execution))
    for candidate in candidates:
        out = task_service.to_out(candidate)
        out = await task_service.project_task_out(
            db, task.user_id, out, active_grant_required=True, task_key=task.statistics_key,
            expected_grant=task_grant_identity(task.input), expected_queued_at=task.queued_at,
        )
        expected = {**out.model_dump(mode="json"), "progress": shown["progress"], "stage": shown["stage"]}
        if _digest(shown) == _digest(expected):
            return True
    return False


def _same_execution_advancing(execution: TaskExecutionIdentity, task: Task) -> bool:
    """持锁行仍是同一执行（UUID、入队轮次、执行键）在运行，且与领取身份的差异只能是已知的合法推进：
    单章保存事务移除了输入中的 `_chapter_claim` 指针（其余输入逐字相同），或已登记管理员取消意图。"""
    if (task.status != "running" or task.id != execution.task_id or task.statistics_key != execution.task_key
            or task.execution_key != execution.execution_key or _time(task.queued_at) != _time(execution.queued_at)
            or task.user_id != execution.actor_id or task.kind != execution.kind):
        return False
    if task.admin_cancel_operation_id is not None or task.admin_cancel_requested_at is not None:
        return True
    original = execution.input
    if "_chapter_claim" not in original or not isinstance(task.input, dict):
        return False
    original.pop("_chapter_claim")
    return _digest(task.input) == _digest(original)


async def _verify_claim_frame(
    db, task: Task, project: Project | None, event: str, data: dict[str, Any], evidence: TaskStreamEvidence,
) -> bool:
    """领取后、首个已核验事件之前的帧（E5-e）。

    来源是引擎在本次领取提交确认后登记的同一执行身份（同一 hub）。非终态帧只许不含模型输出的形状；发送时
    帧租约锁内要求持锁行仍是同一执行在运行（断点/游标可已由同一执行合法推进），快照的任务摘要按持锁行重算；
    若持锁行已由同一执行提交终态，则改用原 hub 登记的终态证明（尚未登记时退出租约等待）。授权：帧租约已按
    订阅原绑定复核认证/成员/活动 editor/作品活跃与可见性戳，这里再直接核对作者模型授权。
    """
    from app.services.task_engine.execution_identity import matches_running_base
    from app.services.task_engine.stream_lease import require_task_stream_lease

    source = evidence.source
    hub = _claim_hub(source)
    execution = source.execution
    if (hub is None or project is None or not isinstance(data, dict) or task.id != execution.task_id
            or task.statistics_key != execution.task_key or not _stream_kind(execution.kind)
            or task.kind != execution.kind):
        return False
    terminal_event = event in ("done", "error", "paused")
    if terminal_event:
        # 领取来源下只可能是首个事件前失败/取消/中断的 error；done/paused 必经已核验事件、不由本来源证明。
        if event != "error" or set(data) != {"task", "detail"}:
            return False
    elif not _claim_payload_shape(event, data):
        return False
    running_same = matches_running_base(execution, task)
    if not terminal_event and running_same:
        if event == "snapshot" and not await _claim_snapshot_matches(db, task, data["task"], execution):
            return False
    elif task.status in ("queued", "running"):
        if not hub.closed and _same_execution_advancing(execution, task):
            # 同一执行仍在运行但已合法推进到不再逐字匹配领取身份（单章保存事务已移除输入中的占用指针、或已有
            # 管理员取消意图，终态尚未提交）：不放宽匹配，退出租约等原 hub 的终态证明后按终态分支重验
            # （受 frame_timeout 约束）。
            raise TerminalEvidencePending(hub)
        # 持锁行是另一次领取（或不是本次提交的行）：不能等待、也不能借终态证明。
        return False
    else:
        terminal = evidence.terminal
        if terminal is None and not hub.closed:
            raise TerminalEvidencePending(hub)
        if (type(terminal) is not TerminalEvidence
                or _TERMINALS.get(terminal) != (_snapshot(terminal.execution), _snapshot(terminal.view))
                or not same_execution(execution, terminal.execution)
                or capture_task_view(task) != terminal.view):
            return False
        if event == "snapshot" and (data["task"].get("id") != task.id
                                    or data["task"].get("task_key") != task.statistics_key):
            return False
    # 与排队/回放帧同一层：同一帧租约内按订阅原绑定（本人、原令牌、原轮次、活动态 editor）复核任务仍可见。
    if not await _bound_task_visible(db, task, project, None) or not await _owner_ai_current(db, task):
        return False
    await require_task_stream_lease(
        db, task.user_id, task.project_id, task_id=task.id, task_key=task.statistics_key,
    )
    return True


async def _verify_queued_frame(
    db, task: Task, project: Project | None, event: str, data: dict[str, Any], evidence: TaskStreamEvidence,
) -> bool:
    """排队位次 progress（E5-e）：只认同一 hub 登记的同一入队轮次；帧内仅排队状态/进度/阶段/位次，无正文。"""
    from app.services.task_engine.stream_lease import require_task_stream_lease

    source = evidence.source
    if (_queued_hub(source) is None or project is None or not isinstance(data, dict) or event != "progress"
            or task.id != source.task_id or task.statistics_key != source.task_key
            or _time(task.queued_at) != source.queued_at or not _stream_kind(task.kind)):
        return False
    position = data.get("queue_position")
    if (set(data) != {"status", "progress", "stage", "queue_position"} or data.get("status") != "queued"
            or not _bounded_progress(data)
            or not (position is None or (type(position) is int and position >= 0))):
        return False
    if not await _bound_task_visible(db, task, project, None) or not await _owner_ai_current(db, task):
        return False
    await require_task_stream_lease(
        db, task.user_id, task.project_id, task_id=task.id, task_key=task.statistics_key,
    )
    return True


# 排队快照中随领取/执行合法变化的摘要字段；其余（身份、类型、标题、关联 ID、Skill、创建时间）须与持锁行重算一致。
_QUEUED_DYNAMIC_FIELDS = ("status", "progress", "stage", "error", "checkpoint", "rich_review", "step_cursor",
                          "queue_position", "started_at", "finished_at")


async def _verify_late_queued_snapshot(
    db, task: Task, project: Project | None, event: str, data: dict[str, Any], source: TaskViewEvidence,
) -> bool:
    """排队快照在投影后、实际发送前恰逢同一入队轮次被领取（或已结束）（E5-e）。

    快照只能是正文为空的排队摘要：动态字段须为全新排队形状（无断点、无错误、未开始），不变字段按持锁行
    （同一 UUID 与入队轮次）重新投影后逐字一致；授权按订阅原绑定在帧租约锁内复核。不证明任何正文。
    """
    from app.services import task_service
    from app.services.collaboration_task_access import task_grant_identity
    from app.services.task_engine.stream_lease import require_task_stream_lease

    shown = data.get("task") if isinstance(data, dict) else None
    position = shown.get("queue_position") if isinstance(shown, dict) else None
    if (event != "snapshot" or set(data) != {"task", "text"} or data.get("text") != "" or project is None
            or not isinstance(shown, dict) or source.task_id != task.id or source.task_key != task.statistics_key
            or source.queued_at != _time(task.queued_at) or not _stream_kind(task.kind)
            or shown.get("status") != "queued" or not _bounded_progress(shown) or shown.get("error") != ""
            or shown.get("checkpoint") is not None or shown.get("rich_review") is not None
            or shown.get("step_cursor") != 0 or shown.get("started_at") is not None
            or shown.get("finished_at") is not None
            or not (position is None or (type(position) is int and position >= 0))):
        return False
    out = task_service.to_out(task)
    out = await task_service.project_task_out(
        db, task.user_id, out, active_grant_required=True, task_key=task.statistics_key,
        expected_grant=task_grant_identity(task.input), expected_queued_at=task.queued_at,
    )
    expected = out.model_dump(mode="json")
    expected.update({key: shown.get(key) for key in _QUEUED_DYNAMIC_FIELDS})
    if set(shown) != set(expected) or _digest(shown) != _digest(expected):
        return False
    if not await _bound_task_visible(db, task, project, None) or not await _owner_ai_current(db, task):
        return False
    await require_task_stream_lease(
        db, task.user_id, task.project_id, task_id=task.id, task_key=task.statistics_key,
    )
    return True


async def verify_stream_frame(
    db, task: Task, project: Project | None, event: str, data: dict[str, Any], evidence: object,
) -> bool:
    """调用方已持原认证、可见性、Project 与 Task 共享锁，锁到实际 ASGI send。"""
    try:
        from app.services.task_engine.stream_lease import require_task_stream_lease

        await require_task_stream_lease(
            db, task.user_id, task.project_id, task_id=task.id, task_key=task.statistics_key,
        )
        if (type(evidence) is not TaskStreamEvidence or evidence.event != event
                or _FRAMES.get(evidence) != (evidence.source, evidence._terminal, evidence.visibility,
                                             evidence.event, evidence.payload_hash)
                or _digest(data) != evidence.payload_hash):
            return False
        stamp = evidence.visibility
        # 帧投影戳已被推进时，只有设定类向导未决帧可经下方「同执行戳后继」证明（本任务自己的收紧保存）。
        stale_stamp = False
        if stamp is not None:
            if (project is None or project.id != stamp.project_id or _time(project.last_deleted_at) != _time(
                    stamp.last_deleted_at)):
                return False
            if project.ai_visibility_revision != stamp.revision or project.ai_history_epoch != stamp.epoch:
                if not _wizard_successor_source(evidence.source):
                    return False
                stale_stamp = True
        elif project is not None:
            return False
        if type(evidence.source) is ClaimSource:
            # E5-e：领取提交后、首个已核验事件前的无输出帧。
            return await _verify_claim_frame(db, task, project, event, data, evidence)
        if type(evidence.source) is QueuedSource:
            # E5-e：排队位次帧。
            return await _verify_queued_frame(db, task, project, event, data, evidence)
        if type(evidence.source) is TaskViewEvidence:
            if evidence.source.status == "queued" and task.status != "queued":
                # E5-e：排队快照的实际发送恰逢领取提交（不以持锁行补签正文）。
                return await _verify_late_queued_snapshot(db, task, project, event, data, evidence.source)
            if task.status == "queued":
                # 排队行没有执行体来源，不能为 delta 或任意客户端正文补签。
                # 快照的全部公开字段和正文必须由同一持锁任务重新投影证明。
                if (event != "snapshot" or set(data) != {"task", "text"}
                        or capture_task_view(task) != evidence.source or not await _owner_ai_current(db, task)):
                    return False
                from app.services import task_service
                from app.services.collaboration_task_access import task_grant_identity

                out = task_service.to_out(task, queue_position=await task_service.queue_position(db, task))
                out = await task_service.project_task_out(
                    db, task.user_id, out, active_grant_required=True, task_key=task.statistics_key,
                    expected_grant=task_grant_identity(task.input), expected_queued_at=task.queued_at,
                )
                return _digest(data) == _digest({"task": out.model_dump(mode="json"),
                                                 "text": task.output_text or ""})
            if project is None or (project.deleted_at is None and project.deletion_started_at is None):
                # 已结束 / 待审核回放：同一持锁行按订阅原绑定重算 _finished_events 的公开投影。
                return await _verify_persisted_replay(db, task, project, event, data, evidence.source)
            # 删除中的作者仅可收到不含私有引用的固定取消通知。
            safe_task = data.get("task") if isinstance(data, dict) else None
            if (project is not None and project.user_id == task.user_id and project.deleted_at is None
                    and project.deletion_started_at is not None and task.status == "cancelled"
                    and event == "error" and set(data) == {"task", "detail"} and data.get("detail") == "任务已取消"
                    and isinstance(safe_task, dict) and all(safe_task.get(key) is None for key in (
                        "checkpoint", "skill", "rich_review", "retry_of_id", "chapter_id", "scene_id",
                        "analysis_id", "character_id", "outline_id",
                    ))):
                from app.services import task_service

                safe = task_service.to_out(task).model_copy(update={
                    "checkpoint": None, "skill": None, "rich_review": None, "retry_of_id": None,
                    "chapter_id": None, "scene_id": None, "analysis_id": None, "character_id": None,
                    "outline_id": None,
                })
                return (capture_task_view(task) == evidence.source
                        and _digest(safe_task) == _digest(safe.model_dump(mode="json")))
            return False
        if type(evidence.source) is not PublicationSource:
            return False
        from app.services.collaboration_ai_guard import (
            SendGuard,
            publication_execution_matches,
            publication_successor,
        )

        original, guard = evidence.source.execution, evidence.source.guard
        hub = _publication_hub(evidence.source)
        if hub is None or type(guard) is not SendGuard or not publication_execution_matches(guard, original):
            return False
        if matches_running(original, task) and not stale_stamp:
            await guard.require_task_read(db)
            await require_task_stream_lease(
                db, task.user_id, task.project_id, task_id=task.id, task_key=task.statistics_key,
            )
            return True
        if _wizard_successor_source(evidence.source) and (
            stale_stamp or not _project_at_stamp(project, getattr(guard, "_stamp", None))
        ):
            # 设定类向导（2026-11-13）：构建戳已不再成立时，未决帧不走下方终态证明（构建 guard 必然失败），
            # 只能由同一 hub 登记的本执行完成仅发布 guard 证明；构建戳仍成立（大纲、非替换首次保存）时不进入。
            return await _verify_wizard_successor(db, task, evidence, hub, guard, original)
        terminal = evidence.terminal
        if terminal is None and not hub.closed and _is_batch_guard(guard):
            # 批量（E5-c2）：断点逐章推进，帧发出时原来源常已被同一执行后来的步骤取代。只接受同一 hub
            # 已登记、由同一执行发布的最新来源与持锁行逐字一致，并以其 guard 在锁内复核当前授权与当前章；
            # 不以持锁行补签，也不放宽到别的执行。行已推进而新来源尚未发布时，退出租约等下一个来源或终态。
            latest, changed = hub.source, hub.source_changed
            if (latest is not evidence.source and type(latest) is PublicationSource
                    and _publication_hub(latest) is hub and hub.task_id == task.id
                    and publication_successor(guard, original, latest.guard, latest.execution)
                    and matches_running(latest.execution, task)):
                await latest.guard.require_task_read(db)
                await require_task_stream_lease(
                    db, task.user_id, task.project_id, task_id=task.id, task_key=task.statistics_key,
                )
                return True
            raise PublicationSourcePending(hub, changed)
        if terminal is None and not hub.closed:
            # 发送者先退出整个租约再等待，同一帧重取锁后重新验证；避免等待死锁。
            raise TerminalEvidencePending(hub)
        if (type(terminal) is not TerminalEvidence
                or _TERMINALS.get(terminal) != (_snapshot(terminal.execution), _snapshot(terminal.view))
                or not same_execution(original, terminal.execution)
                or capture_task_view(task) != terminal.view):
            return False
        await guard.require_terminal_read(db, terminal)
        review = rich_review_of(event, data)
        if review is not None and not await _bound_task_visible(db, task, project, review):
            # 待审核富文档终态帧（单章）：与持久回放同一联表证明，提案在发送前被丢弃/采纳即拒绝。
            return False
        await require_task_stream_lease(
            db, task.user_id, task.project_id, task_id=task.id, task_key=task.statistics_key,
        )
        return True
    except TerminalEvidencePending:
        raise
    except Exception:  # noqa: BLE001  证据不可解析或事务失效时不发出帧。
        return False
