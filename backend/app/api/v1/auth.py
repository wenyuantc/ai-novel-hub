from fastapi import APIRouter, Response, status

from app.core.config import Settings
from app.core.deps import CurrentUser, DbDep, SettingsDep
from app.core.security import ACCESS_TOKEN_COOKIE, create_access_token
from app.models import User
from app.schemas.auth import AuthConfigOut, ChangePasswordRequest, Credentials, UserOut
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_auth_cookie(response: Response, user: User, settings: Settings) -> None:
    token = create_access_token(user.id, user.token_version, settings.jwt_secret, settings.access_token_expire_minutes)
    response.set_cookie(
        key=ACCESS_TOKEN_COOKIE,
        value=token,
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


@router.get("/config", response_model=AuthConfigOut)
async def auth_config(settings: SettingsDep) -> AuthConfigOut:
    return AuthConfigOut(registration_enabled=settings.registration_enabled, mcp_enabled=settings.mcp_enabled)


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: Credentials, response: Response, db: DbDep, settings: SettingsDep) -> User:
    user = await auth_service.register(db, body, registration_enabled=settings.registration_enabled)
    _set_auth_cookie(response, user, settings)
    return user


@router.post("/login", response_model=UserOut)
async def login(body: Credentials, response: Response, db: DbDep, settings: SettingsDep) -> User:
    user = await auth_service.authenticate(db, body)
    _set_auth_cookie(response, user, settings)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(settings: SettingsDep) -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(ACCESS_TOKEN_COOKIE, path="/", httponly=True, secure=settings.cookie_secure, samesite="lax")
    return response


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return user


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(body: ChangePasswordRequest, user: CurrentUser, db: DbDep, settings: SettingsDep) -> Response:
    user = await auth_service.change_password(db, user, body)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    # 其他会话因 token_version 递增而失效；为当前会话签发新令牌以保持登录
    _set_auth_cookie(response, user, settings)
    return response
