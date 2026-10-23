from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, UnauthorizedError
from app.core.security import hash_password, verify_password
from app.models import User
from app.schemas.auth import Credentials


async def register(db: AsyncSession, creds: Credentials, *, registration_enabled: bool) -> User:
    if not registration_enabled:
        raise ForbiddenError("注册已关闭，请联系管理员")
    exists = await db.scalar(select(User.id).where(User.username == creds.username))
    if exists is not None:
        raise ConflictError("用户名已存在")
    password_hash = await to_thread.run_sync(hash_password, creds.password)
    user = User(username=creds.username, password_hash=password_hash)
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        # 并发注册同名用户时由唯一约束兜底
        await db.rollback()
        raise ConflictError("用户名已存在") from exc
    await db.refresh(user)
    return user


async def authenticate(db: AsyncSession, creds: Credentials) -> User:
    user = await db.scalar(select(User).where(User.username == creds.username))
    if user is None or not await to_thread.run_sync(verify_password, creds.password, user.password_hash):
        raise UnauthorizedError("用户名或密码错误")
    return user


async def get_user(db: AsyncSession, user_id: int) -> User | None:
    return await db.get(User, user_id)
