"""真实章节终态的主库提交、独立侧库决定和本地已知失租的顺序。"""

import asyncio
import threading
from contextvars import ContextVar

import pytest
from sqlalchemy import event, func, select

from app.models import Chapter, GenerationLog, Task, TaskStatistic, TaskWorker
from tests.test_tasks import _generate, _setup
from tests.worker_helpers import fence_worker, freeze_heartbeat, worker_row


@pytest.mark.parametrize("boundary", ["local_loss_before_commit", "side_only_before_commit", "commit_submitted"])
async def test_terminal_write_order(app, second_app, client, fake_llm, monkeypatch, boundary):
    source, peer = app.state.task_engine, second_app.state.task_engine
    new_key = await peer.lease.register()
    old_key = source.lease.worker_key
    _, _, (chapter,) = await _setup(client, content="原稿")
    # 由用例决定原引擎何时得知侧库围栏，以及何时重新登记，排除后台循环改变指定时序。
    await freeze_heartbeat(source)
    return_to_scheduler = asyncio.Event()
    replace = source._replace_lost_worker

    async def hold_restart():
        await return_to_scheduler.wait()
        await replace()

    monkeypatch.setattr(source, "_replace_lost_worker", hold_restart)
    finalization = source.complete_state
    in_terminal = ContextVar("chapter_terminal_transaction", default=False)
    reached, resume, propagating = asyncio.Event(), asyncio.Event(), asyncio.Event()
    allow_sqlite_commit = threading.Event()
    loop = asyncio.get_running_loop()

    async def mark_terminal(*args, **kwargs):
        token = in_terminal.set(kwargs.get("status") == "succeeded")
        try:
            return await finalization(*args, **kwargs)
        finally:
            in_terminal.reset(token)

    monkeypatch.setattr(source, "complete_state", mark_terminal)

    def sqlite_trace(sql):
        # 此回调在 SQLite 工作线程执行，DBAPI 已经接到 COMMIT，应用的最后一次租约核验也已结束。
        if sql.strip().upper() == "COMMIT" and not allow_sqlite_commit.is_set():
            loop.call_soon_threadsafe(reached.set)
            allow_sqlite_commit.wait(10)

    def hold_terminal(conn, _cursor, sql, _parameters, _context, _many):
        sql = sql.lower()
        if not (in_terminal.get() and sql.startswith("update tasks set") and "status=" in sql):
            return
        if boundary == "commit_submitted":
            conn.connection.dbapi_connection.run_async(lambda driver: driver.set_trace_callback(sqlite_trace))
        elif not reached.is_set():
            async def wait_before_task_update(_driver):
                reached.set()
                await resume.wait()

            # 已持主库写锁并通过最后一次业务身份核验，还未向驱动发送任务终态 UPDATE。
            conn.connection.dbapi_connection.run_async(wait_before_task_update)

    def observe_propagation(_conn, _cursor, sql, _parameters, _context, _many):
        if sql.lower().startswith("update task_workers set") and "fenced_at=" in sql.lower():
            propagating.set()

    source_main, peer_main = app.state.engine.sync_engine, second_app.state.engine.sync_engine
    event.listen(source_main, "before_cursor_execute", hold_terminal)
    event.listen(peer_main, "before_cursor_execute", observe_propagation)
    recovery = None
    try:
        content = "原任务的完整输出"
        fake_llm.push(content)
        response = await _generate(client, chapter)
        assert response.status_code == 202
        task_id = response.json()["id"]
        await asyncio.wait_for(reached.wait(), 10)
        assert task_id in source._finalizing
        await fence_worker(source, mirror=False)
        assert (await worker_row(peer, old_key)).fenced_at is not None
        if boundary != "side_only_before_commit":
            source._mark_lease_lost()
            assert not source.lease.valid
        else:
            assert source.lease.valid
        original_task = source._running[task_id]
        recovery = asyncio.create_task(peer.recover())
        await asyncio.wait_for(propagating.wait(), 5)
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, task_id)
            assert (row.status, row.worker_key) == ("running", old_key)
            assert (await db.get(TaskWorker, old_key)).fenced_at is None
            assert (await db.get(Chapter, chapter["id"])).content == content
        assert not recovery.done()
        resume.set()
        allow_sqlite_commit.set()
        await asyncio.wait_for(recovery, 10)
        await asyncio.wait_for(original_task, 10)
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, task_id)
            statistic = await db.get(TaskStatistic, row.statistics_key)
            assert row.status == statistic.status == "succeeded"
            assert row.worker_key == (new_key if boundary == "local_loss_before_commit" else old_key)
            assert (await db.get(TaskWorker, old_key)).fenced_at is not None
            assert (await db.get(Chapter, chapter["id"])).content == content
            assert await db.scalar(select(func.count()).select_from(GenerationLog).where(
                GenerationLog.chapter_id == chapter["id"], GenerationLog.status == "success",
            )) == 1
        assert len(fake_llm.calls) == 1
    finally:
        resume.set()
        allow_sqlite_commit.set()
        try:
            if recovery is not None:
                await asyncio.gather(recovery, return_exceptions=True)
        finally:
            return_to_scheduler.set()
            event.remove(source_main, "before_cursor_execute", hold_terminal)
            event.remove(peer_main, "before_cursor_execute", observe_propagation)
