"""真实请求侧取消退出与可证明的项目失败跨进程可见。"""

import asyncio

import httpx
import pytest
from sqlalchemy import select, update

from app.core.errors import ConflictError
from app.models import Task, TaskWorkRecord
from app.services import task_service
from app.services.task_engine import cleanup_work
from app.services.task_engine.registry import get_handler
from tests.helpers import task_params
from tests.schema_helpers import isolated_worker_database
from tests.test_recycle_bin_tasks import generate, setup_chapter
from tests.test_task_abort_identity import _terminal_tasks


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield {**overrides, "allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


async def test_peer_sees_committed_cancel_until_original_request_finishes(
    app, second_app, client, monkeypatch,
):
    _, project, chapter = await setup_chapter(client)
    await app.state.task_engine.stop()
    ref = await generate(client, chapter)
    entered, release, checked_work = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = task_service.refresh_same_task
    exists = task_service.project_work_exists

    async def hold_after_commit(db, task):
        if (task.id == ref["id"] and task.statistics_key == ref["task_key"] and not db.in_transaction()
                and not entered.is_set()):
            entered.set()
            await release.wait()
        return await original(db, task)

    async def observe(db, project_id):
        result = await exists(db, project_id)
        if project_id == project["id"] and result:
            checked_work.set()
        return result

    monkeypatch.setattr(task_service, "refresh_same_task", hold_after_commit)
    monkeypatch.setattr(task_service, "project_work_exists", observe)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second_app), base_url="http://testserver",
                                 cookies=client.cookies) as peer:
        cancel = asyncio.create_task(client.post(f"/api/v1/tasks/{ref['id']}/cancel", params=task_params(ref)))
        deleting = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with second_app.state.sessionmaker() as db:
                assert (await db.get(Task, ref["id"])).status == "cancelled"
                rows = (await db.scalars(select(TaskWorkRecord).where(
                    TaskWorkRecord.kind == "request_cleanup", TaskWorkRecord.project_id == project["id"],
                ))).all()
                assert len(rows) == 1 and rows[0].task_key == ref["task_key"]
                assert rows[0].worker_key == app.state.task_engine.request_worker.worker_key
            deleting = asyncio.create_task(peer.delete(f"/api/v1/projects/{project['id']}"))
            await asyncio.wait_for(checked_work.wait(), 5)
            assert not deleting.done()
        finally:
            release.set()
            await asyncio.wait_for(cancel, 10)
            if deleting is not None:
                await asyncio.wait_for(deleting, 10)
        assert cancel.result().status_code == 200
        assert deleting.result().status_code == 204
    async with app.state.sessionmaker() as db:
        assert not await task_service.project_work_exists(db, project["id"])


@pytest.mark.parametrize("replace_inputs", [False, True])
async def test_peer_retries_only_matching_persisted_abort_failure(
    app, second_app, client, monkeypatch, replace_inputs,
):
    pid, _, task, _ = await _terminal_tasks(app, client)
    engine, peer = app.state.task_engine, second_app.state.task_engine
    sensitive = "只有原任务可以读取的测试草稿"
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task.id)
        task.input = {"request": {"draft": sensitive}}
        await db.commit()
    calls = []

    async def fail_first(ctx, row, **kwargs):
        calls.append(row.statistics_key)
        if len(calls) == 1:
            raise RuntimeError("受控失败")

    monkeypatch.setitem(vars(get_handler(task.kind)), "on_abort", fail_first)
    with pytest.raises(RuntimeError):
        await task_service._abort_loaded_task(engine, task)
    async with second_app.state.sessionmaker() as db:
        assert await cleanup_work.has_failure(db, peer, pid)
        row = await db.scalar(select(TaskWorkRecord).where(TaskWorkRecord.kind == "abort_failure"))
        assert row.task_key == task.statistics_key and sensitive not in str(row.evidence)
        if replace_inputs:
            await db.execute(update(Task).where(Task.id == task.id).values(input={"different": True}))
            await db.commit()
            with pytest.raises(ConflictError):
                await task_service.retry_project_cleanup_failures(db, peer, pid)
            assert calls == [task.statistics_key]
            assert await cleanup_work.has_failure(db, peer, pid)
        else:
            await task_service.retry_project_cleanup_failures(db, peer, pid)
            assert calls == [task.statistics_key] * 2
            assert not await cleanup_work.has_failure(db, peer, pid)
    if not replace_inputs:
        async with app.state.sessionmaker() as db:
            assert not await cleanup_work.has_failure(db, engine, pid)


@pytest.mark.parametrize("lost_commit_reply", [False, True])
async def test_request_exit_retries_transient_failure_with_live_owner(app, client, monkeypatch, lost_commit_reply):
    from sqlalchemy.ext.asyncio import AsyncSession

    _, project, chapter = await setup_chapter(client)
    engine = app.state.task_engine
    await engine.stop()
    ref = await generate(client, chapter)
    execute, commit = AsyncSession.execute, AsyncSession.commit
    attempts = 0

    async def observe_exit(db, statement, *args, **kwargs):
        if getattr(statement, "is_delete", False) and statement.table.name == TaskWorkRecord.__tablename__:
            if "request_cleanup" in statement.compile().params.values():
                db.info["test_request_exit"] = True
        return await execute(db, statement, *args, **kwargs)

    async def transient_failure(db):
        nonlocal attempts
        if db.info.pop("test_request_exit", False):
            attempts += 1
            if attempts <= 2:
                if lost_commit_reply:
                    await commit(db)
                raise RuntimeError("退出确认暂不可读")
        await commit(db)

    monkeypatch.setattr(AsyncSession, "execute", observe_exit)
    monkeypatch.setattr(AsyncSession, "commit", transient_failure)
    cancelled = await client.post(f"/api/v1/tasks/{ref['id']}/cancel", params=task_params(ref))
    assert cancelled.status_code == 200 and attempts == 3
    assert engine.request_worker.valid
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, ref["id"])).status == "cancelled"
        assert not await task_service.project_work_exists(db, project["id"])


async def test_failed_retry_releases_attempt_after_lost_claim_and_exit_replies(app, second_app, client, monkeypatch):
    pid, _, task, _ = await _terminal_tasks(app, client)
    owner, peer = app.state.task_engine, second_app.state.task_engine
    await owner.record_abort_failure(pid, owner.capture_abort_snapshot(task))
    attempts = []

    async def fail_then_succeed(ctx, row):
        attempts.append(row.statistics_key)
        if len(attempts) == 1:
            raise ValueError("业务尚未完成")

    monkeypatch.setitem(vars(get_handler(task.kind)), "on_abort", fail_then_succeed)
    async with owner.ctx.sessionmaker() as db:
        commit = db.commit
        writes = 0

        async def lose_confirmation():
            nonlocal writes
            writes += 1
            if writes in (1, 3):
                await commit()
                raise RuntimeError("领取或归还已提交但回执丢失")
            if writes == 2:
                raise RuntimeError("归还尚未提交")
            await commit()

        monkeypatch.setattr(db, "commit", lose_confirmation)
        with pytest.raises(ValueError, match="业务尚未完成"):
            await task_service.retry_project_cleanup_failures(db, owner, pid)
    assert writes == 4 and owner.request_worker.valid
    async with peer.ctx.sessionmaker() as db:
        only_failure = (await db.scalars(select(TaskWorkRecord).where(TaskWorkRecord.project_id == pid))).one()
        assert only_failure.state == "failed" and only_failure.operation_id is None
        await task_service.retry_project_cleanup_failures(db, peer, pid)
        assert not await cleanup_work.has_failure(db, peer, pid)
    assert attempts == [task.statistics_key, task.statistics_key]


async def test_fenced_executor_cannot_record_late_cleanup_failure(app, client):
    pid, _, task, _ = await _terminal_tasks(app, client)
    engine = app.state.task_engine
    task.worker_key = engine.request_worker.worker_key
    snapshot = engine.capture_abort_snapshot(task)
    assert snapshot is not None
    await engine.request_worker.lease.stop()
    await engine.record_abort_failure(pid, snapshot)
    async with engine.ctx.sessionmaker() as db:
        assert not await cleanup_work.has_failure(db, engine, pid)
