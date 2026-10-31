"""完整任务统计：事务、初始化、去重与业务清理均不依赖保留任务行。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import DashboardTracking, Project, Task, TaskStatistic, User
from app.services import admin_service, recycle_bin_service, task_service
from app.services import task_statistics_service as stats
from app.services.task_engine.hub import TaskHub


async def _user(db, name="statistics_owner", *, admin=False):
    user = User(username=name, password_hash="not-a-real-password", is_admin=admin)
    db.add(user)
    await db.flush()
    return user


async def _task(db, user_id, *, status="queued", project_id=None):
    task = Task(user_id=user_id, project_id=project_id, kind="chapter.analyze", status=status)
    db.add(task)
    await stats.record_created(db, task)
    return task


async def _ended(db, task, status="failed"):
    stamp = stats.now_utc()
    task.status, task.finished_at = status, stamp
    await stats.record_state_change(db, task, status, finished_at=stamp)


async def test_new_creation_is_after_tracking_start_and_idempotent(app):
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        user = await _user(db)
        tracking = await db.get(DashboardTracking, 1)
        task = await _task(db, user.id)
        await stats.record_created(db, task)
        await db.commit()
        record = await db.get(TaskStatistic, task.statistics_key)
        created = record.created_event_at.replace(tzinfo=UTC)
        assert created >= tracking.started_at.replace(tzinfo=UTC)
        assert record.origin == "new"
        assert await db.scalar(select(func.count(TaskStatistic.key))) == 1


async def test_creation_rolls_back_with_original_transaction(app):
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        user = await _user(db)
        await db.commit()
        await _task(db, user.id)
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 0
        assert await db.scalar(select(func.count(TaskStatistic.key))) == 0


async def test_initialize_is_idempotent_and_does_not_backfill_old_events(app):
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        user = await _user(db)
        old = Task(
            user_id=user.id, kind="chapter.analyze", status="failed", statistics_key=None,
            finished_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        db.add(old)
        await db.commit()
        original_start = (await db.get(DashboardTracking, 1)).started_at
    await stats.initialize_tracking(app.state.sessionmaker)
    await stats.initialize_tracking(app.state.sessionmaker)
    async with app.state.sessionmaker() as db:
        record = await db.scalar(select(TaskStatistic))
        assert record.origin == "baseline" and record.status == "failed"
        assert record.created_event_at is None and record.finished_event_at is None
        assert (await db.get(DashboardTracking, 1)).started_at == original_start
        assert await db.scalar(select(func.count(TaskStatistic.key))) == 1


async def test_pause_resume_does_not_create_another_task_and_terminal_is_once(app):
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        user = await _user(db)
        task = await _task(db, user.id)
        for status in ("running", "paused", "queued", "running"):
            task.status = status
            await stats.record_state_change(db, task, status)
        await _ended(db, task, "succeeded")
        await db.commit()
        first_finished = (await db.get(TaskStatistic, task.statistics_key)).finished_event_at
        await stats.record_state_change(db, task, "failed")
        await db.commit()
        record = await db.get(TaskStatistic, task.statistics_key)
        assert record.status == "succeeded" and record.finished_event_at == first_finished
        assert await db.scalar(select(func.count(TaskStatistic.key))) == 1


async def test_task_id_reuse_has_new_statistics_identity(app):
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        user = await _user(db)
        first = await _task(db, user.id)
        await _ended(db, first)
        await db.commit()
        first_id, first_key = first.id, first.statistics_key
        await task_service.delete_task(db, user.id, first.id)
        second = await _task(db, user.id)
        await db.commit()
        assert second.id == first_id  # SQLite最高整数ID确实复用，不只模拟另一个任务。
        assert second.statistics_key != first_key
        old = await db.get(TaskStatistic, first_key)
        assert old.status == "failed" and old.removed_at is not None
        assert old.finished_event_at is not None
        assert await db.scalar(select(func.count(TaskStatistic.key))) == 2


async def test_prune_and_clear_keep_all_creation_and_result_statistics(app):
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        user = await _user(db)
        for _ in range(205):
            task = await _task(db, user.id)
            await _ended(db, task)
        await db.commit()
        await task_service.prune_finished(db, user.id)
        assert await db.scalar(select(func.count(Task.id))) == 200
        assert await db.scalar(select(func.count(TaskStatistic.key))) == 205
        assert await db.scalar(select(func.count(TaskStatistic.key)).where(TaskStatistic.removed_at.is_not(None))) == 5
        assert await task_service.clear_finished(db, user.id) == 200
        assert await db.scalar(select(func.count(Task.id))) == 0
        assert await db.scalar(select(func.count(TaskStatistic.key))) == 205
        assert await db.scalar(select(func.count(TaskStatistic.key)).where(
            TaskStatistic.finished_event_at.is_not(None), TaskStatistic.status == "failed"
        )) == 205


async def test_user_delete_marks_unfinished_without_fabricating_terminal(app):
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        actor = await _user(db, "statistics_admin", admin=True)
        owner = await _user(db)
        task = await _task(db, owner.id, status="running")
        await db.commit()
        key = task.statistics_key
        await admin_service.delete_user(db, actor, owner.id)
        record = await db.get(TaskStatistic, key)
        assert record.origin == "new" and record.status == "running"
        assert record.removed_at is not None and record.finished_event_at is None
        assert await db.scalar(select(func.count(Task.id))) == 0


async def test_purge_and_expiry_do_not_remove_statistics(app):
    await app.state.task_engine.stop()
    now = stats.now_utc()
    async with app.state.sessionmaker() as db:
        actor = await _user(db, "statistics_admin", admin=True)
        owner = await _user(db)
        projects = [Project(user_id=owner.id, title=title, deleted_at=now - timedelta(days=31))
                    for title in ("手动彻删", "自动到期")]
        db.add_all(projects)
        await db.flush()
        keys = []
        for project in projects:
            task = await _task(db, owner.id, project_id=project.id)
            await _ended(db, task)
            keys.append(task.statistics_key)
        await db.commit()
        await recycle_bin_service.purge(
            db, app.state.task_engine, actor.id, projects[0].id, projects[0].deleted_at, title=projects[0].title
        )
    assert await recycle_bin_service.purge_expired(app.state.task_engine, now=now) == 1
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == 0
        records = list(await db.scalars(select(TaskStatistic).where(TaskStatistic.key.in_(keys))))
        assert len(records) == 2
        assert all(row.removed_at is not None and row.finished_event_at is not None for row in records)


async def test_startup_recovery_records_once(app):
    engine = app.state.task_engine
    await engine.stop()
    async with app.state.sessionmaker() as db:
        user = await _user(db)
        task = await _task(db, user.id, status="running")
        await db.commit()
        key = task.statistics_key
    await engine.recover()
    async with app.state.sessionmaker() as db:
        first = (await db.get(TaskStatistic, key)).finished_event_at
    await engine.recover()
    async with app.state.sessionmaker() as db:
        record = await db.get(TaskStatistic, key)
        assert record.status == "interrupted" and record.finished_event_at == first
        assert first is not None


async def test_duplicate_finalize_cannot_overwrite_winning_terminal(app):
    engine = app.state.task_engine
    await engine.stop()
    async with app.state.sessionmaker() as db:
        user = await _user(db)
        task = await _task(db, user.id, status="running")
        await db.commit()
    await engine._finalize(task.id, task, None, TaskHub(task.id), status="succeeded", result={}, error="")
    await engine._finalize(task.id, task, None, TaskHub(task.id), status="failed", result=None, error="旧回调")
    async with app.state.sessionmaker() as db:
        record = await db.get(TaskStatistic, task.statistics_key)
        row = await db.get(Task, task.id)
        assert row.status == record.status == "succeeded"
        assert record.finished_event_at is not None


async def test_competing_finalizers_commit_one_terminal_and_one_result(app):
    engine = app.state.task_engine
    await engine.stop()
    async with app.state.sessionmaker() as db:
        user = await _user(db)
        task = await _task(db, user.id, status="running")
        await db.commit()
    await asyncio.gather(
        engine._finalize(task.id, task, None, TaskHub(task.id), status="succeeded", result={}, error=""),
        engine._finalize(task.id, task, None, TaskHub(task.id), status="failed", result=None, error="竞争失败"),
    )
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task.id)
        record = await db.get(TaskStatistic, task.statistics_key)
        assert row.status == record.status and row.status in ("succeeded", "failed")
        assert record.finished_event_at is not None
        assert await db.scalar(select(func.count(TaskStatistic.key)).where(
            TaskStatistic.finished_event_at.is_not(None)
        )) == 1


async def test_statistics_failure_rolls_back_terminal_update(app, monkeypatch):
    engine = app.state.task_engine
    await engine.stop()
    async with app.state.sessionmaker() as db:
        user = await _user(db)
        task = await _task(db, user.id, status="running")
        await db.commit()
        user_id, task_id, key = user.id, task.id, task.statistics_key

    original = stats.record_state_change

    async def fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("统计写入失败")

    monkeypatch.setattr(stats, "record_state_change", fail)
    with pytest.raises(RuntimeError, match="统计写入失败"):
        async with app.state.sessionmaker() as db:
            await task_service.cancel_task(db, engine, user_id, task_id)
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, task_id)).status == "running"
        record = await db.get(TaskStatistic, key)
        assert record.status == "running" and record.finished_event_at is None
