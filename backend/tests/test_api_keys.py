"""平台 API Key：再次查看、授权隔离、生命周期与并发额度。"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from cryptography.fernet import Fernet
from fastapi import Query
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from app.core.security import SecretCipher
from app.models import ApiKey, ApiKeyProject, User
from tests.helpers import create_project, register

KEYS = "/api/v1/api-keys"
PROBE = "/_test/api-key"


@pytest.fixture
def open_api_probe(app):
    """只在测试 app 注册，验证实际依赖而不添加产品调试入口。"""
    from app.core.deps import DbDep
    from app.core.open_api_deps import OpenApiUser
    from app.services import api_key_service as svc

    @app.get(PROBE)
    async def principal(user: OpenApiUser, amount: Annotated[int, Query(ge=1)] = 1):
        return {"id": user.key_id, "name": user.name, "project_ids": list(user.project_ids), "amount": amount}

    @app.get(PROBE + "/projects/{project_id}")
    async def project(project_id: int, user: OpenApiUser, db: DbDep):
        result = await svc.get_authorized_project(db, user, project_id)
        return {"id": result.id}


async def create_key(client, **overrides):
    body = {"name": "阅读工具", "all_projects": True}
    body.update(overrides)
    response = await client.post(KEYS, json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def test_key_can_be_revealed_after_creation_without_exposing_list(client, app):
    await register(client)
    before = datetime.now(UTC)
    response = await client.post(KEYS, json={"name": "  阅读工具  ", "all_projects": True})
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    created = response.json()
    assert created["key"].startswith("anv_")
    assert created["name"] == "阅读工具"
    assert created["status"] == "active"
    expires = datetime.fromisoformat(created["expires_at"].replace("Z", "+00:00"))
    assert before + timedelta(days=89) < expires < datetime.now(UTC) + timedelta(days=91)

    listing = await client.get(KEYS)
    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "no-store"
    assert listing.json()[0]["id"] == created["id"]
    assert "key" not in listing.json()[0]
    assert created["key"] not in listing.text
    assert "key_hash" not in listing.text and "key_enc" not in listing.text
    reveal = await client.post(f"{KEYS}/{created['id']}/reveal")
    assert reveal.status_code == 200, reveal.text
    assert reveal.json() == {"key": created["key"]}
    assert reveal.headers["cache-control"] == "no-store"

    async with app.state.sessionmaker() as db:
        saved = await db.get(ApiKey, created["id"])
        assert saved.key_enc != created["key"]
        assert saved.key_hash != created["key"]
        assert app.state.cipher.decrypt(saved.key_enc) == created["key"]


async def test_specified_novels_are_validated_and_default_requires_selection(client, make_client):
    await register(client)
    first = await create_project(client)
    second = await create_project(client, title="第二部小说")
    other = make_client()
    await register(other, "bob")
    foreign = await create_project(other)

    assert (await client.post(KEYS, json={"name": "缺少授权"})).status_code == 400
    assert (await client.post(KEYS, json={"name": "非法授权", "project_ids": [foreign["id"]]})).status_code == 404
    created = await create_key(
        client, all_projects=False, project_ids=[second["id"], first["id"], first["id"]], expires_at=None
    )
    assert created["project_ids"] == sorted([first["id"], second["id"]])
    assert created["expires_at"] is None
    assert created["all_projects"] is False


async def test_other_users_and_administrators_cannot_reveal_or_manage_keys(client, make_client, app):
    await register(client)
    created = await create_key(client)
    for username, is_admin in (("bob", False), ("manager", True)):
        other = make_client()
        user = await register(other, username)
        if is_admin:
            async with app.state.sessionmaker() as db:
                await db.execute(update(User).where(User.id == user["id"]).values(is_admin=True))
                await db.commit()
        assert (await other.get(KEYS)).json() == []
        for method, suffix, body in (
            ("POST", "/reveal", None),
            ("POST", "/revoke", None),
            ("PATCH", "", {"name": "篡改"}),
        ):
            result = await other.request(method, f"{KEYS}/{created['id']}{suffix}", json=body)
            assert result.status_code == 404, result.text
            assert result.json()["detail"] == "API Key 不存在"


@pytest.mark.parametrize("body", [
    {"name": "   ", "all_projects": True},
    {"name": "x" * 101, "all_projects": True},
    {"name": "无时区", "all_projects": True, "expires_at": "2099-01-01T00:00:00"},
    {"name": "已过期", "all_projects": True, "expires_at": "2000-01-01T00:00:00Z"},
    {"name": "非法编号", "project_ids": [-1]},
])
async def test_key_input_validation(client, body):
    await register(client)
    result = await client.post(KEYS, json=body)
    assert result.status_code == 422, result.text
    assert isinstance(result.json()["detail"], str)
    assert all("input" not in error for error in result.json()["errors"])


async def test_patch_preserves_omitted_fields_and_validates_final_scope(client):
    await register(client)
    novel = await create_project(client)
    created = await create_key(client, expires_at=None)
    endpoint = f"{KEYS}/{created['id']}"
    assert (await client.patch(endpoint, json={"all_projects": False})).status_code == 400
    assert (await client.patch(endpoint, json={"name": None})).status_code == 422
    assert (await client.patch(endpoint, json={"project_ids": None})).status_code == 422
    updated = await client.patch(endpoint, json={"all_projects": False, "project_ids": [novel["id"]]})
    assert updated.status_code == 200, updated.text
    assert updated.json()["project_ids"] == [novel["id"]]
    assert updated.json()["expires_at"] is None
    assert "key" not in updated.json()
    renamed = await client.patch(endpoint, json={"name": "  新名称  "})
    assert renamed.json()["name"] == "新名称"
    assert renamed.json()["project_ids"] == [novel["id"]]
    universal = await client.patch(endpoint, json={"all_projects": True, "project_ids": [novel["id"]]})
    assert universal.json()["project_ids"] == []
    assert (await client.post(endpoint + "/reveal")).json()["key"] == created["key"]


async def test_only_x_api_key_authenticates_open_api_and_not_internal_api(client, make_client, open_api_probe):
    await register(client)
    created = await create_key(client)
    # 已登录 Cookie 不能代替平台 Key。
    assert (await client.get(PROBE)).status_code == 401
    assert (await client.get(PROBE, headers={"X-API-Key": "invalid"})).status_code == 401
    assert (await client.get(PROBE, headers={"Authorization": "Bearer " + created["key"]})).status_code == 401
    anonymous = make_client()
    assert (await anonymous.get(PROBE, headers={"X-API-Key": created["key"]})).status_code == 200
    for method, suffix in (("GET", ""), ("POST", f"/{created['id']}/reveal")):
        result = await anonymous.request(method, KEYS + suffix, headers={"X-API-Key": created["key"]})
        assert result.status_code == 401


async def test_novel_authorization_and_deleted_selection_do_not_expand_access(
    client, make_client, open_api_probe
):
    await register(client)
    allowed = await create_project(client)
    denied = await create_project(client, title="未授权")
    other = make_client()
    await register(other, "bob")
    foreign = await create_project(other)
    created = await create_key(client, all_projects=False, project_ids=[allowed["id"]])
    anonymous = make_client()
    headers = {"X-API-Key": created["key"]}
    assert (await anonymous.get(f"{PROBE}/projects/{allowed['id']}", headers=headers)).status_code == 200
    for project_id in (denied["id"], foreign["id"], 999999):
        result = await anonymous.get(f"{PROBE}/projects/{project_id}", headers=headers)
        assert result.status_code == 404
        assert result.json()["detail"] == "小说不存在"
    assert (await client.delete(f"/api/v1/projects/{allowed['id']}")).status_code == 204
    assert (await anonymous.get(PROBE, headers=headers)).json()["project_ids"] == []
    assert (await anonymous.get(f"{PROBE}/projects/{denied['id']}", headers=headers)).status_code == 404
    listing = (await client.get(KEYS)).json()[0]
    assert listing["all_projects"] is False and listing["project_ids"] == []


async def test_expired_keys_can_be_revealed_and_extended_but_revocation_erases_ciphertext(
    client, make_client, app, open_api_probe
):
    await register(client)
    created = await create_key(client)
    endpoint = f"{KEYS}/{created['id']}"
    anonymous = make_client()
    headers = {"X-API-Key": created["key"]}
    async with app.state.sessionmaker() as db:
        await db.execute(update(ApiKey).where(ApiKey.id == created["id"]).values(
            expires_at=datetime.now(UTC) - timedelta(seconds=1)
        ))
        await db.commit()
    assert (await client.get(KEYS)).json()[0]["status"] == "expired"
    assert (await client.post(endpoint + "/reveal")).json()["key"] == created["key"]
    assert (await anonymous.get(PROBE, headers=headers)).status_code == 401
    assert (await client.patch(endpoint, json={"expires_at": None})).json()["status"] == "active"
    assert (await anonymous.get(PROBE, headers=headers)).status_code == 200

    for _ in range(2):
        revoked = await client.post(endpoint + "/revoke")
        assert revoked.status_code == 204
    assert (await client.post(endpoint + "/reveal")).status_code == 400
    assert (await client.patch(endpoint, json={"expires_at": None})).status_code == 400
    assert (await anonymous.get(PROBE, headers=headers)).status_code == 401
    assert (await client.get(KEYS)).json()[0]["status"] == "revoked"
    async with app.state.sessionmaker() as db:
        saved = await db.get(ApiKey, created["id"])
        assert saved.key_enc is None
        assert saved.revoked_at is not None


async def test_keys_are_independent_of_login_version_but_follow_account_state(
    client, make_client, app, open_api_probe
):
    user = await register(client)
    created = await create_key(client)
    headers = {"X-API-Key": created["key"]}
    anonymous = make_client()
    changed = await client.post("/api/v1/auth/change-password", json={
        "old_password": "secret123", "new_password": "changed123"
    })
    assert changed.status_code == 204
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    assert (await anonymous.get(PROBE, headers=headers)).status_code == 200
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.id == user["id"]).values(is_active=False))
        await db.commit()
    assert (await anonymous.get(PROBE, headers=headers)).status_code == 401
    async with app.state.sessionmaker() as db:
        await db.execute(update(User).where(User.id == user["id"]).values(is_active=True))
        await db.commit()
    assert (await anonymous.get(PROBE, headers=headers)).status_code == 200


@pytest.mark.parametrize("settings_overrides", [{"open_api_rate_limit_per_minute": 2}])
async def test_concurrent_quota_is_atomic_per_key_and_recovers_next_minute(
    client, make_client, app, open_api_probe, monkeypatch
):
    from app.services import api_key_service as svc

    await register(client)
    first = await create_key(client)
    second = await create_key(client)
    clock = [datetime.now(UTC).replace(second=17, microsecond=0)]
    monkeypatch.setattr(svc, "_now_utc", lambda: clock[0])
    anonymous = make_client()
    headers = {"X-API-Key": first["key"]}
    results = await asyncio.gather(*(anonymous.get(PROBE, headers=headers) for _ in range(6)))
    assert sorted(result.status_code for result in results) == [200, 200, 429, 429, 429, 429]
    for result in results:
        if result.status_code == 429:
            assert result.headers["retry-after"] == "43"
            assert result.json()["detail"] == "API Key 请求过于频繁，请稍后重试"
    assert (await anonymous.get(PROBE, headers={"X-API-Key": second["key"]})).status_code == 200
    clock[0] += timedelta(minutes=1)
    assert (await anonymous.get(PROBE, headers=headers)).status_code == 200
    listing = (await client.get(KEYS)).json()
    assert next(key for key in listing if key["id"] == first["id"])["last_used_at"] is not None
    async with app.state.sessionmaker() as db:
        saved = await db.get(ApiKey, first["id"])
        assert saved.rate_count == 1


@pytest.mark.parametrize(("settings_overrides", "delayed_status"), [
    ({"open_api_rate_limit_per_minute": 1}, 429),
    ({"open_api_rate_limit_per_minute": 2}, 200),
])
async def test_delayed_previous_minute_cannot_reset_current_quota(
    client, make_client, app, open_api_probe, monkeypatch, settings_overrides, delayed_status
):
    """旧分钟请求等待期间新窗口已开始时，不能倒退窗口、时间或重复获得额度。"""
    from app.services import api_key_service as svc

    await register(client)
    created = await create_key(client)
    anonymous = make_client()
    headers = {"X-API-Key": created["key"]}
    clock = [datetime.now(UTC).replace(second=59, microsecond=0)]
    monkeypatch.setattr(svc, "_now_utc", lambda: clock[0])
    reached = asyncio.Event()
    resume = asyncio.Event()
    original_execute = AsyncSession.execute

    async def delayed_execute(db, statement, *args, **kwargs):
        if asyncio.current_task() is delayed_request and isinstance(statement, Update):
            reached.set()
            await resume.wait()
        return await original_execute(db, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", delayed_execute)
    delayed_request = asyncio.create_task(anonymous.get(PROBE, headers=headers))
    try:
        async with asyncio.timeout(5):
            await reached.wait()
        clock[0] += timedelta(seconds=18)
        assert (await anonymous.get(PROBE, headers=headers)).status_code == 200
        resume.set()
        delayed = await delayed_request
        assert delayed.status_code == delayed_status, delayed.text
        if delayed_status == 429:
            assert delayed.headers["retry-after"] == "43"
        limited = await anonymous.get(PROBE, headers=headers)
        assert limited.status_code == 429, limited.text
        assert limited.headers["retry-after"] == "43"
        async with app.state.sessionmaker() as db:
            saved = await db.get(ApiKey, created["id"])
            assert saved.rate_window == int(clock[0].timestamp()) // 60
            assert saved.rate_count == settings_overrides["open_api_rate_limit_per_minute"]
            assert saved.last_used_at.replace(tzinfo=UTC) == clock[0]
    finally:
        resume.set()
        if not delayed_request.done():
            await delayed_request


@pytest.mark.parametrize("settings_overrides", [{"open_api_rate_limit_per_minute": 2}])
async def test_authenticated_errors_consume_quota(client, make_client, open_api_probe):
    await register(client)
    created = await create_key(client)
    anonymous = make_client()
    headers = {"X-API-Key": created["key"]}
    assert (await anonymous.get(PROBE, params={"amount": 0}, headers=headers)).status_code == 422
    assert (await anonymous.get(f"{PROBE}/projects/999999", headers=headers)).status_code == 404
    assert (await anonymous.get(PROBE, headers=headers)).status_code == 429


async def test_expiry_is_rechecked_after_database_authentication(
    client, make_client, app, open_api_probe, monkeypatch
):
    """密钥在等待数据库更新期间到期时，不能按请求开始时的时间继续授权。"""
    from app.services import api_key_service as svc

    await register(client)
    created = await create_key(client)
    before = datetime.now(UTC)
    async with app.state.sessionmaker() as db:
        await db.execute(update(ApiKey).where(ApiKey.id == created["id"]).values(
            expires_at=before + timedelta(seconds=1)
        ))
        await db.commit()
    times = iter((before, before + timedelta(seconds=2)))
    monkeypatch.setattr(svc, "_now_utc", lambda: next(times))
    anonymous = make_client()
    result = await anonymous.get(PROBE, headers={"X-API-Key": created["key"]})
    assert result.status_code == 401, result.text


async def test_encryption_key_change_keeps_mask_and_auth_but_prevents_reveal(
    client, make_client, app, open_api_probe
):
    await register(client)
    created = await create_key(client)
    app.state.cipher = SecretCipher(Fernet.generate_key().decode())
    assert (await client.get(KEYS)).json()[0]["key_masked"] == created["key_masked"]
    reveal = await client.post(f"{KEYS}/{created['id']}/reveal")
    assert reveal.status_code == 400
    assert "ENCRYPTION_KEY" in reveal.json()["detail"]
    assert created["key"] not in reveal.text
    anonymous = make_client()
    assert (await anonymous.get(PROBE, headers={"X-API-Key": created["key"]})).status_code == 200


async def test_user_deletion_cascades_keys_and_grants(client, app, make_client, open_api_probe):
    user = await register(client)
    novel = await create_project(client)
    created = await create_key(client, all_projects=False, project_ids=[novel["id"]])
    async with app.state.sessionmaker() as db:
        saved_user = await db.get(User, user["id"])
        await db.delete(saved_user)
        await db.commit()
        assert await db.scalar(select(func.count(ApiKey.id))) == 0
        assert await db.scalar(select(func.count()).select_from(ApiKeyProject)) == 0
    anonymous = make_client()
    assert (await anonymous.get(PROBE, headers={"X-API-Key": created["key"]})).status_code == 401
