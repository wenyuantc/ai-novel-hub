"""取消请求的在途收尾和原任务失败凭据。记录独立于可裁剪的 Task。"""

import asyncio
import hashlib
import json
import logging
from contextlib import ExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import and_, case, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import ConflictError
from app.models import Task, TaskWorker, TaskWorkRecord
from app.services import project_lifecycle
from app.services.task_engine import workers

logger = logging.getLogger(__name__)


def blocked():
    return ConflictError("原任务清理尚未确认，作品未删除", extra={"code": "project_tasks_not_stopped"})


@dataclass
class RequestCleanup:
    engine: object
    coroutine: asyncio.Task
    stack: ExitStack
    work_key: str | None = None
    worker_key: str | None = None


_scope: ContextVar[RequestCleanup | None] = ContextVar("task_request_cleanup", default=None)


_retry_owner: ContextVar[tuple[str, str, str] | None] = ContextVar("task_cleanup_retry_owner", default=None)


@contextmanager
def finishing_retry(work_key, worker_key, attempt):
    token = _retry_owner.set((work_key, worker_key, attempt))
    try:
        yield
    finally:
        _retry_owner.reset(token)


async def finish_retry(db, engine):
    owner = _retry_owner.get()
    if owner is None:
        return
    key, worker, attempt = owner
    await engine.request_worker.require(db, worker)
    changed = await db.execute(delete(TaskWorkRecord).where(
        TaskWorkRecord.kind == "abort_failure", TaskWorkRecord.work_key == key,
        TaskWorkRecord.worker_key == worker, TaskWorkRecord.operation_id == attempt,
        TaskWorkRecord.state == "retry_applied",
    ))
    if changed.rowcount != 1:
        raise blocked()


def _current(engine):
    scope = _scope.get()
    return scope if scope and scope.engine is engine and scope.coroutine is asyncio.current_task() else None


async def _confirm_exit(db, engine, worker_key, write):
    """结束回执可按同一尝试重写。暂断时退避，原代次失效后由其他持有者接手。"""
    async def confirm():
        delay = 0.05
        while engine.request_worker.worker_key == worker_key and engine.request_worker.valid:
            try:
                await write()
                if not await workers.lock_live_worker(db, worker_key):
                    await db.rollback()
                    return
                await db.commit()
                return
            except Exception:  # noqa: BLE001  丢回执也可能已提交；只重写具有同一尝试身份的原事实。
                await db.rollback()
                if delay == 0.05:
                    logger.warning("请求清理退出尚未确认")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 1.0)

    confirmation = asyncio.create_task(confirm())
    interrupted = False
    while not confirmation.done():
        try:
            await asyncio.shield(confirmation)
        except asyncio.CancelledError:
            interrupted = True
    confirmation.result()
    if interrupted:
        raise asyncio.CancelledError


async def _confirm_request_exit(db, scope):
    same = (TaskWorkRecord.kind == "request_cleanup", TaskWorkRecord.work_key == scope.work_key,
            TaskWorkRecord.worker_key == scope.worker_key)

    async def write():
        await db.execute(delete(TaskWorkRecord).where(*same, TaskWorkRecord.state == "settled"))
        await db.execute(update(TaskWorkRecord).where(*same, TaskWorkRecord.state.in_(("active", "applied")))
                         .values(state=case((TaskWorkRecord.state == "applied", "exited_applied"), else_="exited")))

    await _confirm_exit(db, scope.engine, scope.worker_key, write)


async def relinquish_retry(db, engine, key, worker, attempt, original_worker):
    async def write():
        await db.rollback()
        await db.execute(update(TaskWorkRecord).where(
            TaskWorkRecord.kind == "abort_failure", TaskWorkRecord.work_key == key,
            TaskWorkRecord.worker_key == worker, TaskWorkRecord.operation_id == attempt,
            TaskWorkRecord.state.in_(("retrying", "retry_applied")),
        ).values(state=case((TaskWorkRecord.state == "retry_applied", "failed_applied"), else_="failed"),
                 worker_key=original_worker, operation_id=None))

    await _confirm_exit(db, engine, worker, write)


@asynccontextmanager
async def request_scope(db, engine):
    """外层保留到授权投影结束。只有实际赢得状态更新的一方才登记，等待方不进入排空集合。"""
    if _current(engine) is not None:
        yield
        return
    with ExitStack() as stack:
        scope = RequestCleanup(engine, asyncio.current_task(), stack)
        token = _scope.set(scope)
        try:
            try:
                yield
            except BaseException:
                if scope.work_key is not None:
                    await db.rollback()
                raise
            finally:
                if scope.work_key is not None:
                    await _confirm_request_exit(db, scope)
        finally:
            _scope.reset(token)


async def register(db, engine, task, *, settled=False, phase=None):
    scope = _current(engine)
    if scope is None or scope.work_key is not None:
        return
    worker_key = await engine.request_worker.require(db)
    key = str(uuid4())
    scope.stack.enter_context(engine.cleanup_scope(task))
    snapshot = engine.capture_abort_snapshot(task, recovery=True) if not settled else None
    proof = {"phase": phase, "snapshot": _proof(snapshot)} if snapshot is not None else None
    db.add(TaskWorkRecord(
        work_key=key, kind="request_cleanup", project_id=task.project_id, worker_key=worker_key,
        task_id=task.id, task_key=task.statistics_key, queued_at=task.queued_at, execution_key=task.execution_key,
        state="settled" if settled else "active", evidence=proof,
    ))
    scope.work_key, scope.worker_key = key, worker_key
    await db.flush()


async def settle(db, engine):
    scope = _current(engine)
    if scope is None or scope.work_key is None:
        return
    await engine.request_worker.require(db, scope.worker_key)
    changed = await db.execute(update(TaskWorkRecord).where(
        TaskWorkRecord.work_key == scope.work_key, TaskWorkRecord.worker_key == scope.worker_key,
        TaskWorkRecord.kind == "request_cleanup", TaskWorkRecord.state.in_(("applied", "settled")),
    ).values(state="settled"))
    if changed.rowcount != 1:
        raise blocked()


@asynccontextmanager
async def atomic_handler_context(engine, db):
    """handler 继续使用独立会话；它的提交/回滚只影响外层清理事务的保存点。"""
    connection = await db.connection()
    if connection.dialect.name == "sqlite":
        await connection.exec_driver_sql("BEGIN")
    sessions = async_sessionmaker(bind=connection, expire_on_commit=False,
                                  join_transaction_mode="create_savepoint", info=dict(db.info))
    yield replace(engine.ctx, sessionmaker=sessions)


async def apply_effect(engine, callback, *, work_key=None, worker_key=None):
    """业务清理和收据一起提交。原事务未知时，凭本次工作 UUID 的收据重读。"""
    scope = _current(engine)
    retry = work_key is not None
    if not retry and scope is not None:
        work_key, worker_key = scope.work_key, scope.worker_key
    if work_key is None:
        return await callback(engine.ctx)
    kind = "abort_failure" if retry else "request_cleanup"
    before, after = ("retrying", "retry_applied") if retry else ("active", "applied")
    same = (TaskWorkRecord.work_key == work_key, TaskWorkRecord.worker_key == worker_key,
            TaskWorkRecord.kind == kind)
    if retry:
        owner = _retry_owner.get()
        if owner is None or owner[:2] != (work_key, worker_key):
            raise blocked()
        same = (*same, TaskWorkRecord.operation_id == owner[2])
    async with engine.ctx.sessionmaker() as db:
        async with atomic_handler_context(engine, db) as ctx:
            result = await callback(ctx)
        changed = await db.execute(update(TaskWorkRecord).where(*same, TaskWorkRecord.state == before)
                                   .values(state=after))
        if changed.rowcount != 1:
            raise blocked()
        await engine.request_worker.require(db, worker_key)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            if await db.scalar(select(TaskWorkRecord.state).where(*same)) != after:
                raise
    return result


async def clear_chapter_failure(db, task, receipt):
    """调用者已在同一事务验证本轮的管理员章节释放收据。"""
    pointer = {name: receipt[name] for name in ("claim_key", "chapter_id", "prev_status")}
    rows = await db.scalars(select(TaskWorkRecord).where(
        TaskWorkRecord.kind == "abort_failure", TaskWorkRecord.task_id == receipt["task_id"],
        TaskWorkRecord.task_key == receipt["task_key"], TaskWorkRecord.project_id == receipt["project_id"],
        TaskWorkRecord.queued_at == task.queued_at, TaskWorkRecord.execution_key == receipt["execution_key"],
    ).with_for_update())
    for row in rows:
        proof = row.evidence
        if (isinstance(proof, dict) and proof.get("kind") == task.kind and proof.get("actor") == receipt["actor_id"]
                and proof.get("chapter") == receipt["chapter_id"] and proof.get("pointer") == pointer):
            await db.delete(row)


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _proof(snapshot):
    """正文、提示词和整个断点只存摘要。另存允许被正确归还操作移除的原占用指针。"""
    original_input, checkpoint = json.loads(snapshot.input_json), json.loads(snapshot.checkpoint_json)
    proof = dict(actor=snapshot.actor_id, kind=snapshot.kind, chapter=snapshot.chapter_id,
                 resource=snapshot.resource_key, cursor=snapshot.step_cursor, status=snapshot.status,
                 original_worker=snapshot.worker_key, input_hash=_digest(snapshot.input_json),
                 checkpoint_hash=_digest(snapshot.checkpoint_json))
    if snapshot.kind in ("chapter.generate", "chapter.revise", "chapter.polish"):
        proof["pointer"] = original_input["_chapter_claim"]
    elif snapshot.kind == "chapter.batch" and isinstance(checkpoint, dict) and checkpoint.get("current") is not None:
        proof["batch_current"] = checkpoint["current"]
    elif snapshot.kind == "kb.index" and snapshot.claim is not None:
        proof["document"] = [snapshot.claim.document_id, snapshot.claim.claim_key, snapshot.claim.phase]
    return proof


def _failure_key(project_id, snapshot, proof):
    identity = [project_id] if snapshot is None else [
        project_id, snapshot.task_id, snapshot.task_key, _utc(snapshot.queued_at).isoformat(),
        snapshot.execution_key, proof,
    ]
    return str(uuid5(NAMESPACE_URL, "ainovel/abort-failure/" + _canonical(identity)))


async def record_failure(engine, project_id, snapshot, *, task=None, registration_rolled_back=False):
    proof = _proof(snapshot) if snapshot is not None else None
    source = task if snapshot is None and task is not None and task.project_id == project_id else None
    key = _failure_key(project_id, snapshot, proof)
    if project_id is None and (source is None or source.statistics_key is None):
        return False
    if source is not None and source.statistics_key is not None:
        key = str(uuid5(NAMESPACE_URL, "ainovel/unproven/" + _canonical(
            [project_id, source.id, source.statistics_key,
             _utc(source.queued_at).isoformat() if source.queued_at is not None else None, source.execution_key],
        )))
    async with engine.ctx.sessionmaker() as db:
        insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
        tid = snapshot.task_id if snapshot else (source.id if source else None)
        task_key = snapshot.task_key if snapshot else (source.statistics_key if source else None)
        ident = snapshot or source
        scope = _current(engine)
        state = "failed"
        if project_id is None and await db.scalar(select(Task.id).where(
            Task.id == source.id, Task.statistics_key == source.statistics_key, Task.project_id.is_(None),
            Task.queued_at == source.queued_at, Task.execution_key == source.execution_key,
            Task.worker_key == source.worker_key, Task.status == "running",
        ).with_for_update()) is None:
            return False
        if scope is not None and scope.work_key is not None:
            same = (TaskWorkRecord.kind == "request_cleanup", TaskWorkRecord.work_key == scope.work_key,
                    TaskWorkRecord.worker_key == scope.worker_key)
            previous = await db.scalar(select(TaskWorkRecord.state).where(*same).with_for_update())
            if not await workers.lock_live_worker(db, scope.worker_key):
                return False
            if previous in ("active", "applied"):
                state = "failed_applied" if previous == "applied" else "failed"
                await db.execute(update(TaskWorkRecord).where(*same, TaskWorkRecord.state == previous)
                                 .values(state="settled"))
            elif previous is not None or not registration_rolled_back:
                return False  # 原工作已被移交，迟到协程不得重建失败项。
        else:
            from app.services.task_engine import admin_cleanup

            if admin_cleanup.current.get() is not None:
                await admin_cleanup.check_owner(db)
            elif ident is not None and ident.worker_key is not None:
                # 已被主库围栏的原执行者不能在新处理者结束之后再补上一条失败记录。
                known = await db.scalar(select(TaskWorker.worker_key).where(TaskWorker.worker_key == ident.worker_key))
                if known and not await workers.lock_live_worker(db, ident.worker_key):
                    return False
        await db.execute(insert(TaskWorkRecord).values(
            work_key=key, kind="abort_failure", project_id=project_id,
            worker_key=ident.worker_key if ident else None, task_id=tid, task_key=task_key,
            queued_at=ident.queued_at if ident else None, execution_key=ident.execution_key if ident else None,
            state=state, evidence=proof,
        ).on_conflict_do_nothing(index_elements=["work_key"]))
        await db.commit()
    if project_id is not None:
        engine._durable_abort_failures[key] = (project_id, snapshot)
    return True


async def clear_proven_failure(db, snapshot):
    if snapshot is None:
        return
    key = _failure_key(snapshot.project_id, snapshot, _proof(snapshot))
    await db.execute(delete(TaskWorkRecord).where(TaskWorkRecord.work_key == key,
                                                  TaskWorkRecord.kind == "abort_failure"))


async def has_failure(db, engine, project_id):
    keys = set(await db.scalars(select(TaskWorkRecord.work_key).where(
        TaskWorkRecord.project_id == project_id, TaskWorkRecord.kind == "abort_failure",
    )))
    for key, (project, snapshot) in tuple(engine._durable_abort_failures.items()):
        if project == project_id and key not in keys:
            engine.forget_abort_failure(snapshot)
            engine._durable_abort_failures.pop(key, None)
    return bool(keys) or engine.has_cleanup_failure(project_id)


async def restore_snapshot(db, engine, row):
    data = row.evidence
    if row.kind == "request_cleanup" and isinstance(data, dict):
        data = data.get("snapshot")
    if not isinstance(data, dict):
        raise blocked()
    task = await db.scalar(select(Task).where(
        Task.id == row.task_id, Task.statistics_key == row.task_key, Task.queued_at == row.queued_at,
        Task.project_id == row.project_id, Task.execution_key == row.execution_key,
        Task.worker_key == data.get("original_worker"),
    ).with_for_update())
    if task is None:
        raise blocked()
    original_input, checkpoint = task.input, task.checkpoint
    if "pointer" in data and isinstance(original_input, dict) and "_chapter_claim" not in original_input:
        original_input = {**original_input, "_chapter_claim": data["pointer"]}
    if "batch_current" in data and isinstance(checkpoint, dict) and checkpoint.get("current") is None:
        checkpoint = {**checkpoint, "current": data["batch_current"]}
    if _digest(_canonical(original_input)) != data.get("input_hash") or (
            _digest(_canonical(checkpoint)) != data.get("checkpoint_hash")):
        raise blocked()
    original = Task(
        id=task.id, statistics_key=task.statistics_key, project_id=task.project_id, user_id=task.user_id,
        kind=task.kind, chapter_id=task.chapter_id, resource_key=task.resource_key, queued_at=task.queued_at,
        execution_key=task.execution_key, worker_key=task.worker_key, status=data.get("status"),
        input=original_input, checkpoint=checkpoint, step_cursor=task.step_cursor,
    )
    claim = None
    if "document" in data:
        from app.services.kb_index_service import DocumentIndexClaim

        doc = data["document"]
        if not isinstance(doc, list) or len(doc) != 3:
            raise blocked()
        claim = DocumentIndexClaim(task.id, task.statistics_key, task.user_id, task.project_id, doc[0],
                                   _utc(task.queued_at), doc[1], doc[2], task.worker_key)
    snapshot = engine.capture_abort_snapshot(original, claim=claim, recovery=True)
    if (snapshot is None or _proof(snapshot) != data or (row.kind == "abort_failure"
            and _failure_key(row.project_id, snapshot, data) != row.work_key)):
        raise blocked()
    return snapshot


async def recover_exited_requests(db, engine, project_id, *, target=()):
    """以原请求进入时的快照接回未完成工作；已提交的回退步骤随收据转移，防止重复执行。"""
    transferable = (TaskWorkRecord.state.in_(("exited", "exited_applied"))
                    | (TaskWorkRecord.state.in_(("active", "applied"))
                       & workers.confirmed_terminal(TaskWorkRecord.worker_key)))
    conditions = (TaskWorkRecord.kind == "request_cleanup", TaskWorkRecord.project_id == project_id,
                  transferable, *target)
    keys = list(await db.scalars(select(TaskWorkRecord.work_key).where(*conditions)))
    if not keys:
        return
    if project_id is not None:
        await project_lifecycle.lock_record(db, project_id)
    for key in keys:
        row = await db.scalar(select(TaskWorkRecord).where(*conditions, TaskWorkRecord.work_key == key)
                              .with_for_update())
        if row is None:
            continue
        snapshot = await restore_snapshot(db, engine, row)
        phase = row.evidence.get("phase")
        expected_status = {"queued": "queued", "paused": "cancelled"}.get(phase)
        if (expected_status is None or (phase == "queued" and not row.execution_key)
                or await db.scalar(select(Task.id).where(
                    Task.id == snapshot.task_id, Task.statistics_key == snapshot.task_key,
                    Task.status == expected_status,
                )) is None):
            raise blocked()
        proof = row.evidence["snapshot"]
        failure_key = _failure_key(project_id, snapshot, proof)
        # 原请求退出和转入可重试队列在同一事务。独立失败项不能覆盖当前工作。
        if await db.get(TaskWorkRecord, failure_key) is not None:
            raise blocked()
        db.add(TaskWorkRecord(
            work_key=failure_key, kind="abort_failure", project_id=project_id, worker_key=snapshot.worker_key,
            task_id=snapshot.task_id, task_key=snapshot.task_key, queued_at=snapshot.queued_at,
            execution_key=snapshot.execution_key, evidence=proof,
            state="failed_applied" if row.state in ("applied", "exited_applied") else "failed",
        ))
        await db.delete(row)
    await db.flush()


def settled_request_is_releasable():
    return and_(TaskWorkRecord.kind == "request_cleanup", TaskWorkRecord.state == "settled",
                workers.confirmed_terminal(TaskWorkRecord.worker_key))


async def reap_settled_requests(db, project_id):
    conditions = (TaskWorkRecord.project_id == project_id, settled_request_is_releasable())
    if await db.scalar(select(TaskWorkRecord.work_key).where(*conditions).limit(1)):
        await db.execute(delete(TaskWorkRecord).where(*conditions))
