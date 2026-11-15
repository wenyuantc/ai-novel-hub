"""PostgreSQL 下大纲任务真实出站与原认证撤销的交错。"""

import asyncio
import json
from contextlib import suppress

import httpx
from sqlalchemy import text, update

from app.ai.providers import create_provider
from app.models import Task, User
from app.services import collaboration_ai_guard
from tests.helpers import create_ai_config, create_project, register, wait_until
from tests.test_agent_providers import openai_chunk
from tests.test_ai_visibility_dispatch import _server
from tests.test_collaboration_access_postgresql import _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_retrieval_postgresql",)


async def test_outline_real_send_holds_original_actor_until_headers_then_discards_revoked_output(
    postgres_retrieval_apps, monkeypatch,
):
    app, other_app = postgres_retrieval_apps
    monkeypatch.setattr(app.state.task_engine.ctx.settings, "allow_private_ai_endpoints", True)
    monkeypatch.setattr(app.state.task_engine.ctx, "factory", create_provider)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    send_pid = None
    original = collaboration_ai_guard.SendGuard._verify_locked

    async def capture_pid(self, db, *, preflight):
        nonlocal send_pid
        await original(self, db, preflight=preflight)
        send_pid = await db.scalar(text("SELECT pg_backend_pid()"))

    monkeypatch.setattr(collaboration_ai_guard.SendGuard, "_verify_locked", capture_pid)
    content = json.dumps({"outlines": [{"title": "撤权后的秘密", "summary": "不可交付"}]}, ensure_ascii=False)
    payload = (f"data: {json.dumps(openai_chunk({'content': content}, 'stop'))}\n\n"
               "data: [DONE]\n\n").encode()
    await app.state.task_engine.start()
    try:
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client,
            _server(payload=payload) as (url, received, headers, body, requests),
        ):
            user = await register(client, "outline-pg-original-actor")
            project = await create_project(client)
            await create_ai_config(client, base_url=f"{url}/v1/", model="original-model")
            created = await client.post(
                f"/api/v1/projects/{project['id']}/outlines/continue", json={"count": 1},
            )
            assert created.status_code == 202, created.text
            task_id = created.json()["id"]
            await asyncio.wait_for(received.wait(), 8)
            assert send_pid is not None and len(requests) == 1

            async def revoke_actor():
                async with other_app.state.sessionmaker() as db:
                    await db.execute(update(User).where(User.id == user["id"]).values(
                        token_version=User.token_version + 1,
                    ))
                    await db.commit()

            revoked = asyncio.create_task(revoke_actor())
            try:
                await asyncio.wait_for(_wait_for_blocker(other_app.state.sessionmaker, send_pid), 8)
                assert not revoked.done()
                headers.set()
                await asyncio.wait_for(revoked, 8)
                body.set()
                await wait_until(lambda: task_id not in app.state.task_engine._running, within=10)
                async with app.state.sessionmaker() as db:
                    task = await db.get(Task, task_id)
                    assert task.result is None and "撤权后的秘密" not in task.output_text
                hub = app.state.task_engine.hubs.get(task_id)
                assert hub is None or "撤权后的秘密" not in hub.text
                assert len(requests) == 1
            finally:
                headers.set()
                body.set()
                if not revoked.done():
                    revoked.cancel()
                    with suppress(asyncio.CancelledError):
                        await revoked
    finally:
        await app.state.task_engine.stop()
