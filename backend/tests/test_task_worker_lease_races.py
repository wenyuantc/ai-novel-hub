"""心跳协程与调度协程在不同租约代次交错的回归。"""

import asyncio
import sqlite3
import threading
import time
from uuid import uuid4

import aiosqlite
import pytest
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import TaskWorker
from app.services.task_engine import sqlite_worker_leases, workers
from tests.helpers import wait_until


async def test_late_heartbeat_reply_leaves_replacement_worker_active(second_app, monkeypatch):
    engine = second_app.state.task_engine
    engine.heartbeat_interval = 0.02
    engine.poll_interval = 0.05
    first_request = asyncio.Event()
    release_reply = asyncio.Event()
    second_heartbeat = asyncio.Event()
    heartbeat = engine.lease.heartbeat

    async def delayed_response():
        if not first_request.is_set():
            first_request.set()
            await release_reply.wait()
            return False
        renewed = await heartbeat()
        if renewed:
            second_heartbeat.set()
        return renewed

    monkeypatch.setattr(engine.lease, "heartbeat", delayed_response)
    await engine.start()
    try:
        await asyncio.wait_for(first_request.wait(), 5)
        expired = engine.lease.worker_key
        # 旧心跳的确认还在路上，调度器已发现原租约无效并换了登记。
        engine.lease.invalidate()
        engine.notify()
        await wait_until(lambda: engine.lease.worker_key not in {None, expired} and not engine._lease_lost)
        replacement = engine.lease.worker_key
        release_reply.set()
        await asyncio.wait_for(second_heartbeat.wait(), 5)
        assert engine.lease.worker_key == replacement
        assert engine.lease.valid and not engine._lease_lost
    finally:
        release_reply.set()


async def test_exited_executor_is_recovered_while_its_worker_stays_live(app, client, monkeypatch):
    from app.models import Chapter, Task
    from tests.helpers import wait_task
    from tests.test_tasks import _generate, _setup

    engine = app.state.task_engine
    await engine.stop()
    _, _, (chapter,) = await _setup(client)
    engine.poll_interval = 0.1

    async def fail_after_claim(*args, **kwargs):
        raise OSError("模拟已领取任务的执行体意外退出")

    monkeypatch.setattr(engine, "_run", fail_after_claim)
    await engine.start()
    owner = engine.lease.worker_key
    response = await _generate(client, chapter)
    assert response.status_code == 202
    ref = response.json()
    assert (await wait_task(client, ref))["status"] == "interrupted"
    async with engine.ctx.sessionmaker() as db:
        task = await db.get(Task, ref["id"])
        resource = await db.get(Chapter, chapter["id"])
        assert task.execution_key is None and resource.status == "draft" and resource.generation_claim_key is None
        assert not engine.has_cleanup_failure(task.project_id)
    assert engine.lease.worker_key == owner and engine.lease.valid


async def test_peer_start_retries_sqlite_busy_while_original_worker_keeps_lease(app, second_app):
    owner = app.state.task_engine
    peer = second_app.state.task_engine
    original_key = owner.lease.worker_key
    busy_seen = asyncio.Event()

    def observed_driver_error(context):
        if getattr(context.original_exception, "sqlite_errorcode", None) == sqlite3.SQLITE_BUSY:
            busy_seen.set()

    binding = second_app.state.engine.sync_engine
    event.listen(binding, "handle_error", observed_driver_error)
    try:
        async with owner.ctx.sessionmaker() as db:
            await db.execute(update(TaskWorker).where(TaskWorker.worker_key == original_key)
                             .values(heartbeat_at=TaskWorker.heartbeat_at))
            starting = asyncio.create_task(peer.start())
            try:
                await asyncio.wait_for(busy_seen.wait(), 12)
                assert not starting.done()
                assert await owner.lease.heartbeat()
            finally:
                await db.rollback()
                await asyncio.wait_for(starting, 12)
        assert owner.lease.worker_key == original_key and owner.lease.valid
        assert peer.lease.worker_key not in {None, original_key} and peer.lease.valid
    finally:
        event.remove(binding, "handle_error", observed_driver_error)


async def test_sqlite_scan_fences_worker_just_past_deadline(app):
    pair = app.state.task_engine.lease.sqlite_pair
    assert pair is not None
    key = str(uuid4())
    await sqlite_worker_leases.register(pair, key)
    async with aiosqlite.connect(f"{pair.path.as_uri()}?mode=rw", uri=True) as db:
        await db.execute("UPDATE worker_leases SET heartbeat_at=strftime('%Y-%m-%d %H:%M:%f','now','-30.050 seconds') "
                         "WHERE worker_key=?", (key,))
        await db.commit()
    assert not await sqlite_worker_leases.live(pair, key, 30)
    await sqlite_worker_leases.fence_expired(pair, 30)
    row, = await sqlite_worker_leases.terminal_registrations(pair, [key])
    assert row[0] == key and row[2] is not None
    assert not await sqlite_worker_leases.heartbeat(pair, key, 30)


@pytest.mark.parametrize("state", ["active", "invalidated", "past_deadline", "replaced"])
async def test_commit_checks_original_local_worker_and_releases_connection_guard(second_app, monkeypatch, state):
    engine = second_app.state.task_engine
    key = await engine.lease.register()
    async with second_app.state.engine.connect() as connection:
        # 两个会话明确复用同一条物理连接，旧事务的校验不得进入之后的独立写入。
        async with AsyncSession(connection) as business:
            original = (await business.get(TaskWorker, key)).hostname
            assert await workers.lock_live_worker(business, key)
            await business.execute(update(TaskWorker).where(TaskWorker.worker_key == key).values(hostname="business"))
            with monkeypatch.context() as patch:
                if state == "invalidated":
                    engine.lease.invalidate()
                elif state == "past_deadline":
                    patch.setattr(engine.lease, "_last_ok", time.monotonic() - engine.lease_seconds)
                elif state == "replaced":
                    patch.setattr(engine.lease, "worker_key", str(uuid4()))
                if state == "active":
                    await business.commit()
                else:
                    with pytest.raises(ConflictError, match="执行身份已变化"):
                        await business.commit()
                    await business.rollback()
        async with AsyncSession(connection) as independent:
            row = await independent.get(TaskWorker, key)
            assert row.hostname == ("business" if state == "active" else original)
            row.hostname = "independent"
            await independent.commit()
        async with AsyncSession(connection) as reading:
            assert (await reading.get(TaskWorker, key)).hostname == "independent"


async def test_cancelling_peer_start_waits_for_inflight_side_registration(app, second_app, monkeypatch):
    peer = second_app.state.task_engine
    started, release, inserted = threading.Event(), threading.Event(), threading.Event()
    transaction = sqlite_worker_leases._transaction
    stop = sqlite_worker_leases.stop

    def delayed_insert(pair, write, action):
        if action.__name__ != "insert":
            return transaction(pair, write, action)
        started.set()
        assert release.wait(10)
        result = transaction(pair, write, action)
        inserted.set()
        return result

    async def checked_stop(pair, key):
        assert inserted.is_set(), "停止必须排在该登记的物理提交之后"
        await stop(pair, key)

    monkeypatch.setattr(sqlite_worker_leases, "_transaction", delayed_insert)
    monkeypatch.setattr(sqlite_worker_leases, "stop", checked_stop)
    pending = asyncio.create_task(peer.start())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        key = peer.lease.worker_key
        pending.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not peer.running and not peer.lease.valid
    pair = app.state.task_engine.lease.sqlite_pair
    record, = await sqlite_worker_leases.terminal_registrations(pair, [key])
    assert record[0] == key and record[1] is not None
    async with peer.ctx.sessionmaker() as db:
        assert await db.get(TaskWorker, key) is None
