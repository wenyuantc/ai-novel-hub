"""多个应用的周期维护可竞争同一条持久记录。"""

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.models import AdminAuditLog, AiUsageCall, DashboardDailySnapshot, Project
from app.services import (
    admin_dashboard_service,
    ai_usage_recording,
    ai_usage_storage,
    recycle_bin_service,
    task_statistics_service,
)
from tests.helpers import register
from tests.schema_helpers import isolated_worker_database


@pytest.fixture(params=("sqlite", "postgresql"))
async def settings_overrides(request, monkeypatch):
    if request.param == "sqlite":
        yield {}
    else:
        async with isolated_worker_database(monkeypatch, required=True) as overrides:
            yield overrides


async def test_two_maintenance_runners_preserve_results_without_contention_warnings(
    app, second_app, client, monkeypatch, caplog,
):
    await task_statistics_service.initialize_tracking(app.state.sessionmaker)
    user = await register(client, "periodic_owner")
    now = datetime.now(UTC)
    expired = now - timedelta(days=60)
    old_call, recent_call = str(uuid4()), str(uuid4())
    identity = ai_usage_storage.UsageCallIdentity(
        actor_id=None, payer_id=None, provider="gemini", model="test", model_type="text", kind="chapter.generate",
    )
    async with app.state.sessionmaker() as db:
        project = Project(user_id=user["id"], title="到期作品", deleted_at=expired)
        db.add(project)
        await ai_usage_storage.reserve_call(db, identity, call_id=old_call, started_at=now - timedelta(days=2))
        await ai_usage_storage.reserve_call(db, identity, call_id=recent_call, started_at=now)
        await db.commit()
        project_id = project.id

    both_selected = asyncio.Event()
    scans = 0
    original = recycle_bin_service.purge

    async def purge_after_both_scan(db, engine, actor_id, target_id, deleted_at, **kwargs):
        nonlocal scans
        if target_id == project_id and not both_selected.is_set():
            # SQLite 与 PostgreSQL 均确认两个扫描都在第一个清理提交前看到候选。
            scans += 1
            if scans == 2:
                both_selected.set()
            await both_selected.wait()
        return await original(db, engine, actor_id, target_id, deleted_at, **kwargs)

    monkeypatch.setattr(recycle_bin_service, "purge", purge_after_both_scan)
    async with asyncio.timeout(10):
        cleaned = await asyncio.gather(
            recycle_bin_service.purge_expired(app.state.task_engine, now=now),
            recycle_bin_service.purge_expired(second_app.state.task_engine, now=now),
        )
        with caplog.at_level(logging.WARNING):
            await asyncio.gather(
                app.state.dashboard_sampler._pass(),
                second_app.state.dashboard_sampler._pass(),
                ai_usage_recording.recover_stale_usage(app.state.sessionmaker),
                ai_usage_recording.recover_stale_usage(second_app.state.sessionmaker),
            )

    assert sorted(cleaned) == [0, 1]
    async with app.state.sessionmaker() as db:
        assert await db.get(Project, project_id) is None
        audit = list(await db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "project.expire")))
        assert len(audit) == 1 and audit[0].result == "success"
        snapshots = list(await db.scalars(select(DashboardDailySnapshot)))
        assert len(snapshots) == 1 and snapshots[0].projects == 0
        assert (await db.get(AiUsageCall, old_call)).status == "interrupted"
        assert (await db.get(AiUsageCall, recent_call)).status == "pending"
    assert not any(
        record.levelno >= logging.WARNING and record.name in {
            recycle_bin_service.__name__, admin_dashboard_service.__name__, ai_usage_recording.__name__,
        }
        for record in caplog.records
    ), caplog.text


async def test_cleanup_retries_contended_deletion_recovery_before_scanning_expired(monkeypatch, caplog):
    engine = SimpleNamespace(ctx=SimpleNamespace(sessionmaker=object()), lease_seconds=30)
    calls = []

    async def recover(sessionmaker, *, seconds):
        assert sessionmaker is engine.ctx.sessionmaker and seconds == engine.lease_seconds
        calls.append("recover")
        if len(calls) == 1:
            busy = sqlite3.OperationalError("busy")
            busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise OperationalError(None, None, busy)

    async def purge_expired(actual_engine):
        assert actual_engine is engine
        calls.append("purge")
        return 0

    monkeypatch.setattr(recycle_bin_service, "recover_pending_deletions", recover)
    monkeypatch.setattr(recycle_bin_service, "purge_expired", purge_expired)
    await recycle_bin_service.RecycleBinCleaner(engine)._pass()

    assert calls == ["recover", "recover", "purge"]
    assert not caplog.records
