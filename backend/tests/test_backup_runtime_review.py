"""运行器独立复核：外部副作用发生后的确认丢失不能假定未保存。"""

from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import BackupArtifact, BackupRun
from app.services.backups import crypto
from app.services.backups.process import BackupAdapterError
from app.services.backups.runtime import BackupRuntime
from app.services.backups.service import BackupManager
from app.services.backups.storage import LocalStore, S3Store
from tests.test_backup_runtime import _accept, _configured, _enable_s3, _finished
from tests.test_backup_runtime import s3_memory as s3_memory


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("cleanup_status", ["failed", "unknown", "pending"])
async def test_restart_retries_owned_temporary_files_after_terminal_cleanup_failure(
    app, system_admin, monkeypatch, encrypted, cleanup_status,
):
    manager, runtime = await _configured(app, system_admin)
    run_id = await _accept(system_admin)
    async with app.state.sessionmaker() as db:
        row = await db.get(BackupRun, run_id)
        row.status, row.active_slot = "failed", None
        row.encrypted, row.cleanup_status = encrypted, cleanup_status
        await db.commit()
        local = LocalStore(Path(app.state.settings.backup_dir), row.instance_key)
        directory = local.temporary_directory(row.execution_key)
        (directory / "snapshot.sqlite3").write_bytes(b"owned plaintext left after failed cleanup")
        execution_key = row.execution_key
    unrelated = directory.parent / ".work-unrelated"
    unrelated.mkdir()
    (unrelated / "unrelated").write_text("must not be scanned or removed")
    monkeypatch.setattr(crypto, "cleanup_encryption_home", lambda *_args, **_kwargs: None)
    await runtime.start()
    try:
        assert not directory.exists(), "terminal cleanup failure must not permanently retain plaintext"
        assert unrelated.is_dir() and (unrelated / "unrelated").is_file()
        async with app.state.sessionmaker() as db:
            row = await db.get(BackupRun, run_id)
            assert row.execution_key == execution_key and row.status == "failed"
            # 清理中间文件不能抹去原本的保留清理异常或改写真实备份结果。
            assert row.cleanup_status == cleanup_status
    finally:
        await manager.close()
        await runtime.stop()


@pytest.mark.parametrize("destination", ["local", "s3"])
@pytest.mark.parametrize("restart", [False, True])
async def test_saved_artifact_with_lost_ack_is_reconciled_instead_of_permanently_failed(
    app, system_admin, s3_memory, monkeypatch, destination, restart,
):
    manager, runtime = await _configured(app, system_admin, keep=2)
    revision = 1
    store_type = LocalStore
    if destination == "s3":
        await _enable_s3(system_admin, local=False, keep=2)
        revision = 2
        store_type = S3Store
    original = store_type.save
    lost = False

    def saved_then_lost_confirmation(*args, **kwargs):
        nonlocal lost
        value = original(*args, **kwargs)
        if not lost:
            lost = True
            raise BackupAdapterError("storage_failed")
        return value

    monkeypatch.setattr(store_type, "save", saved_then_lost_confirmation)
    await runtime.start()
    try:
        first_id = await _accept(system_admin, revision)
        assert (await _finished(app, first_id)).status == "failed"
        async with app.state.sessionmaker() as db:
            first = await db.scalar(select(BackupArtifact).where(BackupArtifact.run_id == first_id))
            assert first.status == "unknown"
            assert first.locator and first.size_bytes and first.checksum_sha256

        if restart:
            await manager.close()
            await runtime.stop()
            manager = BackupManager(app.state.settings, app.state.cipher, app.state.sessionmaker)
            runtime = BackupRuntime(manager)
            app.state.backups, app.state.backup_runtime = manager, runtime
            manager.notify, manager.tool_readiness = runtime.notify, runtime.tool_readiness
            await runtime.start()
            async with app.state.sessionmaker() as db:
                first = await db.scalar(select(BackupArtifact).where(BackupArtifact.run_id == first_id))
                assert first.status == "ready"

        second_id = await _accept(system_admin, revision)
        assert (await _finished(app, second_id)).status == "succeeded"
        async with app.state.sessionmaker() as db:
            first = await db.scalar(select(BackupArtifact).where(BackupArtifact.run_id == first_id))
            assert first.status == "ready"
            if destination == "s3":
                assert first.version_id
            else:
                assert "inode" in first.locator and "device" in first.locator
        downloaded = await system_admin.get(
            f"/api/v1/admin/backups/runs/{first_id}/download?destination={destination}",
        )
        assert downloaded.status_code == 200
        assert downloaded.content.startswith(b"SQLite format 3\x00")
    finally:
        await manager.close()
        await runtime.stop()


async def test_gpg_execution_passes_run_identity_and_always_attempts_owned_home_cleanup(
    app, system_admin, monkeypatch,
):
    manager, runtime = await _configured(app, system_admin)
    # 工具由adapter真实容器验收；这里只验证运行器交接的身份与finally边界。
    manager.tool_readiness = lambda _configuration: []
    response = await system_admin.patch("/api/v1/admin/backups/settings", json={
        "revision": 1, "changes": {"encryption_enabled": True, "backup_password": "test-backup-password"},
    })
    assert response.status_code == 200
    encrypted, cleaned = [], []

    def encrypt(source, destination, password, **kwargs):
        encrypted.append((kwargs["instance_key"], kwargs["execution_key"]))
        raise BackupAdapterError("encryption_failed")

    def cleanup(instance_key, execution_key, **kwargs):
        cleaned.append((instance_key, execution_key))

    monkeypatch.setattr(crypto, "encrypt_file", encrypt)
    monkeypatch.setattr(crypto, "cleanup_encryption_home", cleanup)
    await runtime.start()
    try:
        run_id = await _accept(system_admin, 2)
        run = await _finished(app, run_id)
        assert run.status == "failed" and run.error_code == "encryption_failed"
        assert encrypted == [(run.instance_key, run.execution_key)]
        assert cleaned == encrypted
    finally:
        await manager.close()
        await runtime.stop()


@pytest.mark.parametrize("active", [False, True])
async def test_startup_cleans_only_exact_known_gpg_homes_including_terminal_cleanup_failures(
    app, system_admin, monkeypatch, active,
):
    manager, runtime = await _configured(app, system_admin)
    run_id = await _accept(system_admin)
    async with app.state.sessionmaker() as db:
        row = await db.get(BackupRun, run_id)
        row.encrypted = True
        if not active:
            row.status, row.active_slot, row.cleanup_status = "failed", None, "failed"
        await db.commit()
        identity = row.instance_key, row.execution_key
    calls = []

    def cleanup(instance_key, execution_key, **kwargs):
        calls.append((instance_key, execution_key))

    monkeypatch.setattr(crypto, "cleanup_encryption_home", cleanup)
    await runtime.start()
    try:
        assert calls == [identity]
        if active:
            assert (await _finished(app, run_id)).status == "interrupted"
    finally:
        await manager.close()
        await runtime.stop()
