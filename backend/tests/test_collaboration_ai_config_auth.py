"""模型配置写入必须重验 HTTP 认证时捕获的账号和令牌。"""

import asyncio
from contextlib import suppress

import pytest
from sqlalchemy import select, update

from app.models import AIConfig, User
from app.services import auth_service
from tests.helpers import create_ai_config, register


@pytest.mark.parametrize("action", ["create", "update", "default", "delete"])
@pytest.mark.parametrize("change", ["revoke", "disable"])
async def test_config_http_mutation_rechecks_original_login(app, client, monkeypatch, action, change):
    user = await register(client, "config-auth-owner")
    first = await create_ai_config(client, name="原配置", model="original")
    second = await create_ai_config(client, name="另一个", model="other")
    async with app.state.sessionmaker() as db:
        before = list(await db.execute(select(AIConfig.id, AIConfig.name, AIConfig.model, AIConfig.is_default)
                                       .where(AIConfig.user_id == user["id"]).order_by(AIConfig.id)))

    entered, release = asyncio.Event(), asyncio.Event()
    original = auth_service.get_user

    async def pause_after_initial_read(db, user_id):
        actor = await original(db, user_id)
        if user_id == user["id"]:
            entered.set()
            await release.wait()
        return actor

    monkeypatch.setattr(auth_service, "get_user", pause_after_initial_read)
    url = f"/api/v1/ai-configs/{first['id']}"
    method, target, body = {
        "create": ("POST", "/api/v1/ai-configs", {"name": "不应创建", "provider": "openai_compat", "model": "x"}),
        "update": ("PATCH", url, {"model": "不应修改"}),
        "default": ("POST", f"/api/v1/ai-configs/{second['id']}/default", None),
        "delete": ("DELETE", url, None),
    }[action]
    request = asyncio.create_task(client.request(method, target, json=body))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with app.state.sessionmaker() as db:
            values = {"token_version": User.token_version + 1} if change == "revoke" else {"is_active": False}
            await db.execute(update(User).where(User.id == user["id"]).values(**values))
            await db.commit()
        release.set()
        response = await asyncio.wait_for(request, 10)
        assert response.status_code == 401, response.text
        async with app.state.sessionmaker() as db:
            after = list(await db.execute(select(AIConfig.id, AIConfig.name, AIConfig.model, AIConfig.is_default)
                                          .where(AIConfig.user_id == user["id"]).order_by(AIConfig.id)))
        assert after == before
    finally:
        release.set()
        if not request.done():
            request.cancel()
            with suppress(asyncio.CancelledError):
                await request
