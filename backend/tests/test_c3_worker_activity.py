"""从独立连接区分请求租约、并发名额和作品执行体的实际退出。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.core.errors import ConflictError
from app.models import Project, TaskExecutionSlot, TaskWorker, TaskWorkRecord
from app.services.task_engine import execution_slots, execution_work, workers
from tests.helpers import Gate
from tests.schema_helpers import isolated_worker_database
from tests.test_tasks import _generate, _setup


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


async def test_request_lease_outlives_the_scheduler_and_rejects_old_generation(app, second_app):
    engine = app.state.task_engine
    request = engine.request_worker
    assert request.valid and engine.lease.valid and request.worker_key != engine.lease.worker_key
    await engine.stop()
    async with app.state.sessionmaker.begin() as db:
        before = await request.require(db)
    async with second_app.state.sessionmaker() as db:
        assert await workers.lock_live_worker(db, before)
        assert not await db.scalar(select(workers.confirmed_terminal(before)))

    await request.stop()
    async with second_app.state.sessionmaker() as db:
        assert await db.scalar(select(workers.confirmed_terminal(before)))
    await request.start()
    async with app.state.sessionmaker.begin() as db:
        with pytest.raises(ConflictError):
            await request.require(db, before)
        after = await request.require(db)
        assert after != before
    assert not engine.running


@pytest.mark.parametrize("lost_reply", [False, True], ids=["before-write", "after-commit"])
async def test_release_of_slot_precedes_actual_exit_and_durable_confirmation(
    app, second_app, client, fake_llm, monkeypatch, lost_reply,
):
    engine = app.state.task_engine
    slot_reply, exit_write = Gate(), Gate()
    confirmed = asyncio.Event()
    release, acknowledge = execution_slots.release, execution_work.confirm_exit
    attempts = []

    async def release_before_reply(db, execution):
        await release(db, execution)
        slot_reply.reached.set()
        await slot_reply.released.wait()

    async def confirm_with_failure(db, execution):
        attempts.append(execution.execution_key)
        if len(attempts) == 1:
            assert execution.task_id not in engine._running
            exit_write.reached.set()
            await exit_write.released.wait()
            if lost_reply:
                await acknowledge(db, execution)
            raise ConnectionError("确认回执丢失")
        await acknowledge(db, execution)
        confirmed.set()

    monkeypatch.setattr(execution_slots, "release", release_before_reply)
    monkeypatch.setattr(execution_work, "confirm_exit", confirm_with_failure)
    project, _, (chapter,) = await _setup(client)
    fake_llm.push("同一个作品的任务")
    ref = (await _generate(client, chapter)).json()
    try:
        await slot_reply.wait_reached(within=15)
        assert ref["id"] in engine._running
        async with second_app.state.sessionmaker() as db:
            assert await db.scalar(select(TaskExecutionSlot.execution_key)) is None
            work = (await db.scalars(select(TaskWorkRecord).where(TaskWorkRecord.kind == "execution"))).one()
            assert (work.worker_key, work.project_id, work.task_key, work.state) == (
                engine.lease.worker_key, project["id"], ref["task_key"], "active",
            )
            key = work.work_key
        slot_reply.release()
        await exit_write.wait_reached(within=15)
        async with second_app.state.sessionmaker() as db:
            assert await db.get(TaskWorkRecord, key) is not None
        exit_write.release()
        await asyncio.wait_for(confirmed.wait(), 15)
        async with second_app.state.sessionmaker() as db:
            assert await db.get(TaskWorkRecord, key) is None
        assert attempts == [key, key]
    finally:
        slot_reply.release()
        exit_write.release()


async def test_terminal_request_worker_retained_while_project_or_failed_work_references_it(app, second_app, client):
    project, _, _ = await _setup(client)
    await app.state.task_engine.stop()
    peer = second_app.state.task_engine.lease
    old, deletion, unused, failure, execution = (str(uuid4()) for _ in range(5))
    retired = datetime.now(UTC) - timedelta(days=8)
    async with app.state.sessionmaker.begin() as db:
        db.add_all(TaskWorker(worker_key=k, stopped_at=retired, hostname="old", pid=1) for k in (old, deletion, unused))
        db.add_all([
            TaskWorkRecord(work_key=failure, worker_key=old, kind="abort_failure", state="failed",
                           project_id=project["id"]),
            TaskWorkRecord(work_key=execution, worker_key=old, kind="execution", state="active",
                           project_id=project["id"], execution_key=execution, task_id=20, queued_at=retired),
        ])
        await db.execute(update(Project).where(Project.id == project["id"]).values(
            deletion_token=str(uuid4()), deletion_started_at=retired, deletion_worker_key=deletion,
        ))
    await peer.register()
    async with app.state.sessionmaker() as db:
        assert await db.get(TaskWorker, unused) is None
        assert await db.scalar(select(workers.confirmed_terminal(old)))
        assert await db.scalar(select(workers.confirmed_terminal(deletion)))
        assert await db.get(TaskWorkRecord, failure) is not None
        assert await db.get(TaskWorkRecord, execution) is None


async def test_graceful_request_worker_stop_waits_for_original_http(app, second_app, client, monkeypatch):
    worker = app.state.task_engine.request_worker
    original = worker.worker_key
    entered, draining = Gate(), asyncio.Event()
    drain = worker._drain

    async def observe_drain():
        draining.set()
        await drain()

    @app.get("/_test/request-worker")
    async def hold_work(slow: bool = False):
        async with app.state.sessionmaker.begin() as db:
            key = await worker.require(db)
        if slow:
            entered.reached.set()
            await entered.released.wait()
        async with app.state.sessionmaker.begin() as db:
            await worker.require(db, key)
        return {"worker": key}

    monkeypatch.setattr(worker, "_drain", observe_drain)
    active = asyncio.create_task(client.get("/_test/request-worker?slow=true"))
    try:
        await entered.wait_reached()
        stop = asyncio.create_task(worker.stop())
        await asyncio.wait_for(draining.wait(), 5)
        refused = await client.get("/_test/request-worker")
        assert refused.status_code == 409 and not stop.done()
        async with second_app.state.sessionmaker() as db:
            assert not await db.scalar(select(workers.confirmed_terminal(original)))
        entered.release()
        settled = await asyncio.wait_for(active, 10)
        assert settled.status_code == 200 and settled.json() == {"worker": original}
        await asyncio.wait_for(stop, 10)
        async with second_app.state.sessionmaker() as db:
            assert await db.scalar(select(workers.confirmed_terminal(original)))
    finally:
        entered.release()
        await active
