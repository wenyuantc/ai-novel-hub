"""两应用共库时的注册门禁、部署回退值及普通设置传播。"""

import asyncio
import logging
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models import User
from app.services import api_key_service, authorization_service
from app.services.task_engine.engine import TaskEngine
from tests.conftest import _new_client
from tests.helpers import Gate, create_ai_config, create_project, register, stream_task, wait_until
from tests.schema_helpers import isolated_worker_database
from tests.test_system_settings import URL, save_settings


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


async def test_closed_registration_wins_before_preselected_peer_request(
    app, second_app, system_admin, monkeypatch,
):
    first_at_gate, allow_claim = asyncio.Event(), asyncio.Event()
    original_gate = authorization_service.gate

    async def observe(db):
        if asyncio.current_task().get_name() == "peer-register":
            first_at_gate.set()
            await allow_claim.wait()
        await original_gate(db)

    monkeypatch.setattr(authorization_service, "gate", observe)
    async with _new_client(second_app) as peer:
        pending = asyncio.create_task(peer.post(
            "/api/v1/auth/register", json={"username": "racing_user", "password": "secret123"},
        ), name="peer-register")
        try:
            await asyncio.wait_for(first_at_gate.wait(), 10)
            await save_settings(system_admin, registration_enabled=False, mcp_enabled=False)
        finally:
            allow_claim.set()
            result = await pending
        assert result.status_code == 403, result.text
        assert "set-cookie" not in result.headers
        public = await peer.get("/api/v1/auth/config")
        assert public.json() == {"registration_enabled": False, "mcp_enabled": False}
        assert public.headers["cache-control"] == "no-store"
        async with app.state.sessionmaker() as db:
            assert await db.scalar(select(User.id).where(User.username == "racing_user")) is None


async def test_peer_started_after_override_recovers_original_deployment(app, system_admin, peer_app_factory):
    await save_settings(system_admin, registration_enabled=False)
    peer = await peer_app_factory()
    async with _new_client(peer) as client:
        creds = {"username": "new_peer_user", "password": "secret123"}
        assert (await client.post("/api/v1/auth/register", json=creds)).status_code == 403
        await save_settings(system_admin, registration_enabled=None)
        assert (await client.post("/api/v1/auth/register", json=creds)).status_code == 201


async def test_idle_peer_refreshes_settings_and_keeps_api_key_minute_count(
    app, client, peer_app_factory, system_admin, monkeypatch,
):
    period = 1.0
    monkeypatch.setattr(TaskEngine, "poll_interval", period)
    peer = await peer_app_factory()
    committed, published = {}, {}
    for manager, observation in ((app.state.system_settings, committed), (peer.state.system_settings, published)):
        original = manager._publish

        def observe(revision, overrides, updated_at, *, original=original, observation=observation):
            observation[revision] = time.monotonic()
            return original(revision, overrides, updated_at)

        monkeypatch.setattr(manager, "_publish", observe)
    await register(client)
    key = (await client.post("/api/v1/api-keys", json={"name": "共享计数", "all_projects": True})).json()["key"]
    monkeypatch.setattr(api_key_service, "_now_utc", lambda: datetime(2026, 11, 1, 7, 0, 10, tzinfo=UTC))
    async with _new_client(peer) as remote:
        headers = {"X-API-Key": key}
        assert (await remote.get("/api/open/v1/me", headers=headers)).status_code == 200
        saved = await save_settings(system_admin, open_api_rate_limit_per_minute=1, auto_analyze_enabled=True,
                            task_max_concurrency=3, task_max_per_user=2)
        await wait_until(lambda: peer.state.settings.open_api_rate_limit_per_minute == 1, within=period + 0.25)
        revision = saved["revision"]
        elapsed = max(0.0, published[revision] - committed[revision])
        # 轮询发生在独立异步任务，保留 CI 事件循环调度误差。
        assert elapsed <= period + 0.1
        logging.getLogger(__name__).info("配置跨实例发布间隔 %.3f 秒（周期 %.1f 秒）", elapsed, period)
        assert peer.state.settings.auto_analyze_enabled
        assert peer.state.settings.task_max_concurrency == 3 and peer.state.settings.task_max_per_user == 2
        limited = await remote.get("/api/open/v1/me", headers=headers)
        assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0

async def test_peer_auto_analysis_uses_setting_at_success(app, peer_app_factory, system_admin, fake_llm, monkeypatch):
    # 让 B 持有真实单章任务，A 只提供后台设置接口。
    await app.state.task_engine.stop()
    monkeypatch.setattr(TaskEngine, "poll_interval", 0.3)
    await save_settings(system_admin, auto_analyze_enabled=True)
    peer = await peer_app_factory()
    await peer.state.task_engine.start()
    gate = Gate()
    async with _new_client(peer) as client:
        await register(client)
        project = await create_project(client)
        await create_ai_config(client)
        request = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "跨进程自动分析"})
        chapter_id = request.json()["id"]
        fake_llm.push([gate, "章节正文"])
        started = await client.post(f"/api/v1/chapters/{chapter_id}/generate", json={})
        assert started.status_code == 202
        try:
            await gate.wait_reached()
            await save_settings(system_admin, auto_analyze_enabled=False)
            await wait_until(lambda: not peer.state.settings.auto_analyze_enabled, within=1.5)
        finally:
            gate.release()
        assert (await stream_task(client, started.json()))[-1][0] == "done"
        assert (await client.get("/api/v1/tasks", params={"kind": "chapter.analyze"})).json() == []
        assert len(fake_llm.calls) == 1


async def test_registration_that_owns_write_gate_finishes_before_close(second_app, system_admin, monkeypatch):
    from app.services import auth_service

    original = auth_service._record_successful_login
    inserted, release = asyncio.Event(), asyncio.Event()
    closing = asyncio.Event()
    real_require = authorization_service.require

    async def before_commit(db, user):
        if user.username == "accepted_first":
            inserted.set()
            await release.wait()
        return await original(db, user)

    async def before_writer(db, actor, permission, **kwargs):
        if permission == "settings.update":
            closing.set()
        return await real_require(db, actor, permission, **kwargs)

    monkeypatch.setattr(auth_service, "_record_successful_login", before_commit)
    monkeypatch.setattr(authorization_service, "require", before_writer)
    async with _new_client(second_app) as peer:
        registration = asyncio.create_task(peer.post(
            "/api/v1/auth/register", json={"username": "accepted_first", "password": "secret123"},
        ))
        writer = None
        try:
            await asyncio.wait_for(inserted.wait(), 10)
            writer = asyncio.create_task(system_admin.patch(
                URL, json={"revision": 0, "changes": {"registration_enabled": False}},
            ))
            await asyncio.wait_for(closing.wait(), 10)
            assert not writer.done()
        finally:
            release.set()
            registered = await registration
            if writer:
                saved = await writer
        assert registered.status_code == 201 and saved.status_code == 200
        assert (await peer.get("/api/v1/auth/me")).json()["username"] == "accepted_first"
        assert (await peer.post(
            "/api/v1/auth/register", json={"username": "closed_second", "password": "secret123"},
        )).status_code == 403


async def test_older_read_is_published_before_newer_peer_commit(second_app, system_admin, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import SystemSettings
    from app.services.mcp import settings_gate

    await save_settings(system_admin, task_max_per_user=7)
    before_publish, release, writer_at_lock = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_get = AsyncSession.get
    original_lock = settings_gate.lock_for_update

    async def delay_read(db, entity, *args, **kwargs):
        row = await original_get(db, entity, *args, **kwargs)
        if entity is SystemSettings and asyncio.current_task().get_name() == "old-settings-reader":
            before_publish.set()
            await release.wait()
        return row

    async def observe_writer(db):
        writer_at_lock.set()
        await original_lock(db)

    monkeypatch.setattr(AsyncSession, "get", delay_read)
    monkeypatch.setattr(settings_gate, "lock_for_update", observe_writer)
    manager = second_app.state.system_settings
    old_read = asyncio.create_task(manager.load(), name="old-settings-reader")
    writer = None
    try:
        await asyncio.wait_for(before_publish.wait(), 10)
        writer = asyncio.create_task(system_admin.patch(URL, json={"revision": 1, "changes": {"task_max_per_user": 2}}))
        await asyncio.wait_for(writer_at_lock.wait(), 10)
        assert not writer.done()
    finally:
        release.set()
        observed = await old_read
        if writer:
            result = await writer
    assert observed.revision == 1 and result.status_code == 200
    assert (await manager.load()).revision == 2 and second_app.state.settings.task_max_per_user == 2


async def test_startup_failure_closes_settings_poller(peer_app_factory, monkeypatch):
    from app.services import auth_service

    peer = await peer_app_factory()
    manager = peer.state.system_settings

    async def fail_bootstrap(*args):
        assert manager._poller is not None and not manager._poller.done()
        raise RuntimeError("启动中断")

    monkeypatch.setattr(auth_service, "bootstrap_admin", fail_bootstrap)
    with pytest.raises(RuntimeError, match="启动中断"):
        async with peer.router.lifespan_context(peer):
            pytest.fail("初始化尚未成功")
    assert manager._poller.done()

async def test_corrupt_settings_block_peer_registration_and_public_config_until_repaired(
    app, second_app, system_admin,
):
    from app.models import SystemSettings

    await save_settings(system_admin, registration_enabled=True)
    async with app.state.sessionmaker.begin() as db:
        row = await db.get(SystemSettings, 1)
        row.overrides = {"registration_enabled": "true"}
    async with _new_client(second_app) as client:
        data = {"username": "recover_user", "password": "secret123"}
        blocked = await client.post("/api/v1/auth/register", json=data)
        assert blocked.status_code == 503 and "set-cookie" not in blocked.headers
        # 先拒绝缺失密钥。匿名读请求无需等待运行设置恢复。
        assert (await client.get("/api/open/v1/me")).status_code == 401
        public = await client.get("/api/v1/auth/config")
        assert public.status_code == 503 and public.headers["cache-control"] == "no-store"
        async with app.state.sessionmaker.begin() as db:
            assert await db.scalar(select(User.id).where(User.username == "recover_user")) is None
            row = await db.get(SystemSettings, 1)
            row.overrides = {"registration_enabled": True}
            row.revision += 1
        await second_app.state.system_settings.load()
        assert (await client.post("/api/v1/auth/register", json=data)).status_code == 201
