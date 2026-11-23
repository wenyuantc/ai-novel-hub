"""清理重试者失去租约后，接手者只完成尚未提交的业务效果。"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.models import AdminAuditLog, Chapter, Project, Task, TaskStatistic, TaskWorkRecord
from app.services import project_lifecycle, recycle_bin_service, task_service, task_statistics_service
from app.services.task_engine import admin_cleanup, cleanup_work, workers
from app.services.task_engine.registry import get_handler
from tests.conftest import _new_client
from tests.helpers import create_project, register
from tests.schema_helpers import isolated_worker_database
from tests.test_task_abort_identity import _terminal_tasks


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


def _count_durable_effect(monkeypatch, original, chapter_id):
    async def apply_marker(ctx, task):
        assert task.statistics_key == original.statistics_key
        async with ctx.sessionmaker() as db:
            await db.execute(update(Chapter).where(Chapter.id == chapter_id).values(word_count=Chapter.word_count + 1))
            await db.commit()

    monkeypatch.setitem(vars(get_handler(original.kind)), "on_abort", apply_marker)


async def _open_task_with_counter(app, client, status):
    await app.state.task_engine.stop()
    user = await register(client, "cleanup-recovery-owner")
    project = await create_project(client)
    async with app.state.sessionmaker.begin() as db:
        chapter = Chapter(project_id=project["id"], status="draft")
        task = Task(user_id=user["id"], project_id=project["id"], kind="book.import", status=status)
        db.add_all([chapter, task])
        await task_statistics_service.record_created(db, task)
    return project["id"], chapter.id, task


@pytest.mark.parametrize("committed", [False, True], ids=["before-effect", "after-effect-commit"])
async def test_peer_resumes_failed_cleanup_once_after_retry_worker_stops(
    app, second_app, client, monkeypatch, committed,
):
    origin, peer = app.state.task_engine, second_app.state.task_engine
    pid, chapter_id, original, _ = await _terminal_tasks(app, client)
    snapshot = origin.capture_abort_snapshot(original)
    assert snapshot is not None
    await origin.record_abort_failure(pid, snapshot)

    _count_durable_effect(monkeypatch, original, chapter_id)
    reached, resume = asyncio.Event(), asyncio.Event()
    apply = cleanup_work.apply_effect

    async def pause_retry(engine, callback, **identity):
        if engine is not origin:
            return await apply(engine, callback, **identity)
        if committed:
            result = await apply(engine, callback, **identity)
        reached.set()
        await resume.wait()
        if committed:
            return result
        return await apply(engine, callback, **identity)

    monkeypatch.setattr(cleanup_work, "apply_effect", pause_retry)

    async def retry():
        async with app.state.sessionmaker() as db:
            await task_service.retry_project_cleanup_failures(db, origin, pid)

    held = asyncio.create_task(retry())
    try:
        await asyncio.wait_for(reached.wait(), 10)
        worker = origin.request_worker.worker_key
        async with peer.ctx.sessionmaker() as db:
            work = (await db.scalars(select(TaskWorkRecord).where(TaskWorkRecord.project_id == pid))).one()
            assert work.kind == "abort_failure" and work.worker_key == worker
            assert (await db.get(Chapter, chapter_id)).word_count == int(committed)
        # 明确持久化原代次已退出，旧协程恢复时仍会尝试完成其原分支。
        await origin.request_worker.lease.stop()
        async with peer.ctx.sessionmaker() as db:
            assert await db.scalar(select(workers.confirmed_terminal(worker)))
        async with _new_client(second_app) as http:
            http.cookies.update(client.cookies)
            response = await http.delete(f"/api/v1/projects/{pid}")
        assert response.status_code == 204, response.text
    finally:
        resume.set()
        await asyncio.wait_for(asyncio.gather(held, return_exceptions=True), 10)
    async with peer.ctx.sessionmaker() as db:
        assert (await db.get(Project, pid)).deleted_at is not None
        assert (await db.get(Chapter, chapter_id)).word_count == 1
        assert not list(await db.scalars(select(TaskWorkRecord).where(TaskWorkRecord.project_id == pid)))


@pytest.mark.parametrize("status,admin_recovery", [
    ("paused", None), ("queued", None), ("queued", "admin"), ("queued", "deletion"),
], ids=["paused", "queued", "queued-with-admin-scan", "queued-with-admin-deletion"])
@pytest.mark.parametrize("committed", [False, True], ids=["before-effect", "after-effect-commit"])
async def test_peer_finishes_cancel_after_http_exits(
    app, second_app, client, system_admin, monkeypatch, status, committed, admin_recovery,
):
    owner, peer = app.state.task_engine, second_app.state.task_engine
    pid, chapter_id, original = await _open_task_with_counter(app, client, status)

    _count_durable_effect(monkeypatch, original, chapter_id)
    apply = cleanup_work.apply_effect
    interrupted = False

    async def lost_request(engine, callback, **identity):
        nonlocal interrupted
        if engine is owner:
            if committed:
                await apply(engine, callback, **identity)
            interrupted = True
            raise asyncio.CancelledError
        return await apply(engine, callback, **identity)

    monkeypatch.setattr(cleanup_work, "apply_effect", lost_request)
    with pytest.raises(asyncio.CancelledError):
        await client.post(f"/api/v1/tasks/{original.id}/cancel", params={"task_key": original.statistics_key})
    assert interrupted
    async with peer.ctx.sessionmaker() as db:
        assert (await db.get(Chapter, chapter_id)).word_count == int(committed)
    await owner.request_worker.stop()
    async with _new_client(second_app) as http:
        if admin_recovery:
            http.cookies.update(system_admin.cookies)
            with monkeypatch.context() as dispatch:
                if admin_recovery == "deletion":
                    # 把受理后的首次唤醒推迟到作品删除，让删除入口看到原操作。
                    dispatch.setattr(peer, "request_admin_cancel", lambda *_: None)
                result = await http.post(f"/api/v1/admin/tasks/{original.statistics_key}/cancel", json={
                    "confirm": True, "reason": "接手排队任务清理",
                })
            assert result.status_code == 202, result.text
            operation = result.json()["operation_id"]
            if admin_recovery == "admin":
                await peer.wait_admin_cancellations()
                async with peer.ctx.sessionmaker() as db:
                    assert (await db.get(Task, original.id)).status == "cancelled"
                    assert (await db.get(Chapter, chapter_id)).word_count == 1
        http.cookies.update(client.cookies)
        response = await http.delete(f"/api/v1/projects/{pid}")
    assert response.status_code == 204, response.text
    async with peer.ctx.sessionmaker() as db:
        assert (await db.get(Project, pid)).deleted_at is not None
        assert (await db.get(TaskStatistic, original.statistics_key)).status == "cancelled"
        assert (await db.get(Chapter, chapter_id)).word_count == 1
        assert not list(await db.scalars(select(TaskWorkRecord).where(TaskWorkRecord.project_id == pid)))
        if admin_recovery:
            assert (await db.get(Task, original.id)).admin_cancel_operation_id is None
            assert await db.scalar(select(func.count(AdminAuditLog.id)).where(
                AdminAuditLog.operation_id == operation, AdminAuditLog.result == "success",
            )) == 1


async def test_durable_unproven_failure_blocks_peer_purge_and_expiry(app, second_app, client, recycle_admin):
    origin, peer = app.state.task_engine, second_app.state.task_engine
    pid, _, _, _ = await _terminal_tasks(app, client)
    assert (await client.delete(f"/api/v1/projects/{pid}")).status_code == 204
    # 模拟迟到的无证据清理失败，只有原进程掌握本机标记。
    await origin.record_abort_failure(pid, None)
    assert not peer.has_cleanup_failure(pid)
    async with _new_client(second_app) as http:
        http.cookies.update(recycle_admin.cookies)
        listing = await http.get("/api/v1/admin/recycle-bin/projects", params={"page_size": 100})
        assert listing.status_code == 200
        item = next(item for item in listing.json()["items"] if item["id"] == pid)
        response = await http.post(f"/api/v1/admin/recycle-bin/projects/{pid}/purge", json={
            "deleted_at": item["deleted_at"], "title": item["title"], "confirm": True,
        })
        assert response.status_code == 409
    past_retention = project_lifecycle.now_utc() + timedelta(days=project_lifecycle.RETENTION_DAYS + 1)
    assert await recycle_bin_service.purge_expired(peer, now=past_retention) == 0
    async with peer.ctx.sessionmaker() as db:
        assert (await db.get(Project, pid)).deleted_at is not None
        failure = (await db.scalars(select(TaskWorkRecord).where(TaskWorkRecord.project_id == pid))).one()
        assert failure.kind == "abort_failure" and failure.state == "failed"


async def test_third_admin_drain_finishes_once_after_two_owners_stop(
    app, second_app, peer_app_factory, client, system_admin, monkeypatch,
):
    first, second = app.state.task_engine, second_app.state.task_engine
    third = (await peer_app_factory()).state.task_engine
    _, chapter_id, original = await _open_task_with_counter(app, client, "queued")
    monkeypatch.setattr(first, "request_admin_cancel", lambda *_: None)
    response = await system_admin.post(
        f"/api/v1/admin/tasks/{original.statistics_key}/cancel", json={"confirm": True, "reason": "清理失联"},
    )
    assert response.status_code == 202, response.text
    operation = response.json()["operation_id"]

    _count_durable_effect(monkeypatch, original, chapter_id)
    abort_once = admin_cleanup.abort_once
    before, after, resume = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold_drains(engine, task, record, handler):
        if engine is first:
            before.set()
            await resume.wait()
        result = await abort_once(engine, task, record, handler)
        if engine is second:
            after.set()
            await resume.wait()
        return result

    monkeypatch.setattr(admin_cleanup, "abort_once", hold_drains)
    pending = []
    try:
        # 第一位认领者还没执行，第二位已提交业务和回执，两者都在归还前失联。
        for owner, event, committed in ((first, before, 0), (second, after, 1)):
            pending.append(asyncio.create_task(owner.admin_cancellations._drain(original.statistics_key, operation)))
            await asyncio.wait_for(event.wait(), 10)
            async with third.ctx.sessionmaker() as db:
                assert (await db.get(Chapter, chapter_id)).word_count == committed
                assert (await db.get(TaskWorkRecord, operation)).worker_key == owner.request_worker.worker_key
            await owner.request_worker.lease.stop()
        await third.admin_cancellations._drain(original.statistics_key, operation)
    finally:
        resume.set()
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 10)
    async with third.ctx.sessionmaker() as db:
        assert (await db.get(Chapter, chapter_id)).word_count == 1
        assert (await db.get(Task, original.id)).status == "cancelled"
        assert (await db.get(TaskStatistic, original.statistics_key)).status == "cancelled"
        assert await db.get(TaskWorkRecord, operation) is None
        assert await db.scalar(select(func.count(AdminAuditLog.id)).where(
            AdminAuditLog.operation_id == operation, AdminAuditLog.result == "success",
        )) == 1
