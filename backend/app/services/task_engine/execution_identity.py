"""任务领取时的不可变执行身份；重读只能核验，不能代替原领取事实。"""

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Task
from app.services.collaboration_task_access import GRANT_KEY, GrantIdentity, task_grant_identity

_TOKEN_KEYS = {"agent.turn": "_agent_actor_token_version", "wizard.outline": "_wizard_actor_token_version",
               "wizard.characters": "_wizard_actor_token_version", "wizard.world": "_wizard_actor_token_version",
               "wizard.careers": "_wizard_actor_token_version"}
_TASK_TOKEN_KEY = "_task_actor_token_version"


def _changed() -> ConflictError:
    return ConflictError("任务执行身份已变化，请核对后重试", extra={"code": "task_execution_changed"})


def _uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json(value: Any) -> str:
    def validate(item: Any) -> None:
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                raise ValueError
            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        elif type(item) not in (str, int, float, bool, type(None)):
            raise ValueError

    validate(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True, repr=False)
class TaskExecutionIdentity:
    task_id: int
    task_key: str
    actor_id: int
    project_id: int | None
    chapter_id: int | None
    kind: str
    resource_key: str | None
    serial_key: str | None
    retry_of_id: int | None
    queued_at: datetime
    execution_key: str
    grant_identity: GrantIdentity | None
    step_cursor: int
    actor_token_version: int | None
    input_json: str
    checkpoint_json: str
    worker_key: str | None = None

    @property
    def input(self) -> dict[str, Any]:
        return json.loads(self.input_json)

    @property
    def checkpoint(self) -> dict[str, Any] | None:
        return json.loads(self.checkpoint_json)


def from_queued_task(task: Task, execution_key: str, *, worker_key: str | None = None) -> TaskExecutionIdentity:
    """仅领取者在原 queued Task 行锁内调用，execution_key 是本次 CAS 值。"""
    try:
        if (not isinstance(task, Task) or type(task.id) is not int or task.id <= 0
                or type(task.user_id) is not int or task.user_id <= 0 or not _uuid(task.statistics_key)
                or not _uuid(execution_key) or execution_key == task.statistics_key
                or (worker_key is not None and not _uuid(worker_key))
                or task.status != "queued" or task.execution_key is not None
                or task.admin_cancel_operation_id is not None or task.admin_cancel_requested_at is not None
                or not isinstance(task.queued_at, datetime)
                or type(task.kind) is not str or not task.kind or not isinstance(task.input, dict)
                or (task.checkpoint is not None and not isinstance(task.checkpoint, dict))
                or type(task.step_cursor) is not int or task.step_cursor < 0
                or (task.project_id is not None and (type(task.project_id) is not int or task.project_id <= 0))
                or (task.chapter_id is not None and (type(task.chapter_id) is not int or task.chapter_id <= 0
                                                     or task.project_id is None))
                or (task.retry_of_id is not None and (type(task.retry_of_id) is not int or task.retry_of_id <= 0))
                or (task.resource_key is not None and type(task.resource_key) is not str)
                or (task.serial_key is not None and type(task.serial_key) is not str)):
            raise ValueError
        grant = task_grant_identity(task.input)
        if GRANT_KEY in task.input and (grant is None or not isinstance(task.input[GRANT_KEY], dict)
                                        or set(task.input[GRANT_KEY]) != {"invitation_id", "revision"}):
            raise ValueError
        token_key = _TOKEN_KEYS.get(task.kind)
        present_keys = set(task.input) & set(_TOKEN_KEYS.values())
        if present_keys and present_keys != {token_key}:
            raise ValueError
        token_version = task.input.get(_TASK_TOKEN_KEY)
        if (_TASK_TOKEN_KEY in task.input and (type(token_version) is not int or token_version < 0)):
            raise ValueError
        alias = task.input.get(token_key) if token_key is not None else None
        if token_key is not None and (type(token_version) is not int or type(alias) is not int
                                      or alias != token_version):
            raise ValueError
        return TaskExecutionIdentity(
            task.id, task.statistics_key, task.user_id, task.project_id, task.chapter_id, task.kind,
            task.resource_key, task.serial_key, task.retry_of_id,
            _utc(task.queued_at), execution_key, grant, task.step_cursor, token_version, _json(task.input),
            _json(task.checkpoint), worker_key,
        )
    except (TypeError, ValueError, RecursionError):
        raise _changed() from None


def matches_running_base(identity: TaskExecutionIdentity, task: Task) -> bool:
    """比较不随合法多步执行推进的原身份；调用者另须证明当前 attempt。"""
    try:
        return bool(type(identity) is TaskExecutionIdentity and isinstance(task, Task)
                    and type(task.id) is int and task.id == identity.task_id
                    and task.statistics_key == identity.task_key and type(task.user_id) is int
                    and task.user_id == identity.actor_id and type(task.project_id) is type(identity.project_id)
                    and task.project_id == identity.project_id and type(task.chapter_id) is type(identity.chapter_id)
                    and task.chapter_id == identity.chapter_id and task.kind == identity.kind
                    and task.resource_key == identity.resource_key and task.serial_key == identity.serial_key
                    and type(task.retry_of_id) is type(identity.retry_of_id)
                    and task.retry_of_id == identity.retry_of_id
                    and isinstance(task.queued_at, datetime) and _utc(task.queued_at) == identity.queued_at
                    and task.execution_key == identity.execution_key and task.status == "running"
                    and task.worker_key == identity.worker_key
                    and task.admin_cancel_operation_id is None and task.admin_cancel_requested_at is None
                    and _json(task.input) == identity.input_json)
    except (TypeError, ValueError, RecursionError):
        return False


def matches_running(identity: TaskExecutionIdentity, task: Task) -> bool:
    """初次构建或已确认推进后比全部快照，不提供认证或发送 lease。"""
    try:
        return bool(matches_running_base(identity, task) and type(task.step_cursor) is int
                    and task.step_cursor == identity.step_cursor and _json(task.checkpoint) == identity.checkpoint_json)
    except (TypeError, ValueError, RecursionError):
        return False


def same_execution(first: TaskExecutionIdentity, second: TaskExecutionIdentity) -> bool:
    """仅用于比较已证明的进度副本，不能单独证明数据库内容已提交。"""
    if type(first) is not TaskExecutionIdentity or type(second) is not TaskExecutionIdentity:
        return False
    return replace(first, checkpoint_json=second.checkpoint_json, step_cursor=second.step_cursor) == second


def advance_checkpoint(
    identity: TaskExecutionIdentity, checkpoint: dict[str, Any] | None, *, step_cursor: int | None = None,
) -> TaskExecutionIdentity:
    """纯构造：调用方须先以旧快照 CAS 并确认本次已知新值确实提交。"""
    try:
        if type(identity) is not TaskExecutionIdentity:
            raise ValueError
        cursor = identity.step_cursor if step_cursor is None else step_cursor
        if (type(cursor) is not int or cursor < 0
                or (checkpoint is not None and not isinstance(checkpoint, dict))):
            raise ValueError
        return replace(identity, checkpoint_json=_json(checkpoint), step_cursor=cursor)
    except (TypeError, ValueError, RecursionError):
        raise _changed() from None


def frozen_project_owner_id(identity: TaskExecutionIdentity) -> int | None:
    """协作执行领取时冻结的作品作者（E4i-b2）；作者执行（无 grant）为 None。

    只取自原领取快照里服务端写入的模型 / 共享向量绑定（`project_owner_id`），不读当前数据库，
    供引擎在取发送写门禁前与 actor 一起按 ID 升序锁 User 行；锁后仍由 `require_write_execution`
    核对 `project.user_id`。协作任务缺少可用绑定、多个绑定不一致或作者等于 actor 时失败关闭。
    """
    if type(identity) is not TaskExecutionIdentity:
        raise _changed()
    if identity.grant_identity is None:
        return None
    try:
        task_input = identity.input
        owners: set[int] = set()
        for key in ("_project_model", "_shared_embedding"):
            if key not in task_input:
                continue
            binding = task_input[key]
            owner = binding.get("project_owner_id") if isinstance(binding, dict) else None
            if type(owner) is not int or owner <= 0:
                raise ValueError
            owners.add(owner)
    except (TypeError, ValueError, AttributeError, RecursionError):
        raise _changed() from None
    if len(owners) != 1:
        raise _changed()
    owner = owners.pop()
    if owner == identity.actor_id or identity.project_id is None:
        raise _changed()
    return owner


async def require_live_worker(db: AsyncSession, worker_key: str | None, *, read_only: bool = False) -> None:
    """调用方先锁定原 Task 并比对其 worker_key，再在同一事务内持有有效登记。"""
    if worker_key is not None:
        from app.services.task_engine.workers import lock_live_worker

        if not await lock_live_worker(db, worker_key, read_only=read_only):
            raise _changed()


async def require_write_execution(
    db: AsyncSession, identity: TaskExecutionIdentity, *, project_owner_id: int | None = None,
) -> None:
    """同事务认证、可见性门禁、作品和 Task 行锁后核对原领取身份。

    ``project_owner_id``（仅协作执行传入，E4i-b）：在取发送写门禁之前，与 actor 一起按 ID 升序锁定作者 User 行，
    与任务引擎领取一致（全局门 → User 锁 → 发送写门禁 → 作品父行 → Task）。否则后续同事务再锁作者 User 时，
    会与作者发送 lease（先 FOR SHARE 作者 User 再等共享门禁）在 PostgreSQL 上形成死锁。锁后核对作品作者一致。
    作者执行不传，行为不变。

    E4i-d：协作执行未显式传入时，按领取快照冻结的作者（`frozen_project_owner_id`）同样预锁，使 handler 内
    各处构建 / 事件 / 收尾屏障（随后可能经 `guard.require_task_write` 等再锁作者 User）统一为升序；快照缺绑定
    或破损时不猜测作者、退回原锁集合（正确性仍由锁后核对与 handler 守卫失败关闭）。
    """
    from app.services import authorization_service as authz
    from app.services import project_lifecycle
    from app.services.ai_dispatch import lock_visibility_write
    from app.services.collaboration_task_access import (
        owner_ai_allowed,
        task_requires_owner_ai,
        task_visible_to_actor,
    )

    try:
        if type(identity) is not TaskExecutionIdentity or identity.actor_token_version is None:
            raise _changed()
        if project_owner_id is not None and (
            type(project_owner_id) is not int or project_owner_id <= 0 or identity.project_id is None
            or identity.grant_identity is None or project_owner_id == identity.actor_id
        ):
            raise _changed()
        if project_owner_id is None and identity.grant_identity is not None:
            try:
                project_owner_id = frozen_project_owner_id(identity)
            except ConflictError:
                project_owner_id = None
        actor = authz.AuthenticatedActor(identity.actor_id, identity.actor_token_version)
        targets = (project_owner_id,) if project_owner_id is not None else ()
        await authz.require_authenticated(db, actor, write=True, targets=targets)
        if identity.project_id is not None:
            await lock_visibility_write(db, identity.project_id)
            project = await project_lifecycle.lock_active(db, identity.project_id)
            if project_owner_id is not None and project.user_id != project_owner_id:
                raise _changed()
        with db.no_autoflush:
            task = await db.scalar(select(Task).where(
                Task.id == identity.task_id, Task.statistics_key == identity.task_key,
                task_visible_to_actor(db, identity.actor_id, active_grant_required=True,
                                      token_version=identity.actor_token_version),
            ).with_for_update().execution_options(populate_existing=True))
        if not matches_running(identity, task):
            raise _changed()
        await require_live_worker(db, identity.worker_key)
        # 不只依赖“任何授权变更都递增 revision”的约定：作者模型来源须直接核对当前授权开关。
        if (identity.grant_identity is not None and identity.project_id is not None
                and task_requires_owner_ai(task.input)
                and not await owner_ai_allowed(db, identity.project_id, identity.actor_id)):
            raise _changed()
    except BaseException:
        await db.rollback()
        raise
