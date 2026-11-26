"""数据库事务串行消费登录挑战、TOTP 时间步与恢复码。"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, UnauthorizedError
from app.core.security import SecretCipher
from app.models import User
from app.models.two_step import LoginChallenge, RecoveryCode, TwoStepFactor
from app.services import auth_service
from app.services import authorization_service as authz

CHALLENGE_COOKIE = "two_step_challenge"
CHALLENGE_TTL = 300


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _totp(secret: str, step: int) -> str:
    key = base64.b32decode(secret)
    mac = hmac.digest(key, struct.pack(">Q", step), "sha1")
    offset = mac[-1] & 15
    number = int.from_bytes(mac[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


async def enabled(db: AsyncSession, user_id: int) -> bool:
    return bool(await db.scalar(select(TwoStepFactor.enabled).where(TwoStepFactor.user_id == user_id)))


async def _lock_current(db: AsyncSession, user: User) -> None:
    user_id, version, created_at = user.id, user.token_version, user.created_at
    await authz.gate(db)
    await authz.lock_users(db, user_id)
    await db.refresh(user)
    if not user.is_active or user.token_version != version or user.created_at != created_at:
        raise UnauthorizedError("登录已失效，请重新登录")


async def setup(db: AsyncSession, user: User, cipher: SecretCipher) -> dict[str, str]:
    await _lock_current(db, user)
    factor = await db.get(TwoStepFactor, user.id)
    if factor and factor.enabled:
        raise ConflictError("两步验证已开启")
    now = int(time.time())
    secret = base64.b32encode(secrets.token_bytes(20)).decode()
    if factor is None:
        factor = TwoStepFactor(user_id=user.id, enabled=False, failures=0, window_start=now, last_step=-1)
        db.add(factor)
    factor.secret_enc = cipher.encrypt(secret)
    factor.setup_version = user.token_version
    factor.setup_expires = now + 600
    uri = (
        "otpauth://totp/aiNovel:"
        + quote(user.username, safe="")
        + "?"
        + urlencode(
            {"secret": secret, "issuer": "aiNovel", "digits": 6, "period": 30},
        )
    )
    await db.commit()
    return {"secret": secret, "otpauth_uri": uri}


async def _consume(db: AsyncSession, factor: TwoStepFactor, code: str, cipher: SecretCipher) -> None:
    now = int(time.time())
    if now >= factor.window_start + 300:
        factor.window_start, factor.failures = now, 0
    if factor.failures >= 5:
        raise BadRequestError("验证尝试过多，请稍后重试")
    if code.isascii() and code.isdigit() and len(code) == 6:
        secret = cipher.decrypt(factor.secret_enc)
        for step in (now // 30, now // 30 - 1, now // 30 + 1):
            if step > factor.last_step and hmac.compare_digest(_totp(secret, step), code):
                factor.last_step, factor.failures = step, 0
                return
    elif factor.enabled:
        used = await db.execute(
            delete(RecoveryCode).where(
                RecoveryCode.user_id == factor.user_id,
                RecoveryCode.digest == _digest(code),
            )
        )
        if used.rowcount:
            factor.failures = 0
            return
    factor.failures += 1
    await db.commit()
    raise BadRequestError("验证码无效或已使用")


async def enable(db: AsyncSession, user: User, code: str, cipher: SecretCipher) -> list[str]:
    await _lock_current(db, user)
    factor = await db.get(TwoStepFactor, user.id)
    if (
        not factor
        or factor.enabled
        or factor.setup_version != user.token_version
        or factor.setup_expires <= time.time()
    ):
        raise BadRequestError("请重新开始验证器绑定")
    await _consume(db, factor, code, cipher)
    factor.enabled = True
    user.token_version += 1
    codes = [secrets.token_hex(10) for _ in range(10)]
    db.add_all(RecoveryCode(digest=_digest(value), user_id=user.id) for value in codes)
    await db.commit()
    return codes


async def reset_uncommitted(db: AsyncSession, user: User) -> None:
    """调用方已锁住账号；由调用方提交审计及失效操作。"""
    factor = await db.get(TwoStepFactor, user.id)
    if not factor or not factor.enabled:
        raise BadRequestError("账号未开启两步验证")
    await db.delete(factor)
    user.token_version += 1
    await db.flush()


async def disable(db: AsyncSession, user: User, code: str, cipher: SecretCipher) -> None:
    await _lock_current(db, user)
    factor = await db.get(TwoStepFactor, user.id)
    if not factor or not factor.enabled:
        raise BadRequestError("账号未开启两步验证")
    await _consume(db, factor, code, cipher)
    await reset_uncommitted(db, user)
    await db.commit()


async def first_factor(db: AsyncSession, user: User) -> str | None:
    if not await enabled(db, user.id):
        # 首次启用会递增版本，后续写登录时间的 CAS 可拒绝竞态。
        return None
    await _lock_current(db, user)
    now = int(time.time())
    await db.execute(delete(LoginChallenge).where(LoginChallenge.expires <= now))
    ticket = secrets.token_urlsafe(32)
    db.add(
        LoginChallenge(digest=_digest(ticket), user_id=user.id, version=user.token_version, expires=now + CHALLENGE_TTL)
    )
    await db.commit()
    return ticket


async def verify(db: AsyncSession, ticket: str, code: str, cipher: SecretCipher) -> User:
    if len(ticket) != 43 or not ticket.isascii():
        raise BadRequestError("验证已过期，请重新登录")
    digest = _digest(ticket)
    # 无效 Cookie 不占全站授权写锁。真实消费在重新打开的事务中复核。
    exists = await db.scalar(select(LoginChallenge.user_id).where(
        LoginChallenge.digest == digest, LoginChallenge.expires > int(time.time()),
    ))
    if exists is None:
        raise BadRequestError("验证已过期，请重新登录")
    await db.rollback()
    await authz.gate(db)
    challenge = await db.get(LoginChallenge, digest)
    if not challenge or challenge.expires <= time.time():
        raise BadRequestError("验证已过期，请重新登录")
    await authz.lock_users(db, challenge.user_id)
    user = await db.get(User, challenge.user_id)
    factor = await db.get(TwoStepFactor, challenge.user_id)
    if not user or not user.is_active or challenge.version != user.token_version or not factor or not factor.enabled:
        raise UnauthorizedError("登录已失效，请重新登录")
    await _consume(db, factor, code, cipher)
    await db.delete(challenge)
    return await auth_service._record_successful_login(db, user)
