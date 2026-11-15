"""服务端两段受理证明：锁外 DNS，锁内重选同一模型后才可入队。"""

import asyncio
import weakref
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Task
from app.services import ai_config_service, project_ai_config
from app.services.authorization_service import AuthenticatedActor
from app.services.task_engine.execution_identity import _json, _utc
from app.services.task_engine.registry import TaskContext


def _invalid() -> ConflictError:
    return ConflictError("任务受理凭据已变化，请重新发起", extra={"code": "task_admission_changed"})


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class RetrySource:
    task_id: int
    task_key: str
    actor_id: int
    project_id: int | None
    kind: str
    queued_at: datetime
    input_json: str
    checkpoint_json: str
    step_cursor: int


_RETRY_SOURCES: weakref.WeakKeyDictionary[RetrySource, tuple[Any, ...]] = weakref.WeakKeyDictionary()


def _retry_evidence(source: RetrySource) -> tuple[Any, ...]:
    return (source.task_id, source.task_key, source.actor_id, source.project_id, source.kind, source.queued_at,
            source.input_json, source.checkpoint_json, source.step_cursor)


def require_original_retry_source(source: RetrySource, actor: AuthenticatedActor) -> RetrySource:
    """只承认本进程由原客户端任务 UUID 捕获且未被修改的来源快照。"""
    try:
        if (type(source) is not RetrySource or type(actor) is not AuthenticatedActor
                or _RETRY_SOURCES.get(source) != _retry_evidence(source) or source.actor_id != actor.id):
            raise ValueError
        return source
    except (TypeError, ValueError, AttributeError):
        raise _invalid() from None


def require_retry_source_task(source: RetrySource, task: Task, actor: AuthenticatedActor) -> None:
    """比较已注册来源和本次 SQL 快照；不把当前行替换成新来源。"""
    original = require_original_retry_source(source, actor)
    candidate = capture_retry_source(task, expected_task_key=original.task_key)
    if _retry_evidence(candidate) != _retry_evidence(original):
        raise _invalid()


def capture_retry_source(task: Task, *, expected_task_key: str) -> RetrySource:
    """只从本人请求首次读到并核对 UUID 的源任务捕获；锁后还须复比。"""
    try:
        if (not isinstance(task, Task) or type(task.id) is not int or task.id <= 0
                or type(task.user_id) is not int or task.user_id <= 0
                or type(expected_task_key) is not str or str(UUID(expected_task_key)) != expected_task_key
                or task.statistics_key != expected_task_key or not isinstance(task.queued_at, datetime)
                or not isinstance(task.input, dict) or type(task.step_cursor) is not int):
            raise ValueError
        source = RetrySource(task.id, expected_task_key, task.user_id, task.project_id, task.kind, _utc(task.queued_at),
                             _json(task.input), _json(task.checkpoint), task.step_cursor)
        _RETRY_SOURCES[source] = _retry_evidence(source)
        return source
    except (TypeError, ValueError, RecursionError):
        raise _invalid() from None


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False, repr=False)
class TaskAdmission:
    actor: AuthenticatedActor
    kind: str
    params_json: str
    model_preflight: project_ai_config.ProjectModelPreflight
    retry_source: RetrySource | None


_ADMISSIONS: weakref.WeakKeyDictionary[TaskAdmission, tuple[Any, ...]] = weakref.WeakKeyDictionary()
_CONSUMED: weakref.WeakKeyDictionary[AsyncSession, tuple[Any, ...]] = weakref.WeakKeyDictionary()


def _evidence(proof: TaskAdmission) -> tuple[Any, ...]:
    source = proof.retry_source
    frozen_source = None if source is None else _retry_evidence(source)
    return (proof.actor.id, proof.actor.token_version, proof.kind, proof.params_json, id(proof.model_preflight),
            frozen_source)


async def prepare_task_admission(
    ctx: TaskContext, actor: AuthenticatedActor, kind: str, params: dict[str, Any], *,
    model_preflight: project_ai_config.ProjectModelPreflight, retry_source: RetrySource | None = None,
) -> TaskAdmission:
    """必须在调用链取得任何业务写锁前调用；预检会话已关闭，随后才解析 DNS。"""
    if (type(actor) is not AuthenticatedActor or type(kind) is not str or not isinstance(params, dict)
            or (retry_source is not None and (type(retry_source) is not RetrySource
                                              or retry_source.actor_id != actor.id or retry_source.kind != kind))):
        raise _invalid()
    try:
        original = project_ai_config.require_original_preflight(model_preflight)
        if (original.actor != actor or original.binding.actor_id != actor.id
                or original.allow_private != ctx.settings.allow_private_ai_endpoints):
            raise _invalid()
        original_mode = getattr(project_ai_config, "is_original_model_preflight", lambda _: False)(original)
        if original_mode != (retry_source is not None):
            raise _invalid()
        if original_mode:
            project_ai_config.require_retry_model_preflight(original, retry_source)
        if retry_source is not None:
            import json

            if (require_original_retry_source(retry_source, actor).project_id != original.binding.project_id):
                raise _invalid()
            source_input = json.loads(retry_source.input_json)
            bound = project_ai_config.deserialize_project_model(
                source_input.get("_project_model"), actor_id=actor.id, project_id=retry_source.project_id,
                project_owner_id=original.binding.project_owner_id,
            )
            if bound != original.binding:
                raise _invalid()
        params_json = _json(params)
        # 只做可用性/DNS 检查；该 provider 不用于后续传输。
        await ai_config_service.build_provider(
            original.config, ctx.cipher, ctx.factory, ctx.settings.ai_request_timeout,
            allow_private=original.allow_private,
        )
        proof = TaskAdmission(actor, kind, params_json, original, retry_source)
        _ADMISSIONS[proof] = _evidence(proof)
        return proof
    except ConflictError:
        raise
    except Exception:
        # provider 或数据库异常不得带配置、密钥、输入或 SQL。
        raise _invalid() from None


async def consume_task_admission(
    db: AsyncSession, proof: TaskAdmission | None, *, actor: AuthenticatedActor, kind: str, params: dict[str, Any],
    retry_of: Task | None,
) -> project_ai_config.ProjectModelPreflight:
    """同一请求只消费一次，之后还须在当前事务中调用模型 accept 重选。"""
    try:
        if type(proof) is not TaskAdmission:
            raise ValueError
        evidence = _ADMISSIONS.pop(proof, None)
        if (evidence is None or evidence != _evidence(proof)
                or proof.actor != actor or proof.kind != kind or proof.params_json != _json(params)):
            raise ValueError
        if (proof.retry_source is None) != (retry_of is None):
            raise ValueError
        if retry_of is not None:
            if (_RETRY_SOURCES.get(proof.retry_source) != _retry_evidence(proof.retry_source)
                    or _retry_evidence(capture_retry_source(
                        retry_of, expected_task_key=proof.retry_source.task_key,
                    )) != _retry_evidence(proof.retry_source)):
                raise ValueError
        original = project_ai_config.require_original_preflight(proof.model_preflight)
        if project_ai_config.is_original_model_preflight(original) != (retry_of is not None):
            raise ValueError
        if retry_of is not None:
            project_ai_config.require_retry_model_preflight(original, proof.retry_source)
        if retry_of is not None:
            transaction = db.get_transaction()
            if transaction is None or not transaction.is_active or db.get_nested_transaction() is not None:
                raise ValueError
            connection = await db.connection()
            physical = connection.sync_connection
            if connection.closed or connection.invalidated or physical.get_transaction() is None:
                raise ValueError
            _CONSUMED[db] = (proof, _evidence(proof), asyncio.current_task(), transaction.sync_transaction,
                             physical, physical.get_transaction(), physical.connection.dbapi_connection)
        return original
    except (AttributeError, TypeError, ValueError, RecursionError):
        raise _invalid() from None


async def require_consumed_retry_admission(
    db: AsyncSession, proof: TaskAdmission, preflight: project_ai_config.ProjectModelPreflight,
) -> RetrySource:
    """只在消费方原协程、同一物理事务和原 Task 行锁中证明重试来源。"""
    try:
        state = _CONSUMED.pop(db, None)
        if (state is None or state[0] is not proof or state[1] != _evidence(proof)
                or state[2] is not asyncio.current_task() or proof.model_preflight is not preflight
                or proof.retry_source is None or db.info.get("authenticated_actor") != proof.actor):
            raise ValueError
        transaction = db.get_transaction()
        connection = await db.connection()
        physical = connection.sync_connection
        if (transaction is None or not transaction.is_active or transaction.sync_transaction is not state[3]
                or db.get_nested_transaction() is not None or connection.closed or connection.invalidated
                or physical is not state[4] or physical.get_transaction() is not state[5]
                or physical.connection.dbapi_connection is not state[6]):
            raise ValueError
        source = require_original_retry_source(proof.retry_source, proof.actor)
        # populate_existing 会覆盖调用方暂存；原任务有未刷新变化时直接拒绝。
        if any(isinstance(item, Task) and item.id == source.task_id and db.is_modified(item)
               for item in db.dirty):
            raise ValueError
        with db.no_autoflush:
            row = await db.scalar(select(Task).where(
                Task.id == source.task_id, Task.statistics_key == source.task_key, Task.user_id == source.actor_id,
                Task.project_id == source.project_id, Task.kind == source.kind, Task.queued_at == source.queued_at,
                Task.status.in_(("failed", "cancelled", "interrupted")), Task.execution_key.is_(None),
                Task.admin_cancel_operation_id.is_(None), Task.admin_cancel_requested_at.is_(None),
            ).with_for_update().execution_options(populate_existing=True))
        if (row is None or _retry_evidence(capture_retry_source(row, expected_task_key=source.task_key))
                != _retry_evidence(source)):
            raise ValueError
        # 查询之间不得提交、回滚或更换连接，否则已释放的原 Task 行锁不再证明受理。
        transaction = db.get_transaction()
        connection = await db.connection()
        physical = connection.sync_connection
        if (transaction is None or not transaction.is_active or transaction.sync_transaction is not state[3]
                or db.get_nested_transaction() is not None or connection.closed or connection.invalidated
                or physical is not state[4] or physical.get_transaction() is not state[5]
                or physical.connection.dbapi_connection is not state[6]):
            raise ValueError
        return source
    except Exception:  # noqa: BLE001  失效证明和数据库细节均不向调用者泄漏。
        raise _invalid() from None
