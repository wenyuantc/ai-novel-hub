"""只有停止或围栏决定进入主库，其他进程才可以使用原执行者的名额。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import TaskExecutionSlot, TaskWorker
from app.services.task_engine import workers
from tests.schema_helpers import isolated_worker_database
from tests.worker_helpers import expire_worker


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


def slot(worker: str, task_id: int) -> TaskExecutionSlot:
    return TaskExecutionSlot(execution_key=str(uuid4()), task_id=task_id, statistics_key=str(uuid4()),
                             queued_at=datetime.now(UTC), user_id=1, worker_key=worker)


async def owners(maker) -> set[str]:
    async with maker() as db:
        return set(await db.scalars(select(TaskExecutionSlot.worker_key)))


async def test_confirmed_loss_and_stop_release_only_the_original_worker(app, second_app):
    owner, peer = app.state.task_engine, second_app.state.task_engine
    await owner.stop()
    a, b, unknown = await owner.lease.register(), await peer.lease.register(), str(uuid4())
    writer, observer = peer.ctx.sessionmaker, owner.ctx.sessionmaker
    async with writer.begin() as db:
        db.add_all([slot(a, 1), slot(b, 2), slot(unknown, 3)])
    await expire_worker(owner)
    for commit in (False, True):
        async with writer() as db:
            await workers.prepare_orphan_scan(db, peer.lease_seconds)
            assert (await db.get(TaskWorker, a)).fenced_at is not None
            assert set(await db.scalars(select(TaskExecutionSlot.worker_key))) == {b, unknown}
            # 在主库事务提交前，独立观察者仍能看到全部占用。SQLite 侧库在此刻已经围栏。
            assert await owners(observer) == {a, b, unknown}
            if commit:
                await db.commit()
            else:
                await db.rollback()
    assert await owners(observer) == {b, unknown}
    await peer.lease.stop()
    assert await owners(observer) == {unknown}


async def test_registration_prunes_terminal_worker_without_stranding_its_last_slot(app, second_app):
    peer = second_app.state.task_engine
    old, unknown = str(uuid4()), str(uuid4())
    async with app.state.sessionmaker.begin() as db:
        db.add(TaskWorker(worker_key=old, hostname="old", pid=1, stopped_at=datetime.now(UTC) - timedelta(days=8)))
        db.add_all([slot(old, 1), slot(unknown, 2)])
    await peer.lease.register()
    assert await owners(app.state.sessionmaker) == {unknown}
    await peer.lease.stop()
    await peer.lease.register()
    async with app.state.sessionmaker() as db:
        assert await db.get(TaskWorker, old) is None
    assert await owners(app.state.sessionmaker) == {unknown}
