from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.core import security
from app.core.deps import CurrentUser, DbDep, SettingsDep, get_current_user
from app.core.errors import AppError, BadRequestError, UnauthorizedError
from app.models import User
from app.services import authorization_service as authz
from app.services import oauth_service as oauth

router = APIRouter(prefix="/auth/oauth", tags=["auth"])
_PATH = "/api/v1/auth/oauth"
_PRIVATE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


@router.get("/providers")
async def providers(settings: SettingsDep, response: Response):
    response.headers.update(_PRIVATE)
    return {"providers": oauth.configured(settings)}


@router.get("/identities")
async def identities(user: CurrentUser, db: DbDep, response: Response):
    response.headers.update(_PRIVATE)
    return {"providers": await oauth.bound_providers(db, user.id), "has_local_password": user.has_local_password}


@router.post("/{provider}/start")
async def start(provider: oauth.Provider, request: Request, response: Response, settings: SettingsDep,
                db: DbDep, intent: oauth.Intent = "login"):
    user = None
    if intent == "bind":
        user = await get_current_user(request, db, settings)
        security.require_recent_login(request, settings, user)
    url, browser = await oauth.start(db, settings, provider, intent, user)
    response.set_cookie(f"oauth_{provider}_flow", browser, path=_PATH, max_age=oauth.TTL_SECONDS,
                        secure=settings.cookie_secure, httponly=True, samesite="lax")
    response.headers.update(_PRIVATE)
    return {"url": url}


@router.get("/{provider}/callback")
async def callback(provider: oauth.Provider, request: Request, db: DbDep, settings: SettingsDep,
                   state: Annotated[str, Query(max_length=128)] = "",
                   code: Annotated[str, Query(max_length=2048)] = "") -> RedirectResponse:
    from app.api.v1.auth import complete_first_factor
    from app.schemas.auth import UserOut

    cookie = f"oauth_{provider}_flow"
    response = RedirectResponse("/login?oauth=success", status_code=303, headers=_PRIVATE)
    try:
        if provider not in oauth.configured(settings):
            raise BadRequestError("第三方登录方式未启用")
        intent, user_id, version = await oauth.consume(db, provider, state, request.cookies.get(cookie, ""))
        # 删除状态与核对发起人处于同一事务；外部网络不占用连接和锁。
        target_created = None
        if intent == "bind":
            target_created = await db.scalar(select(User.created_at).where(User.id == user_id))
        await db.commit()
        response.delete_cookie(cookie, path=_PATH, secure=settings.cookie_secure, httponly=True, samesite="lax")
        if not code:
            raise BadRequestError("授权已取消，请重新选择登录方式")
        subject = await oauth.fetch_subject(settings, provider, code, state)
        if intent == "bind":
            await authz.gate(db)
            user = await get_current_user(request, db, settings)
            if (user.id, user.token_version, user.created_at) != (user_id, version, target_created):
                raise UnauthorizedError("当前账号已变化，请重新绑定")
            await oauth.bind(db, user, provider, subject)
            response.headers["Location"] = "/login?oauth=bound"
        else:
            user = await oauth.resolve_login(db, provider, subject)
            result = await complete_first_factor(db, user, response, settings)
            if not isinstance(result, UserOut):
                response.headers["Location"] = "/login?oauth=challenge"
    except AppError as exc:
        await db.rollback()
        response.headers["Location"] = "/login?" + urlencode({"oauth": "error", "reason": exc.detail})
    return response


@router.delete("/{provider}/binding", status_code=204)
async def unbind(provider: oauth.Provider, user: CurrentUser, db: DbDep, settings: SettingsDep,
                 request: Request, response: Response):
    from app.api.v1.auth import _set_auth_cookie

    security.require_recent_login(request, settings, user)
    if await oauth.unbind(db, user, provider, settings):
        _set_auth_cookie(response, user, settings)
    response.headers.update(_PRIVATE)
