"""管理员写入的确认若晚到，普通请求也必须使用已在别处生效的新设置。"""

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from app.services import api_key_service, authorization_service, system_settings_service, system_settings_snapshot
from app.services.audit_writer import AuditOperation
from tests.helpers import register
from tests.schema_helpers import isolated_worker_database
from tests.test_system_settings import URL, save_settings


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


async def test_delayed_commit_receipt_cannot_renew_an_obsolete_limit(
    app, client, second_app, system_admin, monkeypatch,
):
    await register(client)
    key = (await client.post("/api/v1/api-keys", json={"name": "限流", "all_projects": True})).json()["key"]
    monkeypatch.setattr(api_key_service, "_now_utc", lambda: datetime(2026, 11, 1, 7, 0, 10, tzinfo=UTC))
    headers = {"X-API-Key": key}
    assert (await client.get("/api/open/v1/me", headers=headers)).status_code == 200

    manager = app.state.system_settings
    now = time.monotonic()
    clock = SimpleNamespace(monotonic=lambda: now)
    monkeypatch.setattr(system_settings_service, "time", clock)
    monkeypatch.setattr(system_settings_snapshot, "time", clock)
    committed, release = asyncio.Event(), asyncio.Event()
    published_after_peer = []
    original_notify = manager.notify

    def notify():
        if release.is_set():
            published_after_peer.append(app.state.settings.open_api_rate_limit_per_minute)
        original_notify()

    monkeypatch.setattr(manager, "notify", notify)
    original_commit = AuditOperation.commit

    async def delayed_receipt(audit):
        await original_commit(audit)
        if audit.values["action"] == "system_settings.update" and authorization_service.runtime_for(
            audit.db
        ) is manager.runtime:
            committed.set()
            await release.wait()

    monkeypatch.setattr(AuditOperation, "commit", delayed_receipt)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second_app), base_url="http://testserver",
                                 cookies=system_admin.cookies) as peer:
        first = asyncio.create_task(system_admin.patch(
            URL, json={"revision": 0, "changes": {"open_api_rate_limit_per_minute": 50}},
        ))
        try:
            await asyncio.wait_for(committed.wait(), 10)
            assert (await save_settings(peer, open_api_rate_limit_per_minute=1))["revision"] == 2
            # B 的新限制已超过一个调度周期。A 还在等待 revision 1 的写入回执。
            now += manager._poll_interval * 2
        finally:
            release.set()
            saved = await first
    assert saved.status_code == 200, saved.text
    assert published_after_peer and set(published_after_peer) == {1}
    assert (await client.get("/api/open/v1/me", headers=headers)).status_code == 429
