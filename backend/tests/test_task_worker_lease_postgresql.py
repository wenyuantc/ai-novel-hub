"""在隔离 PostgreSQL 16 schema 复跑同一组多引擎场景，检验真实行锁与事务行为。"""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from app.models import Base, TaskWorker
from app.services.task_engine import workers
from tests.schema_helpers import create_isolated_schema
from tests.test_task_worker_lease import (
    test_healthy_generation_survives_second_start,
    test_periodic_recovery_releases_chapter_and_rejects_late_worker,
    test_two_reapers_interrupt_and_count_once,
)
from tests.test_task_worker_recovery_crash import test_recovery_is_atomic_after_adopter_exits

__all__ = [
    "test_healthy_generation_survives_second_start",
    "test_periodic_recovery_releases_chapter_and_rejects_late_worker",
    "test_two_reapers_interrupt_and_count_once",
    "test_recovery_is_atomic_after_adopter_exits",
]


@pytest.fixture
async def settings_overrides(monkeypatch):
    raw = os.getenv("AINOVEL_POSTGRES_TEST_URL")
    if not raw:
        pytest.skip("缺少隔离 PostgreSQL 测试库 AINOVEL_POSTGRES_TEST_URL")
    url = make_url(raw)
    if (url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost", "::1"}
            or url.database != "ainovel_test" or url.query):
        raise ValueError("AINOVEL_POSTGRES_TEST_URL 必须指向本机专用 ainovel_test 测试库")
    schema = f"worker_lease_{uuid4().hex}"
    admin = create_async_engine(url)
    try:
        async with admin.begin() as conn:
            await conn.execute(CreateSchema(schema))
        try:
            def make_scoped_engine(database_url):
                assert make_url(database_url) == url
                return create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})

            monkeypatch.setattr("app.main.create_engine", make_scoped_engine)
            monkeypatch.setattr(Base.metadata, "create_all", create_isolated_schema)
            yield {"database_url": raw}
        finally:
            async with admin.begin() as conn:
                await conn.execute(DropSchema(schema, cascade=True))
    finally:
        await admin.dispose()


async def test_open_send_can_renew_and_other_expired_workers_can_be_fenced(second_app):
    """慢请求可以续约；其发送事务只保护本登记，其他失联执行者应能被及时处理。"""
    engine = second_app.state.task_engine
    key = await engine.lease.register()
    maker = engine.ctx.sessionmaker
    other = str(uuid4())
    async with maker.begin() as db:
        db.add(TaskWorker(worker_key=other, hostname="peer", pid=2))
    async with maker() as sender:
        assert await workers.lock_live_worker(sender, key, read_only=True)
        async with asyncio.timeout(3):
            assert await engine.lease.heartbeat()
        async with maker.begin() as reaper:
            # 把扫描界线推进到心跳之后，仅用于验证围栏与活动事务的锁兼容性。
            cutoff = await reaper.scalar(select(func.now())) + timedelta(seconds=60)
            async with asyncio.timeout(3):
                await workers._pg_fence_expired(reaper, cutoff)
            assert (await reaper.get(TaskWorker, key)).fenced_at is None
            assert (await reaper.get(TaskWorker, other)).fenced_at is not None
    async with maker.begin() as reaper:
        await workers._pg_fence_expired(reaper, cutoff)
    async with maker() as db:
        assert (await db.get(TaskWorker, key)).fenced_at is not None
    assert not await engine.lease.heartbeat()
