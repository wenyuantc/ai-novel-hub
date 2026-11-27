from fastapi import APIRouter, HTTPException, Request, Response, status

from app.core.config import Settings
from app.core.deps import CipherDep, CurrentUser, DbDep, SettingsDep
from app.core.security import ACCESS_TOKEN_COOKIE, create_access_token, require_recent_login
from app.models import User
from app.plugins.state import enabled_ids
from app.schemas.auth import AuthConfigOut, ChangePasswordRequest, Credentials, FactorCode, TwoStepRequired, UserOut
from app.services import auth_service, authorization_service, two_step
from app.services.system_settings_snapshot import SettingsUnavailable, effective_settings

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
async def auth_config(db: DbDep, response: Response) -> AuthConfigOut:
    response.headers["Cache-Control"] = "no-store"
    try:
        settings = await effective_settings(db)
    except SettingsUnavailable as exc:
        raise HTTPException(exc.status_code, exc.detail, headers={"Cache-Control": "no-store"}) from None
    return AuthConfigOut(registration_enabled=settings.registration_enabled, mcp_enabled=settings.mcp_enabled,
                         enabled_plugins=await enabled_ids(db))


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(body: Credentials, response: Response, db: DbDep, settings: SettingsDep) -> User:
    user = await auth_service.register(db, body)
    output = await _user_out(db, user)
    _set_auth_cookie(response, user, settings)
    return output


async def complete_first_factor(
    db: DbDep,
    user: User,
    response: Response,
    settings: Settings,
) -> UserOut | TwoStepRequired:
    ticket = await two_step.first_factor(db, user)
    response.headers["Cache-Control"] = "no-store"
    if ticket is not None:
        response.delete_cookie(ACCESS_TOKEN_COOKIE, path="/")
        response.set_cookie(
            two_step.CHALLENGE_COOKIE,
            ticket,
            max_age=two_step.CHALLENGE_TTL,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/api/v1/auth",
        )
        return TwoStepRequired()
    user = await auth_service._record_successful_login(db, user)
    output = await _user_out(db, user)
    _set_auth_cookie(response, user, settings)
    response.delete_cookie(two_step.CHALLENGE_COOKIE, path="/api/v1/auth")
    return output


@router.post("/login", response_model=UserOut | TwoStepRequired)
async def login(body: Credentials, response: Response, db: DbDep, settings: SettingsDep) -> UserOut | TwoStepRequired:
    user = await auth_service.verify_credentials(db, body)
    return await complete_first_factor(db, user, response, settings)


@router.get("/two-step/status")
async def factor_status(user: CurrentUser, db: DbDep, response: Response) -> dict[str, bool]:
    response.headers["Cache-Control"] = "no-store"
    return {"enabled": await two_step.enabled(db, user.id)}


@router.post("/two-step/setup")
async def setup_factor(
    user: CurrentUser,
    db: DbDep,
    request: Request,
    response: Response,
    cipher: CipherDep,
    settings: SettingsDep,
) -> dict[str, str]:
    require_recent_login(request, settings, user)
    response.headers["Cache-Control"] = "no-store"
    return await two_step.setup(db, user, cipher)


@router.post("/two-step/enable")
async def enable_factor(
    request: Request,
    body: FactorCode,
    user: CurrentUser,
    db: DbDep,
    response: Response,
    cipher: CipherDep,
    settings: SettingsDep,
) -> dict[str, list[str]]:
    require_recent_login(request, settings, user)
    codes = await two_step.enable(db, user, body.code, cipher)
    response.headers["Cache-Control"] = "no-store"
    _set_auth_cookie(response, user, settings)
    return {"recovery_codes": codes}


@router.post("/two-step/disable", status_code=204)
async def disable_factor(
    request: Request,
    body: FactorCode,
    user: CurrentUser,
    db: DbDep,
    cipher: CipherDep,
    settings: SettingsDep,
) -> Response:
    require_recent_login(request, settings, user)
    await two_step.disable(db, user, body.code, cipher)
    response = Response(status_code=204)
    _set_auth_cookie(response, user, settings)
    return response


@router.post("/two-step/verify", response_model=UserOut)
async def verify_factor(
    body: FactorCode,
    request: Request,
    response: Response,
    db: DbDep,
    cipher: CipherDep,
    settings: SettingsDep,
) -> UserOut:
    user = await two_step.verify(db, request.cookies.get(two_step.CHALLENGE_COOKIE, ""), body.code, cipher)
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(two_step.CHALLENGE_COOKIE, path="/api/v1/auth")
    _set_auth_cookie(response, user, settings)
    return await _user_out(db, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(settings: SettingsDep) -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(two_step.CHALLENGE_COOKIE, path="/api/v1/auth")
    response.delete_cookie(ACCESS_TOKEN_COOKIE, path="/", httponly=True, secure=settings.cookie_secure, samesite="lax")
    return response


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser, db: DbDep, response: Response) -> UserOut:
    response.headers["Cache-Control"] = "no-store"
    return await _user_out(db, user)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(body: ChangePasswordRequest, user: CurrentUser, db: DbDep, settings: SettingsDep) -> Response:
    user = await auth_service.change_password(db, user, body)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    # 其他会话因 token_version 递增而失效；为当前会话签发新令牌以保持登录
    _set_auth_cookie(response, user, settings)
    return response


async def _user_out(db, user: User) -> UserOut:
    access = await authorization_service.snapshot(db, user.id)
    return UserOut(
        id=access.id,
        username=access.username,
        is_admin=access.is_super_admin,
        has_local_password=user.has_local_password,
        created_at=access.created_at,
        **access.public(),
    )
