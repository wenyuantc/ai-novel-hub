from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.providers import ProviderFactory
from app.core.config import Settings
from app.core.errors import UnauthorizedError
from app.core.security import ACCESS_TOKEN_COOKIE, SecretCipher, decode_access_token
from app.models import User
from app.services import auth_service


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


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
CipherDep = Annotated[SecretCipher, Depends(get_cipher)]
FactoryDep = Annotated[ProviderFactory, Depends(get_provider_factory)]
SessionmakerDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]


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
    user_id = decode_access_token(token, settings.jwt_secret) if token else None
    user = await auth_service.get_user(db, user_id) if user_id is not None else None
    if user is None:
        raise UnauthorizedError("未登录或登录已过期")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
