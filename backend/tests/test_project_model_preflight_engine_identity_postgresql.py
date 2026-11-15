"""真实 PostgreSQL 的 Agent 受理与等价事务引擎、跨应用隔离。"""

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import BadRequestError
from app.core.security import ACCESS_TOKEN_COOKIE, decode_access_token
from app.models import AgentMessage, Task
from app.services import project_ai_config
from app.services.agent import transactions
from app.services.authorization_service import AuthenticatedActor
from tests.helpers import create_ai_config, create_project, register

pytest_plugins = ("tests.test_mcp_settings_gate_postgresql",)


async def test_postgresql_agent_http_admits_same_engine_repeatable_read(postgres_mcp_apps):
    app, _other = postgres_mcp_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        user = await register(client, "pg-preflight-agent-owner")
        await create_ai_config(client)
        project = await create_project(client)
        base = f"/api/v1/projects/{project['id']}/agent"
        response = await client.post(f"{base}/conversations", json={})
        assert response.status_code == 201, response.text
        conversation_id = response.json()["id"]
        response = await client.post(f"{base}/conversations/{conversation_id}/messages", json={"content": "继续创作"})
        assert response.status_code == 202, response.text
        accepted = response.json()
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, accepted["task"]["id"])
            message = await db.get(AgentMessage, accepted["message"]["id"])
            assert task.user_id == user["id"] and task.statistics_key == accepted["task"]["task_key"]
            assert message.turn_task_id == task.id and message.turn_task_key == task.statistics_key
            assert task.input["_project_model"]["version"] == 2


async def test_postgresql_preflight_rejects_same_url_other_engine_and_other_app(postgres_mcp_apps):
    app, other_app = postgres_mcp_apps
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await register(client, "pg-preflight-engine-owner")
        await create_ai_config(client)
        project = await create_project(client)
        token = decode_access_token(client.cookies[ACCESS_TOKEN_COOKIE], app.state.settings.jwt_secret)
        assert token is not None
        actor = AuthenticatedActor(*token)
        preflight = await project_ai_config.preflight_project_model(
            app.state.sessionmaker, actor, project["id"], "text", cipher=app.state.cipher,
        )
        async with app.state.sessionmaker() as db:
            schema = await db.scalar(text("SELECT current_schema()"))
        foreign = create_async_engine(app.state.engine.url, connect_args={"server_settings": {"search_path": schema}})
        assert foreign.sync_engine.pool is not app.state.engine.sync_engine.pool
        try:
            forged = async_sessionmaker(foreign, info=dict(app.state.sessionmaker.kw["info"]))
            async with forged() as db:
                db.info["authenticated_actor"] = actor
                with pytest.raises(BadRequestError):
                    await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)
            # 同数据库同 engine，但另一 app 自己的 runtime 不可消费该 app 的预检。
            async with transactions.session(other_app.state.sessionmaker) as db:
                db.info["authenticated_actor"] = actor
                with pytest.raises(BadRequestError):
                    await project_ai_config.accept_preflight_project_model(db, preflight, cipher=app.state.cipher)
            async with transactions.session(app.state.sessionmaker) as db:
                db.info["authenticated_actor"] = actor
                assert await db.scalar(text("SHOW transaction_isolation")) == "repeatable read"
                assert await project_ai_config.accept_preflight_project_model(
                    db, preflight, cipher=app.state.cipher,
                ) == preflight.binding
                await db.rollback()
        finally:
            await foreign.dispose()
