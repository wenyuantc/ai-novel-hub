"""仅供服务端使用的章节逐次占用身份。

生产单章及批量任务使用此模块。调用方必须在同一事务中受理／领取任务和绑定占用；
不得接受客户端提供的 claim，也不能把这个身份当作作品访问授权。
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import inspect, select, update
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import attributes
from sqlalchemy.orm.util import identity_key

from app.core.errors import AppError, ConflictError
from app.models import Chapter, Project, Task
from app.services import ai_dispatch, project_access, project_lifecycle
from app.services.collaboration_task_access import GRANT_KEY, GrantIdentity, task_grant_identity

_KEY = "_chapter_claim"
_SINGLE_KINDS = frozenset({"chapter.generate", "chapter.polish", "chapter.revise"})
_PREVIOUS = frozenset({"draft", "done"})
_MAX_ID = 2_147_483_647
_CLEANUP_TERMINAL = frozenset({"failed", "cancelled", "interrupted"})


@dataclass(frozen=True, slots=True, repr=False)
class ChapterClaim:
    """不可变的服务端快照；运行时不能以数据库的新 execution_key 替换旧值。"""

    task_id: int
    task_key: str
    actor_id: int
    project_id: int
    chapter_id: int
    queued_at: datetime
    execution_key: str | None
    claim_key: str
    prev_status: Literal["draft", "done"]
    grant_identity: GrantIdentity | None
    phase: Literal["queued", "running"]
    kind: Literal["single", "batch"]

    def __repr__(self) -> str:
        return "<ChapterClaim>"


def _changed() -> ConflictError:
    return ConflictError("章节生成状态已变化，请刷新后重试", extra={"code": "chapter_claim_changed"})


class ChapterClaimStorageError(AppError):
    """不向任务、HTTP 或日志传播可能携带正文的 SQL 和参数。"""

    status_code = 500


def _database_error(exc: DBAPIError) -> AppError:
    original = exc.orig
    cause = getattr(original, "__cause__", None)
    code = next((getattr(value, "sqlstate", None) or getattr(value, "pgcode", None)
                 for value in (original, cause) if value is not None
                 and (getattr(value, "sqlstate", None) or getattr(value, "pgcode", None))), None)
    if code == "40001" or (isinstance(exc, OperationalError) and ai_dispatch.is_sqlite_busy(exc)):
        return _changed()
    return ChapterClaimStorageError("章节生成状态暂不可用")


def _uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _positive(value: object) -> bool:
    return type(value) is int and 0 < value <= _MAX_ID


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _grant(value: Any) -> GrantIdentity | None:
    if not isinstance(value, dict) or GRANT_KEY not in value:
        return None
    raw = value[GRANT_KEY]
    if not isinstance(raw, dict) or set(raw) != {"invitation_id", "revision"}:
        raise _changed()
    identity = task_grant_identity(value)
    if identity is None or identity[1] > _MAX_ID:
        raise _changed()
    return identity


def _valid_identity(claim: ChapterClaim) -> bool:
    if type(claim) is not ChapterClaim:
        return False
    grant = claim.grant_identity
    return (
        all(_positive(v) for v in (claim.task_id, claim.actor_id, claim.project_id, claim.chapter_id))
        and _uuid(claim.task_key) and _uuid(claim.claim_key)
        and claim.claim_key not in (claim.task_key, claim.execution_key)
        and isinstance(claim.queued_at, datetime)
        and isinstance(claim.prev_status, str) and claim.prev_status in _PREVIOUS
        and (grant is None or (isinstance(grant, tuple) and len(grant) == 2
                              and _uuid(grant[0]) and _positive(grant[1])))
        and ((claim.phase == "queued" and claim.kind == "single" and claim.execution_key is None)
             or (claim.phase == "running" and claim.kind in ("single", "batch")
                 and _uuid(claim.execution_key) and claim.execution_key != claim.task_key))
    )


def _reject_dirty(db: AsyncSession, model, identity: int, fields: tuple[str, ...]) -> None:
    """不覆盖或先 flush 调用方缓存中的身份、绑定和状态。"""
    cached = db.identity_map.get(identity_key(model, identity))
    if cached is not None and any(inspect(cached).attrs[name].history.has_changes() for name in fields):
        raise _changed()


_TASK_FIELDS = (
    "id", "statistics_key", "user_id", "project_id", "chapter_id", "kind", "resource_key", "status",
    "queued_at", "execution_key", "input", "checkpoint", "admin_cancel_operation_id",
)
_CHAPTER_FIELDS = (
    "id", "project_id", "status", "generation_claim_key", "generation_claim_task_key",
    "generation_claim_prev_status",
)


async def _task(db: AsyncSession, task_id: int, *, write: bool):
    _reject_dirty(db, Task, task_id, _TASK_FIELDS)
    stmt = select(*(getattr(Task, name) for name in _TASK_FIELDS)).where(Task.id == task_id)
    if db.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(read=not write)
    return (await db.execute(stmt)).one_or_none()


async def _chapter(db: AsyncSession, chapter_id: int, *, write: bool):
    _reject_dirty(db, Chapter, chapter_id, _CHAPTER_FIELDS)
    stmt = select(*(getattr(Chapter, name) for name in _CHAPTER_FIELDS)).where(Chapter.id == chapter_id)
    if db.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update(read=not write)
    return (await db.execute(stmt)).one_or_none()


def _task_matches(task, claim: ChapterClaim, *, cleanup: bool = False) -> bool:
    if task is None or not _valid_identity(claim):
        return False
    if (task.id != claim.task_id or task.statistics_key != claim.task_key or task.user_id != claim.actor_id
            or task.project_id != claim.project_id or not isinstance(task.queued_at, datetime)
            or _utc(task.queued_at) != _utc(claim.queued_at) or not isinstance(task.input, dict)):
        return False
    try:
        if _grant(task.input) != claim.grant_identity:
            return False
    except ConflictError:
        return False
    request = task.input.get("request")
    if not isinstance(request, dict):
        return False
    if "project_id" in request and (type(request["project_id"]) is not int
                                    or request["project_id"] != claim.project_id):
        return False
    if claim.kind == "single":
        if (task.kind not in _SINGLE_KINDS or task.chapter_id != claim.chapter_id
                or task.resource_key != f"chapter:{claim.chapter_id}"
                or type(task.input.get("chapter_id")) is not int or task.input["chapter_id"] != claim.chapter_id
                or type(request.get("chapter_id")) is not int or request["chapter_id"] != claim.chapter_id):
            return False
    else:
        ids = task.input.get("chapter_ids")
        if (task.kind != "chapter.batch" or task.chapter_id is not None
                or task.resource_key != f"batch:{claim.project_id}"
                or type(request.get("project_id")) is not int or request["project_id"] != claim.project_id
                or not isinstance(ids, list) or not ids or any(not _positive(v) for v in ids)
                or len(ids) != len(set(ids)) or claim.chapter_id not in ids):
            return False
    if not cleanup and task.admin_cancel_operation_id is not None:
        return False
    if claim.phase == "queued":
        return task.execution_key is None and task.status in ({"queued"} | (_CLEANUP_TERMINAL if cleanup else set()))
    return (task.execution_key == claim.execution_key
            and task.status in ({"running"} | (_CLEANUP_TERMINAL if cleanup else set())))


def _pointer(task, claim: ChapterClaim) -> bool:
    if claim.kind == "single":
        value = task.input.get(_KEY)
    else:
        checkpoint = task.checkpoint
        current = checkpoint.get("current") if isinstance(checkpoint, dict) else None
        if not isinstance(current, dict) or type(current.get("chapter_id")) is not int \
                or current["chapter_id"] != claim.chapter_id \
                or current.get("prev_status") != claim.prev_status:
            return False
        value = current.get(_KEY)
    return isinstance(value, dict) and type(value.get("chapter_id")) is int and value == {
        "chapter_id": claim.chapter_id, "claim_key": claim.claim_key, "prev_status": claim.prev_status,
    }


def _chapter_matches(row, claim: ChapterClaim) -> bool:
    return bool(row is not None and row.id == claim.chapter_id and row.project_id == claim.project_id
                and row.status == "generating" and row.generation_claim_key == claim.claim_key
                and row.generation_claim_task_key == claim.task_key
                and row.generation_claim_prev_status == claim.prev_status)


def _cache(db: AsyncSession, model, identity: int, values: dict[str, Any]) -> None:
    cached = db.identity_map.get(identity_key(model, identity))
    if cached is not None:
        for name, value in values.items():
            attributes.set_committed_value(cached, name, value)


async def _rollback(db: AsyncSession) -> None:
    """失败后不能让调用方捕获异常再提交半个占用；重复取消也等待清理。"""
    async def cleanup() -> None:
        try:
            await db.rollback()
        except Exception:
            await db.invalidate()

    work = asyncio.create_task(cleanup())
    while True:
        try:
            await asyncio.shield(work)
            return
        except asyncio.CancelledError:
            if work.cancelled():
                raise


async def _authorize(db: AsyncSession, claim: ChapterClaim, *, write: bool) -> Project:
    project = await project_access.require_project(
        db, claim.actor_id, claim.project_id, "ai.use", for_update=write, visibility_write=write,
        grant_identity=claim.grant_identity,
    )
    if (project.user_id == claim.actor_id) != (claim.grant_identity is None):
        raise _changed()
    return project


async def _claim(
    db: AsyncSession, *, task_id: int, task_key: str, actor_id: int, project_id: int, chapter_id: int,
    queued_at: datetime, grant_identity: GrantIdentity | None, execution_key: str | None,
    kind: Literal["single", "batch"],
) -> ChapterClaim:
    phase: Literal["queued", "running"] = "queued" if kind == "single" else "running"
    identity = ChapterClaim(task_id, task_key, actor_id, project_id, chapter_id, queued_at, execution_key,
                            str(uuid4()), "draft", grant_identity, phase, kind)
    try:
        if not _valid_identity(identity):
            raise _changed()
        with db.no_autoflush:
            await _authorize(db, identity, write=True)
            task = await _task(db, task_id, write=True)
            if not _task_matches(task, identity):
                raise _changed()
            chapter = await _chapter(db, chapter_id, write=True)
            if (chapter is None or chapter.project_id != project_id or chapter.status not in _PREVIOUS
                    or any(getattr(chapter, name) is not None for name in _CHAPTER_FIELDS[3:])):
                raise _changed()
            identity = replace(identity, prev_status=chapter.status)
            pointer = {"chapter_id": chapter_id, "claim_key": identity.claim_key, "prev_status": chapter.status}
            if kind == "single":
                payload = deepcopy(task.input)
                if _KEY in payload:
                    raise _changed()
                payload[_KEY] = pointer
                task_values = {"input": payload}
            else:
                checkpoint = deepcopy(task.checkpoint)
                if not isinstance(checkpoint, dict) or checkpoint.get("current") is not None:
                    raise _changed()
                checkpoint["current"] = {"chapter_id": chapter_id, "prev_status": chapter.status, _KEY: pointer}
                task_values = {"checkpoint": checkpoint}
            changed = await db.execute(update(Chapter).where(
                Chapter.id == chapter_id, Chapter.project_id == project_id, Chapter.status == chapter.status,
                Chapter.generation_claim_key.is_(None), Chapter.generation_claim_task_key.is_(None),
                Chapter.generation_claim_prev_status.is_(None),
            ).values(status="generating", generation_claim_key=identity.claim_key,
                     generation_claim_task_key=task_key, generation_claim_prev_status=chapter.status,
                     updated_at=Chapter.updated_at).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise _changed()
            changed = await db.execute(update(Task).where(
                Task.id == task_id, Task.statistics_key == task_key, Task.user_id == actor_id,
                Task.project_id == project_id, Task.status == task.status, Task.queued_at == queued_at,
                Task.execution_key == execution_key if execution_key is not None else Task.execution_key.is_(None),
                Task.admin_cancel_operation_id.is_(None),
            ).values(**task_values, updated_at=Task.updated_at).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise _changed()
            _cache(db, Chapter, chapter_id, {
                "status": "generating", "generation_claim_key": identity.claim_key,
                "generation_claim_task_key": task_key, "generation_claim_prev_status": chapter.status,
            })
            _cache(db, Task, task_id, task_values)
            return identity
    except DBAPIError as exc:
        failure = _database_error(exc)
        await _rollback(db)
        raise failure from None
    except BaseException:
        await _rollback(db)
        raise


async def claim_queued_chapter(
    db: AsyncSession, *, task_id: int, task_key: str, actor_id: int, project_id: int, chapter_id: int,
    queued_at: datetime, grant_identity: GrantIdentity | None,
) -> ChapterClaim:
    """Task 已 flush；由服务端生成并在同事务绑定单章 queued 占用。"""
    return await _claim(db, task_id=task_id, task_key=task_key, actor_id=actor_id, project_id=project_id,
                        chapter_id=chapter_id, queued_at=queued_at, grant_identity=grant_identity,
                        execution_key=None, kind="single")


async def claim_batch_chapter(
    db: AsyncSession, *, task_id: int, task_key: str, actor_id: int, project_id: int, chapter_id: int,
    queued_at: datetime, grant_identity: GrantIdentity | None, execution_key: str,
) -> ChapterClaim:
    """每次批量 attempt 生成不同 claim；与 checkpoint.current 同事务写入。"""
    return await _claim(db, task_id=task_id, task_key=task_key, actor_id=actor_id, project_id=project_id,
                        chapter_id=chapter_id, queued_at=queued_at, grant_identity=grant_identity,
                        execution_key=execution_key, kind="batch")


async def activate_queued_chapter(db: AsyncSession, queued: ChapterClaim, *, execution_key: str) -> ChapterClaim:
    """调用方先持项目屏障，再在同事务执行 queued→running CAS 后调用。"""
    try:
        if not _valid_identity(queued) or queued.phase != "queued" or queued.kind != "single":
            raise _changed()
        running = replace(queued, phase="running", execution_key=execution_key)
        if execution_key == queued.task_key or not _valid_identity(running):
            raise _changed()
        with db.no_autoflush:
            await _authorize(db, running, write=True)
            task = await _task(db, running.task_id, write=True)
            chapter = await _chapter(db, running.chapter_id, write=True)
            if (not _task_matches(task, running) or not _pointer(task, running)
                    or not _chapter_matches(chapter, running)):
                raise _changed()
        return running
    except DBAPIError as exc:
        failure = _database_error(exc)
        await _rollback(db)
        raise failure from None
    except BaseException:
        # 调用方可能已经在本事务将任务改成 running；失败时不能提交孤立的领取。
        await _rollback(db)
        raise


async def require_running_chapter(db: AsyncSession, claim: ChapterClaim, *, for_update: bool = True) -> None:
    """保存前持写屏障；尚无可验证的发送 lease 凭据时只允许写事务模式。"""
    try:
        # 不能依靠调用者声称持有 lease；未来须用绑定同一事务的可验证证明再开放读模式。
        if for_update is not True or type(claim) is not ChapterClaim or claim.phase != "running" \
                or not _valid_identity(claim):
            raise _changed()
        with db.no_autoflush:
            await _authorize(db, claim, write=True)
            task = await _task(db, claim.task_id, write=True)
            chapter = await _chapter(db, claim.chapter_id, write=True)
            if not _task_matches(task, claim) or not _pointer(task, claim) or not _chapter_matches(chapter, claim):
                raise _changed()
    except DBAPIError as exc:
        failure = _database_error(exc)
        await _rollback(db)
        raise failure from None
    except BaseException:
        await _rollback(db)
        raise


async def require_streaming_chapter(db: AsyncSession, claim: ChapterClaim) -> None:
    """受控任务流单帧租约内的只读核对：原执行仍持本次章节占用。

    只在 `SendGuard.require_task_read` 中、帧租约已持认证、可见性共享门禁、作品与 Task 共享锁之后调用。
    不要求模型发送 lease、不取写锁（PostgreSQL FOR SHARE；SQLite 沿用租约事务已持的库锁），失败也不回滚：
    租约会话由调用方关闭。不能用于任何保存或发送路径，那里仍须 `require_running_chapter` / `require_sending_chapter`。
    单章核对 Task.input 指针；批量（E5-c2）核对 checkpoint.current 指针（同 `_pointer`）。
    """
    if type(claim) is not ChapterClaim or not _valid_identity(claim) or claim.phase != "running" \
            or claim.kind not in ("single", "batch"):
        raise _changed()
    with db.no_autoflush:
        await _authorize(db, claim, write=False)
        task = await _task(db, claim.task_id, write=False)
        if not _task_matches(task, claim) or not _pointer(task, claim):
            raise _changed()
        chapter = await _chapter(db, claim.chapter_id, write=False)
        if not _chapter_matches(chapter, claim):
            raise _changed()


async def require_sending_chapter(db: AsyncSession, claim: ChapterClaim) -> None:
    """只在同一真实发送事务内锁后核对原执行及章节占用，持锁到响应头。"""
    try:
        if not _valid_identity(claim) or claim.phase != "running":
            raise _changed()
        await ai_dispatch._require_send_lease(db, claim.actor_id, claim.project_id)
        with db.no_autoflush:
            statement = select(Project.id).where(
                Project.id == claim.project_id, *project_lifecycle.active_conditions(),
            )
            if db.get_bind().dialect.name == "postgresql":
                statement = statement.with_for_update(read=True)
            if await db.scalar(statement) is None:
                raise _changed()
            await _authorize(db, claim, write=False)
            task = await _task(db, claim.task_id, write=False)
            if not _task_matches(task, claim) or not _pointer(task, claim):
                raise _changed()
            chapter = await _chapter(db, claim.chapter_id, write=False)
            if not _chapter_matches(chapter, claim):
                raise _changed()
        await ai_dispatch._require_send_lease(db, claim.actor_id, claim.project_id)
        return
    except Exception:
        # 固定策略错误不能被 SDK 当成可重试的网络故障，也不携带 SQL、正文或身份信息。
        ai_dispatch._invalidate_send_lease(db)
        await _rollback(db)
    except BaseException:
        ai_dispatch._invalidate_send_lease(db)
        await _rollback(db)
        raise
    raise ai_dispatch.VisibilityPolicyError("章节生成状态已变化，已停止发送", extra={
        "code": "chapter_claim_changed",
    })


async def release_chapter_claim(db: AsyncSession, claim: ChapterClaim) -> bool:
    """系统只按原身份回退本次占用，不要求撤权后仍为成员；调用方须已停止旧执行体。"""
    if not _valid_identity(claim):
        return False
    try:
        with db.no_autoflush:
            await project_lifecycle.lock_record(db, claim.project_id)
            task = await _task(db, claim.task_id, write=True)
            chapter = await _chapter(db, claim.chapter_id, write=True)
            if (not _task_matches(task, claim, cleanup=True) or not _pointer(task, claim)
                    or not _chapter_matches(chapter, claim)):
                return False
            changed = await db.execute(update(Chapter).where(
                Chapter.id == claim.chapter_id, Chapter.project_id == claim.project_id,
                Chapter.status == "generating", Chapter.generation_claim_key == claim.claim_key,
                Chapter.generation_claim_task_key == claim.task_key,
                Chapter.generation_claim_prev_status == claim.prev_status,
            ).values(status=claim.prev_status, generation_claim_key=None, generation_claim_task_key=None,
                     generation_claim_prev_status=None, updated_at=Chapter.updated_at)
                .execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise _changed()
            if claim.kind == "single":
                payload = deepcopy(task.input)
                payload.pop(_KEY)
                task_values = {"input": payload}
            else:
                checkpoint = deepcopy(task.checkpoint)
                checkpoint["current"] = None
                task_values = {"checkpoint": checkpoint}
            changed = await db.execute(update(Task).where(
                Task.id == claim.task_id, Task.statistics_key == claim.task_key, Task.user_id == claim.actor_id,
                Task.project_id == claim.project_id, Task.queued_at == claim.queued_at,
                Task.execution_key == claim.execution_key if claim.execution_key is not None
                else Task.execution_key.is_(None),
            ).values(**task_values, updated_at=Task.updated_at).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise _changed()
            _cache(db, Chapter, claim.chapter_id, {
                "status": claim.prev_status, "generation_claim_key": None,
                "generation_claim_task_key": None, "generation_claim_prev_status": None,
            })
            _cache(db, Task, claim.task_id, task_values)
            return True
    except DBAPIError as exc:
        failure = _database_error(exc)
        await _rollback(db)
        raise failure from None
    except BaseException:
        await _rollback(db)
        raise


async def reset_generating_chapter(
    db: AsyncSession, actor_id: int, project_id: int, chapter_id: int, status: Literal["draft", "done"],
) -> bool:
    """人工复位时原子废止当前 claim；仅供未来接线且须保证旧发送也使用本协议。"""
    try:
        if not isinstance(status, str) or status not in _PREVIOUS:
            raise ValueError("无效章节状态")
        with db.no_autoflush:
            await project_access.require_project(db, actor_id, project_id, "content.edit", for_update=True,
                                                 visibility_write=True)
            chapter = await _chapter(db, chapter_id, write=True)
            if chapter is None or chapter.project_id != project_id or chapter.status != "generating":
                return False
            changed = await db.execute(update(Chapter).where(
                Chapter.id == chapter_id, Chapter.project_id == project_id, Chapter.status == "generating",
                Chapter.generation_claim_key == chapter.generation_claim_key if chapter.generation_claim_key is not None
                else Chapter.generation_claim_key.is_(None),
                Chapter.generation_claim_task_key == chapter.generation_claim_task_key
                if chapter.generation_claim_task_key is not None else Chapter.generation_claim_task_key.is_(None),
                Chapter.generation_claim_prev_status == chapter.generation_claim_prev_status
                if chapter.generation_claim_prev_status is not None else Chapter.generation_claim_prev_status.is_(None),
            ).values(status=status, generation_claim_key=None, generation_claim_task_key=None,
                     generation_claim_prev_status=None, updated_at=Chapter.updated_at)
                .execution_options(synchronize_session=False))
            if changed.rowcount == 1:
                _cache(db, Chapter, chapter_id, {
                    "status": status, "generation_claim_key": None, "generation_claim_task_key": None,
                    "generation_claim_prev_status": None,
                })
            return changed.rowcount == 1
    except DBAPIError as exc:
        failure = _database_error(exc)
        await _rollback(db)
        raise failure from None
    except BaseException:
        await _rollback(db)
        raise
