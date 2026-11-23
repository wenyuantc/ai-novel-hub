"""SQLite 两文件登记的发布与补偿，使用真实进程退出和真实提交丢失回执。"""

import asyncio
import multiprocessing
import os

import aiosqlite
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task, TaskWorker
from app.services.task_engine import sqlite_worker_leases, workers
from tests.helpers import make_settings, wait_task
from tests.test_tasks import _generate, _setup
from tests.worker_helpers import worker_row

_EXIT_AT_BOUNDARY = 71


def _leave_registration(tmp_path, boundary, key_file, task_id, task_key):
    """只在子进程中调用。os._exit 跳过 finally，保留退出瞬间真正提交的两库内容。"""
    from app.main import create_app

    async def run():
        application = create_app(make_settings(tmp_path), start_task_engine=False)
        engine = application.state.task_engine
        await application.state.system_settings.load()
        side_register = sqlite_worker_leases.register

        async def after_side_commit(pair, key):
            await side_register(pair, key)
            key_file.write_text(key)
            assert not engine.lease.valid
            # 即使有待办且设置已加载，也不能使用尚未完成登记的执行者身份领取。
            await engine._claim_and_run(task_id, task_key)
            if boundary == "side_only":
                os._exit(_EXIT_AT_BOUNDARY)

        def exit_before_publish(_started):
            # 主库事务已经提交，但调用者尚未得到可用的本地执行身份。
            os._exit(_EXIT_AT_BOUNDARY)

        sqlite_worker_leases.register = after_side_commit
        engine.lease._confirmed = exit_before_publish
        await engine.lease.register()
        raise AssertionError("未到达登记提交的故障断点")

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["side_only", "both_committed"])
async def test_exit_between_registration_and_local_confirmation(
    app, client, second_app, fake_llm, tmp_path, boundary,
):
    await app.state.task_engine.stop()
    _, _, (chapter,) = await _setup(client)
    created = await _generate(client, chapter)
    assert created.status_code == 202
    task = created.json()
    survivor = second_app.state.task_engine
    await survivor.lease.register()
    key_file = tmp_path / "abandoned-worker.txt"
    child = multiprocessing.get_context("spawn").Process(
        target=_leave_registration, args=(tmp_path, boundary, key_file, task["id"], task["task_key"]),
    )
    child.start()
    try:
        await asyncio.to_thread(child.join, 15)
        assert child.exitcode == _EXIT_AT_BOUNDARY
    finally:
        if child.is_alive():
            child.kill()
            await asyncio.to_thread(child.join)
        child.close()

    abandoned = key_file.read_text()
    pair = survivor.lease.sqlite_pair
    assert await sqlite_worker_leases.live(pair, abandoned, survivor.lease_seconds)
    async with survivor.ctx.sessionmaker() as db:
        queued = await db.get(Task, task["id"])
        assert (queued.status, queued.worker_key, queued.execution_key) == ("queued", None, None)
        diagnostic = await db.get(TaskWorker, abandoned)
        if boundary == "side_only":
            assert diagnostic is None
            assert not await workers.lock_live_worker(db, abandoned)
        else:
            assert diagnostic is not None and diagnostic.fenced_at is None

    # 使用权威侧库的时钟制造过期。两种退出均由存活执行者推进到永久失效。
    async with aiosqlite.connect(f"{pair.path.as_uri()}?mode=rw", uri=True) as side:
        changed = await side.execute(
            "UPDATE worker_leases SET heartbeat_at=datetime('now','-1 hour') WHERE worker_key=?", (abandoned,),
        )
        assert changed.rowcount == 1
        await side.commit()
    await survivor.recover()
    assert (await worker_row(survivor, abandoned)).fenced_at is not None
    assert not await sqlite_worker_leases.heartbeat(pair, abandoned, survivor.lease_seconds)
    async with survivor.ctx.sessionmaker() as db:
        assert not await workers.lock_live_worker(db, abandoned)
        diagnostic = await db.get(TaskWorker, abandoned)
        if boundary == "side_only":
            assert diagnostic is None
        else:
            assert diagnostic.fenced_at is not None

    fake_llm.push("由已完成登记的执行者生成")
    await survivor.start()
    assert (await wait_task(client, task))["status"] == "succeeded"
    async with survivor.ctx.sessionmaker() as db:
        done = await db.get(Task, task["id"])
        assert done.worker_key == survivor.lease.worker_key and done.worker_key != abandoned


@pytest.mark.parametrize("stage", ["side", "main"])
@pytest.mark.parametrize("committed", [True, False], ids=["commit_succeeded", "commit_failed"])
async def test_registration_uses_durable_proof_after_lost_reply(app, second_app, monkeypatch, stage, committed):
    engine = second_app.state.task_engine
    keys = []
    failures = []
    side_register = sqlite_worker_leases.register
    real_commit = AsyncSession.commit

    async def lose_side_reply(pair, key):
        keys.append(key)
        if stage != "side" or committed:
            await side_register(pair, key)
        if stage == "side":
            failures.append("side")
            raise ConnectionError("侧库登记回执丢失")

    async def lose_main_reply(db):
        registration = stage == "main" and any(isinstance(row, TaskWorker) for row in db.new)
        if not registration or committed:
            await real_commit(db)
        if registration:
            failures.append("main")
            raise ConnectionError("主库登记回执丢失")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite_worker_leases, "register", lose_side_reply)
        patch.setattr(AsyncSession, "commit", lose_main_reply)
        if committed:
            await engine.start()
        else:
            with pytest.raises(ConnectionError, match="登记回执丢失"):
                await engine.start()

    assert failures == [stage]
    key, = keys
    peer = app.state.task_engine
    if committed:
        assert engine.lease.valid and engine.lease.worker_key == key
        assert await sqlite_worker_leases.live(peer.lease.sqlite_pair, key, peer.lease_seconds)
        async with peer.ctx.sessionmaker() as db:
            assert await workers.lock_live_worker(db, key)
            row = await db.get(TaskWorker, key)
            assert row.fenced_at is None and row.stopped_at is None
    else:
        assert not engine.running and not engine.lease.valid
        assert not await sqlite_worker_leases.heartbeat(peer.lease.sqlite_pair, key, peer.lease_seconds)
        if stage == "main":
            assert (await worker_row(peer, key)).stopped_at is not None
        async with peer.ctx.sessionmaker() as db:
            assert await db.get(TaskWorker, key) is None
            assert not await workers.lock_live_worker(db, key)
        await engine.start()
        assert engine.lease.worker_key not in (None, key)
    assert engine.running and await engine.lease.heartbeat()
