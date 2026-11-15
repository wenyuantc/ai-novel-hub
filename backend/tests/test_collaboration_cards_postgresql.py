"""卡片导入在隔离 PostgreSQL 上按锁后授权串行化。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import select, text

from app.ai.providers import OpenAICompatProvider
from app.models import Character, ProjectCollaborator
from app.services import ai_dispatch, card_service, project_lifecycle
from app.services.ai_dispatch import guard_provider, read_visibility_stamp
from tests import pg_support
from tests.test_ai_visibility_dispatch import _collect, _server, _sse
from tests.test_collaboration_access_postgresql import _assert_project_row_locked, _setup, _wait_for_blocker
from tests.test_collaboration_rowlock_postgresql import _assert_blocked_on_project_row


@pytest.fixture
async def postgres_collaboration_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    """专用测试库避免 PostgreSQL advisory lock 在并行 schema 间共享。"""
    url = pg_support.collab_test_url("AINOVEL_COLLAB_CARDS_POSTGRES_TEST_URL", "collab_cards", purpose="卡片测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2,
    ) as apps:
        yield apps


async def _body(owner, base, version):
    if version == 1:
        return {"data": {"format": "ainovel.cards", "version": 1, "characters": [{"name": "导入新角色"}]}}
    created = await owner.post(f"{base}/characters", json={"name": "卡片来源"})
    assert created.status_code == 201, created.text
    export = await owner.post(f"{base}/cards/export", json={"character_ids": [created.json()["id"]]})
    assert export.status_code == 200, export.text
    data = export.json()
    data["characters"][0]["name"] = "导入新角色"
    preview = await owner.post(f"{base}/cards/import/preview", json={"data": data})
    assert preview.status_code == 200, preview.text
    expected = preview.json()
    return {"data": data, "expected_fields_revision": expected["fields_revision"],
            "expected_categories_revision": expected["categories_revision"],
            "expected_ai_visibility_revision": expected["ai_visibility_revision"]}


@pytest.mark.parametrize("version", [1, 6])
@pytest.mark.parametrize("revoke", [True, False])
async def test_card_import_rechecks_after_revoke_or_downgrade(postgres_collaboration_apps, version, revoke):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        body = await _body(owner, base, version)
        pending = None
        try:
            async with owner_app.state.sessionmaker() as holder:
                await project_lifecycle.lock_active(holder, project["id"])
                member = await holder.scalar(select(ProjectCollaborator).where(
                    ProjectCollaborator.project_id == project["id"], ProjectCollaborator.user_id == user["id"],
                ))
                if revoke:
                    member.status = "revoked"
                else:
                    member.role = "viewer"
                member.revision += 1
                await holder.flush()
                holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
                pending = asyncio.create_task(guest.post(f"{base}/cards/import", json=body))
                await asyncio.wait_for(_wait_for_blocker(guest_app.state.sessionmaker, holder_pid), 6)
                await _assert_blocked_on_project_row(guest_app.state.sessionmaker, holder_pid)
                await holder.commit()
            response = await asyncio.wait_for(pending, 6)
            assert response.status_code == (404 if revoke else 403), response.text
            async with owner_app.state.sessionmaker() as db:
                assert await db.scalar(select(Character.id).where(
                    Character.project_id == project["id"], Character.name == "导入新角色",
                )) is None
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending


@pytest.mark.parametrize("version", [1, 6])
async def test_card_import_commits_before_waiting_revoke(postgres_collaboration_apps, monkeypatch, version):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, user, base = await _setup(owner, guest)
        body = await _body(owner, base, version)
        reached, release = asyncio.Event(), asyncio.Event()
        holder = {}
        original = card_service._visible_series_names

        async def gated(db, actor_id, project_id, names):
            value = await original(db, actor_id, project_id, names)
            if actor_id == user["id"]:
                holder["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                reached.set()
                await release.wait()
            return value

        monkeypatch.setattr(card_service, "_visible_series_names", gated)
        write = asyncio.create_task(guest.post(f"{base}/cards/import", json=body))
        revoke = None
        try:
            await asyncio.wait_for(reached.wait(), 6)
            await _assert_project_row_locked(owner_app.state.sessionmaker, project["id"])
            revoke = asyncio.create_task(owner.delete(
                f"{base}/collaborators/{user['id']}", params={"expected_revision": 2},
            ))
            await asyncio.wait_for(_wait_for_blocker(owner_app.state.sessionmaker, holder["pid"]), 6)
            release.set()
            assert (await asyncio.wait_for(write, 6)).status_code == 200
            assert (await asyncio.wait_for(revoke, 6)).status_code == 204
            async with owner_app.state.sessionmaker() as db:
                assert await db.scalar(select(Character.id).where(
                    Character.project_id == project["id"], Character.name == "导入新角色",
                )) is not None
        finally:
            release.set()
            for task in (write, revoke):
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task


@pytest.mark.parametrize("version", [1, 6])
async def test_card_import_waits_for_actual_model_send(postgres_collaboration_apps, monkeypatch, version):
    owner_app, guest_app = postgres_collaboration_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
    ):
        project, _user, base = await _setup(owner, guest)
        body = await _body(owner, base, version)
        async with owner_app.state.sessionmaker() as db:
            stamp = await read_visibility_stamp(db, project["id"])
        monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.2)
        async with _server(payload=_sse("openai")) as (url, received, headers, response_body, requests):
            provider = guard_provider(
                OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, owner_app.state.sessionmaker,
            )
            stream = provider.generate_stream([{"role": "user", "content": "设定"}], "test")
            sending = asyncio.create_task(_collect(stream))
            try:
                await asyncio.wait_for(received.wait(), 5)
                response = await guest.post(f"{base}/cards/import", json=body)
                assert response.status_code == 409 and response.json()["code"] == "ai_visibility_busy", response.text
                async with owner_app.state.sessionmaker() as db:
                    assert await db.scalar(select(Character.id).where(
                        Character.project_id == project["id"], Character.name == "导入新角色",
                    )) is None
                headers.set()
                response_body.set()
                assert await asyncio.wait_for(sending, 5) == ["ok"]
                saved = await guest.post(f"{base}/cards/import", json=body)
                assert saved.status_code == 200, saved.text
                assert len(requests) == 1
            finally:
                headers.set()
                response_body.set()
                if not sending.done():
                    sending.cancel()
                await asyncio.gather(sending, return_exceptions=True)
