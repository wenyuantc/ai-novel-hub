import asyncio
import base64
import time

from sqlalchemy import select

from app.models import User
from app.models.two_step import LoginChallenge, RecoveryCode, TwoStepFactor
from app.services.two_step import _totp
from tests.helpers import register

AUTH = "/api/v1/auth"
CREDS = {"username": "alice", "password": "secret123"}


async def enroll(client):
    setup = await client.post(f"{AUTH}/two-step/setup")
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    assert secret in setup.json()["otpauth_uri"]
    assert setup.headers["Cache-Control"] == "no-store"
    code = _totp(secret, int(time.time()) // 30)
    enabled = await client.post(f"{AUTH}/two-step/enable", json={"code": code})
    assert enabled.status_code == 200, enabled.text
    return enabled.json()["recovery_codes"], secret


async def challenge(client):
    login = await client.post(f"{AUTH}/login", json=CREDS)
    assert login.json() == {"two_step_required": True}
    assert (await client.get(f"{AUTH}/me")).status_code == 401


def test_rfc6238_sha1_vector():
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert _totp(secret, 59 // 30) == "287082"


async def test_recovery_is_one_use_and_old_sessions_are_invalidated(app, client, make_client):
    user = await register(client)
    old = make_client()
    old.cookies.update(client.cookies)
    codes, secret = await enroll(client)
    assert len(codes) == len(set(codes)) and len(codes) > 1
    assert (await old.get(f"{AUTH}/me")).status_code == 401
    assert (await client.get(f"{AUTH}/two-step/status")).json() == {"enabled": True}
    async with app.state.sessionmaker() as db:
        before = (await db.get(User, user["id"])).last_login_at
        factor = await db.get(TwoStepFactor, user["id"])
        assert factor.secret_enc != secret
        hashes = list(await db.scalars(select(RecoveryCode.digest)))
        assert set(hashes).isdisjoint(codes)

    other = make_client()
    await challenge(client)
    await challenge(other)
    async with app.state.sessionmaker() as db:
        assert (await db.get(User, user["id"])).last_login_at == before
    res = await asyncio.gather(*(
        c.post(f"{AUTH}/two-step/verify", json={"code": codes[0]}) for c in (client, other)
    ))
    assert sorted(r.status_code for r in res) == [200, 400]
    ok, pending = (client, other) if res[0].status_code == 200 else (other, client)
    assert (await ok.get(f"{AUTH}/me")).json()["id"] == user["id"]
    assert (await ok.post(f"{AUTH}/two-step/disable", json={"code": codes[1]})).status_code == 204
    assert (await pending.post(f"{AUTH}/two-step/verify", json={"code": codes[2]})).status_code == 400
    async with app.state.sessionmaker() as db:
        assert not await db.scalar(select(RecoveryCode.digest))
        assert not await db.scalar(select(LoginChallenge.digest))
    assert (await ok.post(f"{AUTH}/login", json=CREDS)).json()["id"] == user["id"]


async def test_challenge_bound_to_browser_and_throttling_spans_logins(client, make_client):
    await register(client)
    codes, _ = await enroll(client)
    second = make_client()
    assert (await second.post(f"{AUTH}/two-step/verify", json={"code": codes[0]})).status_code == 400
    await challenge(client)
    for _ in range(5):
        assert (await client.post(f"{AUTH}/two-step/verify", json={"code": "incorrect"})).status_code == 400
    await challenge(second)
    result = await second.post(f"{AUTH}/two-step/verify", json={"code": codes[0]})
    assert result.status_code == 400 and "尝试过多" in result.text


async def test_dynamic_code_login_cannot_replay_same_step(client, make_client):
    await register(client)
    _, secret = await enroll(client)
    await challenge(client)
    code = _totp(secret, int(time.time()) // 30 + 1)
    assert (await client.post(f"{AUTH}/two-step/verify", json={"code": code})).status_code == 200
    second = make_client()
    await challenge(second)
    assert (await second.post(f"{AUTH}/two-step/verify", json={"code": code})).status_code == 400


async def test_password_change_from_pre_enrollment_session_cannot_mint_new_cookie(app, client):
    import pytest

    from app.core.errors import UnauthorizedError
    from app.schemas.auth import ChangePasswordRequest
    from app.services.auth_service import change_password

    details = await register(client)
    async with app.state.sessionmaker() as db:
        old_identity = await db.get(User, details["id"])
        await enroll(client)
        with pytest.raises(UnauthorizedError):
            await change_password(db, old_identity, ChangePasswordRequest(
                old_password=CREDS["password"], new_password="replacement123",
            ))
    await challenge(client)


async def test_enrollment_requires_recent_login(app, client):
    from datetime import UTC, datetime, timedelta

    import jwt

    from app.core.security import JWT_ALGORITHM

    details = await register(client)
    client.cookies.clear()
    now = datetime.now(UTC)
    token = jwt.encode(
        {"sub": str(details["id"]), "ver": 0, "iat": now - timedelta(minutes=11), "exp": now + timedelta(hours=1)},
        app.state.settings.jwt_secret, algorithm=JWT_ALGORITHM,
    )
    client.headers["Authorization"] = "Bearer " + token
    assert (await client.get(f"{AUTH}/me")).status_code == 200
    denied = await client.post(f"{AUTH}/two-step/setup")
    assert denied.status_code == 403
    assert "重新登录" in denied.text
