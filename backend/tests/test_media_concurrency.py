"""独立连接覆盖配额预留、传输中的删除与两个清理者的争用。"""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.errors import ConflictError
from app.media import service
from app.media.backends import LocalBackend
from app.models.media import MediaObject, MediaStorageSettings
from tests.helpers import register
from tests.schema_helpers import isolated_worker_database
from tests.test_media_service import VIDEO, chunks


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as settings:
        yield settings


async def test_pending_transfer_reserves_quota_and_observes_delete_without_holding_write_lock(app, client, monkeypatch):
    user = (await register(client))["id"]
    factory = app.state.sessionmaker
    async with factory() as db:
        (await db.get(MediaStorageSettings, 1)).default_quota_bytes = len(VIDEO)
        await db.commit()

    async def upload():
        async with factory() as db:
            return await service.store(db, owner_id=user, scope="user", kind="video", mime="video/mp4",
                                       source=chunks(VIDEO), max_bytes=len(VIDEO))

    writing, resume = asyncio.Event(), asyncio.Event()
    real_put = LocalBackend.put

    async def slow(backend, *args, **kwargs):
        writing.set()
        await resume.wait()
        return await real_put(backend, *args, **kwargs)

    monkeypatch.setattr(LocalBackend, "put", slow)
    task = asyncio.create_task(upload())
    try:
        async with asyncio.timeout(10):
            await writing.wait()
            with pytest.raises(ConflictError) as quota:
                await upload()
            assert quota.value.extra["code"] == "media_quota_exceeded"
            async with factory() as db:
                row = await db.scalar(select(MediaObject))
                assert row.status == "pending"
                await service.mark_deleted(db, [row.id])
                await db.commit()
            assert (await service.sweep(factory)).removed == 0
    finally:
        resume.set()
        with pytest.raises(ConflictError, match="进入清理"):
            await task
    deletes = []
    original = LocalBackend.delete

    async def record(backend, key):
        deletes.append(key)
        await original(backend, key)

    monkeypatch.setattr(LocalBackend, "delete", record)
    async with asyncio.timeout(10):
        result = await asyncio.gather(service.sweep(factory), service.sweep(factory))
    assert sum(s.removed for s in result) == 1
    assert deletes == [row.storage_key]
    assert not (Path(app.state.settings.media_dir) / row.storage_key).exists()
    async with factory() as db:
        assert await service.usage_of(db, user) == 0
        assert await db.get(MediaObject, row.id) is None


async def test_slow_delete_allows_other_transactions_and_reclaims_expired_lease(app, client, monkeypatch):
    from datetime import UTC, datetime, timedelta

    user = (await register(client))["id"]
    factory = app.state.sessionmaker
    async with factory() as db:
        row = await service.store(db, owner_id=user, scope="user", kind="video", mime="video/mp4",
                                  source=chunks(VIDEO), max_bytes=len(VIDEO))
        await service.mark_deleted(db, [row.id])
        row.cleanup_token = "abandoned"
        row.cleanup_claimed_at = datetime.now(UTC) - timedelta(minutes=11)
        await db.commit()
    entered, resume = asyncio.Event(), asyncio.Event()
    real_delete = LocalBackend.delete
    calls = []

    async def block(backend, key):
        calls.append(key)
        entered.set()
        await resume.wait()
        await real_delete(backend, key)

    monkeypatch.setattr(LocalBackend, "delete", block)
    cleaning = asyncio.create_task(service.sweep(factory))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            async with factory() as db:
                (await db.get(MediaStorageSettings, 1)).default_quota_bytes = len(VIDEO)
                await db.commit()
            assert (await service.sweep(factory)).removed == 0
    finally:
        resume.set()
        await cleaning
    assert calls == [row.storage_key]
    async with factory() as db:
        assert await db.get(MediaObject, row.id) is None
