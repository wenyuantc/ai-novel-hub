"""任务 SSE 的原身份捕获与同协程单帧锁域。"""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from weakref import WeakKeyDictionary

import anyio
from fastapi.responses import StreamingResponse
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import NotFoundError
from app.models import Project, ProjectCollaborator, Task, User
from app.models.entities import OPEN_TASK_STATUSES
from app.services import ai_config_service, ai_dispatch, authorization_service, project_lifecycle, task_service
from app.services.authorization_service import AuthenticatedActor
from app.services.collaboration_task_access import (
    GrantIdentity,
    owner_ai_allowed,
    task_grant_identity,
    task_requires_owner_ai,
    task_visible_to_actor,
)


class TaskStreamLeaseError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("任务流授权凭据无效")


@dataclass(slots=True, repr=False)
class _LeaseState:
    actor: AuthenticatedActor
    project_id: int | None
    task_id: int
    task_key: str
    queued_at: datetime | None
    event: str
    payload_hash: str
    evidence: object
    owner: asyncio.Task
    session_transaction: object
    connection: object
    connection_transaction: object
    driver_connection: object
    valid: bool = True
    # 订阅入口冻结的原绑定；仅供中央帧证明在同一租约内按原轮次重算持久投影。
    binding: object = None


@dataclass(slots=True, repr=False)
class _SqliteSharedLease:
    owner: asyncio.Task
    session_transaction: object
    connection: object
    connection_transaction: object
    driver_connection: object


# 唯一登记点在 _task_frame_lease 的真实锁序之后；不接受调用方提供的“已持锁”布尔值。
_LIVE_STREAM_LEASES: WeakKeyDictionary[AsyncSession, _LeaseState] = WeakKeyDictionary()
# 仅在 SQLite 非 WAL 模式的 _task_frame_lease 持有真实 SHARED 读事务期间登记，
# 避免受控 SSE 帧租约与慢模型出站 _send_lease 的 RESERVED 写锁互斥。
_SQLITE_SHARED_FRAME_LEASES: WeakKeyDictionary[AsyncSession, _SqliteSharedLease] = WeakKeyDictionary()


async def is_sqlite_shared_frame_lease(db: AsyncSession) -> bool:
    """确认当前会话与协程正处于 _task_frame_lease 建立的真实 SQLite SHARED 读事务内。"""
    state = _SQLITE_SHARED_FRAME_LEASES.get(db)
    if state is None or state.owner is not asyncio.current_task():
        return False
    try:
        if db.get_bind().dialect.name != "sqlite":
            return False
        root = db.get_transaction()
        if (root is None or not root.is_active or root.sync_transaction is not state.session_transaction
                or db.get_nested_transaction() is not None):
            return False
        connection = await db.connection()
        physical = connection.sync_connection
        raw = getattr(physical.connection.dbapi_connection, "_connection", None)
        return (not connection.closed and not connection.invalidated and physical is state.connection
                and physical.get_transaction() is state.connection_transaction
                and physical.get_nested_transaction() is None
                and physical.connection.dbapi_connection is state.driver_connection
                and getattr(raw, "in_transaction", False) is True)
    except Exception:  # noqa: BLE001  任一校验失败按未持共享读租约处理（失败关闭）。
        return False


def _payload_hash(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def require_task_stream_lease(
    db: AsyncSession, actor_id: int, project_id: int | None, *, task_id: int, task_key: str,
) -> None:
    """证明当前协程仍持原 actor、项目与任务的同一根事务和物理连接。"""
    state = _LIVE_STREAM_LEASES.get(db)
    if (type(actor_id) is not int or type(task_id) is not int or type(task_key) is not str
            or (project_id is not None and type(project_id) is not int)
            or state is None or not state.valid or state.actor.id != actor_id or state.project_id != project_id
            or state.task_id != task_id or state.task_key != task_key or state.owner is not asyncio.current_task()):
        raise TaskStreamLeaseError()
    try:
        root = db.get_transaction()
        if (db.info.get("authenticated_actor") != state.actor or root is None or not root.is_active
                or root.sync_transaction is not state.session_transaction or db.get_nested_transaction() is not None):
            raise TaskStreamLeaseError()
        connection = await db.connection()
        physical = connection.sync_connection
        if (connection.closed or connection.invalidated or physical is not state.connection
                or physical.get_transaction() is not state.connection_transaction
                or physical.get_nested_transaction() is not None
                or physical.connection.dbapi_connection is not state.driver_connection):
            raise TaskStreamLeaseError()
    except Exception:
        state.valid = False
        raise TaskStreamLeaseError() from None


async def _require_frame_integrity(
    db: AsyncSession, state: _LeaseState, event: str, data: dict, evidence: object,
) -> None:
    from app.services.task_engine.peer_guard import ReceivedFrame

    await require_task_stream_lease(
        db, state.actor.id, state.project_id, task_id=state.task_id, task_key=state.task_key,
    )
    try:
        if (state.event != event or state.evidence is not evidence or state.payload_hash != _payload_hash(data)
                or type(evidence) is ReceivedFrame and not evidence.current()):
            raise TaskStreamLeaseError()
    except Exception:
        state.valid = False
        raise TaskStreamLeaseError() from None


@dataclass(frozen=True, slots=True, repr=False)
class _StreamBinding:
    """只保存 HTTP 入口观察到的原客户端、任务和成员身份。"""

    actor: AuthenticatedActor
    task_id: int
    task_key: str
    project_id: int | None
    owner_id: int | None
    project_last_deleted_at: datetime | None
    queued_at: datetime | None
    task_grant: GrantIdentity | None
    member_grant: GrantIdentity | None
    active_grant_required: bool


async def _capture_stream_binding(db: AsyncSession, task: Task, actor: AuthenticatedActor) -> _StreamBinding:
    project_id, task_key, queued_at = task.project_id, task.statistics_key, task.queued_at
    task_grant = task_grant_identity(task.input)
    active = task.status in OPEN_TASK_STATUSES
    owner_id, last_deleted_at, member_grant = None, None, None
    if project_id is not None:
        project = await db.scalar(select(Project).where(
            Project.id == project_id, *project_lifecycle.active_conditions(),
        ))
        if project is None:
            raise NotFoundError("任务不存在")
        owner_id = project.user_id
        last_deleted_at = project.last_deleted_at
        if owner_id != actor.id:
            member = await db.scalar(select(ProjectCollaborator).where(
                ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == actor.id,
                ProjectCollaborator.status == "accepted",
            ))
            if member is None:
                raise NotFoundError("任务不存在")
            member_grant = task_grant_identity({"_collaboration_grant": {
                "invitation_id": member.invitation_id, "revision": member.revision,
            }})
            if member_grant is None or (active and (member.role != "editor" or task_grant != member_grant)):
                raise NotFoundError("任务不存在")
    # 首次读与成员读取之间也可能改变，不能把新任务实例替换进原请求。
    current = await task_service.get_owned_task(db, actor.id, task.id, expected_task_key=task_key)
    if (not isinstance(task_key, str) or current.project_id != project_id or current.queued_at != queued_at
            or task_grant_identity(current.input) != task_grant
            or (not active and current.status in OPEN_TASK_STATUSES)):
        raise NotFoundError("任务不存在")
    return _StreamBinding(
        actor, task.id, task_key, project_id, owner_id, last_deleted_at, queued_at, task_grant, member_grant, active,
    )


@asynccontextmanager
async def _task_frame_lease(
    sessionmaker: async_sessionmaker[AsyncSession], binding: _StreamBinding, event: str, data: dict,
) -> AsyncIterator[tuple[AsyncSession, Task, Project | None]]:
    """仅取得锁；独立调用本 scope 不会建立物理发送证明。"""
    db = sessionmaker()
    db.info["authenticated_actor"] = binding.actor
    try:
        with db.no_autoflush:
            if db.get_bind().dialect.name == "sqlite":
                connection = await db.connection()
                journal_mode = (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar()
                if isinstance(journal_mode, str) and journal_mode.lower() != "wal":
                    # 默认回滚日志模式：显式开启读事务并先读一页，使 SQLite C 层真实持有 SHARED 锁；
                    # 既不与慢模型 _send_lease 的 RESERVED 写锁冲突，又能阻塞撤权/改权限等写方 COMMIT（需 EXCLUSIVE）。
                    # 若外部手工启用 WAL，SHARED 不阻塞写方提交，故保守回退既有写锁路径。
                    await connection.exec_driver_sql("BEGIN")
                    await connection.exec_driver_sql("SELECT 1 FROM sqlite_master LIMIT 1")
                    root = db.get_transaction()
                    owner = asyncio.current_task()
                    physical = connection.sync_connection
                    raw = getattr(physical.connection.dbapi_connection, "_connection", None)
                    if (root is not None and root.is_active and db.get_nested_transaction() is None
                            and owner is not None and not connection.closed and not connection.invalidated
                            and physical.get_transaction() is not None and physical.get_nested_transaction() is None
                            and getattr(raw, "in_transaction", False) is True):
                        _SQLITE_SHARED_FRAME_LEASES[db] = _SqliteSharedLease(
                            owner, root.sync_transaction, physical, physical.get_transaction(),
                            physical.connection.dbapi_connection,
                        )
            await authorization_service.require_authenticated_for_send(db, binding.actor)
            if binding.actor.editor_created_at is not None:
                # 上一行已锁住同一账号。SQLite 可复用整数 ID，原账号创建时间也是身份的一部分。
                from app.services.task_engine.stream_evidence import _time

                created_at = await db.scalar(select(User.created_at).where(User.id == binding.actor.id))
                if _time(created_at) != _time(binding.actor.editor_created_at):
                    raise NotFoundError("任务不存在")
            project = None
            if binding.project_id is not None:
                await ai_dispatch._lock(db, binding.project_id, write=False, include_recycled=True)
                project = await db.scalar(
                    select(Project).where(Project.id == binding.project_id).with_for_update(read=True)
                )
                if (project is None or project.user_id != binding.owner_id
                        or project.last_deleted_at != binding.project_last_deleted_at):
                    raise NotFoundError("任务不存在")
            owner_cancel = bool(
                project is not None and project.user_id == binding.actor.id and project.deleted_at is None
                and project.deletion_started_at is not None and event == "error" and data.get("detail") == "任务已取消"
            )
            if project is not None and not owner_cancel:
                project_lifecycle.ensure_active(project)
            # 先取两方配置选择共享门禁，再锁 Task；证据校验不能用当前配置代替原绑定。
            config_owners = {binding.actor.id}
            if binding.owner_id is not None:
                config_owners.add(binding.owner_id)
            for config_owner in sorted(config_owners):
                await ai_config_service.lock_config_owner(db, config_owner, write=False)
            statement = select(Task).where(
                Task.id == binding.task_id, Task.statistics_key == binding.task_key, Task.user_id == binding.actor.id,
                Task.project_id == binding.project_id, Task.queued_at == binding.queued_at,
            )
            if not owner_cancel:
                statement = statement.where(task_visible_to_actor(
                    db, binding.actor.id, active_grant_required=binding.active_grant_required,
                    token_version=binding.actor.token_version,
                ))
            task = await db.scalar(statement.with_for_update(read=True).execution_options(populate_existing=True))
            if task is None or task_grant_identity(task.input) != binding.task_grant:
                raise NotFoundError("任务不存在")
            if owner_cancel and task.status != "cancelled":
                raise NotFoundError("任务不存在")
            if binding.member_grant is not None:
                member = await db.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == binding.project_id,
                    ProjectCollaborator.user_id == binding.actor.id, ProjectCollaborator.status == "accepted",
                    ProjectCollaborator.invitation_id == binding.member_grant[0],
                    ProjectCollaborator.revision == binding.member_grant[1],
                ).with_for_update(read=True))
                if member is None or (binding.active_grant_required and member.role != "editor"):
                    raise NotFoundError("任务不存在")
                # E5-f：活动协作订阅依赖作者模型时，锁内直接核对当前 allow_owner_ai（不只依赖成员轮次递增）。
                if (binding.active_grant_required and task_requires_owner_ai(task.input)
                        and not await owner_ai_allowed(db, binding.project_id, binding.actor.id)):
                    raise NotFoundError("任务不存在")
        yield db, task, project
    finally:
        _SQLITE_SHARED_FRAME_LEASES.pop(db, None)
        # 取消时也限时释放；若关闭失败则失效连接，不能把持锁连接归还池。
        closed = False
        with anyio.move_on_after(1, shield=True):
            try:
                await db.close()
                closed = True
            except Exception:
                pass
        if not closed:
            with anyio.move_on_after(1, shield=True):
                try:
                    await db.invalidate()
                except Exception:
                    pass
            raise RuntimeError("任务流授权租约释放失败") from None


async def task_stream_binding(
    db: AsyncSession, actor_id: int, project_id: int | None, *, task_id: int, task_key: str,
) -> "_StreamBinding":
    """仅在真实单帧发送租约内返回订阅时冻结的原绑定；裸锁 scope 或伪造状态均失败。"""
    await require_task_stream_lease(db, actor_id, project_id, task_id=task_id, task_key=task_key)
    state = _LIVE_STREAM_LEASES.get(db)
    binding = state.binding if state is not None else None
    if (type(binding) is not _StreamBinding or binding.actor != state.actor or binding.task_id != task_id
            or binding.task_key != task_key or binding.project_id != project_id
            or binding.queued_at != state.queued_at):
        if state is not None:
            state.valid = False
        raise TaskStreamLeaseError()
    return binding


async def require_task_stream_physical_lease(
    db: AsyncSession, actor_id: int, project_id: int | None, *, task_id: int, task_key: str,
) -> None:
    """只接受真实单帧发送函数内登记的证明，裸锁 scope 不能获得。"""
    await require_task_stream_lease(db, actor_id, project_id, task_id=task_id, task_key=task_key)


async def require_task_stream_terminal(
    db: AsyncSession, actor_id: int, project_id: int | None, *, task_id: int, task_key: str, terminal: object,
) -> None:
    """终态只能使用中央原帧中同一份服务端收据。"""
    from app.services.task_engine.stream_evidence import TaskStreamEvidence, TerminalEvidence

    await require_task_stream_physical_lease(db, actor_id, project_id, task_id=task_id, task_key=task_key)
    state = _LIVE_STREAM_LEASES.get(db)
    if (state is None or not state.event or type(state.evidence) is not TaskStreamEvidence
            or type(terminal) is not TerminalEvidence
            or state.evidence.terminal is not terminal):
        raise TaskStreamLeaseError()


def _format_task_frame(event: str, data: dict) -> bytes:
    # 与 app.api.sse 的公共帧格式保持一致；证据永不序列化。
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def _send_guarded_frame(
    sessionmaker, binding: _StreamBinding, event: str, data: dict, evidence: object, *,
    send=None, peer_write=None, source_worker: str | None = None, frame_timeout: float,
) -> str:
    from app.services.task_engine import peer_guard, stream_evidence

    @asynccontextmanager
    async def registered_frame():
        """只在真正负责写出的协程登记物理事务；本地、内部和最终客户端写入共享锁序。"""
        async with _task_frame_lease(sessionmaker, binding, event, data) as (db, task, project):
            root = db.get_transaction()
            connection = await db.connection()
            owner = asyncio.current_task()
            physical = connection.sync_connection
            if (root is None or not root.is_active or db.get_nested_transaction() is not None or owner is None
                    or connection.closed or connection.invalidated or physical.get_transaction() is None
                    or physical.get_nested_transaction() is not None or db in _LIVE_STREAM_LEASES):
                raise TaskStreamLeaseError()
            state = _LeaseState(
                binding.actor, binding.project_id, binding.task_id, binding.task_key,
                binding.queued_at, event, _payload_hash(data), evidence, owner, root.sync_transaction,
                physical, physical.get_transaction(), physical.connection.dbapi_connection,
                binding=binding,
            )

            def transaction_created(_session, transaction) -> None:
                if transaction.nested:
                    state.valid = False

            def transaction_ended(_session, transaction) -> None:
                if transaction is state.session_transaction:
                    state.valid = False

            watching = False
            try:
                _LIVE_STREAM_LEASES[db] = state
                sqlalchemy_event.listen(db.sync_session, "after_transaction_create", transaction_created)
                sqlalchemy_event.listen(db.sync_session, "after_transaction_end", transaction_ended)
                watching = True
                yield db, task, project, state
            finally:
                state.valid = False
                if _LIVE_STREAM_LEASES.get(db) is state:
                    del _LIVE_STREAM_LEASES[db]
                if watching:
                    sqlalchemy_event.remove(db.sync_session, "after_transaction_create", transaction_created)
                    sqlalchemy_event.remove(db.sync_session, "after_transaction_end", transaction_ended)

    entered = False
    try:
        with anyio.fail_after(frame_timeout):
            while True:
                try:
                    async with registered_frame() as (db, task, project, state):
                        conditions = None
                        if peer_write is not None:
                            conditions = await peer_guard.capture(
                                db, binding, task, project, event, data, evidence, source_worker=source_worker,
                            )
                        elif type(evidence) is peer_guard.ReceivedFrame:
                            if evidence.ctx.sessionmaker is not sessionmaker:
                                raise NotFoundError("任务不存在")
                            await peer_guard.validate(db, binding, task, project, event, data, evidence)
                        elif not await stream_evidence.verify_stream_frame(db, task, project, event, data, evidence):
                            raise NotFoundError("任务不存在")
                        await _require_frame_integrity(db, state, event, data, evidence)
                        entered = True
                        if peer_write is None:
                            await send({"type": "http.response.body", "body": _format_task_frame(event, data),
                                        "more_body": True})
                        else:
                            await peer_write(event, data, conditions)
                        await _require_frame_integrity(db, state, event, data, evidence)
                    return "sent"
                except Exception as exc:
                    if entered:
                        raise
                    if isinstance(exc, stream_evidence.TerminalEvidencePending):
                        # 此时已释放所有锁；等待原执行的终态/后继，再用原证据重取租约。
                        await exc.wait()
                    elif isinstance(exc, peer_guard.PeerFrameAdvanced) and type(evidence) is peer_guard.ReceivedFrame:
                        evidence = await evidence.refresh()
                    else:
                        raise
    except Exception as exc:
        if isinstance(exc, TimeoutError) and (entered or type(evidence) is not peer_guard.ReceivedFrame):
            return "abort"
        if entered:
            if isinstance(exc, OSError):
                raise OSError("任务流连接已断开") from None
            raise RuntimeError("任务流传输中断") from None
        denied = {"task": None, "detail": "任务访问权限已失效", "code": "permission_denied"}
        try:
            with anyio.fail_after(frame_timeout):
                if peer_write is None:
                    await send({"type": "http.response.body", "body": _format_task_frame("error", denied),
                                "more_body": True})
                else:
                    await peer_write("error", denied, None)
        except (TimeoutError, OSError):
            return "abort"
        except Exception:
            raise RuntimeError("任务流传输中断") from None
        return "error"


async def send_peer_source_frame(
    sessionmaker, binding: _StreamBinding, event: str, data: dict, evidence: object, write, *,
    source_worker: str, frame_timeout: float = 5.0,
) -> str:
    """执行端仅在原来源验证通过的真实帧租约中写出；确认及重核由调用方在返回后处理。"""
    return await _send_guarded_frame(
        sessionmaker, binding, event, data, evidence, peer_write=write, frame_timeout=frame_timeout,
        source_worker=source_worker,
    )


class _TaskFrameGuardedResponse(StreamingResponse):
    """唯一证明登记点：实际 ASGI send 循环内持有该帧的根事务及全部锁。"""

    def __init__(
        self, events: AsyncIterator[tuple[str, dict, object]], sessionmaker: async_sessionmaker[AsyncSession],
        binding: _StreamBinding, frame_timeout: float,
    ) -> None:
        super().__init__(events, media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no",
        })
        self._events, self._sessionmaker, self._binding = events, sessionmaker, binding
        self._frame_timeout = frame_timeout

    async def _send_frame(self, send, event: str, data: dict, evidence: object) -> str:
        return await _send_guarded_frame(
            self._sessionmaker, self._binding, event, data, evidence, send=send, frame_timeout=self._frame_timeout,
        )

    async def _close_events(self) -> None:
        failed = False
        with anyio.CancelScope(shield=True):
            try:
                with anyio.fail_after(self._frame_timeout):
                    await self._events.aclose()
            except Exception:
                failed = True
        if failed:
            raise RuntimeError("任务流清理失败") from None

    async def stream_response(self, send) -> None:
        try:
            await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            with anyio.fail_after(self._frame_timeout):
                await send({"type": "http.response.body", "body": b": stream-start\n\n", "more_body": True})
            async for event, data, evidence in self._events:
                if not event:
                    with anyio.fail_after(self._frame_timeout):
                        await send({"type": "http.response.body", "body": b": ping\n\n", "more_body": True})
                    continue
                if event == "error" and data in (
                    {"task": None, "detail": "任务不存在"},
                    {"task": None, "detail": "任务访问权限已失效", "code": "permission_denied"},
                ):
                    with anyio.fail_after(self._frame_timeout):
                        await send({"type": "http.response.body", "body": _format_task_frame(event, data),
                                    "more_body": True})
                    from app.services.task_engine.peer_guard import ReceivedFrame

                    if type(evidence) is ReceivedFrame and evidence.current():
                        # 公网已收到固定终态。对端已关闭时仍正常结束本次响应。
                        try:
                            with anyio.fail_after(self._frame_timeout):
                                await evidence.subscriber.ack()
                        except Exception:
                            pass
                    break
                outcome = await self._send_frame(send, event, data, evidence)
                if outcome == "abort":
                    return
                if outcome == "error":
                    break
            with anyio.fail_after(self._frame_timeout):
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except TimeoutError:
            return
        finally:
            await self._close_events()


def task_sse_response(
    events: AsyncIterator[tuple[str, dict, object]], *, sessionmaker: async_sessionmaker[AsyncSession],
    binding: _StreamBinding, frame_timeout: float = 5.0,
) -> StreamingResponse:
    return _TaskFrameGuardedResponse(events, sessionmaker, binding, frame_timeout)


