"""旧请求/执行体与SQLite真实整数ID复用不能串号或串记任务统计。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.core.errors import ConflictError, NotFoundError
from app.models import Task, TaskStatistic, User
from app.models.entities import FINISHED_TASK_STATUSES
from app.services import admin_service, task_service
from app.services import task_statistics_service as stats
from app.services.task_engine import engine as engine_module
from app.services.task_engine.hub import TaskHub


async def _users(db):
    actor = User(username="identity_admin", password_hash="x", is_admin=True)
    owner = User(username="identity_old", password_hash="x")
    other = User(username="identity_new", password_hash="x")
    db.add_all([actor, owner, other])
    await db.flush()
    return actor.id, owner.id, other.id


async def _new_task(db, user_id, status="queued"):
    task = Task(user_id=user_id, kind="chapter.analyze", status=status, output_text="新用户内容")
    db.add(task)
    await stats.record_created(db, task)
    if status in FINISHED_TASK_STATUSES:
        task.finished_at = stats.now_utc()
        await stats.record_state_change(db, task, status, finished_at=task.finished_at)
    return task


async def _replace_owner(sm, actor_id, owner_id, other_id, status):
    async with sm() as db:
        await admin_service.delete_user(db, await db.get(User, actor_id), owner_id)
        replacement = await _new_task(db, other_id, status)
        await db.commit()
        return replacement.id, replacement.statistics_key


@asynccontextmanager
async def _reused_task(app, status="queued", replacement_status="queued"):
    await app.state.task_engine.stop()
    sm = app.state.sessionmaker
    async with sm() as stale_db:
        actor_id, owner_id, other_id = await _users(stale_db)
        original = await _new_task(stale_db, owner_id, status)
        await stale_db.commit()
        old_id, old_key = original.id, original.statistics_key
        new_id, new_key = await _replace_owner(sm, actor_id, owner_id, other_id, replacement_status)
        assert new_id == old_id and new_key != old_key
        yield stale_db, original, new_key, other_id


async def _assert_replacement_intact(app, original, new_key, status="queued"):
    async with app.state.sessionmaker() as db:
        current = await db.get(Task, original.id)
        previous = await db.get(TaskStatistic, original.statistics_key)
        replacement = await db.get(TaskStatistic, new_key)
        assert current.statistics_key == new_key and current.status == status
        assert replacement.status == status and replacement.removed_at is None
        assert replacement.finished_event_at is None
        assert previous.removed_at is not None
        if previous.status not in FINISHED_TASK_STATUSES:
            assert previous.finished_event_at is None


@pytest.mark.parametrize("status", ["queued", "paused", "running"])
async def test_stale_cancel_cannot_cancel_reused_task_or_invent_old_terminal(app, status):
    async with _reused_task(app, status) as (db, original, new_key, _):
        with pytest.raises(NotFoundError):
            await task_service._cancel_loaded_impl(db, app.state.task_engine, original)
        await _assert_replacement_intact(app, original, new_key)


@pytest.mark.parametrize("operation", ["resume_task", "finish_paused_task"])
async def test_stale_paused_mutation_cannot_change_replacement(app, monkeypatch, operation):
    async with _reused_task(app, "paused", "paused") as (db, original, new_key, _):
        async def loaded(*args):
            return original

        monkeypatch.setattr(task_service, "get_owned_task", loaded)
        with pytest.raises((NotFoundError, ConflictError)):
            await getattr(task_service, operation)(db, app.state.task_engine, original.user_id, original.id)
        await _assert_replacement_intact(app, original, new_key, "paused")


async def test_stale_delete_does_not_remove_replacement_or_mark_its_statistics(app, monkeypatch):
    async with _reused_task(app, "failed") as (db, original, new_key, _):
        async def loaded(*args):
            return original

        monkeypatch.setattr(task_service, "get_owned_task", loaded)
        with pytest.raises(NotFoundError):
            await task_service.delete_task(db, original.user_id, original.id)
        await _assert_replacement_intact(app, original, new_key)


async def test_stale_prune_snapshot_cannot_remove_reused_active_task(app, monkeypatch):
    await app.state.task_engine.stop()
    sm = app.state.sessionmaker
    async with sm() as db:
        _, owner_id, other_id = await _users(db)
        for _ in range(201):
            await _new_task(db, owner_id, "failed")
        await db.commit()
    new_key = None

    async def replace_history(*args):
        nonlocal new_key
        async with sm() as other_db:
            await stats.mark_removed(other_db, Task.user_id == owner_id)
            await other_db.execute(delete(Task).where(Task.user_id == owner_id))
            replacement = await _new_task(other_db, other_id)
            await other_db.commit()
            assert replacement.id == 1
            new_key = replacement.statistics_key
        return set()

    monkeypatch.setattr(task_service, "_lock_history_projects", replace_history)
    async with sm() as db:
        await task_service.prune_finished(db, owner_id)
    async with sm() as db:
        row = await db.get(Task, 1)
        record = await db.get(TaskStatistic, new_key)
        assert row.statistics_key == new_key and row.status == "queued"
        assert record.removed_at is None


async def test_stale_dispatch_identity_does_not_claim_replacement(app):
    async with _reused_task(app) as (_, original, new_key, _):
        await app.state.task_engine._claim_and_run(original.id, original.statistics_key)
        await _assert_replacement_intact(app, original, new_key)


async def test_old_running_execution_does_not_start_replacement(app):
    async with _reused_task(app, "running") as (_, original, new_key, _):
        old_hub = TaskHub(original.id, statistics_key=original.statistics_key, status="running")
        await app.state.task_engine._run(original.id, old_hub)
        assert old_hub.closed
        await _assert_replacement_intact(app, original, new_key)


@pytest.mark.parametrize("bound", [True, False])
async def test_old_or_unbound_hub_cannot_flush_read_or_stream_replacement(app, bound):
    async with _reused_task(app, "running", "running") as (_, original, new_key, other_id):
        engine = app.state.task_engine
        old_hub = TaskHub(
            original.id, statistics_key=original.statistics_key if bound else None,
            status="running", progress=77, text="旧账号私有输出",
        )
        engine.hubs[original.id] = old_hub
        await engine._flush(old_hub)
        async with app.state.sessionmaker() as db:
            assert (await db.get(Task, original.id)).output_text == "新用户内容"
            assert await task_service.get_output(db, engine, other_id, original.id) == "新用户内容"
            assert (await task_service.get_task(db, engine, other_id, original.id)).progress == 0
        stale = engine.subscribe(original.id, statistics_key=original.statistics_key)
        assert (await anext(stale))[0] == "error"
        await stale.aclose()
        current = engine.subscribe(original.id, statistics_key=new_key)
        event, payload = await anext(current)
        assert event == "snapshot" and payload["text"] == "新用户内容"
        assert engine.hubs[original.id].statistics_key == new_key and old_hub.closed
        await current.aclose()
        await _assert_replacement_intact(app, original, new_key, "running")


async def test_recovery_snapshot_does_not_interrupt_replacement(app, monkeypatch):
    engine = app.state.task_engine
    await engine.stop()
    sm = app.state.sessionmaker
    async with sm() as db:
        actor_id, owner_id, other_id = await _users(db)
        original = await _new_task(db, owner_id, "running")
        await db.commit()
    new_key = None

    async def replace_during_abort(*args):
        nonlocal new_key
        new_id, new_key = await _replace_owner(sm, actor_id, owner_id, other_id, "running")
        assert new_id == original.id
        engine.ensure_hub(new_id, statistics_key=new_key, status="running", text="新用户内容")

    monkeypatch.setattr(engine_module, "_try_handler", lambda kind: SimpleNamespace(on_abort=replace_during_abort))
    await engine.recover()
    await _assert_replacement_intact(app, original, new_key, "running")
    assert engine.hubs[original.id].statistics_key == new_key and not engine.hubs[original.id].closed


async def test_old_close_or_project_cleanup_cannot_close_replacement_hub(app):
    async with _reused_task(app) as (_, original, new_key, _):
        engine = app.state.task_engine
        current, _ = engine.ensure_hub(original.id, statistics_key=new_key)
        engine.close_hub(original.id, "error", {}, statistics_key=original.statistics_key)
        engine.forget_project_hubs([original.id], statistics_keys={original.id: original.statistics_key})
        engine.close_hub(original.id, "error", {})  # 无身份的旧内部调用也不能关闭真实绑定。
        assert engine.hubs[original.id] is current and not current.closed
        engine.close_hub(original.id, "error", {}, statistics_key=new_key)
        assert current.closed and original.id not in engine.hubs


async def test_old_cancel_running_cannot_cancel_replacement_execution(app):
    async with _reused_task(app, "running", "running") as (_, original, new_key, _):
        engine = app.state.task_engine
        execution = asyncio.create_task(asyncio.Event().wait())
        engine._running[original.id] = execution
        engine._running_statistics_keys[original.id] = new_key
        try:
            await engine.cancel_running(original.id, statistics_key=original.statistics_key)
            await engine.cancel_running(original.id)
            assert not execution.done()
            await engine.cancel_running(original.id, statistics_key=new_key)
            assert execution.cancelled()
        finally:
            engine._running.pop(original.id, None)
            engine._running_statistics_keys.pop(original.id, None)
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)


async def test_cancel_cleanup_after_prune_await_keeps_replacement_hub(app, monkeypatch):
    engine = app.state.task_engine
    await engine.stop()
    sm = app.state.sessionmaker
    async with sm() as db:
        actor_id, owner_id, other_id = await _users(db)
        original = await _new_task(db, owner_id)
        await db.commit()
    new_key = None

    async def replace_during_prune(*args):
        nonlocal new_key
        new_id, new_key = await _replace_owner(sm, actor_id, owner_id, other_id, "queued")
        assert new_id == original.id
        engine.ensure_hub(new_id, statistics_key=new_key, text="新用户内容")

    monkeypatch.setattr(task_service, "prune_finished", replace_during_prune)
    async with sm() as db:
        await task_service.cancel_task(db, engine, owner_id, original.id)
    assert engine.hubs[original.id].statistics_key == new_key and not engine.hubs[original.id].closed
    await _assert_replacement_intact(app, original, new_key)


@pytest.mark.parametrize("operation", ["resume_task", "finish_paused_task"])
async def test_identity_swap_after_refresh_still_cannot_mutate_replacement(app, monkeypatch, operation):
    await app.state.task_engine.stop()
    sm = app.state.sessionmaker
    async with sm() as db:
        actor_id, owner_id, other_id = await _users(db)
        original = await _new_task(db, owner_id, "paused")
        await db.commit()
    actual_refresh = task_service.refresh_same_task
    new_key = None

    async def replace_after_refresh(db, task):
        nonlocal new_key
        result = await actual_refresh(db, task)
        if new_key is None:
            new_id, new_key = await _replace_owner(sm, actor_id, owner_id, other_id, "paused")
            assert new_id == original.id
        return result

    monkeypatch.setattr(task_service, "refresh_same_task", replace_after_refresh)
    async with sm() as db:
        with pytest.raises(ConflictError):
            await getattr(task_service, operation)(db, app.state.task_engine, owner_id, original.id)
    await _assert_replacement_intact(app, original, new_key, "paused")
