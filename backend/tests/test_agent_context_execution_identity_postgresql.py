"""摘要真实模型发送和身份写回使用隔离 PostgreSQL 竞争验证。"""

import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, text, update

from app.ai.providers import create_provider
from app.core.errors import ConflictError
from app.models import AgentConversation, AIConfig, GenerationLog, Project, Task, User
from app.services.agent import context
from tests.test_agent_context_execution_identity import _provider, _setup
from tests.test_ai_visibility_dispatch import _server, _sse
from tests.test_collaboration_access_postgresql import _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def _summary(app, execution, config_id):
    async with app.state.sessionmaker() as db:
        config = await db.get(AIConfig, config_id)
        return await context.build_messages(
            db, await db.get(AgentConversation, execution.conversation_id),
            await db.get(Project, execution.project_id), config, await _provider(app, config, create_provider),
            task_id=execution.task_id, execution=execution,
        )


async def test_summary_send_lock_releases_at_headers_before_body_and_rechecks_actor(
    postgres_collaboration_apps, fake_llm, monkeypatch,
):
    owner_app, other_app = postgres_collaboration_apps
    async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
        owner_app.state.settings.allow_private_ai_endpoints = True
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver",
        ) as owner:
            execution, config_id = await _setup(owner_app, owner, base_url=f"{url}/v1/")
        send_pid = None
        original = context.authorize_send_actor

        async def record_pid(db, actor):
            nonlocal send_pid
            await original(db, actor)
            send_pid = await db.scalar(text("SELECT pg_backend_pid()"))

        monkeypatch.setattr(context, "authorize_send_actor", record_pid)
        work = asyncio.create_task(_summary(owner_app, execution, config_id))
        writer = None
        try:
            await asyncio.wait_for(received.wait(), 10)
            assert send_pid is not None

            async def disable():
                async with other_app.state.sessionmaker() as db:
                    await db.execute(update(User).where(User.id == execution.user_id).values(is_active=False))
                    await db.commit()

            writer = asyncio.create_task(disable())
            await asyncio.wait_for(_wait_for_blocker(other_app.state.sessionmaker, send_pid), 10)
            assert not writer.done()
            headers.set()
            await asyncio.wait_for(writer, 10)
            assert not work.done()  # 响应体仍未发送；锁只持到响应头。
            body.set()
            with pytest.raises(ConflictError):
                await work
            assert len(requests) == 1
            async with owner_app.state.sessionmaker() as db:
                conversation = await db.get(AgentConversation, execution.conversation_id)
                assert conversation.summary == "" and conversation.summarized_until is None
                assert await db.scalar(select(func.count(GenerationLog.id))) == 0
        finally:
            headers.set()
            body.set()
            for task in (work, writer):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (work, writer) if task is not None), return_exceptions=True)


async def test_summary_replaced_execution_sends_no_http(postgres_collaboration_apps, fake_llm):
    owner_app, _ = postgres_collaboration_apps
    async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
        owner_app.state.settings.allow_private_ai_endpoints = True
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver",
        ) as owner:
            execution, config_id = await _setup(owner_app, owner, base_url=f"{url}/v1/")
        async with owner_app.state.sessionmaker() as db:
            (await db.get(Task, execution.task_id)).execution_key = str(uuid4())
            await db.commit()
        headers.set()
        body.set()
        with pytest.raises(ConflictError):
            await _summary(owner_app, execution, config_id)
        assert not requests and not received.is_set()
        async with owner_app.state.sessionmaker() as db:
            assert await db.scalar(select(func.count(GenerationLog.id))) == 0
