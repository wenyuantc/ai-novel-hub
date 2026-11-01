"""管理员功能：引导、会话失效、用户管理、护栏、自助改密。"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import ConflictError, UnauthorizedError
from app.core.security import JWT_ALGORITHM, decode_access_token, verify_password
from app.models import AIConfig, Chapter, Project, User
from app.services import admin_service, auth_service
from app.services import authorization_service as authz
from tests.helpers import TEST_FERNET_KEY, TEST_JWT_SECRET, create_ai_config, create_project, register
from tests.rbac_helpers import assign_roles, authorization_revision

ADMIN = {"admin_username": "root", "admin_password": "rootpass123"}
USERS = "/api/v1/admin/users"


async def login(client, username: str, password: str):
    return await client.post("/api/v1/auth/login", json={"username": username, "password": password})


async def bootstrap(app) -> None:
    await auth_service.bootstrap_admin(app.state.sessionmaker, app.state.settings)


async def get_db_user(app, username: str) -> User | None:
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(User).where(User.username == username))


@pytest.fixture
async def admin_client(app, make_client):
    """已登录的引导管理员 root。"""
    await bootstrap(app)
    c = make_client()
    resp = await login(c, "root", "rootpass123")
    assert resp.status_code == 200, resp.text
    return c


async def find_user(admin_client, username: str) -> dict:
    items = (await admin_client.get(USERS, params={"q": username})).json()["items"]
    return next(u for u in items if u["username"] == username)


async def super_role_id(client) -> int:
    roles = (await client.get("/api/v1/admin/roles")).json()["items"]
    return next(role["id"] for role in roles if role["system_key"] == "super_admin")


async def promote(client, user_id: int):
    return await assign_roles(client, user_id, [await super_role_id(client)])


# ---------- 引导 ----------


@pytest.mark.parametrize("settings_overrides", [ADMIN])
async def test_bootstrap_creates_admin(app, client):
    await bootstrap(app)
    resp = await login(client, "root", "rootpass123")
    assert resp.status_code == 200
    assert resp.json()["is_admin"] is True
    me = (await client.get("/api/v1/auth/me")).json()
    assert me["is_admin"] is True and "password_hash" not in me


@pytest.mark.parametrize("settings_overrides", [{**ADMIN, "registration_enabled": False}])
async def test_bootstrap_ignores_registration_switch(app, client):
    await bootstrap(app)
    assert (await login(client, "root", "rootpass123")).status_code == 200


@pytest.mark.parametrize("settings_overrides", [ADMIN])
async def test_bootstrap_is_idempotent_and_keeps_password(app, client, caplog):
    await bootstrap(app)
    first = await get_db_user(app, "root")
    await bootstrap(app)  # 模拟重启
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(User.id))) == 1
    second = await get_db_user(app, "root")
    assert second.password_hash == first.password_hash
    assert "rootpass123" not in caplog.text


@pytest.mark.parametrize("settings_overrides", [ADMIN])
async def test_bootstrap_promotes_existing_without_overwriting_password(app, client):
    await register(client, "root", "original-pass")
    async with app.state.sessionmaker() as db:
        user = await db.scalar(select(User).where(User.username == "root"))
        user.is_active = False
        await db.commit()
    await bootstrap(app)
    user = await get_db_user(app, "root")
    assert user.is_admin is True and user.is_active is True
    assert verify_password("original-pass", user.password_hash)
    assert not verify_password("rootpass123", user.password_hash)
    assert (await login(client, "root", "rootpass123")).status_code == 401
    assert (await login(client, "root", "original-pass")).status_code == 200


async def test_bootstrap_noop_without_env(app):
    await bootstrap(app)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(User.id))) == 0


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, jwt_secret=TEST_JWT_SECRET, encryption_key=TEST_FERNET_KEY, **kw)  # type: ignore[call-arg]


@pytest.mark.parametrize("kw", [{"admin_username": "root"}, {"admin_password": "rootpass123"}])
def test_admin_env_must_be_paired(kw):
    with pytest.raises(ValidationError) as exc:
        _settings(**kw)
    assert "ADMIN_USERNAME" in str(exc.value)


@pytest.mark.parametrize(
    "kw",
    [{"admin_username": "ab", "admin_password": "rootpass123"}, {"admin_username": "root", "admin_password": "123"}],
)
def test_admin_env_follows_registration_rules(kw):
    with pytest.raises(ValidationError) as exc:
        _settings(**kw)
    assert "不符合注册规则" in str(exc.value)
    assert kw["admin_password"] not in str(exc.value)


def test_admin_env_empty_strings_mean_unset():
    s = _settings(admin_username="", admin_password="  ")
    assert s.admin_credentials is None
    s = _settings(admin_username=" root ", admin_password="rootpass123")
    assert s.admin_username == "root"


# ---------- 权限 ----------


@pytest.mark.parametrize("settings_overrides", [ADMIN])
async def test_non_admin_gets_403(admin_client, client):
    await register(client, "alice")
    assert (await client.get("/api/v1/auth/me")).json()["is_admin"] is False
    alice_id = (await find_user(admin_client, "alice"))["id"]
    checks = [
        ("GET", USERS, None),
        ("POST", USERS, {"username": "eve", "password": "secret123"}),
        ("PATCH", f"{USERS}/{alice_id}", {"is_admin": True}),
        ("POST", f"{USERS}/{alice_id}/reset-password", {"new_password": "newpass123"}),
        ("DELETE", f"{USERS}/{alice_id}", None),
    ]
    for method, url, body in checks:
        resp = await client.request(method, url, json=body)
        assert resp.status_code == 403, (method, url, resp.text)
        assert resp.json()["detail"] == "需要管理员权限"


async def test_admin_routes_require_login(client):
    assert (await client.get(USERS)).status_code == 401


@pytest.mark.parametrize("settings_overrides", [ADMIN])
async def test_unknown_user_404(admin_client):
    for method, url, body in [
        ("PATCH", f"{USERS}/9999", {"is_active": False}),
        ("POST", f"{USERS}/9999/reset-password", {"new_password": "newpass123"}),
        ("DELETE", f"{USERS}/9999", None),
    ]:
        resp = await admin_client.request(method, url, json=body)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "用户不存在"


# ---------- 列表 ----------


@pytest.mark.parametrize("settings_overrides", [ADMIN])
async def test_list_search_paginate_project_count(admin_client, make_client):
    for name in ("alice", "alfred", "bob"):
        c = make_client()
        await register(c, name)
        if name == "alice":
            await create_project(c)
            await create_project(c, title="第二部")

    body = (await admin_client.get(USERS)).json()
    assert body["total"] == 4
    assert [u["username"] for u in body["items"]] == ["root", "alice", "alfred", "bob"]
    assert set(body["items"][0]) == {
        "id", "username", "is_admin", "is_active", "created_at", "project_count",
        "is_super_admin", "is_management_account", "roles", "authorization_revision",
    }
    counts = {u["username"]: u["project_count"] for u in body["items"]}
    assert counts == {"root": 0, "alice": 2, "alfred": 0, "bob": 0}

    body = (await admin_client.get(USERS, params={"q": "AL"})).json()
    assert body["total"] == 2 and {u["username"] for u in body["items"]} == {"alice", "alfred"}

    body = (await admin_client.get(USERS, params={"page": 2, "page_size": 3})).json()
    assert body["total"] == 4 and [u["username"] for u in body["items"]] == ["bob"]

    # LIKE 通配符按字面匹配
    assert (await admin_client.get(USERS, params={"q": "%"})).json()["total"] == 0
    assert (await admin_client.get(USERS, params={"page_size": 101})).status_code == 422
    assert (await admin_client.get(USERS, params={"page": 0})).status_code == 422


# ---------- 创建用户 ----------


@pytest.mark.parametrize("settings_overrides", [{**ADMIN, "registration_enabled": False}])
async def test_admin_creates_users_when_registration_disabled(admin_client, make_client):
    resp = await admin_client.post(USERS, json={"username": " carol ", "password": "carolpass1"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["username"] == "carol"
    assert (body["is_admin"], body["is_active"], body["project_count"]) == (False, True, 0)

    c = make_client()
    resp = await login(c, "carol", "carolpass1")
    assert resp.status_code == 200 and resp.json()["is_admin"] is False

    resp = await admin_client.post(USERS, json={"username": "dave", "password": "davepass1"})
    assert resp.status_code == 201
    assert (await promote(admin_client, resp.json()["id"]))["is_admin"] is True
    d = make_client()
    await login(d, "dave", "davepass1")
    assert (await d.get("/api/v1/auth/me")).json()["is_admin"] is True
    assert (await d.get(USERS)).status_code == 200

    resp = await admin_client.post(USERS, json={"username": "carol", "password": "another1"})
    assert resp.status_code == 409 and resp.json()["detail"] == "用户名已存在"
    resp = await admin_client.post(USERS, json={"username": "x", "password": "1"})
    assert resp.status_code == 422


# ---------- 会话失效 ----------


@pytest.mark.parametrize("settings_overrides", [ADMIN])
async def test_reset_password_invalidates_sessions(admin_client, client):
    await register(client, "alice", "oldpass123")
    uid = (await find_user(admin_client, "alice"))["id"]
    resp = await admin_client.post(f"{USERS}/{uid}/reset-password", json={"new_password": "newpass123"})
    assert resp.status_code == 204

    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 401 and me.json()["detail"] == "未登录或登录已过期"
    assert (await login(client, "alice", "oldpass123")).status_code == 401
    assert (await login(client, "alice", "newpass123")).status_code == 200
    assert (await client.get("/api/v1/auth/me")).status_code == 200

    resp = await admin_client.post(f"{USERS}/{uid}/reset-password", json={"new_password": "12"})
    assert resp.status_code == 422


@pytest.mark.parametrize("settings_overrides", [ADMIN])
async def test_disable_and_enable(admin_client, client):
    await register(client, "alice")
    uid = (await find_user(admin_client, "alice"))["id"]

    resp = await admin_client.patch(f"{USERS}/{uid}", json={"is_active": False})
    assert resp.status_code == 200 and resp.json()["is_active"] is False
    assert (await client.get("/api/v1/auth/me")).status_code == 401

    resp = await login(client, "alice", "secret123")
    assert resp.status_code == 403 and resp.json()["detail"] == "账号已被禁用，请联系管理员"
    # 密码错误时仍提示用户名或密码错误，不暴露禁用状态
    assert (await login(client, "alice", "wrongpass")).status_code == 401

    resp = await admin_client.patch(f"{USERS}/{uid}", json={"is_active": True})
    assert resp.status_code == 200 and resp.json()["is_active"] is True
    assert (await login(client, "alice", "secret123")).status_code == 200
    assert (await client.get("/api/v1/auth/me")).status_code == 200


async def test_disabled_user_rejected_even_with_matching_version(app, client):
    await register(client, "alice")
    async with app.state.sessionmaker() as db:
        user = await db.scalar(select(User).where(User.username == "alice"))
        user.is_active = False  # 不改版本号，单独验证 is_active 检查
        await db.commit()
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_legacy_token_without_ver_still_valid(app, client):
    user = await register(client, "alice")
    now = datetime.now(UTC)
    payload = {"sub": str(user["id"]), "iat": now, "exp": now + timedelta(minutes=5)}
    legacy = jwt.encode(payload, TEST_JWT_SECRET, algorithm=JWT_ALGORITHM)
    assert decode_access_token(legacy, TEST_JWT_SECRET) == (user["id"], 0)
    client.cookies.clear()
    client.cookies.set("access_token", legacy)
    assert (await client.get("/api/v1/auth/me")).status_code == 200

    # 版本号递增后旧令牌（视为 ver=0）失效
    async with app.state.sessionmaker() as db:
        u = await db.get(User, user["id"])
        u.token_version += 1
        await db.commit()
    assert (await client.get("/api/v1/auth/me")).status_code == 401


def test_token_with_bad_ver_rejected():
    now = datetime.now(UTC)
    token = jwt.encode({"sub": "1", "ver": "x", "exp": now + timedelta(minutes=5)}, TEST_JWT_SECRET, JWT_ALGORITHM)
    assert decode_access_token(token, TEST_JWT_SECRET) is None


# ---------- 设为 / 取消管理员 ----------


@pytest.mark.parametrize("settings_overrides", [ADMIN])
async def test_promote_and_demote(admin_client, client):
    await register(client, "alice")
    uid = (await find_user(admin_client, "alice"))["id"]
    assert (await promote(admin_client, uid))["is_admin"] is True
    # 设为管理员不影响现有会话
    assert (await client.get("/api/v1/auth/me")).json()["is_admin"] is True
    assert (await client.get(USERS)).status_code == 200

    assert (await assign_roles(admin_client, uid, []))["is_admin"] is False
    assert (await client.get(USERS)).status_code == 403


# ---------- 护栏 ----------


@pytest.mark.parametrize("settings_overrides", [ADMIN])
async def test_cannot_act_on_self(admin_client):
    me = (await admin_client.get("/api/v1/auth/me")).json()
    for method, url, body in [
        ("PATCH", f"{USERS}/{me['id']}", {"is_active": False}),

        ("DELETE", f"{USERS}/{me['id']}", None),
    ]:
        resp = await admin_client.request(method, url, json=body)
        assert resp.status_code == 400, (method, body)
        assert resp.json()["detail"] == "不能对自己执行该操作"
    resp = await admin_client.put(f"{USERS}/{me['id']}/roles", json={
        "authorization_revision": await authorization_revision(admin_client), "role_ids": [],
    })
    assert resp.status_code == 400
    # 无实际状态变更的 PATCH 允许；角色变更仅走专用接口。
    resp = await admin_client.patch(f"{USERS}/{me['id']}", json={"is_active": True})
    assert resp.status_code == 200
    root = (await admin_client.get("/api/v1/auth/me")).json()
    assert root["is_admin"] is True


@pytest.mark.parametrize("settings_overrides", [ADMIN])
async def test_last_active_admin_protected(app, admin_client, make_client):
    # 另一名管理员 bob；root 被禁用后 bob 成为唯一启用中的管理员
    resp = await admin_client.post(USERS, json={"username": "bob", "password": "bobpass123"})
    bob_id = resp.json()["id"]
    await promote(admin_client, bob_id)
    root_id = (await admin_client.get("/api/v1/auth/me")).json()["id"]
    bob = make_client()
    await login(bob, "bob", "bobpass123")

    resp = await bob.patch(f"{USERS}/{root_id}", json={"is_active": False})
    assert resp.status_code == 200

    # 最后一名启用超管约束仍在底层；禁用 actor 无法绕过写前认证。
    async with app.state.sessionmaker() as db:
        await authz.gate(db)
        with pytest.raises(ConflictError, match="至少需要保留一名启用中的管理员"):
            await admin_service._ensure_other_active_admin(db, bob_id)
        actor = await db.get(User, root_id)
        with pytest.raises(UnauthorizedError):
            await admin_service.delete_user(db, actor, bob_id)
    bob_row = await get_db_user(app, "bob")
    assert (bob_row.is_admin, bob_row.is_active) == (True, True)

    # 被禁用的管理员不算数：重新启用 root 后才能降级 bob
    resp = await bob.patch(f"{USERS}/{root_id}", json={"is_active": True})
    assert resp.status_code == 200
    root2 = make_client()
    await login(root2, "root", "rootpass123")
    assert (await assign_roles(root2, bob_id, []))["is_admin"] is False


@pytest.mark.parametrize("settings_overrides", [ADMIN])
async def test_multi_admin_flow_via_http(app, admin_client, make_client):
    """HTTP 层面：操作者本身是启用中的管理员且不能操作自己，因此最后一名管理员只能由自我护栏兜住。"""
    resp = await admin_client.post(USERS, json={"username": "bob", "password": "bobpass123"})
    bob_id = resp.json()["id"]
    await promote(admin_client, bob_id)
    resp = await admin_client.post(USERS, json={"username": "carl", "password": "carlpass1"})
    carl_id = resp.json()["id"]
    await promote(admin_client, carl_id)
    carl = make_client()
    await login(carl, "carl", "carlpass1")
    # 还有其他管理员时可以降级 / 删除别的管理员
    root_id = (await admin_client.get("/api/v1/auth/me")).json()["id"]
    assert (await assign_roles(carl, root_id, []))["is_admin"] is False
    # carl 删除 bob 后仍有 carl 一名管理员，允许
    assert (await carl.delete(f"{USERS}/{bob_id}")).status_code == 204
    # 现在 carl 是唯一启用中的管理员，root 已无管理员权限
    assert (await admin_client.put(f"{USERS}/{carl_id}/roles", json={
        "authorization_revision": await authorization_revision(carl), "role_ids": [],
    })).status_code == 403
    resp = await carl.put(f"{USERS}/{carl_id}/roles", json={
        "authorization_revision": await authorization_revision(carl), "role_ids": [],
    })
    assert resp.status_code == 400


# ---------- 删除 ----------


@pytest.mark.parametrize("settings_overrides", [ADMIN])
async def test_delete_user_cascades(app, admin_client, client):
    await register(client, "alice")
    p = await create_project(client)
    await client.post(f"/api/v1/projects/{p['id']}/chapters", json={"title": "第一章", "content": "正文"})
    await create_ai_config(client)
    uid = (await find_user(admin_client, "alice"))["id"]

    assert (await admin_client.delete(f"{USERS}/{uid}")).status_code == 409
    assert (await client.delete(f"/api/v1/projects/{p['id']}")).status_code == 204
    assert (await admin_client.delete(f"{USERS}/{uid}")).status_code == 409
    recycled = (await admin_client.get("/api/v1/admin/recycle-bin/projects")).json()["items"][0]
    assert (await admin_client.post(
        f"/api/v1/admin/recycle-bin/projects/{p['id']}/purge",
        json={"deleted_at": recycled["deleted_at"], "title": recycled["title"], "confirm": True},
    )).status_code == 204
    assert (await admin_client.delete(f"{USERS}/{uid}")).status_code == 204
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    async with app.state.sessionmaker() as db:
        assert await db.get(User, uid) is None
        assert await db.scalar(select(func.count(Project.id))) == 0
        assert await db.scalar(select(func.count(Chapter.id))) == 0
        assert await db.scalar(select(func.count(AIConfig.id))) == 0
    assert (await admin_client.get(USERS)).json()["total"] == 1


# ---------- 自助改密 ----------


async def test_change_password_keeps_current_session(client, make_client):
    await register(client, "alice", "oldpass123")
    other = make_client()
    assert (await login(other, "alice", "oldpass123")).status_code == 200

    resp = await client.post(
        "/api/v1/auth/change-password", json={"old_password": "wrongpass", "new_password": "newpass123"}
    )
    assert resp.status_code == 400 and resp.json()["detail"] == "原密码不正确"
    assert (await other.get("/api/v1/auth/me")).status_code == 200

    resp = await client.post(
        "/api/v1/auth/change-password", json={"old_password": "oldpass123", "new_password": "newpass123"}
    )
    assert resp.status_code == 204
    header = resp.headers["set-cookie"].lower()
    assert "access_token=" in header and "httponly" in header

    assert (await client.get("/api/v1/auth/me")).status_code == 200
    assert (await other.get("/api/v1/auth/me")).status_code == 401
    assert (await login(other, "alice", "oldpass123")).status_code == 401
    assert (await login(other, "alice", "newpass123")).status_code == 200


async def test_change_password_validation(client):
    await register(client, "alice")
    resp = await client.post("/api/v1/auth/change-password", json={"old_password": "secret123", "new_password": "1"})
    assert resp.status_code == 422
    assert "secret123" not in resp.text


async def test_change_password_requires_login(client):
    resp = await client.post("/api/v1/auth/change-password", json={"old_password": "a", "new_password": "abcdef"})
    assert resp.status_code == 401


@pytest.mark.parametrize("settings_overrides", [ADMIN])
async def test_lifespan_runs_bootstrap(app):
    async with app.router.lifespan_context(app):
        user = await get_db_user(app, "root")
        assert user is not None and user.is_admin is True
