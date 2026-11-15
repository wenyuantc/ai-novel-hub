"""作者实际 HTTP 发送与人工复位的 PostgreSQL 跨连接竞争。"""

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from sqlalchemy import text

from app.ai.providers import create_provider
from app.models import Chapter, Task
from app.services import chapter_claim, chapter_service
from tests.helpers import create_ai_config, create_project, register, wait_task
from tests.test_ai_visibility_dispatch import _server, _sse
from tests.test_chapter_claim_author_wiring import _seed_retrieval
from tests.test_collaboration_access_postgresql import postgres_collaboration_apps as _postgres_fixture

postgres_author_apps = _postgres_fixture


async def _wait_for_blocked_connection(maker, pid):
    while True:
        async with maker() as db:
            blocked = await db.scalar(text("SELECT COALESCE(array_length(pg_blocking_pids(:pid), 1), 0) > 0"),
                                      {"pid": pid})
        if blocked:
            return
        await asyncio.sleep(0.01)


async def _new_batch_claim(maker, actor_id, project_id, chapter_id):
    queued_at, execution = datetime(2026, 4, 2, tzinfo=UTC), str(uuid4())
    async with maker() as db:
        batch = Task(user_id=actor_id, project_id=project_id, kind="chapter.batch",
                     resource_key=f"batch:{project_id}", status="running", queued_at=queued_at,
                     execution_key=execution, input={"request": {"project_id": project_id},
                     "chapter_ids": [chapter_id]}, checkpoint={"current": None})
        db.add(batch)
        await db.flush()
        claim = await chapter_claim.claim_batch_chapter(
            db, task_id=batch.id, task_key=batch.statistics_key, actor_id=actor_id, project_id=project_id,
            chapter_id=chapter_id, queued_at=queued_at, execution_key=execution, grant_identity=None,
        )
        await db.commit()
        return claim


async def test_author_real_send_reset_and_new_claim_fence_old_continuation(postgres_author_apps, monkeypatch):
    app, _ = postgres_author_apps
    app.state.provider_factory = create_provider
    app.state.task_engine.ctx.factory = create_provider
    app.state.task_engine.ctx.settings.allow_private_ai_endpoints = True
    maker = app.state.sessionmaker
    entered = asyncio.Event()
    waiter = {}
    original = chapter_service.lock_manual_project_visibility

    async def observe_reset(db, user_id, project_id):
        waiter["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
        entered.set()
        return await original(db, user_id, project_id)

    monkeypatch.setattr(chapter_service, "lock_manual_project_visibility", observe_reset)
    await app.state.task_engine.start()
    patch = None
    try:
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client,
            _server(payload=_sse("openai")) as (url, received, headers, body, requests),
        ):
            actor = await register(client, "pg-real-chapter-author")
            project = await create_project(client)
            await create_ai_config(client, base_url=f"{url}/v1/")
            chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
                "title": "实发章", "content": "原正文",
            })).json()
            created = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={
                "target_words": 300, "auto_continue": True,
            })
            assert created.status_code == 202, created.text
            task = created.json()
            await asyncio.wait_for(received.wait(), 10)
            assert len(requests) == 1
            patch = asyncio.create_task(client.patch(
                f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}", json={"status": "done"},
            ))
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.wait_for(_wait_for_blocked_connection(maker, waiter["pid"]), 5)
            assert not patch.done()  # 模型响应头尚未返回，作者复位必须等待同一物理 lease。
            headers.set()
            response = await asyncio.wait_for(patch, 5)
            assert response.status_code == 200, response.text
            assert not body.is_set()  # 响应体未完成，复位不应等待整个模型流。

            new_claim = await _new_batch_claim(maker, actor["id"], project["id"], chapter["id"])
            body.set()
            ended = await wait_task(client, task, within=10)
            assert ended["status"] == "failed"
            assert len(requests) == 1  # 原首轮可返回，但复位后自动续写不能再实际发送。
            async with maker() as db:
                current = await db.get(Chapter, chapter["id"])
                assert (current.status, current.content, current.generation_claim_key) == (
                    "generating", "原正文", new_claim.claim_key,
                )
    finally:
        if patch is not None and not patch.done():
            patch.cancel()
            with suppress(asyncio.CancelledError):
                await patch
        await app.state.task_engine.stop()


async def test_author_real_embedding_reset_fences_later_text_send(postgres_author_apps, monkeypatch):
    app, _ = postgres_author_apps
    app.state.provider_factory = create_provider
    app.state.task_engine.ctx.factory = create_provider
    app.state.task_engine.ctx.settings.allow_private_ai_endpoints = True
    maker = app.state.sessionmaker
    entered = asyncio.Event()
    waiter = {}
    original = chapter_service.lock_manual_project_visibility

    async def observe_reset(db, user_id, project_id):
        waiter["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
        entered.set()
        return await original(db, user_id, project_id)

    monkeypatch.setattr(chapter_service, "lock_manual_project_visibility", observe_reset)
    vectors = json.dumps({"object": "list", "model": "embedding-pg", "data": [
        {"index": 0, "embedding": [0.1] * 64},
    ]}).encode()
    await app.state.task_engine.start()
    patch = None
    try:
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client,
            _server(payload=_sse("openai")) as (text_url, text_received, text_headers, text_body, text_requests),
            _server(payload=vectors, mime="application/json") as (vector_url, received, headers, body, requests),
        ):
            text_headers.set()
            text_body.set()
            actor = await register(client, "pg-real-vector-author")
            project = await create_project(client)
            await create_ai_config(client, base_url=f"{text_url}/v1/")
            await create_ai_config(client, name="向量", model="embedding-pg", model_type="embedding",
                                   base_url=f"{vector_url}/v1/", is_default=True)
            chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
                "title": "星舰启航", "content": "原正文",
            })).json()
            await _seed_retrieval(app, project["id"], "embedding-pg", 64)
            created = await client.post(f"/api/v1/chapters/{chapter['id']}/generate")
            assert created.status_code == 202, created.text
            task = created.json()
            await asyncio.wait_for(received.wait(), 10)
            assert len(requests) == 1 and not text_received.is_set()
            patch = asyncio.create_task(client.patch(
                f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}", json={"status": "done"},
            ))
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.wait_for(_wait_for_blocked_connection(maker, waiter["pid"]), 5)
            assert not patch.done()
            headers.set()
            response = await asyncio.wait_for(patch, 5)
            assert response.status_code == 200, response.text
            assert not body.is_set()
            new_claim = await _new_batch_claim(maker, actor["id"], project["id"], chapter["id"])
            body.set()
            ended = await wait_task(client, task, within=10)
            assert ended["status"] == "failed" and len(requests) == 1
            assert not text_received.is_set() and text_requests == []
            async with maker() as db:
                current = await db.get(Chapter, chapter["id"])
                assert (current.status, current.content, current.generation_claim_key) == (
                    "generating", "原正文", new_claim.claim_key,
                )
    finally:
        if patch is not None and not patch.done():
            patch.cancel()
            with suppress(asyncio.CancelledError):
                await patch
        await app.state.task_engine.stop()
