"""日采样的持久化、失败安全与无泄漏启停。"""

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.main import create_app
from app.models import Chapter, DashboardDailySnapshot, DashboardTracking, Project
from app.services import admin_dashboard_service as svc
from app.services import task_statistics_service
from tests.helpers import register

NOW = datetime(2026, 10, 31, 15, 59, 30, tzinfo=UTC)


async def initialized(app):
    await task_statistics_service.initialize_tracking(app.state.sessionmaker)
    async with app.state.sessionmaker() as db:
        (await db.get(DashboardTracking, 1)).started_at = NOW - timedelta(days=10)
        await db.commit()


async def test_snapshot_latest_success_and_no_backfill(app, client):
    await initialized(app)
    user = await register(client, "sample_owner")
    async with app.state.sessionmaker() as db:
        project = Project(user_id=user["id"], title="采样作品", deleted_at=NOW)
        db.add(project)
        await db.flush()
        chapter = Chapter(project_id=project.id, word_count=2, content="正文")
        db.add(chapter)
        await db.commit()
        chapter_id = chapter.id
    await svc.collect_snapshot(app.state.sessionmaker, captured_at=NOW)
    async with app.state.sessionmaker() as db:
        (await db.get(Chapter, chapter_id)).word_count = 5
        await db.commit()
    later = NOW + timedelta(seconds=10)
    await svc.collect_snapshot(app.state.sessionmaker, captured_at=later)
    # 迟到的旧采样不能覆盖较新一次已成功持久化的数据。
    await svc.collect_snapshot(app.state.sessionmaker, captured_at=NOW - timedelta(seconds=1))
    await svc.collect_snapshot(app.state.sessionmaker, captured_at=NOW + timedelta(days=2))
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(DashboardDailySnapshot).order_by(DashboardDailySnapshot.day)))
        assert [row.day for row in rows] == [date(2026, 10, 31), date(2026, 11, 2)]
        assert rows[0].captured_at.replace(tzinfo=UTC) == later
        assert (rows[0].users, rows[0].projects, rows[0].chapters, rows[0].total_words) == (1, 1, 1, 5)


async def test_snapshot_shanghai_midnight_and_failed_aggregate_does_not_write(app, monkeypatch):
    await initialized(app)
    await svc.collect_snapshot(app.state.sessionmaker, captured_at=NOW)
    midnight = datetime(2026, 10, 31, 16, tzinfo=UTC)
    await svc.collect_snapshot(app.state.sessionmaker, captured_at=midnight)

    async def fail(_db):
        raise RuntimeError("采样不可用")

    monkeypatch.setattr(svc, "_totals", fail)
    with pytest.raises(RuntimeError, match="采样不可用"):
        await svc.collect_snapshot(app.state.sessionmaker, captured_at=midnight + timedelta(days=1))
    async with app.state.sessionmaker() as db:
        days = list(await db.scalars(select(DashboardDailySnapshot.day).order_by(DashboardDailySnapshot.day)))
        assert days == [date(2026, 10, 31), date(2026, 11, 1)]
        assert await db.scalar(select(func.count(DashboardDailySnapshot.day))) == 2


async def test_snapshot_before_tracking_is_rejected_without_creating_history(app):
    await initialized(app)
    with pytest.raises(ValueError, match="早于统计起点"):
        await svc.collect_snapshot(app.state.sessionmaker, captured_at=NOW - timedelta(days=11))
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(DashboardDailySnapshot.day))) == 0


async def test_sampler_starts_immediately_and_concurrent_start_is_idempotent(monkeypatch):
    calls = []

    async def collect(sessionmaker):
        calls.append(sessionmaker)

    marker = object()
    monkeypatch.setattr(svc, "collect_snapshot", collect)
    sampler = svc.DashboardSampler(marker)
    await asyncio.gather(sampler.start(), sampler.start())
    assert calls == [marker]
    running = sampler._task
    await sampler.stop()
    await sampler.stop()
    assert running.done() and sampler._task is None
    await sampler.start()
    assert calls == [marker, marker]
    await sampler.stop()


async def test_sampler_continues_after_failure_and_cancels_inflight_pass(monkeypatch, caplog):
    reached = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0

    async def collect(_sessionmaker):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("暂时不可用")
        reached.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(svc, "collect_snapshot", collect)
    sampler = svc.DashboardSampler(object(), interval=0.001)
    await sampler.start()  # 首次失败不阻止服务启动，下一周期继续。
    await asyncio.wait_for(reached.wait(), 1)
    await asyncio.wait_for(sampler.stop(), 1)
    assert cancelled.is_set()
    assert sampler._task is None
    assert "管理员统计采样失败" in caplog.text


async def test_lifespan_initializes_before_engine_and_stops_sampler_before_dispose(app, monkeypatch):
    app = create_app(app.state.settings)
    events = []
    original_initialize = task_statistics_service.initialize_tracking
    engine_class = type(app.state.engine)
    original_dispose = engine_class.dispose

    async def initialize(sessionmaker):
        events.append("initialize")
        await original_initialize(sessionmaker)

    async def start_engine():
        async with app.state.sessionmaker() as db:
            assert await db.get(DashboardTracking, 1) is not None
        events.append("engine-start")

    async def start_sampler():
        events.append("sampler-start")

    async def stop_sampler():
        events.append("sampler-stop")

    async def dispose(engine, *args, **kwargs):
        if engine is app.state.engine:
            events.append("dispose")
        await original_dispose(engine, *args, **kwargs)

    monkeypatch.setattr(task_statistics_service, "initialize_tracking", initialize)
    monkeypatch.setattr(app.state.task_engine, "start", start_engine)
    monkeypatch.setattr(app.state.dashboard_sampler, "start", start_sampler)
    monkeypatch.setattr(app.state.dashboard_sampler, "stop", stop_sampler)
    monkeypatch.setattr(engine_class, "dispose", dispose)
    async with app.router.lifespan_context(app):
        assert events[:3] == ["initialize", "engine-start", "sampler-start"]
    assert events.index("sampler-stop") < events.index("dispose")
    assert not app.state.recycle_bin_cleaner._task
