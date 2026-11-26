import asyncio
import hashlib
import logging
from base64 import urlsafe_b64encode
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import func, select

from app.core.oauth_access_log import redact_oauth_callback
from app.models import OAuthIdentity, User
from app.services import oauth_service as oauth
from tests.helpers import register
from tests.test_system_settings import save_settings

BASE = "/api/v1/auth/oauth"


@pytest.fixture
def settings_overrides():
    return {f"oauth_{provider}_{field}": value for provider in oauth.ENDPOINTS for field, value in
            [("client_id", f"{provider}-id"), ("client_secret", "fixture-secret"),
             ("redirect_uri", f"https://books.example{BASE}/{provider}/callback")]}


async def start(client, provider="github", intent="login"):
    result = await client.post(f"{BASE}/{provider}/start", params={"intent": intent})
    assert result.status_code == 200, result.text
    query = parse_qs(urlsplit(result.json()["url"]).query)
    assert "client_secret" not in query
    return query


async def finish(client, query, provider="github", code="demo"):
    return await client.get(f"{BASE}/{provider}/callback", params={"state": query["state"][0], "code": code})


@pytest.fixture
def mock_identity(monkeypatch):
    async def subject(settings, provider, code, state):
        return code
    monkeypatch.setattr(oauth, "fetch_subject", subject)


async def test_both_providers_are_independent_and_bindings_protect_last_login(client, make_client, mock_identity):
    assert (await client.get(f"{BASE}/providers")).json()["providers"] == ["github", "linuxdo"]
    assert (await finish(client, await start(client))).headers["location"] == "/login?oauth=success"
    own = (await client.get("/api/v1/auth/me")).json()["id"]
    assert (await client.get(f"{BASE}/identities")).json() == {"providers": ["github"], "has_local_password": False}
    assert (await client.delete(f"{BASE}/github/binding")).status_code == 409
    other = make_client()
    query = await start(other, "linuxdo")
    assert (await finish(other, query, "linuxdo")).headers["location"].endswith("=success")
    assert (await other.get("/api/v1/auth/me")).json()["id"] != own
    bind = await start(client, "linuxdo", "bind")
    assert "oauth=error" in (await finish(client, bind, "linuxdo")).headers["location"]
    bind = await start(client, "linuxdo", "bind")
    assert (await finish(client, bind, "linuxdo", code="second")).headers["location"].endswith("=bound")
    assert (await client.delete(f"{BASE}/github/binding")).status_code == 204
    assert (await client.get(f"{BASE}/identities")).json()["providers"] == ["linuxdo"]


async def test_browser_and_provider_bound_state_consumed_once(app, client, make_client, mock_identity):
    query = await start(client)
    assert "oauth=error" in (await finish(make_client(), query)).headers["location"]
    assert "oauth=error" in (await finish(client, query, "linuxdo")).headers["location"]
    assert (await finish(client, query)).headers["location"].endswith("=success")
    assert "oauth=error" in (await finish(client, query)).headers["location"]
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(OAuthIdentity)) == 1


async def test_registration_race_and_already_bound_login(client, make_client, system_admin, mock_identity):
    assert (await finish(client, await start(client))).headers["location"].endswith("=success")
    other = make_client()
    pending = await start(other)
    await save_settings(system_admin, registration_enabled=False)
    assert "oauth=error" in (await finish(other, pending, code="new")).headers["location"]
    assert (await finish(other, await start(other))).headers["location"].endswith("=success")
    assert (await client.get("/api/v1/auth/me")).json()["id"] == (await other.get("/api/v1/auth/me")).json()["id"]


async def test_simultaneous_signup_returns_one_bookcase(app, client, make_client, mock_identity):
    other = make_client()
    a, b = await asyncio.gather(start(client), start(other))
    replies = await asyncio.gather(finish(client, a), finish(other, b))
    assert all(r.headers["location"].endswith("=success") for r in replies)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(User)) == 1
        assert await db.scalar(select(func.count()).select_from(OAuthIdentity)) == 1


async def test_binding_refuses_changed_first_party_session(client, mock_identity):
    await register(client, "author-one", "password-1")
    query = await start(client, intent="bind")
    await client.post("/api/v1/auth/logout")
    await register(client, "author-two", "password-2")
    assert "oauth=error" in (await finish(client, query)).headers["location"]
    assert (await client.get(f"{BASE}/identities")).json()["providers"] == []


@pytest.mark.parametrize("provider", ["github", "linuxdo"])
async def test_provider_protocol(app, client, provider, monkeypatch):
    query = await start(client, provider)
    requests = []
    def exchange(request):
        requests.append(request)
        if request.method == "POST":
            body = parse_qs(request.content.decode())
            assert body["code"] == ["issued-here"]
            assert body["redirect_uri"] == query["redirect_uri"]
            if provider == "github":
                digest = hashlib.sha256(body["code_verifier"][0].encode()).digest()
                assert query["code_challenge"] == [urlsafe_b64encode(digest).rstrip(b"=").decode()]
            return httpx.Response(200, json={"access_token": "transient"})
        assert request.headers["Authorization"] == "Bearer transient"
        return httpx.Response(200, json={"id": 123, "name": "mutable"})
    ctor = httpx.AsyncClient
    monkeypatch.setattr(oauth.httpx, "AsyncClient", lambda **kw: ctor(**kw, transport=httpx.MockTransport(exchange)))
    response = await finish(client, query, provider, code="issued-here")
    assert response.headers["location"].endswith("=success")
    assert [str(r.url) for r in requests] == list(oauth.ENDPOINTS[provider][1:])


def test_access_log_masks_callback_query_only():
    record = logging.LogRecord("uvicorn.access", 20, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("peer", "GET", BASE + "/github/callback?state=sample&code=sample", "1.1", 303), None)
    assert redact_oauth_callback(record)
    assert "/github/callback HTTP" in record.getMessage()
    assert "sample" not in record.getMessage()


@pytest.mark.parametrize("provider", ["github", "linuxdo"])
async def test_oauth_waits_for_second_factor_before_login(app, client, provider, mock_identity):
    import time

    from app.services.two_step import _totp

    user_info = await register(client)
    binding = await start(client, provider, "bind")
    assert (await finish(client, binding, provider)).headers["location"].endswith("=bound")
    setup = await client.post("/api/v1/auth/two-step/setup")
    initial_code = _totp(setup.json()["secret"], int(time.time()) // 30)
    enabled = await client.post("/api/v1/auth/two-step/enable", json={"code": initial_code})
    backup = enabled.json()["recovery_codes"][0]
    await client.post("/api/v1/auth/logout")
    async with app.state.sessionmaker() as db:
        previous = (await db.get(User, user_info["id"])).last_login_at
    result = await finish(client, await start(client, provider), provider)
    assert result.headers["location"].endswith("=challenge")
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    async with app.state.sessionmaker() as db:
        assert (await db.get(User, user_info["id"])).last_login_at == previous
    verified = await client.post("/api/v1/auth/two-step/verify", json={"code": backup})
    assert verified.json()["id"] == user_info["id"]
