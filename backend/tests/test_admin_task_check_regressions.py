"""独立检查回归：已提交受理的确认丢失，以及 HTTP 生命周期与后台收尾隔离。"""

import asyncio
from uuid import uuid4

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAuditLog, Chapter, Project, Task, TaskStatistic, User
from app.services import admin_task_service, task_service, task_statistics_service
from app.services.task_engine.registry import get_handler
from tests.helpers import register

CANCEL_BODY = {"confirm": True, "reason": "检查已受理任务的安全收尾"}
PRIVATE_ERROR = "sk-check-private-6b8a https://private.example.test/SQL_PRIVATE_BODY"


async def _prepare(app, client, *, project=False):
    # 禁用兜底调度，下面显式扫描持久意图，确定验证确认丢失后的真实恢复路径。
    await app.state.task_engine.stop()
    actor = await register(client, "check_regression_admin")
    async with app.state.sessionmaker() as db:
        user = await db.get(User, actor["id"])
        user.is_admin = True
        record = Project(user_id=user.id, title="独立 HTTP 收尾") if project else None
        if record is not None:
            db.add(record)
            await db.flush()
        task = Task(user_id=user.id, project_id=record.id if record is not None else None,
                    kind="agent.turn", status="queued")
        db.add(task)
        await task_statistics_service.record_created(db, task)
        await db.commit()
        return task


async def _assert_completed_once(app, task, operation_id):
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        statistic = await db.get(TaskStatistic, task.statistics_key)
        logs = list(await db.scalars(select(AdminAuditLog).where(
            AdminAuditLog.action == "task.cancel_request"
        )))
        assert row.statistics_key == task.statistics_key
        assert row.status == statistic.status == "cancelled"
        assert row.admin_cancel_operation_id is row.admin_cancel_requested_at is None
        assert row.finished_at == statistic.finished_event_at
        assert len(logs) == 1 and logs[0].result == "success"
        assert logs[0].operation_id == operation_id
        assert logs[0].summary["cancel_reason"] == CANCEL_BODY["reason"]
        assert await db.scalar(select(func.count(TaskStatistic.key)).where(
            TaskStatistic.finished_event_at.is_not(None)
        )) == 1


async def test_acceptance_commit_confirmation_loss_keeps_success_and_scan_drains_once(
    app, client, monkeypatch, caplog, fake_llm
):
    task = await _prepare(app, client)
    engine = app.state.task_engine
    actual_commit = AsyncSession.commit
    lost_operation = None
    signals, aborts = [], []
    reached, release = asyncio.Event(), asyncio.Event()
    actual_signal = engine.request_admin_cancel

    def signal(*args):
        signals.append(args)
        actual_signal(*args)

    async def commit(db):
        nonlocal lost_operation
        accepted = next((row for row in db.identity_map.values() if isinstance(row, AdminAuditLog)
                         and row.action == "task.cancel_request" and row.result == "success"), None)
        should_lose = lost_operation is None and accepted is not None
        if should_lose:
            lost_operation = accepted.operation_id
        await actual_commit(db)  # 必须先完成真实数据库 commit，再丢失确认。
        if should_lose:
            raise RuntimeError(PRIVATE_ERROR)

    async def abort(ctx, row):
        aborts.append(row.statistics_key)
        reached.set()
        await release.wait()

    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(engine, "request_admin_cancel", signal)
    monkeypatch.setattr(get_handler(task.kind), "on_abort", abort)
    try:
        response = await client.post(f"/api/v1/admin/tasks/{task.statistics_key}/cancel", json=CANCEL_BODY)
        assert response.status_code == 500 and response.json() == {"detail": "服务器内部错误"}
        assert lost_operation is not None
        assert signals == aborts == fake_llm.calls == []
        assert PRIVATE_ERROR not in response.text + caplog.text
        assert all(record.exc_info is None for record in caplog.records)
        async with app.state.sessionmaker() as db:
            current = await db.get(Task, task.id)
            logs = list(await db.scalars(select(AdminAuditLog)))
            assert current.status == "queued" and current.admin_cancel_operation_id == lost_operation
            assert len(logs) == 1 and logs[0].result == "success"
            assert logs[0].operation_id == lost_operation
            assert (await db.get(TaskStatistic, task.statistics_key)).finished_event_at is None

        await engine.scan_admin_cancellations()
        await asyncio.wait_for(reached.wait(), 3)
        identity = (task.statistics_key, lost_operation)
        work = engine.admin_cancellations.work[identity]
        await engine.scan_admin_cancellations()
        assert engine.admin_cancellations.work == {identity: work}
        assert aborts == [task.statistics_key]
        release.set()
        await asyncio.wait_for(engine.wait_admin_cancellations(), 3)
        await _assert_completed_once(app, task, lost_operation)
        await engine.scan_admin_cancellations()
        assert engine.admin_cancellations.work == {}
        assert aborts == [task.statistics_key] and fake_llm.calls == []
    finally:
        release.set()
        await asyncio.wait_for(engine.wait_admin_cancellations(), 3)


async def test_cancelled_http_request_cannot_cancel_independent_registered_drain(
    app, client, monkeypatch, fake_llm
):
    task = await _prepare(app, client, project=True)
    engine = app.state.task_engine
    actual_cancel = admin_task_service.cancel_task
    response_ready, hold_response = asyncio.Event(), asyncio.Event()
    abort_reached, release_abort = asyncio.Event(), asyncio.Event()
    accepted, aborts = [], []

    async def cancel(*args, **kwargs):
        result = await actual_cancel(*args, **kwargs)
        accepted.append(result)
        response_ready.set()
        await hold_response.wait()  # 提交且登记独立 drain 后，HTTP 响应仍未交付。
        return result

    async def abort(ctx, row):
        aborts.append(row.statistics_key)
        abort_reached.set()
        await release_abort.wait()

    monkeypatch.setattr(admin_task_service, "cancel_task", cancel)
    monkeypatch.setattr(get_handler(task.kind), "on_abort", abort)
    request = asyncio.create_task(client.post(
        f"/api/v1/admin/tasks/{task.statistics_key}/cancel", json=CANCEL_BODY
    ))
    try:
        await asyncio.wait_for(response_ready.wait(), 3)
        await asyncio.wait_for(abort_reached.wait(), 3)
        identity = (task.statistics_key, accepted[0].operation_id)
        work = engine.admin_cancellations.work[identity]
        assert work in engine.project_work(task.project_id)
        request.cancel()
        outcome = await asyncio.gather(request, return_exceptions=True)
        assert isinstance(outcome[0], asyncio.CancelledError)
        assert not work.done() and work.cancelling() == 0
        assert work in engine.project_work(task.project_id)
        async with app.state.sessionmaker() as db:
            current = await db.get(Task, task.id)
            assert current.status == "queued" and current.admin_cancel_operation_id == accepted[0].operation_id
        await engine.scan_admin_cancellations()
        assert engine.admin_cancellations.work[identity] is work
        assert aborts == [task.statistics_key]
        release_abort.set()
        await asyncio.wait_for(engine.wait_admin_cancellations(), 3)
        await _assert_completed_once(app, task, accepted[0].operation_id)
        assert not engine.project_work(task.project_id)
        assert aborts == [task.statistics_key] and fake_llm.calls == []
    finally:
        hold_response.set()
        release_abort.set()
        await asyncio.gather(request, return_exceptions=True)
        await asyncio.wait_for(engine.wait_admin_cancellations(), 3)


async def test_drain_commit_confirmation_loss_cannot_replay_old_abort_on_new_resource(
    app, client, monkeypatch
):
    task = await _prepare(app, client, project=True)
    engine = app.state.task_engine
    async with app.state.sessionmaker() as db:
        chapter = Chapter(project_id=task.project_id, chapter_no=1, title="资源代次", status="generating")
        db.add(chapter)
        await db.flush()
        task = await db.get(Task, task.id)
        task.kind, task.status, task.chapter_id = "chapter.generate", "running", chapter.id
        task.resource_key = f"chapter:{chapter.id}"
        task.input = {"chapter_id": chapter.id, "prev_status": "draft"}
        await task_statistics_service.record_state_change(db, task, "running")
        await db.commit()
    actual_signal = engine.request_admin_cancel
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: None)
    response = await client.post(f"/api/v1/admin/tasks/{task.statistics_key}/cancel", json=CANCEL_BODY)
    assert response.status_code == 202, response.text
    operation_id = response.json()["operation_id"]
    handler = get_handler(task.kind)
    actual_abort = handler.on_abort

    async def fail_abort(*args):
        raise RuntimeError(PRIVATE_ERROR)

    monkeypatch.setattr(handler, "on_abort", fail_abort)
    old_hub, _ = engine.ensure_hub(task.id, statistics_key=task.statistics_key, status="running")
    await engine._finalize(task.id, task, handler, old_hub, status="cancelled", result=None, error="任务已取消")
    assert task.project_id in engine._abort_failures
    # owner 可以重连仍然 pending 的任务；后台成功后必须正确关闭这个新 Hub。
    hub, _ = engine.ensure_hub(task.id, statistics_key=task.statistics_key, status="running")
    actual_commit = AsyncSession.commit
    lost = False
    aborts, replacement_key = [], None

    async def abort(ctx, row):
        aborts.append(row.statistics_key)
        if row.statistics_key == replacement_key:
            async with app.state.sessionmaker() as db:
                assert (await db.get(Chapter, chapter.id)).status == "generating"
        await actual_abort(ctx, row)

    async def commit(db):
        nonlocal lost
        completion = not lost and any(isinstance(row, Task) and row.status == "cancelled"
                                     and row.statistics_key == task.statistics_key
                                     for row in db.identity_map.values())
        await actual_commit(db)
        if completion:
            lost = True
            raise RuntimeError(PRIVATE_ERROR)

    monkeypatch.setattr(handler, "on_abort", abort)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    actual_signal(task.statistics_key, operation_id)
    await asyncio.wait_for(engine.wait_admin_cancellations(), 3)
    async with app.state.sessionmaker() as db:
        ended = await db.get(Task, task.id)
        assert lost and ended.status == "cancelled" and ended.admin_cancel_operation_id is None
        assert engine.admin_cancellations.completed_round(ended)
        assert hub.closed
        fresh = Task(user_id=task.user_id, project_id=task.project_id, chapter_id=chapter.id,
                     kind=task.kind, status="queued", resource_key=task.resource_key, input=task.input)
        db.add(fresh)
        (await db.get(Chapter, chapter.id)).status = "generating"
        await task_statistics_service.record_created(db, fresh)
        await db.commit()
        replacement_key = fresh.statistics_key
    async with app.state.sessionmaker() as db:
        await task_service.stop_project_tasks(db, engine, task.project_id)
    # 旧代次只清理一次；真实项目排空仍须清理新任务，且进入其 abort 前资源保持 generating。
    assert aborts == [task.statistics_key, replacement_key]


async def test_cached_completion_lookup_is_bounded_without_blocking_pending_scan(app, client, monkeypatch):
    task = await _prepare(app, client)
    engine = app.state.task_engine
    operation_id = str(uuid4())
    async with app.state.sessionmaker() as db:
        current = await db.get(Task, task.id)
        current.admin_cancel_operation_id = operation_id
        current.admin_cancel_requested_at = task_statistics_service.now_utc()
        completed = [Task(user_id=task.user_id, kind="agent.turn", status="cancelled") for _ in range(601)]
        db.add_all(completed)
        await db.commit()
    for row in completed:
        engine.admin_cancellations.remember_completed(row)
    requests, parameter_counts = [], []
    monkeypatch.setattr(engine.admin_cancellations, "request", lambda *args: requests.append(args))

    def capture(conn, cursor, statement, parameters, context, many):
        if statement.startswith("SELECT") and "tasks.statistics_key" in statement:
            parameter_counts.append(len(parameters))

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await engine.scan_admin_cancellations()
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", capture)
    assert requests == [(task.statistics_key, operation_id)]
    assert parameter_counts and max(parameter_counts) <= 500
    assert all(engine.admin_cancellations.completed_round(row) for row in completed)
