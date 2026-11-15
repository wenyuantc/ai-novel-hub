"""真实 PostgreSQL/HTTP 校验 Agent 原令牌撤销与摘要写回屏障。"""

import asyncio

import httpx
import pytest
from sqlalchemy import func, select

from app.ai.providers import create_provider
from app.models import AgentConversation, AIConfig, GenerationLog, Project, Task, User
from app.services.agent import context
from app.services.ai_dispatch import VisibilityPolicyError
from tests.test_agent_context_execution_identity import _provider, _setup
from tests.test_ai_visibility_dispatch import _server, _sse

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


@pytest.mark.parametrize("changed", ["token_revoked", "task_version_missing"])
async def test_original_actor_version_change_blocks_real_model_send(postgres_collaboration_apps, changed):
    app, other_app = postgres_collaboration_apps
    async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
        app.state.settings.allow_private_ai_endpoints = True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            execution, config_id = await _setup(app, client, base_url=f"{url}/v1/")
        async with app.state.sessionmaker() as db:
            config = await db.get(AIConfig, config_id)
            provider = await _provider(app, config, create_provider)
            guarded = await context.guard_agent_provider(
                db, await db.get(AgentConversation, execution.conversation_id),
                await db.get(Project, execution.project_id), config,
                provider, execution=execution,
            )
            await db.commit()
        async with other_app.state.sessionmaker() as db:
            if changed == "token_revoked":
                (await db.get(User, execution.user_id)).token_version += 1
            else:
                task = await db.get(Task, execution.task_id)
                data = dict(task.input)
                data.pop("_agent_actor_token_version")
                task.input = data
            await db.commit()
        headers.set()
        body.set()
        with pytest.raises(VisibilityPolicyError):
            _ = [item async for item in guarded.chat_tools_stream(
                [{"role": "user", "content": "原请求"}], "gpt-test", [],
            )]
        assert not requests and not received.is_set()


async def test_revoked_actor_after_headers_does_not_persist_summary(postgres_collaboration_apps):
    app, other_app = postgres_collaboration_apps
    async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
        app.state.settings.allow_private_ai_endpoints = True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            execution, config_id = await _setup(app, client, base_url=f"{url}/v1/")

        async def summarize():
            async with app.state.sessionmaker() as db:
                config = await db.get(AIConfig, config_id)
                return await context.build_messages(
                    db, await db.get(AgentConversation, execution.conversation_id),
                    await db.get(Project, execution.project_id), config, await _provider(app, config, create_provider),
                    task_id=execution.task_id, execution=execution,
                )

        work = asyncio.create_task(summarize())
        try:
            await asyncio.wait_for(received.wait(), 10)
            headers.set()
            async with other_app.state.sessionmaker() as db:
                (await db.get(User, execution.user_id)).token_version += 1
                await asyncio.wait_for(db.commit(), 10)
            assert not work.done()  # 响应体尚未发送；已发出的请求不能撤回。
            body.set()
            with pytest.raises(VisibilityPolicyError):
                await asyncio.wait_for(work, 10)
            assert len(requests) == 1
            async with app.state.sessionmaker() as db:
                conversation = await db.get(AgentConversation, execution.conversation_id)
                assert conversation.summary == "" and conversation.summarized_until is None
                assert await db.scalar(select(func.count(GenerationLog.id))) == 0
        finally:
            headers.set()
            body.set()
            if not work.done():
                work.cancel()
            await asyncio.gather(work, return_exceptions=True)
