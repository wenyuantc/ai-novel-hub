"""向导真实 SDK/TCP 出站与 PostgreSQL 双连接身份顺序。"""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers import create_provider
from app.models import Character, Task, User
from app.services import collaboration_ai_guard, wizard_service
from tests.helpers import create_ai_config, create_project, register, wait_until
from tests.test_agent_providers import openai_chunk
from tests.test_ai_visibility_dispatch import _server
from tests.test_collaboration_access_postgresql import _wait_for_blocker

pytest_plugins = ("tests.test_collaboration_access_postgresql",)


def _payload():
    chunk = openai_chunk({"content": '[{"name":"模型新角色"}]'}, "stop")
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode()


async def _start_real_owner(app, owner, url):
    user = await register(owner, "wizard-real-owner")
    app.state.task_engine.ctx.factory = create_provider
    app.state.task_engine.ctx.settings.allow_private_ai_endpoints = True
    await create_ai_config(owner, base_url=f"{url}/v1/", model="wizard-real")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    created = await owner.post(f"{base}/characters", json={"name": "原角色"})
    assert created.status_code == 201, created.text
    await app.state.task_engine.start()
    return user, project, base


async def _names(app, project_id):
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(Character.name).where(Character.project_id == project_id)))


async def test_replaced_task_before_dispatch_sends_zero_real_http(postgres_collaboration_apps, monkeypatch):
    owner_app, other_app = postgres_collaboration_apps
    async with _server(payload=_payload()) as (url, received, headers, body, requests):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver",
        ) as owner:
            actor, project, base = await _start_real_owner(owner_app, owner, url)
            ready, release = asyncio.Event(), asyncio.Event()
            original = wizard_service.build_wizard_job

            async def pause_before_send(*args, **kwargs):
                job = await original(*args, **kwargs)
                ready.set()
                await release.wait()
                return job

            monkeypatch.setattr(wizard_service, "build_wizard_job", pause_before_send)
            response = await owner.post(f"{base}/wizard/characters", json={})
            assert response.status_code == 202, response.text
            task_id = response.json()["id"]
            try:
                await asyncio.wait_for(ready.wait(), 15)
                running = owner_app.state.task_engine._running[task_id]
                async with other_app.state.sessionmaker() as db:
                    await db.execute(delete(Task).where(Task.id == task_id))
                    db.add(Task(id=task_id, user_id=actor["id"], project_id=project["id"], kind="wizard.characters",
                                status="running", execution_key=str(uuid4()), input={}))
                    await db.commit()
                release.set()
                await asyncio.wait_for(asyncio.shield(running), 15)
                assert not received.is_set() and requests == []
                assert await _names(owner_app, project["id"]) == ["原角色"]
            finally:
                release.set()
                headers.set()
                body.set()
                await owner_app.state.task_engine.stop()


async def test_real_send_lock_blocks_token_change_until_headers_and_prevents_write(
    postgres_collaboration_apps, monkeypatch,
):
    owner_app, other_app = postgres_collaboration_apps
    async with _server(payload=_payload()) as (url, received, headers, body, requests):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver",
        ) as owner:
            actor, project, base = await _start_real_owner(owner_app, owner, url)
            holder = {}
            original = collaboration_ai_guard.SendGuard.authorize

            async def remember_pid(guard, db):
                await original(guard, db)
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))

            monkeypatch.setattr(collaboration_ai_guard.SendGuard, "authorize", remember_pid)
            response = await owner.post(f"{base}/wizard/characters", json={})
            assert response.status_code == 202, response.text
            task_id = response.json()["id"]
            writer = None
            try:
                await asyncio.wait_for(received.wait(), 15)
                assert len(requests) == 1 and holder.get("pid") is not None
                running = owner_app.state.task_engine._running[task_id]

                async def rotate_token():
                    async with other_app.state.sessionmaker() as db:
                        await db.execute(update(User).where(User.id == actor["id"]).values(
                            token_version=User.token_version + 1,
                        ))
                        await db.commit()

                writer = asyncio.create_task(rotate_token())
                await asyncio.wait_for(_wait_for_blocker(other_app.state.sessionmaker, holder["pid"]), 15)
                assert not writer.done()
                headers.set()
                await asyncio.wait_for(writer, 15)
                assert not running.done()
                body.set()
                await asyncio.wait_for(asyncio.shield(running), 15)
                assert await _names(owner_app, project["id"]) == ["原角色"]
            finally:
                headers.set()
                body.set()
                if writer is not None and not writer.done():
                    writer.cancel()
                    await asyncio.gather(writer, return_exceptions=True)
                await owner_app.state.task_engine.stop()


@pytest.mark.parametrize("outcome", ["committed", "rolled_back"])
async def test_real_http_unknown_commit_ack_never_blindly_retries(postgres_collaboration_apps, monkeypatch, outcome):
    owner_app, _ = postgres_collaboration_apps
    async with _server(payload=_payload()) as (url, received, headers, body, requests):
        headers.set()
        body.set()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver",
        ) as owner:
            _, project, base = await _start_real_owner(owner_app, owner, url)
            original_commit, lost = AsyncSession.commit, False

            async def lose_ack(db):
                nonlocal lost
                if not lost and any(
                    isinstance(row, Character) and row.name == "模型新角色" for row in db.identity_map.values()
                ):
                    lost = True
                    if outcome == "committed":
                        await original_commit(db)
                    else:
                        await db.rollback()
                    raise RuntimeError("模拟 PostgreSQL 提交确认丢失")
                await original_commit(db)

            monkeypatch.setattr(AsyncSession, "commit", lose_ack)
            response = await owner.post(f"{base}/wizard/characters", json={})
            assert response.status_code == 202, response.text
            ref = response.json()
            try:
                await wait_until(lambda: lost and ref["id"] not in owner_app.state.task_engine._running, within=30)
                assert received.is_set() and len(requests) == 1
                detail = await owner.get(f"/api/v1/tasks/{ref['id']}", params={"task_key": ref["task_key"]})
                assert detail.status_code == 200 and detail.json()["checkpoint"] is None
                retry = await owner.post(f"/api/v1/tasks/{ref['id']}/retry", params={"task_key": ref["task_key"]})
                assert retry.status_code == 409, retry.text
                assert await _names(owner_app, project["id"]) == (
                    ["模型新角色"] if outcome == "committed" else ["原角色"]
                )
            finally:
                await owner_app.state.task_engine.stop()
