"""PostgreSQL 旧快照内的 Agent 模型风格投影竞争。"""

import asyncio
import json
from contextlib import AsyncExitStack

import httpx
import pytest
from sqlalchemy import update

from app.ai.providers import create_provider
from app.models import AgentConversation, AgentMessage, AIConfig, Project, WritingStyle
from app.services import project_service
from app.services.agent import context, transactions
from app.services.agent.tools import get_tool
from app.services.agent.tools.base import ToolCtx
from app.services.ai_dispatch import VisibilityPolicyError
from tests.helpers import create_ai_config
from tests.test_agent_context_execution_identity import _provider
from tests.test_agent_context_execution_identity import _setup as _setup_guarded_execution
from tests.test_agent_project_style_privacy import _world
from tests.test_ai_visibility_dispatch import _server, _sse
from tests.test_collaboration_style_privacy import _style

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


@pytest.mark.parametrize("actor_name", ["editor", "manager"])
async def test_style_binding_changes_after_project_projection_are_not_disclosed(
    postgres_collaboration_apps, monkeypatch, actor_name,
):
    app, other_app = postgres_collaboration_apps
    async with AsyncExitStack() as stack:
        owner = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        )
        clients = []

        def make_client():
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
            clients.append(client)
            return client

        try:
            _, editor, manager, _, _, project, owner_style, editor_style, _ = await _world(app, owner, make_client)
            actor = editor if actor_name == "editor" else manager
            projected, resume = asyncio.Event(), asyncio.Event()
            original = project_service.project_out

            async def pause_after_project_projection(db, row, **kwargs):
                out = await original(db, row, **kwargs)
                projected.set()
                await resume.wait()
                return out

            monkeypatch.setattr(project_service, "project_out", pause_after_project_projection)

            async def read_overview():
                async with transactions.session(app.state.sessionmaker) as db:
                    row = await db.get(Project, project["id"])
                    return await get_tool("get_project_overview").run(
                        ToolCtx(db, row, actor["id"], app.state.task_engine.ctx), {},
                    )

            read = asyncio.create_task(read_overview())
            try:
                await asyncio.wait_for(projected.wait(), 10)
                async with other_app.state.sessionmaker() as db:
                    await db.execute(update(Project).where(Project.id == project["id"]).values(
                        default_style_id=editor_style["id"],
                    ))
                    await db.commit()
                resume.set()
                result = await asyncio.wait_for(read, 10)
                assert result["default_style_id"] is None and result["default_style"] is None
                text = json.dumps(result, ensure_ascii=False)
                assert "作者风格秘密名称" not in text and "作者风格秘密描述" not in text
                assert owner_style["id"] != editor_style["id"]
            finally:
                resume.set()
                if not read.done():
                    read.cancel()
                await asyncio.gather(read, return_exceptions=True)
        finally:
            for client in clients:
                await client.aclose()


async def test_old_admin_tool_result_remains_hidden_when_style_owner_changes_during_read(
    postgres_collaboration_apps, fake_llm, monkeypatch,
):
    app, other_app = postgres_collaboration_apps
    async with AsyncExitStack() as stack:
        owner = await stack.enter_async_context(
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        )
        clients = []

        def make_client():
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
            clients.append(client)
            return client

        try:
            _, _, manager, _, manager_client, project, owner_style, _, _ = await _world(app, owner, make_client)
            config = await create_ai_config(manager_client)
            async with app.state.sessionmaker() as db:
                row = await db.get(Project, project["id"])
                unsafe = (await project_service.project_out(
                    db, row, actor_user_id=manager["id"],
                )).model_dump(mode="json")
                assert unsafe["default_style"]["name"] == "作者风格秘密名称"
                conversation = AgentConversation(user_id=manager["id"], project_id=project["id"])
                db.add(conversation)
                await db.flush()
                db.add_all([
                    AgentMessage(conversation_id=conversation.id, role="user", content="继续"),
                    AgentMessage(conversation_id=conversation.id, role="assistant", content="", tool_calls=[{
                        "id": "old", "name": "get_project_overview", "arguments": "{}",
                    }]),
                    AgentMessage(conversation_id=conversation.id, role="tool", tool_call_id="old",
                                 tool_name="get_project_overview", content=context.tool_result(unsafe)),
                ])
                await db.commit()
                conversation_id = conversation.id
            frozen, resume = asyncio.Event(), asyncio.Event()
            original = context._history_project_style

            async def pause_after_style_snapshot(db, *args):
                result = await original(db, *args)
                frozen.set()
                await resume.wait()
                return result

            monkeypatch.setattr(context, "_history_project_style", pause_after_style_snapshot)

            async def build():
                async with transactions.session(app.state.sessionmaker) as db:
                    return await context.build_messages(
                        db, await db.get(AgentConversation, conversation_id), await db.get(Project, project["id"]),
                        await db.get(AIConfig, config["id"]), fake_llm.factory("openai_compat", "test", None, 30),
                    )

            work = asyncio.create_task(build())
            try:
                await asyncio.wait_for(frozen.wait(), 10)
                async with other_app.state.sessionmaker() as db:
                    await db.execute(update(WritingStyle).where(WritingStyle.id == owner_style["id"]).values(
                        user_id=manager["id"], name="新持有人风格", description="新持有人描述",
                    ))
                    await db.commit()
                resume.set()
                messages, _ = await asyncio.wait_for(work, 10)
                text = json.dumps(messages, ensure_ascii=False)
                assert "作者风格秘密名称" not in text and "作者风格秘密描述" not in text
            finally:
                resume.set()
                if not work.done():
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)
        finally:
            for client in clients:
                await client.aclose()


async def test_style_changed_before_real_provider_send_stops_without_http(postgres_collaboration_apps):
    app, other_app = postgres_collaboration_apps
    async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
        app.state.settings.allow_private_ai_endpoints = True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as owner:
            # 执行身份绑定领取时的模型配置；端点须在冻结执行前确定，否则会被识别为配置已变化而提前拒绝。
            execution, config_id = await _setup_guarded_execution(app, owner, base_url=f"{url}/v1/")
            style = await _style(owner, "作者原风格", "原风格描述")
        async with app.state.sessionmaker() as db:
            (await db.get(Project, execution.project_id)).default_style_id = style["id"]
            await db.commit()
        async with app.state.sessionmaker() as db:
            config = await db.get(AIConfig, config_id)
            # 与生产 handler 相同经受控工厂构造，guard 才会进入风格复验而非提前拒绝来源。
            provider = await _provider(app, config, create_provider)
            guarded = await context.guard_agent_provider(
                db, await db.get(AgentConversation, execution.conversation_id),
                await db.get(Project, execution.project_id), config,
                provider, execution=execution,
            )
            await db.commit()
        async with other_app.state.sessionmaker() as db:
            (await db.get(WritingStyle, style["id"])).description = "更新后的描述"
            await db.commit()
        headers.set()
        body.set()
        with pytest.raises(VisibilityPolicyError):
            _ = [event async for event in guarded.chat_tools_stream(
                [{"role": "user", "content": "旧风格"}], "gpt-test", [],
            )]
        assert not requests and not received.is_set()
