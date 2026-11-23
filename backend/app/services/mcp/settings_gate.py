"""MCP 系统开关的跨实例物理门禁；调用者负责在响应头后结束事务。"""

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import authorization_service as authz
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.system_settings_snapshot import effective_settings

# 与作品可见性、用户模型选择使用不同的双整数 advisory namespace。
_MCP_SETTINGS_NAMESPACE = 0x41494D43
_MCP_SETTINGS_ID = 1


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


async def require_agent_enabled(db: AsyncSession) -> None:
    try:
        enabled = (await effective_settings(db)).mcp_enabled
    except Exception:
        raise _unavailable() from None
    if not enabled:
        raise _disabled()


async def acquire_send(db: AsyncSession, actor: authz.AuthenticatedActor) -> None:
    """原 actor → MCP 设置共享门禁；仅在真实发送租约中使用，事务不在此处关闭。"""
    if (db.new or db.dirty or db.deleted or type(actor) is not authz.AuthenticatedActor
            or type(actor.id) is not int or actor.id <= 0
            or type(actor.token_version) is not int or actor.token_version < 0):
        raise _unavailable()
    bound = db.info.get("authenticated_actor")
    if bound is not None and bound != actor:
        raise _unavailable()
    try:
        with db.no_autoflush:
            await authz.require_authenticated_for_send(db, actor)
            await _lock(db, exclusive=False)
            await require_agent_enabled(db)
    except VisibilityPolicyError:
        raise
    except Exception:
        # 不泄露 SQL、持久值或外部端点详情；会话由真实发送租约关闭。
        raise _unavailable() from None
