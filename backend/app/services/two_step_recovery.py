"""两步验证的人工恢复。业务写入和审计均由同一个事务提交。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ForbiddenError, NotFoundError
from app.models import User
from app.services import authorization_service as authz
from app.services import two_step
from app.services.audit_writer import AuditOperation


async def reset_for_user(db: AsyncSession, actor: User, user_id: int, *, identity_verified: bool) -> None:
    await authz.require(db, actor, write=True, targets=(user_id,))
    async with AuditOperation(db, actor, "user.two_step.reset", "user", user_id) as audit:
        target = await db.get(User, user_id, populate_existing=True)
        if target is None:
            raise NotFoundError("用户不存在")
        audit.target(target)
        if (await authz.snapshot(db, target.id)).is_management_account:
            raise ForbiddenError("此操作仅用于普通用户")
        if not identity_verified:
            raise BadRequestError("请先核实用户身份")
        await two_step.reset_uncommitted(db, target)
        await audit.commit()


async def recover_on_host(db: AsyncSession, user_id: int) -> None:
    """本机 CLI 专用。授权门保护唯一启用超管的判断直到重置提交。"""
    await authz.gate(db)
    await authz.lock_users(db, user_id, missing_ok=True)
    async with AuditOperation(db, None, "user.two_step.local_recovery", "user", user_id, local_recovery=True) as audit:
        target = await db.get(User, user_id, populate_existing=True)
        if target is None:
            raise NotFoundError("用户不存在")
        audit.target(target)
        enabled_admins = select(User.id).where(User.is_admin.is_(True), User.is_active.is_(True))
        other_id = await db.scalar(enabled_admins.where(User.id != user_id).limit(1))
        if not target.is_admin or not target.is_active or other_id is not None:
            raise ForbiddenError("本机恢复仅用于唯一启用的超级管理员")
        await two_step.reset_uncommitted(db, target)
        await audit.commit()
