"""MCP 真实发送依据持久总开关与固定部署默认值，而不是进程内可变 Settings。"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.main import create_app
from app.models import Base, Project, SystemSettings, User
from app.services import authorization_service
from app.services.ai_dispatch import VisibilityPolicyError
from app.services.authorization_service import AuthenticatedActor
from app.services.mcp import settings_gate
from tests.helpers import create_project, make_settings, register
from tests.test_system_settings import save_settings


async def _actor(app, client):
    user = await register(client)
    async with app.state.sessionmaker() as db:
        row = await db.get(User, user["id"])
        return AuthenticatedActor(row.id, row.token_version)


async def test_persisted_disable_wins_over_stale_process_settings(app, client, system_admin):
    actor = await _actor(app, client)
    async with app.state.sessionmaker() as db:
        await settings_gate.acquire_send(db, actor)
    await save_settings(system_admin, mcp_enabled=False)
    # 模拟另一实例尚未发布数据库中的新值。
    app.state.settings.mcp_enabled = True
    async with app.state.sessionmaker() as db:
        with pytest.raises(VisibilityPolicyError):
            await settings_gate.acquire_send(db, actor)


async def test_persisted_reenable_wins_over_stale_process_settings(app, client, system_admin):
    actor = await _actor(app, client)
    await save_settings(system_admin, mcp_enabled=False)
    await save_settings(system_admin, mcp_enabled=True)
    app.state.settings.mcp_enabled = False
    async with app.state.sessionmaker() as db:
        await settings_gate.acquire_send(db, actor)


async def test_absent_settings_row_uses_frozen_deployment_default(tmp_path, fake_llm):
    app = create_app(make_settings(tmp_path, mcp_enabled=False), fake_llm.factory, start_task_engine=False)
    async with app.state.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await authorization_service.initialize(app.state.sessionmaker)
    await app.state.system_settings.load()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            actor = await _actor(app, client)
        app.state.settings.mcp_enabled = True
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(SystemSettings.id)) is None
            with pytest.raises(VisibilityPolicyError):
                await settings_gate.acquire_send(db, actor)
    finally:
        await app.state.system_settings.close()
        await app.state.engine.dispose()


async def test_unconfirmed_runtime_cannot_send_even_when_db_is_enabled(app, client):
    actor = await _actor(app, client)
    app.state.system_settings.runtime.settings_confirmed = False
    try:
        async with app.state.sessionmaker() as db:
            with pytest.raises(VisibilityPolicyError):
                await settings_gate.acquire_send(db, actor)
    finally:
        app.state.system_settings.runtime.settings_confirmed = True


async def test_corrupt_persisted_setting_fails_closed(app, client):
    actor = await _actor(app, client)
    async with app.state.sessionmaker() as db:
        db.add(SystemSettings(id=1, revision=1, overrides={"mcp_enabled": "true"},
                              last_operation_id=str(uuid4()), updated_at=datetime.now(UTC)))
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(VisibilityPolicyError):
            await settings_gate.acquire_send(db, actor)


async def test_send_gate_never_flushes_unverified_pending_data(app, client):
    actor = await _actor(app, client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        pending = Project(user_id=actor.id, title="尚未验证的草稿")
        db.add(pending)
        with pytest.raises(VisibilityPolicyError):
            await settings_gate.acquire_send(db, actor)
        assert pending in db.new and pending.id is None
    async with app.state.sessionmaker() as db:
        assert list(await db.scalars(select(Project.id))) == [project["id"]]


async def test_sqlite_sender_first_blocks_setting_commit_until_lease_closes(app, client, system_admin, monkeypatch):
    actor = await _actor(app, client)
    started = asyncio.Event()
    original = authorization_service.require

    async def observed(db, user, permission, **kwargs):
        if permission == "settings.update":
            started.set()
        return await original(db, user, permission, **kwargs)

    monkeypatch.setattr(authorization_service, "require", observed)
    holder = app.state.sessionmaker()
    writer = None
    try:
        await settings_gate.acquire_send(holder, actor)
        writer = asyncio.create_task(system_admin.patch(
            "/api/v1/admin/system-settings", json={"revision": 0, "changes": {"mcp_enabled": False}},
        ))
        await asyncio.wait_for(started.wait(), 5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(writer), 0.2)
        await holder.close()
        result = await asyncio.wait_for(writer, 10)
        assert result.status_code == 200, result.text
        async with app.state.sessionmaker() as db:
            with pytest.raises(VisibilityPolicyError):
                await settings_gate.acquire_send(db, actor)
    finally:
        await holder.close()
        if writer is not None and not writer.done():
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)


async def test_sqlite_disable_first_prevents_waiting_send(app, client, system_admin, monkeypatch):
    actor = await _actor(app, client)
    acquired, release = asyncio.Event(), asyncio.Event()
    original = settings_gate.lock_for_update

    async def hold(db):
        await original(db)
        acquired.set()
        await release.wait()

    monkeypatch.setattr(settings_gate, "lock_for_update", hold)
    writer = asyncio.create_task(system_admin.patch(
        "/api/v1/admin/system-settings", json={"revision": 0, "changes": {"mcp_enabled": False}},
    ))
    sender = None

    async def send():
        async with app.state.sessionmaker() as db:
            await settings_gate.acquire_send(db, actor)

    try:
        await asyncio.wait_for(acquired.wait(), 5)
        sender = asyncio.create_task(send())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(sender), 0.2)
        release.set()
        assert (await asyncio.wait_for(writer, 10)).status_code == 200
        with pytest.raises(VisibilityPolicyError):
            await asyncio.wait_for(sender, 10)
    finally:
        release.set()
        for pending in (writer, sender):
            if pending is not None and not pending.done():
                pending.cancel()
        await asyncio.gather(*(pending for pending in (writer, sender) if pending is not None),
                             return_exceptions=True)
