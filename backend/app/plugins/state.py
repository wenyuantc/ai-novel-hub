from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models import FeaturePluginState, Task
from app.models.entities import OPEN_TASK_STATUSES
from app.plugins.manifest import PluginManifest
from app.plugins.registry import current
from app.schemas.feature_plugins import PluginStateOut, PluginUpdate
from app.services import authorization_service as authz
from app.services.audit_writer import AuditOperation

if TYPE_CHECKING:
    from app.services.task_engine.engine import TaskEngine


class PluginDisabled(ForbiddenError):
    def __init__(self, plugin_id: str):
        super().__init__("功能插件未启用", extra={"code": "plugin_disabled", "plugin": plugin_id})


async def is_enabled(db: AsyncSession, plugin_id: str) -> bool:
    return bool(await db.scalar(select(FeaturePluginState.enabled).where(FeaturePluginState.plugin_id == plugin_id)))


async def require_enabled(db: AsyncSession, plugin_id: str) -> None:
    if not await is_enabled(db, plugin_id):
        raise PluginDisabled(plugin_id)


async def require_kind_enabled(db: AsyncSession, kind: str) -> None:
    if owner := current.get().plugin_of_kind(kind):
        await require_enabled(db, owner)


def _project(manifest: PluginManifest, row: FeaturePluginState | None) -> PluginStateOut:
    stamp = row.updated_at if row else None
    return PluginStateOut(
        id=manifest.id,
        name=manifest.name,
        description=manifest.description,
        version=manifest.version,
        enabled=row.enabled if row else False,
        revision=row.revision if row else 0,
        updated_at=stamp.replace(tzinfo=UTC) if stamp and stamp.tzinfo is None else stamp,
    )


async def list_states(db: AsyncSession) -> list[PluginStateOut]:
    registered = current.get().all()
    if not registered:
        return []
    rows = {row.plugin_id: row for row in await db.scalars(select(FeaturePluginState))}
    return [_project(item, rows.get(item.id)) for item in registered]


async def enabled_ids(db: AsyncSession) -> list[str]:
    registered = current.get().all()
    if not registered:
        return []
    enabled = set(await db.scalars(select(FeaturePluginState.plugin_id).where(FeaturePluginState.enabled.is_(True))))
    return [item.id for item in registered if item.id in enabled]


def mark_for_cancellation(task: Task, now: datetime) -> str:
    if task.admin_cancel_operation_id is None:
        task.admin_cancel_operation_id = str(uuid4())
        task.admin_cancel_requested_at = now
    return task.admin_cancel_operation_id


async def set_enabled(
    db: AsyncSession, engine: "TaskEngine", actor: authz.AuthenticatedActor, plugin_id: str, patch: PluginUpdate,
) -> PluginStateOut:
    # 所有实例的启停、受理与领取共用授权写门；首写和无变化的请求也须在锁内比对版本。
    await authz.require(db, actor, "plugins.update", write=True)
    item = current.get().get(plugin_id)
    if item is None:
        raise NotFoundError("功能插件不存在")
    row = await db.get(FeaturePluginState, plugin_id, with_for_update=True, populate_existing=True)
    if patch.revision != (row.revision if row else 0):
        raise ConflictError("状态已被其他管理员修改", extra={"code": "stale_revision"})
    if patch.enabled == bool(row and row.enabled):
        return _project(item, row)
    action = "feature_plugin.enable" if patch.enabled else "feature_plugin.disable"
    pending = []
    async with AuditOperation(db, actor.id, action, "feature_plugin") as audit:
        now = datetime.now(UTC)
        if row is None:
            row = FeaturePluginState(plugin_id=plugin_id)
            db.add(row)
        row.enabled = patch.enabled
        row.revision = patch.revision + 1
        row.updated_at = now
        row.last_operation_id = audit.operation_id
        audit.values["target_label"] = item.name
        if not patch.enabled and item.task_handlers:
            kinds = [kind for handler in item.task_handlers for kind in handler.kinds]
            tasks = await db.scalars(
                select(Task)
                .where(Task.kind.in_(kinds), Task.status.in_(OPEN_TASK_STATUSES))
                .order_by(Task.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            for task in tasks:
                pending.append((task.statistics_key, mark_for_cancellation(task, now), task.worker_key))
            audit.summary.affected_count = len(pending)
        result = _project(item, row)
        await audit.commit()
    for key, operation, worker in pending:
        engine.request_admin_cancel(key, operation)
        engine.peer_wakes.schedule(worker)
    return result
