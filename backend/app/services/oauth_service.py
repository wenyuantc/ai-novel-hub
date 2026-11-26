"""第三方身份只由平台和不可变 ID 决定。授权码和访问令牌仅在当前请求中使用。"""
import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import BadRequestError, ConflictError, ForbiddenError
from app.models import OAuthIdentity, OAuthState, User
from app.services import authorization_service as authz
from app.services.system_settings_snapshot import effective_settings

Provider = Literal["github", "linuxdo"]
Intent = Literal["login", "bind"]
TTL_SECONDS = 600
ENDPOINTS = {
    "github": ("https://github.com/login/oauth/authorize", "https://github.com/login/oauth/access_token",
               "https://api.github.com/user"),
    "linuxdo": ("https://connect.linux.do/oauth2/authorize", "https://connect.linux.do/oauth2/token",
                "https://connect.linux.do/api/user"),
}


def configured(settings: Settings) -> list[str]:
    return [name for name in ENDPOINTS if getattr(settings, f"oauth_{name}_client_id")]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _base64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _verifier(settings: Settings, state: str) -> str:
    return _base64(hmac.digest(settings.jwt_secret.encode(), ("oauth-pkce:" + state).encode(), "sha256"))


async def start(db: AsyncSession, settings: Settings, provider: Provider, intent: Intent,
                user: User | None) -> tuple[str, str]:
    if provider not in configured(settings):
        raise BadRequestError("该登录方式尚未配置")
    state = secrets.token_urlsafe(32)
    browser = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    await db.execute(delete(OAuthState).where(OAuthState.expires_at <= now))
    db.add(OAuthState(digest=_digest(state), provider=provider, browser_digest=_digest(browser),
                      intent=intent, expires_at=now + timedelta(seconds=TTL_SECONDS),
                      user_id=user.id if user else None, token_version=user.token_version if user else None))
    await db.commit()
    params = {"client_id": getattr(settings, f"oauth_{provider}_client_id"),
              "redirect_uri": getattr(settings, f"oauth_{provider}_redirect_uri"),
              "response_type": "code", "state": state}
    if provider == "github":
        params.update(code_challenge_method="S256",
                      code_challenge=_base64(hashlib.sha256(_verifier(settings, state).encode()).digest()))
    else:
        params["scope"] = "user"
    return ENDPOINTS[provider][0] + "?" + urlencode(params), browser


async def consume(db: AsyncSession, provider: Provider, state: str, browser: str) -> tuple[str, int | None, int | None]:
    row = (await db.execute(delete(OAuthState).where(
        OAuthState.digest == _digest(state), OAuthState.browser_digest == _digest(browser),
        OAuthState.provider == provider, OAuthState.expires_at > datetime.now(UTC),
    ).returning(OAuthState.intent, OAuthState.user_id, OAuthState.token_version))).one_or_none()
    if row is None:
        raise BadRequestError("授权已过期或浏览器已变化，请重新发起")
    return row.intent, row.user_id, row.token_version


async def fetch_subject(settings: Settings, provider: Provider, code: str, state: str) -> str:
    _, token_url, user_url = ENDPOINTS[provider]
    data = {"client_id": getattr(settings, f"oauth_{provider}_client_id"),
            "client_secret": getattr(settings, f"oauth_{provider}_client_secret"),
            "redirect_uri": getattr(settings, f"oauth_{provider}_redirect_uri"),
            "grant_type": "authorization_code", "code": code}
    if provider == "github":
        data["code_verifier"] = _verifier(settings, state)
    try:
        async with httpx.AsyncClient(
            timeout=12, follow_redirects=False, headers={"Accept": "application/json"},
        ) as client:
            token_reply = await client.post(token_url, data=data)
            token_reply.raise_for_status()
            token = token_reply.json()["access_token"]
            if not isinstance(token, str) or not token:
                raise ValueError
            identity = await client.get(user_url, headers={"Authorization": "Bearer " + token})
            identity.raise_for_status()
            subject = identity.json()["id"]
            if type(subject) is not int or subject <= 0 or len(str(subject)) > 64:
                raise ValueError
            return str(subject)
    except (httpx.HTTPError, TypeError, ValueError, KeyError):
        raise BadRequestError("第三方身份验证失败，请重新授权") from None


async def resolve_login(db: AsyncSession, provider: Provider, subject: str) -> User:
    await authz.gate(db)
    identity = await db.get(OAuthIdentity, (provider, subject))
    if identity:
        user = await db.get(User, identity.user_id)
        if user is None or not user.is_active:
            raise ForbiddenError("账号已被禁用，请联系管理员")
        return user
    if not (await effective_settings(db)).registration_enabled:
        raise ForbiddenError("注册已关闭，请使用已绑定的账号登录")
    # 外部昵称可变且可能与已有作者重名。系统用户名独立生成，密码入口使用不可验证的标记。
    user = User(username=f"{provider}-{secrets.token_hex(8)}", password_hash="!", has_local_password=False)
    db.add(user)
    await db.flush()
    db.add(OAuthIdentity(provider=provider, subject=subject, user_id=user.id))
    await db.flush()
    return user


async def bind(db: AsyncSession, user: User, provider: Provider, subject: str) -> None:
    await authz.require_authenticated(db, user, write=True)
    current = await db.get(OAuthIdentity, (provider, subject))
    if current is not None and current.user_id != user.id:
        raise ConflictError("该第三方身份已绑定其他账号")
    own = await db.scalar(select(OAuthIdentity).where(
        OAuthIdentity.user_id == user.id, OAuthIdentity.provider == provider,
    ))
    if own and own.subject != subject:
        raise ConflictError("请先解除原有的平台绑定")
    if not current:
        db.add(OAuthIdentity(provider=provider, subject=subject, user_id=user.id))
    await db.commit()


async def bound_providers(db: AsyncSession, user_id: int) -> list[str]:
    return list(await db.scalars(select(OAuthIdentity.provider).where(OAuthIdentity.user_id == user_id)
                                 .order_by(OAuthIdentity.provider)))


async def unbind(db: AsyncSession, user: User, provider: Provider, settings: Settings) -> bool:
    await authz.require_authenticated(db, user, write=True)
    await db.refresh(user)
    bound = await bound_providers(db, user.id)
    if provider not in bound:
        return False
    remaining = (set(bound) - {provider}).intersection(configured(settings))
    if not user.has_local_password and not remaining:
        raise ConflictError("请先保留一种其他可用的登录方式")
    await db.execute(delete(OAuthIdentity).where(OAuthIdentity.user_id == user.id, OAuthIdentity.provider == provider))
    user.token_version += 1
    await db.commit()
    return True
