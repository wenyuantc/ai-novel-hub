from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.providers import ProviderFactory
from app.core.config import Settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.security import ACCESS_TOKEN_COOKIE, SecretCipher, decode_access_token
from app.models import User
from app.services import auth_service
from app.services.task_engine.engine import TaskEngine


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.sessionmaker


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_cipher(request: Request) -> SecretCipher:
    return request.app.state.cipher


def get_provider_factory(request: Request) -> ProviderFactory:
    return request.app.state.provider_factory


def get_task_engine(request: Request) -> TaskEngine:
    return request.app.state.task_engine


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
CipherDep = Annotated[SecretCipher, Depends(get_cipher)]
FactoryDep = Annotated[ProviderFactory, Depends(get_provider_factory)]
SessionmakerDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
TaskEngineDep = Annotated[TaskEngine, Depends(get_task_engine)]


def _extract_token(request: Request) -> str | None:
    token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


async def get_current_user(request: Request, db: DbDep, settings: SettingsDep) -> User:
    token = _extract_token(request)
    decoded = decode_access_token(token, settings.jwt_secret) if token else None
    user = await auth_service.get_user(db, decoded[0]) if decoded is not None else None
    # 令牌版本不一致（已重置密码 / 已禁用 / 已改密）或账号被禁用：一律按未登录处理，前端统一跳转登录页
    if user is None or decoded is None or decoded[1] != user.token_version or not user.is_active:
        raise UnauthorizedError("未登录或登录已过期")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_admin_user(user: CurrentUser) -> User:
    if not user.is_admin:
        raise ForbiddenError("需要管理员权限")
    return user


AdminUser = Annotated[User, Depends(get_admin_user)]
