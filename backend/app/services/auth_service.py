import logging

from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, UnauthorizedError
from app.core.security import hash_password, verify_password
from app.models import User
from app.schemas.auth import ChangePasswordRequest, Credentials

logger = logging.getLogger(__name__)


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
    return await create_user(db, creds)


async def authenticate(db: AsyncSession, creds: Credentials) -> User:
    user = await db.scalar(select(User).where(User.username == creds.username))
    if user is None or not await to_thread.run_sync(verify_password, creds.password, user.password_hash):
        raise UnauthorizedError("用户名或密码错误")
    # 密码正确后才提示禁用，避免向不知道密码的人暴露账号状态
    if not user.is_active:
        raise ForbiddenError("账号已被禁用，请联系管理员")
    return user


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
    """按 ADMIN_USERNAME / ADMIN_PASSWORD 引导管理员。

    账号不存在则创建为管理员；已存在则确保为启用中的管理员，**不覆盖密码**。
    不受 REGISTRATION_ENABLED 影响。日志只记录用户名。
    """
    creds = settings.admin_credentials
    if creds is None:
        return
    async with sessionmaker() as db:
        user = await db.scalar(select(User).where(User.username == creds.username))
        if user is None:
            try:
                await create_user(db, creds, is_admin=True)
                logger.info("已创建引导管理员账号：%s", creds.username)
                return
            except ConflictError:
                # 多实例同时启动时可能被其他进程抢先创建，转为提升已有账号
                user = await db.scalar(select(User).where(User.username == creds.username))
                if user is None:
                    raise
        if user.is_admin and user.is_active:
            logger.info("引导管理员账号已存在：%s（不修改密码）", creds.username)
            return
        user.is_admin = True
        user.is_active = True
        await db.commit()
        logger.info("已将账号 %s 设为启用中的管理员（不修改密码）", creds.username)
