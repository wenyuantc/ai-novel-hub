"""拆书阶段提交与旧认证/执行轮次变化的真实 PostgreSQL 竞争。"""

import asyncio
from contextlib import suppress
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text, update

from app.ai.providers import create_provider
from app.models import Character, Project, Task
from app.services import collaboration_ai_guard
from app.services.book_import import reverse
from tests.test_ai_visibility_dispatch import _server, _sse
from tests.test_book_import import _setup, _start
from tests.test_book_import_actor_identity import _script
from tests.test_collaboration_access_postgresql import _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


@pytest.mark.parametrize("change", ["token", "execution"])
async def test_apply_commit_serializes_with_original_identity_change(
    postgres_collaboration_apps, fake_llm, monkeypatch, change,
):
    owner_app, other_app = postgres_collaboration_apps
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    entered, release = asyncio.Event(), asyncio.Event()
    holder = {}
    original = reverse.apply_final

    async def hold_while_locked(db, project, final, checkpoint):
        holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
        entered.set()
        await release.wait()
        return await original(db, project, final, checkpoint)

    monkeypatch.setattr(reverse, "apply_final", hold_while_locked)
    await owner_app.state.task_engine.start()
    try:
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://testserver") as other,
        ):
            rec = await _setup(owner)
            login = await other.post("/api/v1/auth/login", json={"username": "alice", "password": "secret123"})
            assert login.status_code == 200, login.text
            _script(fake_llm)
            response = await _start(owner, rec, deep_analyze=True, extract_style=False)
            assert response.status_code == 202, response.text
            task_id, project_id = response.json()["task"]["id"], response.json()["project_id"]
            pending = None

            async def replace_execution():
                async with other_app.state.sessionmaker() as db:
                    await db.execute(update(Task).where(Task.id == task_id).values(execution_key=str(uuid4())))
                    await db.commit()

            try:
                await asyncio.wait_for(entered.wait(), 15)
                pending = asyncio.create_task(
                    other.post("/api/v1/auth/change-password", json={
                        "old_password": "secret123", "new_password": "new-secret123",
                    }) if change == "token" else replace_execution()
                )
                await asyncio.wait_for(_wait_for_blocker(other_app.state.sessionmaker, holder["pid"]), 15)
                assert not pending.done()
                release.set()
                changed = await asyncio.wait_for(pending, 15)
                if change == "token":
                    assert changed.status_code == 204, changed.text
                running = owner_app.state.task_engine._running.get(task_id)
                if running is not None:
                    done, _ = await asyncio.wait({running}, timeout=15)
                    assert running in done
                async with other_app.state.sessionmaker() as db:
                    task = await db.get(Task, task_id)
                    project = await db.get(Project, project_id)
                    assert project.synopsis == "少年林远踏上修行之路。"
                    assert await db.scalar(select(Character.id).where(Character.project_id == project_id).limit(1))
                    assert task.checkpoint["phase"] == "style"
                    assert (await db.scalars(select(Task).where(
                        Task.project_id == project_id, Task.kind == "chapter.analyze",
                    ))).all() == []
                assert len(fake_llm.calls) == 5
            finally:
                release.set()
                if pending is not None and not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending
    finally:
        release.set()
        await owner_app.state.task_engine.stop()


async def test_password_change_waits_for_real_send_headers_then_blocks_retry(
    postgres_collaboration_apps, monkeypatch,
):
    owner_app, other_app = postgres_collaboration_apps
    owner_app.state.settings.allow_private_ai_endpoints = True
    owner_app.state.task_engine.ctx.factory = create_provider
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    monkeypatch.setattr(reverse, "RETRY_DELAYS", (0.0, 0.0))
    verified, completed = asyncio.Event(), asyncio.Event()
    holder = {}
    original_verify = collaboration_ai_guard.SendGuard.verify
    original_call = reverse._call

    async def observe_verify(guard, db):
        await original_verify(guard, db)
        holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
        verified.set()

    async def observe_call(*args, **kwargs):
        try:
            async for event in original_call(*args, **kwargs):
                yield event
        finally:
            completed.set()

    monkeypatch.setattr(collaboration_ai_guard.SendGuard, "verify", observe_verify)
    monkeypatch.setattr(reverse, "_call", observe_call)
    await owner_app.state.task_engine.start()
    try:
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://testserver") as other,
        ):
            rec = await _setup(owner)
            login = await other.post("/api/v1/auth/login", json={"username": "alice", "password": "secret123"})
            assert login.status_code == 200, login.text
            async with _server(payload=_sse("openai")) as (url, received, headers, body, calls):
                config = (await owner.get("/api/v1/ai-configs")).json()[0]
                updated = await owner.patch(f"/api/v1/ai-configs/{config['id']}", json={"base_url": f"{url}/v1/"})
                assert updated.status_code == 200, updated.text
                response = await _start(owner, rec, deep_analyze=True, extract_style=False)
                assert response.status_code == 202, response.text
                pending = None
                try:
                    await asyncio.wait_for(verified.wait(), 15)
                    await asyncio.wait_for(received.wait(), 15)
                    pending = asyncio.create_task(other.post("/api/v1/auth/change-password", json={
                        "old_password": "secret123", "new_password": "new-secret123",
                    }))
                    await asyncio.wait_for(_wait_for_blocker(other_app.state.sessionmaker, holder["pid"]), 15)
                    assert not pending.done()
                    headers.set()
                    assert (await asyncio.wait_for(pending, 15)).status_code == 204
                    body.set()
                    await asyncio.wait_for(completed.wait(), 15)
                    assert len(calls) == 1
                    async with other_app.state.sessionmaker() as db:
                        assert (await db.scalars(select(Task).where(
                            Task.project_id == response.json()["project_id"], Task.kind == "chapter.analyze",
                        ))).all() == []
                finally:
                    headers.set()
                    body.set()
                    if pending is not None and not pending.done():
                        pending.cancel()
                        with suppress(asyncio.CancelledError):
                            await pending
    finally:
        await owner_app.state.task_engine.stop()
