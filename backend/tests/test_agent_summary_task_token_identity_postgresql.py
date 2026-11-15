"""真实模型连接与 PostgreSQL 写事务之间，摘要不得接受变更后的任务认证代次。"""

import asyncio

import httpx
import pytest
from sqlalchemy import func, select, text, update

from app.core.errors import ConflictError
from app.models import AgentConversation, GenerationLog, Task
from app.services.agent import context
from tests.test_agent_context_execution_identity import _setup
from tests.test_agent_context_execution_identity_postgresql import _summary
from tests.test_ai_visibility_dispatch import _server, _sse
from tests.test_collaboration_access_postgresql import _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


async def test_task_token_changes_after_headers_do_not_commit_summary(postgres_collaboration_apps, monkeypatch):
    owner_app, other_app = postgres_collaboration_apps
    async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
        owner_app.state.settings.allow_private_ai_endpoints = True
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver",
        ) as owner:
            execution, config_id = await _setup(owner_app, owner, base_url=f"{url}/v1/")
        send_pid = None
        authorize = context.authorize_send_actor

        async def capture_send_pid(db, actor):
            nonlocal send_pid
            await authorize(db, actor)
            send_pid = await db.scalar(text("SELECT pg_backend_pid()"))

        monkeypatch.setattr(context, "authorize_send_actor", capture_send_pid)
        work = asyncio.create_task(_summary(owner_app, execution, config_id))
        writer = None
        try:
            await asyncio.wait_for(received.wait(), 10)
            assert send_pid is not None

            async def change_task_version():
                async with other_app.state.sessionmaker() as db:
                    task = await db.get(Task, execution.task_id)
                    data = {**task.input, "_task_actor_token_version": execution.actor_token_version + 1}
                    await db.execute(update(Task).where(Task.id == execution.task_id).values(input=data))
                    await db.commit()

            writer = asyncio.create_task(change_task_version())
            await asyncio.wait_for(_wait_for_blocker(other_app.state.sessionmaker, send_pid), 10)
            assert not writer.done()
            headers.set()
            await asyncio.wait_for(writer, 10)
            assert not work.done()
            body.set()
            with pytest.raises(ConflictError):
                await asyncio.wait_for(work, 10)
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
