import logging
from datetime import UTC, datetime
from typing import NoReturn

from anyio import to_thread
from sqlalchemy import case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import AppError, BadRequestError, ConflictError, ForbiddenError, UnauthorizedError
from app.core.security import hash_password, verify_password
from app.models import User
from app.schemas.auth import ChangePasswordRequest, Credentials

logger = logging.getLogger(__name__)


class AuthenticationPersistenceError(AppError):
    """认证写入故障不能把包含密码哈希的 SQL 参数送进全局异常日志。"""

    status_code = 500


def _now_utc() -> datetime:
    return datetime.now(UTC)


async def create_user_uncommitted(db: AsyncSession, creds: Credentials, *, is_admin: bool = False) -> User:
    """共用的不提交原语：重名检查、bcrypt、flush 唯一约束兜底。"""
    exists = await db.scalar(select(User.id).where(User.username == creds.username))
    if exists is not None:
        raise ConflictError("用户名已存在")
    password_hash = await to_thread.run_sync(hash_password, creds.password)
    user = User(username=creds.username, password_hash=password_hash, is_admin=is_admin, is_active=True)
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as exc:
        # 并发创建同名用户时由唯一约束兜底
        await db.rollback()
        raise ConflictError("用户名已存在") from exc
    return user


async def create_user(db: AsyncSession, creds: Credentials, *, is_admin: bool = False) -> User:
    user = await create_user_uncommitted(db, creds, is_admin=is_admin)
    await db.commit()
    await db.refresh(user)
    return user


async def register(db: AsyncSession, creds: Credentials, *, registration_enabled: bool) -> User:
    if not registration_enabled:
        raise ForbiddenError("注册已关闭，请联系管理员")
    try:
        user = await create_user_uncommitted(db, creds)
        return await _record_successful_login(db, user)
    except AppError:
        raise
    except Exception as exc:
        await _authentication_storage_failure(db, exc)


async def authenticate(db: AsyncSession, creds: Credentials) -> User:
    user = await db.scalar(select(User).where(User.username == creds.username))
    if user is None or not await to_thread.run_sync(verify_password, creds.password, user.password_hash):
        raise UnauthorizedError("用户名或密码错误")
    # 密码正确后才提示禁用，避免向不知道密码的人暴露账号状态
    if not user.is_active:
        raise ForbiddenError("账号已被禁用，请联系管理员")
    return await _record_successful_login(db, user)


async def _record_successful_login(db: AsyncSession, user: User) -> User:
    """写前复核已验凭据，提交后只返回同一认证快照，不升级为并发重置后的版本。"""
    user_id, password_hash, token_version = user.id, user.password_hash, user.token_version
    now = _now_utc()
    try:
        result = await db.execute(
            update(User)
            .where(
                User.id == user_id, User.is_active.is_(True),
                User.password_hash == password_hash, User.token_version == token_version,
            )
            .values(
                last_login_at=case(
                    (or_(User.last_login_at.is_(None), User.last_login_at < now), now),
                    else_=User.last_login_at,
                ),
                updated_at=User.updated_at,
            )
            .returning(User)
            .execution_options(populate_existing=True, synchronize_session=False)
        )
        authenticated = result.scalar_one_or_none()
        if authenticated is None:
            await db.rollback()
            current = (await db.execute(
                select(User.password_hash, User.is_active).where(User.id == user_id)
            )).one_or_none()
            if current is not None and current.password_hash == password_hash and not current.is_active:
                raise ForbiddenError("账号已被禁用，请联系管理员")
            raise UnauthorizedError("用户名或密码错误")
        await db.commit()
        # 不 refresh：之后的密码重置/禁用只能让这份旧版本失效，不能被签成新版本。
        return authenticated
    except AppError:
        raise
    except Exception as exc:
        await _authentication_storage_failure(db, exc)


async def _authentication_storage_failure(db: AsyncSession, exc: Exception) -> NoReturn:
    try:
        await db.rollback()
    except Exception as rollback_exc:
        logger.error("认证记录回滚失败（%s）", type(rollback_exc).__name__)
    logger.error("认证记录保存失败（%s）", type(exc).__name__)
    raise AuthenticationPersistenceError("登录信息保存失败，请稍后重试") from None


async def get_user(db: AsyncSession, user_id: int) -> User | None:
    return await db.get(User, user_id)


async def change_password(db: AsyncSession, user: User, data: ChangePasswordRequest) -> User:
    """自助改密：校验原密码后更新，并递增 token_version 使其他会话失效（调用方为当前会话重新签发 Cookie）。"""
    if not await to_thread.run_sync(verify_password, data.old_password, user.password_hash):
        raise BadRequestError("原密码不正确")
    user.password_hash = await to_thread.run_sync(hash_password, data.new_password)
    # SQL 端自增，避免与并发的管理员重置 / 禁用基于旧值写回同一版本号
    user.token_version = User.token_version + 1
    await db.commit()
    await db.refresh(user)
    return user


async def bootstrap_admin(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    """显式部署引导：启用超管而不覆盖密码，真实变更写窄内部系统审计。"""
    from app.schemas.audit import RbacAuditChange
    from app.services import authorization_service as authz
    from app.services.audit_writer import AuditOperation

    creds = settings.admin_credentials
    if creds is None:
        return
    await authz.initialize(sessionmaker)
    # 与自主注册的同名竞争只重读已确认的唯一键结果，不重放成功业务。
    for attempt in range(2):
        async with sessionmaker() as db:
            await authz.gate(db)
            user = await db.scalar(select(User).where(User.username == creds.username))
            if user is not None and user.is_admin and user.is_active:
                logger.info("引导管理员账号已存在：%s（不修改密码）", creds.username)
                return
            try:
                async with AuditOperation(db, None, "user.roles.bootstrap", "user", bootstrap=True) as audit:
                    before = []
                    if user is None:
                        user = await create_user_uncommitted(db, creds, is_admin=True)
                    else:
                        await authz.lock_users(db, user.id)
                        before = [role.id for role in (await authz.snapshot(db, user.id)).roles]
                        user.is_admin = True
                        user.is_active = True
                    await db.flush()
                    after = [role.id for role in (await authz.snapshot(db, user.id)).roles]
                    audit.target(user)
                    audit.summary.rbac_change = RbacAuditChange(
                        role_ids_before=before[:100], role_ids_after=after[:100],
                        ids_truncated=len(before) > 100 or len(after) > 100,
                    )
                    await authz.advance(db)
                    await audit.commit()
                    logger.info("已将账号 %s 设为启用中的超级管理员（不修改已有密码）", creds.username)
                    return
            except ConflictError:
                if attempt:
                    raise
