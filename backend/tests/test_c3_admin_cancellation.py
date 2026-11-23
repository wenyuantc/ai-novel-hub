"""两个进程先看到同一管理意图，再让第二个进程真正进入 drain。"""

import asyncio

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAuditLog, Chapter, Project, Task, TaskStatistic, TaskWorkRecord, User
from app.services.task_engine import admin_cleanup
from app.services.task_engine.registry import get_handler
from tests.helpers import Gate, register, wait_task
from tests.schema_helpers import isolated_worker_database
from tests.test_admin_task_cancellation import accept, setup_task
from tests.test_batch_generate import _setup, _start


@pytest.fixture
async def settings_overrides(monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield overrides


@pytest.mark.parametrize("status,first_is_peer", [("queued", False), ("paused", True)])
async def test_only_one_admin_cleanup_runs_across_workers(app, second_app, client, monkeypatch, status, first_is_peer):
    first, second = app.state.task_engine, second_app.state.task_engine
    await first.stop()
    await second.stop()
    if first_is_peer:
        first, second = second, first
    user = await register(client, "admin-drain")
    async with app.state.sessionmaker() as db:
        actor = await db.get(User, user["id"])
        actor.is_admin = True
        row = Task(user_id=actor.id, kind="agent.turn", status=status, title="双端取消", input={})
        db.add(row)
        await db.commit()
    monkeypatch.setattr(app.state.task_engine, "request_admin_cancel", lambda *_: None)
    result = await accept(app, actor, row)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def abort(ctx, task, **kwargs):
        calls.append(task.statistics_key)
        if len(calls) == 1:
            entered.set()
            await release.wait()

    handler = get_handler(row.kind)
    monkeypatch.setitem(vars(handler), "on_abort", abort)
    work = asyncio.create_task(first.admin_cancellations._drain(row.statistics_key, result.operation_id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(second.admin_cancellations._drain(row.statistics_key, result.operation_id), 4)
        async with app.state.sessionmaker() as db:
            actual = await db.get(Task, row.id)
            assert actual.status == status and actual.admin_cancel_operation_id == result.operation_id
        assert calls == [row.statistics_key]
    finally:
        release.set()
        await asyncio.wait_for(work, 5)
    async with app.state.sessionmaker() as db:
        actual = await db.get(Task, row.id)
        assert actual.status == "cancelled" and actual.admin_cancel_operation_id is None
        assert await db.scalar(select(func.count()).select_from(AdminAuditLog).where(
            AdminAuditLog.operation_id == result.operation_id)) == 1
        assert (await db.get(TaskStatistic, row.statistics_key)).status == "cancelled"


@pytest.mark.parametrize("kind", ["chapter.generate", "chapter.batch"])
async def test_admin_on_peer_only_executor_cancels_running_work(app, second_app, client, fake_llm, monkeypatch, kind):
    project, base, chapters, _ = await _setup(client, [""])
    engine, peer = app.state.task_engine, second_app.state.task_engine
    chapter = chapters[0]
    async with app.state.sessionmaker() as db:
        user = await db.scalar(select(User))
        user.is_admin = True
        await db.commit()
    gate = Gate()
    fake_llm.push(["原执行", gate, "余文"])
    if kind == "chapter.batch":
        response = await _start(client, base, chapter, chapter)
    else:
        response = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
    assert response.status_code == 202, response.text
    ref = response.json()
    signals = []

    async def unexpected_cancel(*args, **kwargs):
        raise AssertionError("另一进程不得向本地执行表发送取消")

    cancel = engine.cancel_running

    async def stop(*args, **kwargs):
        signals.append(args[0])
        return await cancel(*args, **kwargs)

    monkeypatch.setattr(peer, "cancel_running", unexpected_cancel)
    monkeypatch.setattr(engine, "cancel_running", stop)
    try:
        await gate.wait_reached()
        await peer.start()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second_app), base_url="http://testserver",
                                     cookies=client.cookies) as remote:
            endpoint = f"/api/v1/admin/tasks/{ref['task_key']}/cancel"
            answer = await remote.post(endpoint, json={"confirm": True, "reason": "异地受理"})
            assert answer.status_code == 202, answer.text
            operation = answer.json()["operation_id"]
        await peer.wait_admin_cancellations()
        # 受理者确实扫描并排空了自己的 drain；只有 A 能发送真正的停止信号。
        await engine.scan_admin_cancellations()
        await engine.wait_admin_cancellations()
        ended = await wait_task(client, ref)
        assert ended["status"] == "cancelled"
        assert signals == [ref["id"]]
        async with second_app.state.sessionmaker() as db:
            original = await db.get(Chapter, chapter["id"])
            assert original.generation_claim_key is None and original.project_id == project["id"]
            assert (await db.get(Task, ref["id"])).admin_cancel_operation_id is None
            assert (await db.get(TaskStatistic, ref["task_key"])).status == "cancelled"
            assert await db.scalar(select(func.count(AdminAuditLog.id)).where(
                AdminAuditLog.operation_id == operation, AdminAuditLog.result == "success")) == 1
    finally:
        gate.release()


@pytest.mark.parametrize("fault", ["inside_handler", "before_terminal"])
async def test_fenced_admin_worker_resumes_at_committed_receipt(app, second_app, client, monkeypatch, fault):
    first, second = app.state.task_engine, second_app.state.task_engine
    actor, task = await setup_task(app, client)
    await second.stop()
    async with app.state.sessionmaker() as db:
        resource = Project(user_id=actor.id, title="资源")
        db.add(resource)
        await db.flush()
        (await db.get(Task, task.id)).project_id = resource.id
        await db.commit()
    monkeypatch.setattr(first, "request_admin_cancel", lambda *_: None)
    operation = (await accept(app, actor, task)).operation_id
    calls = []

    async def abort(ctx, row, **kwargs):
        calls.append(row.id)
        async with ctx.sessionmaker() as child:
            project = await child.get(Project, row.project_id)
            project.title += "已释放"
            await child.commit()
        if len(calls) == 1 and fault == "inside_handler":
            raise RuntimeError("业务提交后、清理回执前丢失进程")

    async def fail_terminal(*args, **kwargs):
        raise RuntimeError("清理已提交，任务终态前丢失进程")

    # 故障退出没有归还认领：另一端须等原 HTTP worker 被确认停止。
    relinquish = admin_cleanup.relinquish

    async def simulate_crash(engine, record):
        if engine is not first:
            await relinquish(engine, record)

    monkeypatch.setitem(vars(get_handler(task.kind)), "on_abort", abort)
    monkeypatch.setattr(first, "complete_state", fail_terminal)
    monkeypatch.setattr(admin_cleanup, "relinquish", simulate_crash)
    await first.admin_cancellations._drain(task.statistics_key, operation)
    await second.admin_cancellations._drain(task.statistics_key, operation)
    async with app.state.sessionmaker() as db:
        work = await db.get(TaskWorkRecord, operation)
        assert work.worker_key == first.request_worker.worker_key
        assert work.state == ("active" if fault == "inside_handler" else "aborted")
        assert (await db.get(Project, resource.id)).title == ("资源" if fault == "inside_handler" else "资源已释放")
        assert (await db.get(Task, task.id)).status == "queued"
        assert calls == [task.id]

    await first.request_worker.stop()
    await second.admin_cancellations._drain(task.statistics_key, operation)
    async with second_app.state.sessionmaker() as db:
        assert (await db.get(Project, resource.id)).title == "资源已释放"
        assert (await db.get(Task, task.id)).status == "cancelled"
        assert (await db.get(TaskStatistic, task.statistics_key)).status == "cancelled"
        assert await db.get(TaskWorkRecord, operation) is None
        assert calls == ([task.id, task.id] if fault == "inside_handler" else [task.id])


async def test_online_orphan_adoption_survives_crash_before_cleanup_claim(app, second_app, client, monkeypatch):
    actor, task = await setup_task(app, client, status="running")
    engine, peer = app.state.task_engine, second_app.state.task_engine
    await peer.stop()
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *_: None)
    operation = (await accept(app, actor, task)).operation_id
    real_load = engine.admin_cancellations._load
    loads = 0

    async def crash_after_adopt(*args):
        nonlocal loads
        loads += 1
        return await real_load(*args) if loads == 1 else None

    monkeypatch.setattr(engine.admin_cancellations, "_load", crash_after_adopt)
    await engine.admin_cancellations._drain(task.statistics_key, operation)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.worker_key == engine.request_worker.worker_key and row.status == "running"
    await engine.request_worker.stop()
    await peer.start()
    await peer.wait_admin_cancellations()
    async with second_app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == "cancelled" and row.admin_cancel_operation_id is None
        assert await db.get(TaskWorkRecord, operation) is None


async def test_live_http_worker_retries_lost_admin_release_with_scheduler_stopped(app, second_app, client, monkeypatch):
    actor, task = await setup_task(app, client)
    first, peer = app.state.task_engine, second_app.state.task_engine
    await peer.stop()
    await first.request_worker.stop()
    monkeypatch.setattr(first, "heartbeat_interval", .03)
    await first.request_worker.start()
    monkeypatch.setattr(first, "request_admin_cancel", lambda *_: None)
    operation = (await accept(app, actor, task)).operation_id
    relinquish = admin_cleanup.relinquish
    allow_retry, released = asyncio.Event(), asyncio.Event()
    effects = []

    async def abort(ctx, row, **kwargs):
        effects.append(row.id)

    async def fail_terminal(*args, **kwargs):
        raise RuntimeError("终态前失败")

    async def lose_release(engine, record):
        if engine is first and not allow_retry.is_set():
            raise RuntimeError("工作退出确认暂时不可用")
        await relinquish(engine, record)
        if engine is first:
            released.set()

    monkeypatch.setitem(vars(get_handler(task.kind)), "on_abort", abort)
    monkeypatch.setattr(first, "complete_state", fail_terminal)
    monkeypatch.setattr(admin_cleanup, "relinquish", lose_release)
    await first.admin_cancellations._drain(task.statistics_key, operation)
    await peer.admin_cancellations._drain(task.statistics_key, operation)
    async with app.state.sessionmaker() as db:
        work = await db.get(TaskWorkRecord, operation)
        assert work.state == "aborted" and work.worker_key == first.request_worker.worker_key
    allow_retry.set()
    await asyncio.wait_for(released.wait(), 3)
    await peer.admin_cancellations._drain(task.statistics_key, operation)
    async with second_app.state.sessionmaker() as db:
        assert await db.get(TaskWorkRecord, operation) is None
        assert (await db.get(Task, task.id)).status == "cancelled"
        assert effects == [task.id]


@pytest.mark.parametrize("winner", ["succeeded", "paused"])
async def test_admin_after_batch_output_preserves_proven_result(app, second_app, client, fake_llm, monkeypatch, winner):
    _, base, chapters, _ = await _setup(client, ["", ""] if winner == "paused" else [""])
    engine, peer = app.state.task_engine, second_app.state.task_engine
    async with app.state.sessionmaker() as db:
        actor = await db.scalar(select(User))
        actor.is_admin = True
        await db.commit()
    reached, release = asyncio.Event(), asyncio.Event()
    real_complete = engine.complete_state

    async def complete(*args, **kwargs):
        if kwargs["status"] == winner and not reached.is_set():
            reached.set()
            await release.wait()
        return await real_complete(*args, **kwargs)

    fake_llm.push("已保存的整章正文")
    monkeypatch.setattr(engine, "complete_state", complete)
    ref = (await _start(client, base, chapters[0], chapters[-1], review_every=1)).json()
    try:
        await asyncio.wait_for(reached.wait(), 5)
        execution = engine._running[ref["id"]]
        await accept(second_app, actor, ref["task_key"])
        await peer.wait_admin_cancellations()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.shield(execution), 6)
    await engine.scan_admin_cancellations()
    await engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, ref["id"])
        assert row.status == winner and row.admin_cancel_operation_id is None
        assert row.result["done"] == 1 and (await db.get(Chapter, chapters[0]["id"])).content == "已保存的整章正文"
        assert (await db.get(TaskStatistic, ref["task_key"])).status == winner


@pytest.mark.parametrize("status", ["queued", "running"])
async def test_live_worker_reconciles_lost_claim_commit_without_scheduler(app, second_app, client, monkeypatch, status):
    actor, task = await setup_task(app, client, status=status)
    first, peer = app.state.task_engine, second_app.state.task_engine
    await peer.stop()
    await first.request_worker.stop()
    monkeypatch.setattr(first, "heartbeat_interval", .03)
    await first.request_worker.start()
    monkeypatch.setattr(first, "request_admin_cancel", lambda *_: None)
    operation = (await accept(app, actor, task)).operation_id
    allow_retry, completed, unavailable = asyncio.Event(), asyncio.Event(), asyncio.Event()
    schedule, finish = first.admin_cancellations.schedule_confirmations, first.admin_cancellations._after_completion
    load = first.admin_cancellations._load
    commit = AsyncSession.commit
    lost = False
    calls = []

    async def lose_commit(db):
        nonlocal lost
        requested = any(isinstance(row, TaskWorkRecord) and row.work_key == operation for row in db.new)
        await commit(db)
        if requested and not lost:
            lost = True
            raise RuntimeError("原认领已提交，但未收到确认")

    async def abort(ctx, row, **kwargs):
        calls.append(row.id)

    async def confirm(row):
        await finish(row)
        completed.set()

    def heartbeat():
        if allow_retry.is_set():
            schedule()

    async def uncertain_read(*args):
        if allow_retry.is_set() and not unavailable.is_set():
            unavailable.set()
            raise RuntimeError("第一次核对时主库仍未恢复")
        return await load(*args)

    monkeypatch.setattr(first.admin_cancellations, "_load", uncertain_read)
    monkeypatch.setattr(AsyncSession, "commit", lose_commit)
    monkeypatch.setattr(first.admin_cancellations, "schedule_confirmations", heartbeat)
    monkeypatch.setattr(first.admin_cancellations, "_after_completion", confirm)
    monkeypatch.setitem(vars(get_handler(task.kind)), "on_abort", abort)
    await first.admin_cancellations._drain(task.statistics_key, operation)
    await peer.admin_cancellations._drain(task.statistics_key, operation)
    async with second_app.state.sessionmaker() as db:
        assert lost and calls == [] and (await db.get(Task, task.id)).status == status
        work = await db.get(TaskWorkRecord, operation)
        assert work.state == ("active" if status == "queued" else "ready")
    allow_retry.set()
    await asyncio.wait_for(completed.wait(), 4)
    assert unavailable.is_set()
    async with second_app.state.sessionmaker() as db:
        assert (await db.get(Task, task.id)).status == "cancelled"
        assert await db.get(TaskWorkRecord, operation) is None
        assert calls == [task.id]
