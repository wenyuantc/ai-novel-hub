"""真实独立会话与事件屏障验证受理、资源持有和条件写，不调用模型。"""

import asyncio
from uuid import uuid4

import aiosqlite
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import AdminAuditLog, Chapter, ChapterVersion, GenerationLog, Project, Task, User
from app.models.dashboard import TaskStatistic
from app.schemas.admin_task import AdminTaskCancelRequest
from app.services import admin_task_service, task_service
from app.services.audit_writer import AuditOperationError
from app.services.task_engine.registry import get_handler
from tests.helpers import Gate, create_ai_config, create_project, register, wait_task


async def setup_task(app, client, *, status="queued", kind="agent.turn", project_id=None):
    await app.state.task_engine.stop()
    actor = await register(client, "task_manager")
    async with app.state.sessionmaker() as db:
        user = await db.get(User, actor["id"])
        user.is_admin = True
        task = Task(user_id=user.id, project_id=project_id, kind=kind, title="私密指令 sk-secret", status=status,
                    resource_key="test:resource", input={"secret": "secret"})
        db.add(task)
        await db.commit()
        return user, task


REQUEST = AdminTaskCancelRequest(confirm=True, reason="人工处理排队异常")


async def accept(app, actor, task):
    async with app.state.sessionmaker() as db:
        return await admin_task_service.cancel_task(db, app.state.task_engine, actor, task.statistics_key, REQUEST)


async def test_audit_failure_rolls_back_intent_without_signal(app, client, monkeypatch):
    actor, task = await setup_task(app, client)
    signals = []
    monkeypatch.setattr(app.state.task_engine, "request_admin_cancel", lambda *args: signals.append(args))
    original = admin_task_service.AuditOperation._event

    def fail_success(self, result, **kwargs):
        if result == "success":
            raise RuntimeError("secret body sk-secret https://private")
        return original(self, result, **kwargs)

    monkeypatch.setattr(admin_task_service.AuditOperation, "_event", fail_success)
    with pytest.raises(AuditOperationError, match="服务器内部错误"):
        await accept(app, actor, task)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.admin_cancel_operation_id is None and row.status == "queued"
        assert await db.scalar(select(func.count(TaskStatistic.key)).where(TaskStatistic.status == "cancelled")) == 0
        logs = list(await db.scalars(select(AdminAuditLog)))
        assert len(logs) == 1 and logs[0].result == "failure"
    assert signals == []


async def test_accepted_queued_holds_resource_until_abort_and_blocks_claim(app, client, monkeypatch):
    actor, task = await setup_task(app, client)
    reached, release = asyncio.Event(), asyncio.Event()

    async def abort(ctx, row):
        reached.set()
        await release.wait()

    monkeypatch.setattr(get_handler(task.kind), "on_abort", abort)
    accepted = await accept(app, actor, task)
    await asyncio.wait_for(reached.wait(), 3)
    engine = app.state.task_engine
    await engine._claim_and_run(task.id, task.statistics_key)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == "queued" and row.admin_cancel_operation_id == accepted.operation_id
        assert row.resource_key == "test:resource"
    release.set()
    await engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == "cancelled" and row.admin_cancel_operation_id is None
        logs = list(await db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "task.cancel_request")))
        assert len(logs) == 1 and logs[0].actor_user_id == actor.id
        assert logs[0].target_label == f"项目智能体 #{task.id}"
        assert logs[0].summary["accepted_from_status"] == "queued"


async def test_abort_failure_retains_queued_marker_then_retries(app, client, monkeypatch):
    actor, task = await setup_task(app, client)
    calls = []

    async def abort(ctx, row):
        calls.append(row.id)
        raise RuntimeError("private")

    handler = get_handler(task.kind)
    monkeypatch.setattr(handler, "on_abort", abort)
    accepted = await accept(app, actor, task)
    await app.state.task_engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == "queued" and row.admin_cancel_operation_id == accepted.operation_id
    async def success(ctx, row):
        calls.append(row.id)
    monkeypatch.setattr(handler, "on_abort", success)
    await app.state.task_engine.scan_admin_cancellations()
    await app.state.task_engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == "cancelled" and row.admin_cancel_operation_id is None
    assert len(calls) == 2


async def test_duplicate_pending_is_same_operation_and_not_new_audit(app, client, monkeypatch):
    actor, task = await setup_task(app, client, status="paused")
    monkeypatch.setattr(app.state.task_engine, "request_admin_cancel", lambda *args: None)
    first, second = await accept(app, actor, task), await accept(app, actor, task)
    assert first == second
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(AdminAuditLog.id))) == 1
        with pytest.raises(ConflictError):
            await task_service.resume_task(db, app.state.task_engine, actor.id, task.id)
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await task_service.finish_paused_task(db, app.state.task_engine, actor.id, task.id)


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "interrupted"])
async def test_terminal_cannot_be_accepted(app, client, status):
    actor, task = await setup_task(app, client, status=status)
    with pytest.raises(ConflictError):
        await accept(app, actor, task)


async def test_terminal_marker_anomaly_is_not_aborted_or_pruned(app, client, monkeypatch):
    actor, task = await setup_task(app, client, status="succeeded")
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == task.id).values(admin_cancel_operation_id=str(uuid4())))
        await db.commit()
    async def forbidden_abort(*args):
        raise AssertionError("不能对已成功任务重放旧abort")
    monkeypatch.setattr(get_handler(task.kind), "on_abort", forbidden_abort)
    await app.state.task_engine.scan_admin_cancellations()
    await app.state.task_engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        assert await task_service.clear_finished(db, actor.id) == 0
        with pytest.raises(ConflictError):
            await task_service.delete_task(db, actor.id, task.id)


@pytest.mark.parametrize("winner", ["succeeded", "paused"])
async def test_late_winner_clears_marker_in_original_finalization(app, client, monkeypatch, winner):
    actor, task = await setup_task(app, client, status="running")
    engine = app.state.task_engine
    reached, release = asyncio.Event(), asyncio.Event()
    original = engine.complete_state
    aborts = []

    async def gate(*args, **kwargs):
        reached.set()
        await release.wait()
        return await original(*args, **kwargs)

    async def abort(*args):
        aborts.append(True)

    monkeypatch.setattr(engine, "complete_state", gate)
    handler = get_handler(task.kind)
    monkeypatch.setattr(handler, "on_abort", abort)
    hub, _ = engine.ensure_hub(task.id, statistics_key=task.statistics_key, status="running")
    execution = asyncio.create_task(engine._finalize(
        task.id, task, handler, hub, status=winner, result={"accepted": True}, error=""
    ))
    engine._running[task.id] = execution
    engine._running_statistics_keys[task.id] = task.statistics_key
    await asyncio.wait_for(reached.wait(), 3)
    await accept(app, actor, task)
    release.set()
    await execution
    await engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == winner and row.admin_cancel_operation_id is None
        assert row.admin_cancel_requested_at is None
        statistic = await db.get(TaskStatistic, task.statistics_key)
        assert statistic.status == winner
        assert (statistic.finished_event_at is None) == (winner == "paused")
    assert aborts == []
    engine._on_done(task.id)


@pytest.mark.parametrize("original_status", ["queued", "paused", "running"])
async def test_restart_recovers_persisted_intent_without_model(app, client, monkeypatch, fake_llm, original_status):
    actor, task = await setup_task(app, client, status=original_status)
    engine = app.state.task_engine
    original = engine.request_admin_cancel
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: None)
    await accept(app, actor, task)
    monkeypatch.setattr(engine, "request_admin_cancel", original)
    await engine.start()
    await engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == ("interrupted" if original_status == "running" else "cancelled")
        assert row.admin_cancel_operation_id is None
    assert fake_llm.calls == []


async def test_slow_running_cleanup_stays_pending_and_cancel_is_once(app, client, fake_llm, monkeypatch):
    user = await register(client, "running_manager")
    async with app.state.sessionmaker() as db:
        actor = await db.get(User, user["id"])
        actor.is_admin = True
        await db.commit()
    project = await create_project(client)
    await create_ai_config(client)
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "取消中"})).json()
    stream, abort_gate = Gate(), Gate()
    fake_llm.push(["片段", stream, "不应继续"])
    task_out = (await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})).json()
    await stream.wait_reached()
    engine = app.state.task_engine
    engine.cancel_wait_seconds = .01
    handler = get_handler("chapter.generate")
    original_abort = handler.on_abort
    calls = []

    async def abort(ctx, task):
        calls.append(task.id)
        abort_gate.reached.set()
        await abort_gate.released.wait()
        await original_abort(ctx, task)

    monkeypatch.setattr(handler, "on_abort", abort)
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_out["id"])
    first = await accept(app, actor, task)
    await abort_gate.wait_reached()
    await engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == "running" and row.admin_cancel_operation_id == first.operation_id
    second = await accept(app, actor, task)
    assert first == second and calls == [task.id]
    assert engine.project_work(project["id"])
    abort_gate.release()
    ended = await wait_task(client, task.id)
    assert ended["status"] == "cancelled"
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task.id)).admin_cancel_operation_id is None


async def test_resume_starts_new_queued_clock(app, client):
    from datetime import UTC, datetime, timedelta

    actor, task = await setup_task(app, client, status="paused", kind="chapter.analyze")
    old = datetime.now(UTC) - timedelta(days=4)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == task.id).values(created_at=old, queued_at=old))
        await db.commit()
        await task_service.resume_task(db, app.state.task_engine, actor.id, task.id)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.created_at.replace(tzinfo=UTC) == old
        assert row.queued_at.replace(tzinfo=UTC) > old + timedelta(days=3)


@pytest.mark.parametrize("cancel_at", ["returning", "committed"])
async def test_admin_cancel_preserves_real_atomic_save_boundary(app, client, fake_llm, monkeypatch, cancel_at):
    """先持久受理，再把真实信号放到RETURNING/commit屏障，不能取消活驱动游标。"""
    user = await register(client, "atomic_manager")
    async with app.state.sessionmaker() as db:
        (await db.get(User, user["id"])).is_admin = True
        await db.commit()
    project = await create_project(client)
    await create_ai_config(client)
    chapter = (await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "保存竞争"})).json()
    stream, saving = Gate(), Gate()
    signal_ready, cancel_sent = asyncio.Event(), asyncio.Event()
    driver_cancelled = False
    engine = app.state.task_engine
    execute_original, commit_original = aiosqlite.Cursor.execute, AsyncSession.commit
    cancel_original = engine.cancel_running

    async def execute(cursor, sql, parameters=None):
        nonlocal driver_cancelled
        result = await execute_original(cursor, sql, parameters)
        if cancel_at == "returning" and sql.startswith("INSERT INTO generation_logs"):
            try:
                saving.reached.set()
                await saving.released.wait()
            except asyncio.CancelledError:
                driver_cancelled = True
                raise
        return result

    async def commit(db):
        chapter_save = any(isinstance(row, Chapter) and row.status == "done" for row in db.identity_map.values())
        await commit_original(db)
        if cancel_at == "committed" and chapter_save:
            saving.reached.set()
            await saving.released.wait()

    async def cancel(*args, **kwargs):
        signal_ready.set()
        await saving.wait_reached()
        waiting = asyncio.create_task(cancel_original(*args, **kwargs))
        await asyncio.sleep(0)  # 确切让cancel_running执行到它的首次await，不依赖墙钟猜测。
        assert args[0] in engine._cancel_requested
        cancel_sent.set()
        await waiting

    monkeypatch.setattr(aiosqlite.Cursor, "execute", execute)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(engine, "cancel_running", cancel)
    fake_llm.push(["开始", stream, "结束"])
    task_out = (await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})).json()
    await stream.wait_reached()
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_out["id"])
    response = await client.post(f"/api/v1/admin/tasks/{task.statistics_key}/cancel",
                                 json={"confirm": True, "reason": "验证安全收尾"})
    assert response.status_code == 202, response.text
    await asyncio.wait_for(signal_ready.wait(), 3)
    stream.release()
    try:
        await asyncio.wait_for(cancel_sent.wait(), 3)
    finally:
        saving.release()
    ended = await wait_task(client, task.id)
    await engine.wait_admin_cancellations()
    expected = "cancelled" if cancel_at == "returning" else "succeeded"
    assert ended["status"] == expected and not driver_cancelled
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        record = await db.get(TaskStatistic, task.statistics_key)
        assert row.status == record.status == expected
        assert row.admin_cancel_operation_id is None and row.admin_cancel_requested_at is None
        assert record.finished_event_at == row.finished_at
        assert await db.scalar(select(func.count(TaskStatistic.key)).where(
            TaskStatistic.finished_event_at.is_not(None)
        )) == 1
        saved = await db.get(Chapter, chapter["id"])
        assert saved.content == ("" if cancel_at == "returning" else "开始结束")
        logs = list(await db.scalars(select(GenerationLog).where(GenerationLog.chapter_id == chapter["id"])))
        assert [log.status for log in logs] == ["cancelled" if cancel_at == "returning" else "success"]
        assert bool(list(await db.scalars(select(ChapterVersion)))) == (cancel_at == "committed")
        saved.title = "后续独立SQLite事务可正常提交"
        await db.commit()


@pytest.mark.parametrize("winner", ["succeeded", "paused"])
async def test_final_state_save_failure_retries_known_winner_not_abort(app, client, monkeypatch, winner):
    actor, task = await setup_task(app, client, status="running")
    engine = app.state.task_engine
    request = engine.request_admin_cancel
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: None)
    accepted = await accept(app, actor, task)
    monkeypatch.setattr(engine, "request_admin_cancel", request)
    original_commit = AsyncSession.commit
    failed = False
    aborts = []

    async def commit(db):
        nonlocal failed
        if not failed and any(isinstance(row, Task) and row.status == winner for row in db.identity_map.values()):
            failed = True
            raise RuntimeError("private commit parameters")
        await original_commit(db)

    async def abort(*args):
        aborts.append(True)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    handler = get_handler(task.kind)
    monkeypatch.setattr(handler, "on_abort", abort)
    hub, _ = engine.ensure_hub(task.id, statistics_key=task.statistics_key, status="running")
    await engine._finalize(task.id, task, handler, hub, status=winner, result={"saved": True}, error="")
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert failed and row.status == "running" and row.admin_cancel_operation_id == accepted.operation_id
    await engine.scan_admin_cancellations()
    await engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == winner and row.admin_cancel_operation_id is None and row.result == {"saved": True}
    assert aborts == []


async def test_running_abort_failure_retains_barrier_and_restart_interrupts(app, client, monkeypatch):
    actor, task = await setup_task(app, client, status="running")
    engine = app.state.task_engine
    request = engine.request_admin_cancel
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: None)
    accepted = await accept(app, actor, task)
    handler = get_handler(task.kind)

    async def fail(*args):
        raise RuntimeError("private abort details")

    monkeypatch.setattr(handler, "on_abort", fail)
    hub, _ = engine.ensure_hub(task.id, statistics_key=task.statistics_key, status="running")
    await engine._finalize(task.id, task, handler, hub, status="cancelled", result=None, error="任务已取消")
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == "running" and row.admin_cancel_operation_id == accepted.operation_id
        record = await db.get(TaskStatistic, task.statistics_key)
        assert record is None or record.finished_event_at is None

    async def success(*args):
        return None

    monkeypatch.setattr(handler, "on_abort", success)
    monkeypatch.setattr(engine, "request_admin_cancel", request)
    await engine.start()
    await engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == "interrupted" and row.admin_cancel_operation_id is None


async def test_shutdown_waits_independent_drain_without_cancelling_cleanup(app, client, monkeypatch):
    actor, task = await setup_task(app, client)
    engine = app.state.task_engine
    abort_gate = Gate()
    stop_waiting = asyncio.Event()
    original_wait = engine.wait_admin_cancellations

    async def abort(*args):
        abort_gate.reached.set()
        await abort_gate.released.wait()

    async def waiting():
        stop_waiting.set()
        await original_wait()

    monkeypatch.setattr(get_handler(task.kind), "on_abort", abort)
    await accept(app, actor, task)
    await abort_gate.wait_reached()
    monkeypatch.setattr(engine, "wait_admin_cancellations", waiting)
    stopping = asyncio.create_task(engine.stop())
    await asyncio.wait_for(stop_waiting.wait(), 3)
    assert not stopping.done()
    abort_gate.release()
    await asyncio.wait_for(stopping, 3)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == "cancelled" and row.admin_cancel_operation_id is None


async def test_old_round_known_winner_cannot_finalize_resumed_round(app, client, monkeypatch):
    from datetime import timedelta

    actor, task = await setup_task(app, client, status="running")
    engine = app.state.task_engine
    engine.admin_cancellations.winners[task.statistics_key] = (
        task.queued_at.replace(tzinfo=None),
        dict(status="paused", result={"old": True}, error="", text="旧轮", progress=50),
    )
    async with app.state.sessionmaker() as db:
        await db.execute(update(Task).where(Task.id == task.id).values(queued_at=task.queued_at+timedelta(seconds=1)))
        await db.commit()
    calls = []

    async def abort(*args):
        calls.append(True)

    monkeypatch.setattr(get_handler(task.kind), "on_abort", abort)
    await accept(app, actor, task)
    await engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == "cancelled" and row.result is None
    assert calls == [True]


@pytest.mark.parametrize("winner", ["succeeded", "paused"])
async def test_final_commit_confirmation_loss_replays_persisted_fact(app, client, monkeypatch, winner):
    actor, task = await setup_task(app, client, status="running")
    engine = app.state.task_engine
    request = engine.request_admin_cancel
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: None)
    await accept(app, actor, task)
    monkeypatch.setattr(engine, "request_admin_cancel", request)
    original_commit = AsyncSession.commit
    lost = False

    async def commit(db):
        nonlocal lost
        completion = not lost and any(isinstance(row, Task) and row.status == winner
                                      for row in db.identity_map.values())
        await original_commit(db)
        if completion:
            lost = True
            raise RuntimeError("确认丢失，不得输出原SQL参数")

    monkeypatch.setattr(AsyncSession, "commit", commit)
    handler = get_handler(task.kind)
    hub, _ = engine.ensure_hub(task.id, statistics_key=task.statistics_key, status="running")
    _, subscriber = hub.subscribe()
    await engine._finalize(task.id, task, handler, hub, status=winner, result={"saved": True}, error="")
    event, payload = subscriber.queue.get_nowait()
    assert lost and event == ("done" if winner == "succeeded" else "paused")
    assert payload["task"]["status"] == winner and payload["result"] == {"saved": True}
    assert task.statistics_key not in engine.admin_cancellations.winners
    await engine.scan_admin_cancellations()
    await engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == winner and row.admin_cancel_operation_id is None
        assert (await db.get(TaskStatistic, task.statistics_key)).status == winner


async def test_completed_old_paused_round_cannot_skip_new_round_abort_repair(app, client, monkeypatch):
    actor, task = await setup_task(app, client, status="running", kind="chapter.analyze")
    engine = app.state.task_engine
    async with app.state.sessionmaker() as db:
        project = Project(user_id=actor.id, title="轮次清理")
        db.add(project)
        await db.flush()
        chapter = Chapter(project_id=project.id, chapter_no=1, status="draft")
        db.add(chapter)
        await db.flush()
        await db.execute(update(Task).where(Task.id == task.id).values(project_id=project.id))
        await db.commit()
        task = await db.get(Task, task.id)
    request = engine.request_admin_cancel
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: None)
    await accept(app, actor, task)
    monkeypatch.setattr(engine, "request_admin_cancel", request)
    hub, _ = engine.ensure_hub(task.id, statistics_key=task.statistics_key, status="running")
    handler = get_handler(task.kind)
    await engine._finalize(task.id, task, handler, hub, status="paused", result={}, error="")
    assert engine.admin_cancellations.completed_round(task)
    # 冻结物理时间也不得复用上一轮queued_at。
    from datetime import UTC

    from app.services import task_statistics_service
    frozen = task.queued_at.replace(tzinfo=UTC)
    monkeypatch.setattr(task_statistics_service, "now_utc", lambda: frozen)
    async with app.state.sessionmaker() as db:
        await task_service.resume_task(db, engine, actor.id, task.id)
        await db.execute(update(Chapter).where(Chapter.id == chapter.id).values(status="generating"))
        await db.commit()
        resumed = await db.get(Task, task.id)
        assert resumed.queued_at > task.queued_at.replace(tzinfo=None)
        assert not engine.admin_cancellations.completed_round(resumed)
    calls = []

    async def abort(ctx, row):
        calls.append(row.id)
        if len(calls) == 1:
            raise RuntimeError("新一轮普通取消收尾暂时失败")
        async with ctx.sessionmaker() as db:
            await db.execute(update(Chapter).where(Chapter.id == chapter.id).values(status="draft"))
            await db.commit()

    monkeypatch.setattr(handler, "on_abort", abort)
    async with app.state.sessionmaker() as db:
        with pytest.raises(RuntimeError):
            await task_service.cancel_task(db, engine, actor.id, task.id)
    assert project.id in engine._abort_failures
    async with app.state.sessionmaker() as db:
        await task_service.stop_project_tasks(db, engine, project.id)
        assert (await db.get(Chapter, chapter.id)).status == "draft"
    assert calls == [task.id, task.id]


async def test_cancelled_commit_ack_loss_never_replays_old_abort_on_new_resource(app, client, monkeypatch):
    actor, task = await setup_task(app, client, status="running", kind="chapter.analyze")
    engine = app.state.task_engine
    async with app.state.sessionmaker() as db:
        project = Project(user_id=actor.id, title="提交确认")
        db.add(project)
        await db.flush()
        chapter = Chapter(project_id=project.id, chapter_no=1, status="generating")
        db.add(chapter)
        await db.flush()
        await db.execute(update(Task).where(Task.id == task.id).values(project_id=project.id))
        await db.commit()
        task = await db.get(Task, task.id)
    request = engine.request_admin_cancel
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: None)
    await accept(app, actor, task)
    monkeypatch.setattr(engine, "request_admin_cancel", request)
    original_commit = AsyncSession.commit
    lost, aborts = False, []

    async def commit(db):
        nonlocal lost
        completion = not lost and any(isinstance(row, Task) and row.status == "cancelled"
                                      for row in db.identity_map.values())
        await original_commit(db)
        if completion:
            lost = True
            raise RuntimeError("提交成功但确认丢失")

    async def old_abort(ctx, row):
        aborts.append(row.id)
        async with ctx.sessionmaker() as db:
            await db.execute(update(Chapter).where(Chapter.id == chapter.id).values(status="draft"))
            await db.commit()

    old_handler = get_handler(task.kind)
    monkeypatch.setattr(old_handler, "on_abort", old_abort)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    hub, _ = engine.ensure_hub(task.id, statistics_key=task.statistics_key, status="running")
    await engine._finalize(task.id, task, old_handler, hub, status="cancelled", result=None, error="任务已取消")
    assert lost and engine.admin_cancellations.completed_round(task)
    async with app.state.sessionmaker() as db:
        await db.execute(update(Chapter).where(Chapter.id == chapter.id).values(status="generating"))
        new = Task(user_id=actor.id, project_id=project.id, chapter_id=chapter.id, kind="chapter.generate",
                   status="queued", resource_key=task.resource_key,
                   input={"chapter_id": chapter.id, "prev_status": "draft"})
        db.add(new)
        await db.commit()
    handler = get_handler("chapter.generate")
    original_abort = handler.on_abort

    async def new_abort(ctx, row):
        async with ctx.sessionmaker() as db:
            assert (await db.get(Chapter, chapter.id)).status == "generating"  # 旧abort未先误伤。
        await original_abort(ctx, row)

    monkeypatch.setattr(handler, "on_abort", new_abort)
    engine._abort_failures.add(project.id)  # 其他收尾故障也不能让项目级重试重放旧代次。
    async with app.state.sessionmaker() as db:
        await task_service.stop_project_tasks(db, engine, project.id)
        assert (await db.get(Chapter, chapter.id)).status == "draft"
    assert aborts == [task.id]


async def test_unknown_handler_recovery_preserves_admin_intent(app, client, monkeypatch, fake_llm):
    actor, task = await setup_task(app, client, status="running", kind="unknown-private-kind")
    engine = app.state.task_engine
    request = engine.request_admin_cancel
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: None)
    accepted = await accept(app, actor, task)
    monkeypatch.setattr(engine, "request_admin_cancel", request)
    await engine.start()
    await engine.wait_admin_cancellations()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == "running" and row.admin_cancel_operation_id == accepted.operation_id
        assert row.resource_key == task.resource_key
    assert fake_llm.calls == []


async def test_unknown_handler_finalization_cannot_release_pending_resource(app, client, monkeypatch):
    actor, task = await setup_task(app, client, status="running", kind="unknown-private-kind")
    engine = app.state.task_engine
    monkeypatch.setattr(engine, "request_admin_cancel", lambda *args: None)
    accepted = await accept(app, actor, task)
    hub, _ = engine.ensure_hub(task.id, statistics_key=task.statistics_key, status="running")
    await engine._finalize(task.id, task, None, hub, status="failed", result=None, error="未知内部错误")
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        assert row.status == "running" and row.admin_cancel_operation_id == accepted.operation_id


async def test_cancellation_runtime_caches_follow_existing_task_lifecycle(app, client):
    actor, task = await setup_task(app, client)
    engine = app.state.task_engine
    await accept(app, actor, task)
    await engine.wait_admin_cancellations()
    assert engine.admin_cancellations.completed_round(task)
    async with app.state.sessionmaker() as db:
        await task_service.delete_task(db, actor.id, task.id)
    await engine.scan_admin_cancellations()
    assert task.statistics_key not in engine.admin_cancellations.completed
