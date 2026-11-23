"""SQLite 实际恢复入口的双文件提交顺序。"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, update

from app.core.errors import ConflictError
from app.models import Task, TaskStatistic, TaskWorker
from app.services import task_statistics_service
from app.services.task_engine import engine as engine_module
from app.services.task_engine import workers
from tests.helpers import register
from tests.worker_helpers import expire_worker, worker_row


class ProcessExited(BaseException):
    """模拟进程在两个已分开的事务之间退出。"""


async def test_repeated_dispatch_errors_do_not_postpone_a_due_recovery(app, monkeypatch):
    engine = app.state.task_engine
    await engine.stop()
    await engine.lease.register()
    engine.poll_interval = 3
    engine._stopping = False
    engine.notify()
    elapsed = 0
    recoveries = []

    async def reap():
        recoveries.append(elapsed)

    async def notify_and_fail():
        nonlocal elapsed
        elapsed += 1
        if elapsed == 5:
            engine._stopping = True
        engine.notify()
        raise OSError("派发暂时失败")

    async def scan_cancellations():
        pass

    monkeypatch.setattr(engine, "_reap_orphans", reap)
    monkeypatch.setattr(engine, "_dispatch", notify_and_fail)
    monkeypatch.setattr(engine, "scan_admin_cancellations", scan_cancellations)
    monkeypatch.setattr(engine, "_maybe_scan_collaboration_paused_soon", lambda: None)
    with monkeypatch.context() as patch:
        patch.setattr(engine_module, "time", SimpleNamespace(monotonic=lambda: elapsed))
        await asyncio.wait_for(engine._scheduler(), 2)
    assert recoveries == [3]


async def seed_running(app, user, owner):
    async with app.state.sessionmaker() as db:
        row = Task(user_id=user["id"], kind="outline.continue", status="running",
                   execution_key=str(uuid4()), worker_key=owner)
        db.add(row)
        await task_statistics_service.record_created(db, row)
        await db.commit()
        return row.id


async def test_stale_main_heartbeat_does_not_make_a_healthy_worker_an_orphan(app, second_app, client):
    user = await register(client)
    source = app.state.task_engine
    await source.stop()
    owner = await source.lease.register()
    peer = second_app.state.task_engine
    await peer.lease.register()
    task_id = await seed_running(app, user, owner)
    async with app.state.sessionmaker.begin() as db:
        now = await db.scalar(select(func.now()))
        await db.execute(update(TaskWorker).where(TaskWorker.worker_key == owner).values(
            heartbeat_at=now - timedelta(hours=1),
        ))

    await peer.recover()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        assert (row.status, row.worker_key, row.recovered_at) == ("running", owner, None)
        assert (await db.get(TaskWorker, owner)).fenced_at is None
    assert await source.lease.heartbeat()


@pytest.mark.parametrize("decision", ["fenced_at", "stopped_at"])
async def test_new_reaper_finishes_a_sidecar_decision_after_main_rollback(
    app, second_app, peer_app_factory, client, monkeypatch, decision,
):
    user = await register(client)
    source = app.state.task_engine
    await source.stop()
    owner = await source.lease.register()
    first_reaper = second_app.state.task_engine
    await first_reaper.lease.register()
    task_id = await seed_running(app, user, owner)
    original_pair = source.lease.sqlite_pair
    if decision == "fenced_at":
        await expire_worker(source)
        prepare = workers.prepare_orphan_scan

        async def exit_before_main_commit(db, seconds):
            await prepare(db, seconds)
            raise ProcessExited

        with monkeypatch.context() as patch:
            patch.setattr(workers, "prepare_orphan_scan", exit_before_main_commit)
            with pytest.raises(ProcessExited):
                await first_reaper.recover()
    else:
        def exit_before_main_stop(_conn, _cursor, statement, *_args):
            if statement.lstrip().upper().startswith("UPDATE TASK_WORKERS ") and "stopped_at" in statement.lower():
                raise ProcessExited

        main = app.state.engine.sync_engine
        event.listen(main, "before_cursor_execute", exit_before_main_stop)
        try:
            with pytest.raises(ProcessExited):
                await source.lease.stop()
        finally:
            event.remove(main, "before_cursor_execute", exit_before_main_stop)

    assert getattr(await worker_row(source, owner, previous_pair=original_pair), decision) is not None
    async with app.state.sessionmaker() as db:
        # 主库镜像和任务仍指向原执行者，尝试从旧执行体提交真实的收尾写入。
        assert getattr(await db.get(TaskWorker, owner), decision) is None
        row = await db.get(Task, task_id)
        old_text = row.output_text
        assert (row.status, row.worker_key) == ("running", owner)
        assert not await workers.lock_live_worker(db, owner)

    attempted_write = source.complete_state(row, expected_status="running", status="interrupted", result=None,
                                            error="旧执行体", text="迟到的输出", progress=99, abort_ok=True)
    if decision == "fenced_at":
        with pytest.raises(ConflictError):
            await attempted_write
    else:
        _, committed = await attempted_write
        assert not committed
    async with app.state.sessionmaker() as db:
        untouched = await db.get(Task, task_id)
        assert (untouched.status, untouched.output_text) == ("running", old_text)
        assert (await db.get(TaskStatistic, untouched.statistics_key)).status == "running"

    replacement = (await peer_app_factory()).state.task_engine
    await replacement.start()
    async with app.state.sessionmaker() as db:
        assert getattr(await db.get(TaskWorker, owner), decision) is not None
        row = await db.get(Task, task_id)
        assert (row.status, row.worker_key, row.execution_key) == ("interrupted", replacement.lease.worker_key, None)
        assert (await db.get(TaskStatistic, row.statistics_key)).status == "interrupted"


async def test_main_lock_delays_takeover_without_blocking_either_peers_heartbeat(
    app, second_app, peer_app_factory, client, monkeypatch,
):
    user = await register(client)
    source = app.state.task_engine
    await source.stop()
    owner = await source.lease.register()
    task_id = await seed_running(app, user, owner)
    peer = second_app.state.task_engine
    observer = (await peer_app_factory()).state.task_engine
    began_main_fence = asyncio.Event()
    renewed = []
    for engine in (peer, observer):
        engine.poll_interval = 0.1 if engine is peer else 60
        engine.heartbeat_interval = 0.05
        await engine.start()
        signal = asyncio.Event()
        renewed.append(signal)
        heartbeat = engine.lease.heartbeat

        async def observe_heartbeat(heartbeat=heartbeat, signal=signal):
            ok = await heartbeat()
            if ok and began_main_fence.is_set():
                signal.set()
            return ok

        monkeypatch.setattr(engine.lease, "heartbeat", observe_heartbeat)

    allow_recovery = asyncio.Event()
    cleaned = asyncio.Event()
    reap = peer._reap_orphans
    finish = peer._recover_adopted

    async def wait_for_writer():
        await allow_recovery.wait()
        await reap()

    async def observe_cleanup(task):
        done = await finish(task)
        if task.id == task_id and done:
            cleaned.set()
        return done

    monkeypatch.setattr(peer, "_reap_orphans", wait_for_writer)
    monkeypatch.setattr(peer, "_recover_adopted", observe_cleanup)
    await expire_worker(source)

    def observe_fence(_conn, _cursor, statement, *_args):
        sql = statement.lstrip().lower()
        if sql.startswith("update task_workers ") and "fenced_at" in sql:
            began_main_fence.set()

    main = second_app.state.engine.sync_engine
    event.listen(main, "before_cursor_execute", observe_fence)
    try:
        async with app.state.sessionmaker() as blocker:
            await blocker.execute(update(Task).where(Task.id == task_id).values(id=Task.id))
            allow_recovery.set()
            await asyncio.wait_for(began_main_fence.wait(), 5)
            await asyncio.wait_for(asyncio.gather(*(signal.wait() for signal in renewed)), 3)
            assert (await worker_row(source, owner)).fenced_at is not None
            assert not cleaned.is_set()
            assert (await blocker.get(Task, task_id)).worker_key == owner
            assert peer.lease.valid and observer.lease.valid
            await blocker.rollback()
        await asyncio.wait_for(cleaned.wait(), 6)
    finally:
        allow_recovery.set()
        event.remove(main, "before_cursor_execute", observe_fence)

    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task_id)
        assert (row.status, row.worker_key) == ("interrupted", peer.lease.worker_key)
