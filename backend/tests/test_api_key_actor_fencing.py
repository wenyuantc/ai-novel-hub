"""平台 Key 写入和明文交付必须沿用 HTTP 请求原认证身份。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, update

from app.models import ApiKey, ApiKeyProject, User
from app.services import api_key_service, auth_service
from tests.helpers import create_project, register
from tests.test_api_keys import create_key

# 平台 Key 写入/明文交付的原认证身份屏障与 ASGI 响应租约尚未纳入实现：design.md 对 api_key_service
# 只要求保持原用户级隔离，任何阶段研究记录均未登记该功能，api_keys 路由未改动。
pytestmark = pytest.mark.xfail(strict=True, reason="待实现：平台 Key 动作沿用请求原认证身份并持有至响应正文发送")


@pytest.mark.parametrize("action", ["create", "update", "revoke", "reveal"])
@pytest.mark.parametrize("change", ["token", "disabled"])
async def test_key_actions_reject_original_login_revoked_before_lock(app, client, monkeypatch, action, change):
    user = await register(client, "api-key-fenced-user")
    book = await create_project(client)
    existing = await create_key(client, all_projects=False, project_ids=[book["id"]], expires_at=None)
    async with app.state.sessionmaker() as db:
        before = (await db.execute(select(
            ApiKey.id, ApiKey.name, ApiKey.all_projects, ApiKey.revoked_at, ApiKey.key_hash, ApiKey.key_enc,
        ).where(ApiKey.user_id == user["id"]).order_by(ApiKey.id))).all()
        before_scope = (await db.execute(select(ApiKeyProject.api_key_id, ApiKeyProject.project_id))).all()

    entered, release = asyncio.Event(), asyncio.Event()
    get_user = auth_service.get_user
    generator = api_key_service.secrets.token_urlsafe
    generated = []

    async def pause_after_original_login(db, user_id):
        actor = await get_user(db, user_id)
        if user_id == user["id"]:
            entered.set()
            await release.wait()
        return actor

    def count_generated(length):
        generated.append(length)
        return generator(length)

    monkeypatch.setattr(auth_service, "get_user", pause_after_original_login)
    monkeypatch.setattr(api_key_service.secrets, "token_urlsafe", count_generated)
    key_url = f"/api/v1/api-keys/{existing['id']}"
    method, target, body = {
        "create": ("POST", "/api/v1/api-keys", {"name": "不能签发", "all_projects": True}),
        "update": ("PATCH", key_url, {"all_projects": True, "name": "不能更改"}),
        "revoke": ("POST", key_url + "/revoke", None),
        "reveal": ("POST", key_url + "/reveal", None),
    }[action]
    work = asyncio.create_task(client.request(method, target, json=body))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        async with app.state.sessionmaker() as db:
            values = {"token_version": User.token_version + 1} if change == "token" else {"is_active": False}
            await db.execute(update(User).where(User.id == user["id"]).values(**values))
            await db.commit()
        release.set()
        response = await asyncio.wait_for(work, 10)
        assert response.status_code == 401, response.text
        assert existing["key"] not in response.text and generated == []
        async with app.state.sessionmaker() as db:
            after = (await db.execute(select(
                ApiKey.id, ApiKey.name, ApiKey.all_projects, ApiKey.revoked_at, ApiKey.key_hash, ApiKey.key_enc,
            ).where(ApiKey.user_id == user["id"]).order_by(ApiKey.id))).all()
            after_scope = (await db.execute(select(ApiKeyProject.api_key_id, ApiKeyProject.project_id))).all()
        assert after == before and after_scope == before_scope
        # 已签发平台 Key 独立于登录令牌；禁用账号时才不能用于开放 API。
        result = await client.get("/api/open/v1/novels", headers={"X-API-Key": existing["key"]})
        assert result.status_code == (200 if change == "token" else 401)
    finally:
        release.set()
        if not work.done():
            work.cancel()
            with suppress(asyncio.CancelledError):
                await work


@pytest.mark.parametrize("action", ["create", "reveal"])
async def test_secret_is_not_sent_if_actor_revoked_before_response_lease(app, client, monkeypatch, action):
    user = await register(client, "key-delivery-user")
    existing = await create_key(client, all_projects=True)
    entered, release = asyncio.Event(), asyncio.Event()
    name = "create_key" if action == "create" else "reveal_key"
    original = getattr(api_key_service, name)
    issued = []

    async def pause(*args, **kwargs):
        if action == "create":
            result = await original(*args, **kwargs)
            issued.append(result)
            entered.set()
            await release.wait()
            return result
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(api_key_service, name, pause)
    target = "/api/v1/api-keys" if action == "create" else f"/api/v1/api-keys/{existing['id']}/reveal"
    payload = {"name": "提交后撤销", "all_projects": True} if action == "create" else None
    work = asyncio.create_task(client.post(target, json=payload))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == user["id"]).values(token_version=User.token_version + 1))
            await db.commit()
        release.set()
        response = await asyncio.wait_for(work, 10)
        assert response.status_code == 401, response.text
        assert existing["key"] not in response.text
        if action == "create":
            assert len(issued) == 1 and issued[0].key not in response.text
            # 写入在撤销前已合法提交；失败关闭仅抑制延后的明文响应。
            async with app.state.sessionmaker() as db:
                assert await db.get(ApiKey, issued[0].id) is not None
    finally:
        release.set()
        if not work.done():
            work.cancel()
            with suppress(asyncio.CancelledError):
                await work


async def test_reveal_holds_original_actor_until_actual_asgi_body_send(app, client):
    user = await register(client, "key-asgi-user")
    key = await create_key(client, all_projects=True)
    entered, release = asyncio.Event(), asyncio.Event()
    target = f"/api/v1/api-keys/{key['id']}/reveal"

    async def gated(scope, receive, send):
        async def gated_send(message):
            if scope.get("path") == target and message["type"] == "http.response.body":
                entered.set()
                await release.wait()
            await send(message)
        await app(scope, receive, gated_send)

    async def revoke():
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == user["id"]).values(token_version=User.token_version + 1))
            await db.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gated), base_url="http://testserver",
                                 cookies=client.cookies) as guarded:
        work = asyncio.create_task(guarded.post(target))
        change = None
        try:
            await asyncio.wait_for(entered.wait(), 10)
            change = asyncio.create_task(revoke())
            await asyncio.sleep(0.1)
            assert not change.done()
            release.set()
            response = await asyncio.wait_for(work, 10)
            await asyncio.wait_for(change, 10)
            assert response.status_code == 200 and response.json()["key"] == key["key"]
        finally:
            release.set()
            for task in (work, change):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
