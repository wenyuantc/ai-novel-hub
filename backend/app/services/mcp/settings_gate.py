"""MCP 系统开关的跨实例物理门禁；调用者负责在响应头后结束事务。"""

from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SystemSettings
from app.schemas.system_settings import SystemSettingsChanges
from app.services import authorization_service as authz
from app.services.ai_dispatch import VisibilityPolicyError

# 与作品可见性、用户模型选择使用不同的双整数 advisory namespace。
_MCP_SETTINGS_NAMESPACE = 0x41494D43
_MCP_SETTINGS_ID = 1
_DEPLOYMENT_KEY = "mcp_settings_deployment_default"


@dataclass(frozen=True, slots=True)
class _DeploymentDefault:
    enabled: bool


def configure_deployment_default(maker: async_sessionmaker[AsyncSession], enabled: bool) -> None:
    """捕获本进程启动时的部署默认值；运行期可变 Settings 不再作为事实来源。"""
    if type(enabled) is not bool:
        raise ValueError("MCP 部署设置无效")
    info = maker.kw.setdefault("info", {})
    previous = info.get(_DEPLOYMENT_KEY)
    value = _DeploymentDefault(enabled)
    if previous is not None and previous != value:
        raise ValueError("MCP 部署设置已固定")
    info[_DEPLOYMENT_KEY] = value


def _unavailable() -> VisibilityPolicyError:
    return VisibilityPolicyError("MCP 系统设置不可用，已停止发送", extra={"code": "mcp_settings_unavailable"})


def _disabled() -> VisibilityPolicyError:
    return VisibilityPolicyError("MCP 功能已关闭", extra={"code": "mcp_disabled"})


async def _lock(db: AsyncSession, *, exclusive: bool) -> None:
    """PG 锁覆盖无行和首次插入；SQLite 使用调用方已取得的物理写锁。"""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        # REPEATABLE READ 的快照可能在等待 advisory 锁之前已建立，不能用旧快照判断开关。
        isolation = await db.scalar(text("SHOW transaction_isolation"))
        if isolation != "read committed":
            raise _unavailable()
        function = func.pg_advisory_xact_lock if exclusive else func.pg_advisory_xact_lock_shared
        await db.execute(select(function(_MCP_SETTINGS_NAMESPACE, _MCP_SETTINGS_ID)))
    elif dialect != "sqlite":
        raise _unavailable()


async def lock_for_update(db: AsyncSession) -> None:
    """管理员已持授权/User 写屏障；调用方持锁至设置和审计同事务提交。"""
    with db.no_autoflush:
        await _lock(db, exclusive=True)


async def lock_for_reload(db: AsyncSession) -> None:
    """读取并发布进程快照时持共享门禁；不得用于证明真实 HTTP 发送。"""
    with db.no_autoflush:
        await _lock(db, exclusive=False)


async def acquire_send(db: AsyncSession, actor: authz.AuthenticatedActor) -> None:
    """原 actor → MCP 设置共享门禁；仅在真实发送租约中使用，事务不在此处关闭。"""
    if (db.new or db.dirty or db.deleted or type(actor) is not authz.AuthenticatedActor
            or type(actor.id) is not int or actor.id <= 0
            or type(actor.token_version) is not int or actor.token_version < 0):
        raise _unavailable()
    bound = db.info.get("authenticated_actor")
    if bound is not None and bound != actor:
        raise _unavailable()
    default = db.info.get(_DEPLOYMENT_KEY)
    if type(default) is not _DeploymentDefault or type(default.enabled) is not bool:
        raise _unavailable()
    try:
        with db.no_autoflush:
            await authz.require_authenticated_for_send(db, actor)
            runtime = authz.runtime_for(db)
            if runtime.settings_confirmed is not True:
                raise _unavailable()
            await _lock(db, exclusive=False)
            row = (await db.execute(select(SystemSettings.revision, SystemSettings.overrides)
                                    .where(SystemSettings.id == 1))).one_or_none()
            if row is None:
                enabled = default.enabled
            else:
                if type(row.revision) is not int or row.revision < 1 or type(row.overrides) is not dict:
                    raise _unavailable()
                changes = SystemSettingsChanges.model_validate(row.overrides).model_dump(exclude_unset=True)
                if any(value is None for value in changes.values()):
                    raise _unavailable()
                enabled = changes.get("mcp_enabled", default.enabled)
            if runtime.settings_confirmed is not True or type(enabled) is not bool:
                raise _unavailable()
            if not enabled:
                raise _disabled()
    except VisibilityPolicyError:
        raise
    except Exception:
        # 不泄露 SQL、持久值或外部端点详情；会话由真实发送租约关闭。
        raise _unavailable() from None
