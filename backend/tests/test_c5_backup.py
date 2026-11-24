"""两个独立应用共享数据库的备份竞争与存储归属。"""

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BackupArtifact, BackupRun, BackupSettings
from app.services.backups import database, mail
from app.services.backups.service import BackupGoneError, BackupUnavailableError
from app.services.backups.storage import LocalStore
from tests.test_backup_runtime import _accept, _finished


async def _configure(client, **changes):
    response = await client.patch("/api/v1/admin/backups/settings", json={
        "revision": 0, "changes": {"encryption_enabled": False, **changes},
    })
    assert response.status_code == 200, response.text


async def test_only_one_database_claim_can_execute_and_notify(app, second_app, system_admin, monkeypatch, tmp_path):
    await _configure(
        system_admin, smtp_host="smtp.example.com", smtp_from_address="backup@example.com",
        smtp_recipients=["alerts@example.com"],
    )
    first, peer = app.state.backup_runtime, second_app.state.backup_runtime
    # 不同挂载点使两方都能获得文件锁，确保真正由数据库条件更新决定胜负。
    peer.settings.backup_dir = str(tmp_path / "different-backup-root")
    run_id = await _accept(system_admin)
    both_saw_queued = asyncio.Barrier(2)

    def align_candidate(original):
        async def read_candidate(model, row_id):
            row = await original(model, row_id)
            if model is BackupRun and row_id == run_id and row.status == "queued":
                await both_saw_queued.wait()
            return row

        return read_candidate

    for runtime in (first, peer):
        monkeypatch.setattr(runtime, "_read", align_candidate(runtime._read))
    snapshots, notices = [], []

    def failed_snapshot(*args, **kwargs):
        snapshots.append(args[1])
        raise OSError("controlled snapshot failure")

    def sent(_config, notice, _policy, **kwargs):
        notices.append(notice.run_id)
        return mail.MailResult("sent")

    monkeypatch.setattr(database, "snapshot_database", failed_snapshot)
    monkeypatch.setattr(mail, "send_failure_notification", sent)
    async with asyncio.timeout(5):
        attempted = await asyncio.gather(first._execute(run_id), peer._execute(run_id))
    row = await _finished(app, run_id)
    assert sorted(attempted) == [False, True]
    assert len(snapshots) == 1 and notices == [run_id]
    assert row.status == "failed" and row.notification_status == "sent"


async def test_invalid_backup_directory_finishes_attempt_and_releases_slot(app, system_admin, tmp_path):
    await _configure(system_admin)
    invalid = tmp_path / "backup-dir-is-a-file"
    invalid.write_text("运维误配置，原文件须保留", encoding="utf-8")
    runtime = app.state.backup_runtime
    runtime.settings.backup_dir = str(invalid)
    run_id = await _accept(system_admin)

    assert await runtime._execute(run_id)
    row = await _finished(app, run_id)
    assert row.status == "failed" and row.error_code == "storage_failed"
    assert invalid.read_text(encoding="utf-8") == "运维误配置，原文件须保留"


async def test_unconfirmed_claim_can_be_recovered_without_replaying_snapshot(app, system_admin, monkeypatch):
    await _configure(system_admin)
    runtime = app.state.backup_runtime
    run_id = await _accept(system_admin)
    commit = AsyncSession.commit
    check_claim = runtime._check_claim

    async def lost_commit_response(db):
        reserved = any(
            isinstance(row, BackupRun) and row.id == run_id and row.status == "running"
            for row in db.identity_map.values()
        )
        await commit(db)
        if reserved:
            raise OSError("领取提交回执丢失")

    async def lost_first_confirmation(db, claim):
        if not claim.recovery:
            raise OSError("首次领取核对同时断开")
        await check_claim(db, claim)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", lost_commit_response)
        patch.setattr(runtime, "_check_claim", lost_first_confirmation)
        with pytest.raises(BackupUnavailableError, match="暂时无法确认"):
            await runtime._execute(run_id)
    async with app.state.sessionmaker() as db:
        reserved = await db.get(BackupRun, run_id)
        assert reserved.status == "running" and reserved.worker_key == runtime.worker.worker_key

    await runtime._recover(include_legacy_queued=False)
    async with app.state.sessionmaker() as db:
        checked = await db.get(BackupRun, run_id)
        artifact = await db.scalar(select(BackupArtifact).where(BackupArtifact.run_id == run_id))
        assert checked.status == "interrupted" and checked.active_slot is None
        assert artifact.filename is None


async def test_starting_peer_does_not_interrupt_an_existing_backup(app, second_app, system_admin, monkeypatch):
    await _configure(system_admin)
    first, peer = app.state.backup_runtime, second_app.state.backup_runtime
    await first.start()
    reached = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    snapshot = database.snapshot_database
    workdir = None

    def blocked(database_url, output, **kwargs):
        nonlocal workdir
        workdir = output.parent
        loop.call_soon_threadsafe(reached.set)
        if not release.wait(5):
            raise TimeoutError
        return snapshot(database_url, output, **kwargs)

    monkeypatch.setattr(database, "snapshot_database", blocked)
    try:
        run_id = await _accept(system_admin)
        await asyncio.wait_for(reached.wait(), 3)
        await peer.start()
        async with app.state.sessionmaker() as db:
            row = await db.get(BackupRun, run_id)
            assert row.status == "running" and row.active_slot == 1
        assert workdir is not None and workdir.is_dir()
        release.set()
        assert (await _finished(app, run_id)).status == "succeeded"
    finally:
        release.set()
        await first.stop()
        await peer.stop()


async def test_peer_recovers_an_abandoned_run_and_an_abandoned_recovery(
    app, second_app, peer_app_factory, system_admin,
):
    await _configure(system_admin)
    recovery_peer = await peer_app_factory()
    first_owner = app.state.task_engine.request_worker
    old_recovery = recovery_peer.state.task_engine.request_worker
    run_id = await _accept(system_admin)
    async with app.state.sessionmaker() as db:
        row = await db.get(BackupRun, run_id)
        row.status = "running"
        row.started_at = datetime.now(UTC)
        row.worker_key = first_owner.worker_key
        row.recovery_worker_key = old_recovery.worker_key
        row.execution_host = second_app.state.backup_runtime.hostname
        row.notification_status = "sending"
        local = LocalStore(Path(app.state.settings.backup_dir), row.instance_key)
        workdir = local.temporary_directory(row.execution_key)
        (workdir / "owned.txt").write_text("此前执行者留下的文件", encoding="utf-8")
        await db.commit()
    await first_owner.stop()
    await old_recovery.stop()
    peer = second_app.state.backup_runtime
    await peer.start()
    try:
        row = await _finished(app, run_id)
        assert row.status == "interrupted" and row.error_code == "interrupted"
        assert row.worker_key is None and row.recovery_worker_key is None
        assert row.notification_status == "unknown"
        assert not workdir.exists()
    finally:
        await peer.stop()


async def test_peers_competing_for_same_schedule_treat_loser_as_noop(
    app, second_app, system_admin, monkeypatch, caplog,
):
    await _configure(system_admin)
    first, peer = app.state.backups, second_app.state.backups
    now = datetime(2026, 11, 22, 4, tzinfo=UTC)
    async with app.state.sessionmaker() as db:
        settings = await db.get(BackupSettings, 1)
        settings.configuration = {**settings.configuration, "schedule_enabled": True}
        settings.next_scheduled_at = now
        await db.commit()

    loaded = asyncio.Barrier(2)

    def synchronize_load(original):
        async def load_both():
            await original()
            await loaded.wait()

        return load_both

    for manager in (first, peer):
        await manager.load()
        monkeypatch.setattr(manager, "_load_locked", synchronize_load(manager._load_locked))

    async with asyncio.timeout(5):
        results = await asyncio.gather(first.accept_scheduled(now), peer.accept_scheduled(now))
    assert sum(result is not None for result in results) == 1
    assert all(record.levelno < 30 for record in caplog.records if record.name.startswith("app.services.backups"))
    async with app.state.sessionmaker() as db:
        assert list(await db.scalars(select(BackupRun.scheduled_date))) == ["2026-11-22"]


async def test_wrong_host_missing_local_file_keeps_download_record(app, second_app, system_admin, tmp_path):
    await _configure(system_admin)
    first, peer = app.state.backup_runtime, second_app.state.backup_runtime
    first.hostname = "backup-writer"
    peer.hostname = "reader-on-another-host"
    await first.start()
    try:
        run_id = await _accept(system_admin)
        assert (await _finished(app, run_id)).status == "succeeded"
        async with app.state.sessionmaker() as db:
            artifact = await db.scalar(select(BackupArtifact).where(BackupArtifact.run_id == run_id))
        shared = await peer.prepare_download(run_id, "local")
        assert (b"".join([chunk async for chunk in shared.iter_chunks()])).startswith(b"SQLite format 3\x00")
        peer.settings.backup_dir = str(tmp_path / "different-backup-root")
        with pytest.raises(BackupUnavailableError, match="写入主机"):
            await peer.prepare_download(run_id, "local")
        async with app.state.sessionmaker() as db:
            assert (await db.get(BackupArtifact, artifact.id)).status == "ready"

        path = tmp_path / "backups" / artifact.locator["instance_key"] / artifact.filename
        path.unlink()
        with pytest.raises(BackupGoneError, match="已不存在"):
            await first.prepare_download(run_id, "local")
        async with app.state.sessionmaker() as db:
            assert (await db.get(BackupArtifact, artifact.id)).status == "missing"
    finally:
        await first.stop()
