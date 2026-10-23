import pytest

from tests.helpers import register


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_register_login_logout_me(client):
    user = await register(client, "alice", "secret123")
    assert user["username"] == "alice"
    assert "password_hash" not in user

    set_cookie = client.cookies.get("access_token")
    assert set_cookie

    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"

    resp = await client.post("/api/v1/auth/logout")
    assert resp.status_code == 204
    assert client.cookies.get("access_token") is None
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 401
    assert me.json() == {"detail": "未登录或登录已过期"}

    resp = await client.post("/api/v1/auth/login", json={"username": "alice", "password": "secret123"})
    assert resp.status_code == 200
    assert (await client.get("/api/v1/auth/me")).status_code == 200


async def test_cookie_attributes(client):
    resp = await client.post("/api/v1/auth/register", json={"username": "bob", "password": "secret123"})
    header = resp.headers["set-cookie"].lower()
    assert "access_token=" in header
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "secure" not in header


async def test_login_wrong_password(client):
    await register(client, "alice", "secret123")
    resp = await client.post("/api/v1/auth/login", json={"username": "alice", "password": "wrongpass"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "用户名或密码错误"
    resp = await client.post("/api/v1/auth/login", json={"username": "nobody", "password": "wrongpass"})
    assert resp.status_code == 401


async def test_duplicate_username(client):
    await register(client, "alice")
    resp = await client.post("/api/v1/auth/register", json={"username": "alice", "password": "secret123"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "用户名已存在"


async def test_validation_error_format(client):
    resp = await client.post("/api/v1/auth/register", json={"username": "a", "password": "1"})
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)
    assert resp.json()["detail"].startswith("请求参数校验失败")


async def test_custom_validator_error_is_422_without_echo(client):
    # 自定义 field_validator 抛 ValueError 时 ctx 含异常对象，曾导致序列化失败返回 500
    resp = await client.post("/api/v1/auth/register", json={"username": "a b c", "password": "secret123"})
    assert resp.status_code == 422
    body = resp.json()
    assert "用户名不能包含空白字符" in body["detail"]
    assert "secret123" not in resp.text


async def test_invalid_token_rejected(client):
    client.cookies.set("access_token", "not-a-jwt")
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401


async def test_auth_config(client):
    resp = await client.get("/api/v1/auth/config")
    assert resp.json() == {"registration_enabled": True}


@pytest.mark.parametrize("settings_overrides", [{"registration_enabled": False}])
async def test_registration_disabled(client):
    resp = await client.post("/api/v1/auth/register", json={"username": "alice", "password": "secret123"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "注册已关闭，请联系管理员"
    assert (await client.get("/api/v1/auth/config")).json() == {"registration_enabled": False}


@pytest.mark.parametrize("settings_overrides", [{"cookie_secure": True}])
async def test_cookie_secure_flag(client):
    resp = await client.post("/api/v1/auth/register", json={"username": "alice", "password": "secret123"})
    assert "secure" in resp.headers["set-cookie"].lower()


async def test_unknown_api_path_returns_json_404(client):
    resp = await client.get("/api/v1/nope")
    assert resp.status_code == 404
    assert isinstance(resp.json()["detail"], str)
