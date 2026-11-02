import asyncio
import io
import threading
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models import BackupArtifact, BackupRun, BackupSettings
from app.services.backups import database
from app.services.backups.runtime import BackupRuntime
from app.services.backups.service import BackupManager
from app.services.backups.storage import LocalStore, S3Store


async def _configured(app, system_admin, *, keep=7):
    manager = BackupManager(app.state.settings, app.state.cipher, app.state.sessionmaker)
    runtime = BackupRuntime(manager)
    app.state.backups, app.state.backup_runtime = manager, runtime
    manager.notify = runtime.notify
    manager.tool_readiness = runtime.tool_readiness
    await manager.load()
    response = await system_admin.patch("/api/v1/admin/backups/settings", json={
        "revision": 0, "changes": {"encryption_enabled": False, "local_keep": keep},
    })
    assert response.status_code == 200, response.text
    return manager, runtime


async def _accept(client, revision=1):
    response = await client.post("/api/v1/admin/backups/runs", json={
        "request_id": str(uuid4()), "settings_revision": revision,
    })
    assert response.status_code == 202, response.text
    return response.json()["id"]


async def _finished(app, run_id, within=10):
    async with asyncio.timeout(within):
        while True:
            async with app.state.sessionmaker() as db:
                row = await db.get(BackupRun, run_id)
                if row.active_slot is None:
                    return row
            await asyncio.sleep(0.02)


async def test_sqlite_runtime_real_backup_retention_and_native_download(app, system_admin):
    manager, runtime = await _configured(app, system_admin, keep=1)
    await runtime.start()
    try:
        first_id = await _accept(system_admin)
        assert (await _finished(app, first_id)).status == "succeeded"
        second_id = await _accept(system_admin)
        second = await _finished(app, second_id)
        assert second.status == "succeeded"
        assert second.config_snapshot["secrets_enc"] == {}
        async with app.state.sessionmaker() as db:
            artifacts = list(await db.scalars(select(BackupArtifact).order_by(BackupArtifact.id)))
            assert [item.status for item in artifacts] == ["deleted", "ready"]
        gone = await system_admin.get(f"/api/v1/admin/backups/runs/{first_id}/download?destination=local")
        assert gone.status_code == 410
        response = await system_admin.get(f"/api/v1/admin/backups/runs/{second_id}/download?destination=local")
        assert response.status_code == 200
        assert response.content.startswith(b"SQLite format 3\x00")
        assert int(response.headers["content-length"]) == len(response.content)
        assert response.headers["cache-control"] == "no-store"
    finally:
        await manager.close()
        await runtime.stop()


async def test_stop_waits_for_underlying_snapshot_before_releasing_lease(app, system_admin, monkeypatch):
    manager, runtime = await _configured(app, system_admin)
    reached, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = database.snapshot_database

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(reached.set)
        release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(database, "snapshot_database", blocked)
    await runtime.start()
    run_id = await _accept(system_admin)
    await asyncio.wait_for(reached.wait(), 3)
    stopping = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0.05)
    assert not stopping.done()
    async with app.state.sessionmaker() as db:
        assert (await db.get(BackupRun, run_id)).active_slot == 1
    release.set()
    await stopping
    row = await _finished(app, run_id)
    assert row.status == "interrupted"
    assert row.config_snapshot["secrets_enc"] == {}
    await manager.close()


async def test_cancelled_stop_still_drains_underlying_work_before_returning(app, system_admin, monkeypatch):
    manager, runtime = await _configured(app, system_admin)
    reached, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = database.snapshot_database

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(reached.set)
        release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(database, "snapshot_database", blocked)
    await runtime.start()
    run_id = await _accept(system_admin)
    await asyncio.wait_for(reached.wait(), 3)
    stopping = asyncio.create_task(runtime.stop())
    await asyncio.sleep(0.02)
    stopping.cancel()
    await asyncio.sleep(0.05)
    assert not stopping.done()
    async with app.state.sessionmaker() as db:
        assert (await db.get(BackupRun, run_id)).active_slot == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert (await _finished(app, run_id)).status == "interrupted"
    await manager.close()


async def test_failed_snapshot_does_not_evict_last_success(app, system_admin, monkeypatch):
    from app.services.backups.process import BackupAdapterError

    manager, runtime = await _configured(app, system_admin, keep=1)
    await runtime.start()
    try:
        first_id = await _accept(system_admin)
        assert (await _finished(app, first_id)).status == "succeeded"

        def failure(*_args, **_kwargs):
            raise BackupAdapterError("snapshot_failed")

        monkeypatch.setattr(database, "snapshot_database", failure)
        second_id = await _accept(system_admin)
        assert (await _finished(app, second_id)).status == "failed"
        async with app.state.sessionmaker() as db:
            artifacts = list(await db.scalars(select(BackupArtifact).order_by(BackupArtifact.id)))
            assert [item.status for item in artifacts] == ["ready", "failed"]
        assert (await system_admin.head(
            f"/api/v1/admin/backups/runs/{first_id}/download?destination=local",
        )).status_code == 200
    finally:
        await manager.close()
        await runtime.stop()


async def test_restart_marks_queued_interrupted_without_replaying(app, system_admin):
    manager, runtime = await _configured(app, system_admin)
    run_id = await _accept(system_admin)
    await runtime.start()
    try:
        row = await _finished(app, run_id)
        assert row.status == "interrupted"
        assert row.error_code == "interrupted"
        async with app.state.sessionmaker() as db:
            artifact = await db.scalar(select(BackupArtifact).where(BackupArtifact.run_id == run_id))
            assert artifact.status == "unknown"
    finally:
        await manager.close()
        await runtime.stop()


async def test_restart_due_schedule_catches_only_latest_beijing_slot(app, system_admin):
    manager, runtime = await _configured(app, system_admin)
    async with app.state.sessionmaker() as db:
        settings = await db.get(BackupSettings, 1)
        settings.configuration = {**settings.configuration, "schedule_enabled": True}
        settings.next_scheduled_at = datetime(2026, 10, 29, tzinfo=UTC)
        await db.commit()
    await manager.load()
    accepted = await manager.accept_scheduled(datetime(2026, 11, 2, 12, tzinfo=UTC))
    assert accepted is not None
    async with app.state.sessionmaker() as db:
        row = await db.get(BackupRun, accepted.id)
        assert row.scheduled_date == "2026-11-02"
    assert await manager.accept_scheduled(datetime(2026, 11, 2, 14, tzinfo=UTC)) is None
    await manager.close()


async def test_download_refuses_replaced_local_identity(app, system_admin, tmp_path):
    manager, runtime = await _configured(app, system_admin)
    await runtime.start()
    try:
        run_id = await _accept(system_admin)
        assert (await _finished(app, run_id)).status == "succeeded"
        async with app.state.sessionmaker() as db:
            row = await db.scalar(select(BackupArtifact).where(BackupArtifact.run_id == run_id))
            locator, size = row.locator, row.size_bytes
        path = tmp_path / "backups" / locator["instance_key"] / locator["filename"]
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"x" * size)
        replacement.replace(path)
        response = await system_admin.get(f"/api/v1/admin/backups/runs/{run_id}/download?destination=local")
        assert response.status_code == 503
        assert b"xxxxxxxx" not in response.content
    finally:
        await manager.close()
        await runtime.stop()


@pytest.fixture
def s3_memory(monkeypatch):
    """只隔离对象服务；真实数据库快照、本地文件与运行事务仍执行。"""
    objects, calls = {}, []

    def save(store, source, size, checksum, **kwargs):
        value = (source.read_bytes(), checksum, str(uuid4()))
        objects[store.location.key] = value
        calls.append(("save", store.location.locator(), store.credentials.access_key))
        return value[2]

    def head(store, size, checksum, version_id, **kwargs):
        from app.services.backups.process import BackupAdapterError

        value = objects.get(store.location.key)
        if value is None:
            raise BackupAdapterError("storage_missing")
        assert len(value[0]) == size and value[1] == checksum
        assert version_id is None or value[2] == version_id
        return value[2]

    def opened(store, size, checksum, version_id, **kwargs):
        head(store, size, checksum, version_id)
        calls.append(("open", store.location.locator(), store.credentials.access_key))
        return io.BytesIO(objects[store.location.key][0])

    def delete(store, version_id, **kwargs):
        calls.append(("delete", store.location.locator(), version_id))
        value = objects.pop(store.location.key, None)
        assert value is None or value[2] == version_id

    monkeypatch.setattr(S3Store, "save", save)
    monkeypatch.setattr(S3Store, "head", head)
    monkeypatch.setattr(S3Store, "open", opened)
    monkeypatch.setattr(S3Store, "delete", delete)
    return objects, calls


async def _enable_s3(client, *, local=True, keep=7):
    response = await client.patch("/api/v1/admin/backups/settings", json={"revision": 1, "changes": {
        "local_enabled": local, "s3_enabled": True, "s3_endpoint": "https://s3.example.test",
        "s3_bucket": "owned-backups", "s3_access_key": "old-access", "s3_secret_key": "old-secret",
        "s3_keep": keep,
    }})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("local", [True, False])
async def test_s3_storage_combinations_and_independent_retention(app, system_admin, s3_memory, local):
    manager, runtime = await _configured(app, system_admin, keep=1)
    await _enable_s3(system_admin, local=local, keep=2)
    await runtime.start()
    try:
        ids = []
        for _ in range(3):
            run_id = await _accept(system_admin, 2)
            assert (await _finished(app, run_id)).status == "succeeded"
            ids.append(run_id)
        async with app.state.sessionmaker() as db:
            all_artifacts = list(await db.scalars(select(BackupArtifact).order_by(BackupArtifact.id)))
            by_destination = {
                destination: [item.status for item in all_artifacts if item.destination == destination]
                for destination in ("local", "s3")
            }
        assert by_destination["s3"] == ["deleted", "ready", "ready"]
        assert by_destination["local"] == (["deleted", "deleted", "ready"] if local else [])
        objects, calls = s3_memory
        assert len(objects) == 2
        assert len([call for call in calls if call[0] == "delete"]) == 1
        response = await system_admin.get(f"/api/v1/admin/backups/runs/{ids[-1]}/download?destination=s3")
        assert response.status_code == 200
        assert response.content.startswith(b"SQLite format 3\x00")
    finally:
        await manager.close()
        await runtime.stop()


async def test_s3_failure_preserves_local_success_and_reports_partial(app, system_admin, monkeypatch):
    from app.services.backups.process import BackupAdapterError

    manager, runtime = await _configured(app, system_admin)
    await _enable_s3(system_admin)

    def failed(*_args, **_kwargs):
        raise BackupAdapterError("storage_failed")

    monkeypatch.setattr(S3Store, "save", failed)
    await runtime.start()
    try:
        run_id = await _accept(system_admin, 2)
        row = await _finished(app, run_id)
        assert row.status == "partial"
        async with app.state.sessionmaker() as db:
            artifacts = list(await db.scalars(select(BackupArtifact).where(BackupArtifact.run_id == run_id)))
        # 通用存储错误不能证明上传没有发生，副本结果保留unknown供身份核验。
        assert {item.destination: item.status for item in artifacts} == {"local": "ready", "s3": "unknown"}
        assert next(item for item in artifacts if item.destination == "s3").error_code == "storage_failed"
    finally:
        await manager.close()
        await runtime.stop()


async def test_old_s3_locator_survives_new_bucket_and_credentials_rotate_in_place(
    app, system_admin, s3_memory,
):
    manager, runtime = await _configured(app, system_admin)
    await _enable_s3(system_admin, local=False)
    await runtime.start()
    try:
        run_id = await _accept(system_admin, 2)
        assert (await _finished(app, run_id)).status == "succeeded"
        response = await system_admin.patch("/api/v1/admin/backups/settings", json={"revision": 2, "changes": {
            "s3_access_key": "new-access", "s3_secret_key": "new-secret",
        }})
        assert response.status_code == 200, response.text
        response = await system_admin.get(f"/api/v1/admin/backups/runs/{run_id}/download?destination=s3")
        assert response.status_code == 200
        assert s3_memory[1][-1][2] == "new-access"
        response = await system_admin.patch("/api/v1/admin/backups/settings", json={"revision": 3, "changes": {
            "s3_bucket": "other-bucket", "s3_enabled": False,
        }})
        assert response.status_code == 200, response.text
        response = await system_admin.get(f"/api/v1/admin/backups/runs/{run_id}/download?destination=s3")
        assert response.status_code == 200
        assert s3_memory[1][-1][1]["bucket"] == "owned-backups"
        assert s3_memory[1][-1][2] == "old-access"
    finally:
        await manager.close()
        await runtime.stop()


async def test_accepted_run_freezes_storage_configuration(app, system_admin, s3_memory, monkeypatch):
    manager, runtime = await _configured(app, system_admin)
    await _enable_s3(system_admin)
    reached, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = database.snapshot_database

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(reached.set)
        release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(database, "snapshot_database", blocked)
    await runtime.start()
    try:
        run_id = await _accept(system_admin, 2)
        await asyncio.wait_for(reached.wait(), 3)
        response = await system_admin.patch("/api/v1/admin/backups/settings", json={"revision": 2, "changes": {
            "s3_enabled": False, "local_enabled": False, "s3_bucket": "future-bucket",
        }})
        assert response.status_code == 200, response.text
        release.set()
        assert (await _finished(app, run_id)).status == "succeeded"
        assert s3_memory[1][0][1]["bucket"] == "owned-backups"
        async with app.state.sessionmaker() as db:
            artifacts = list(await db.scalars(select(BackupArtifact).where(BackupArtifact.run_id == run_id)))
        assert {item.destination: item.status for item in artifacts} == {"local": "ready", "s3": "ready"}
    finally:
        release.set()
        await manager.close()
        await runtime.stop()


async def test_snapshot_failure_notification_state_does_not_replace_backup_failure(
    app, system_admin, monkeypatch,
):
    from app.services.backups import mail
    from app.services.backups.process import BackupAdapterError

    manager, runtime = await _configured(app, system_admin)
    response = await system_admin.patch("/api/v1/admin/backups/settings", json={"revision": 1, "changes": {
        "smtp_host": "smtp.example.test", "smtp_from_address": "backup@example.org",
        "smtp_recipients": ["operator@example.org"],
    }})
    assert response.status_code == 200, response.text

    def failed(*args, **kwargs):
        raise BackupAdapterError("snapshot_failed")

    captured = []

    def send(config, notice, policy, **kwargs):
        captured.append(notice)
        return mail.MailResult("unknown", "mail_unknown", "邮件受理结果未知，请核对邮件服务器记录")

    monkeypatch.setattr(database, "snapshot_database", failed)
    monkeypatch.setattr(mail, "send_failure_notification", send)
    await runtime.start()
    try:
        run_id = await _accept(system_admin, 2)
        row = await _finished(app, run_id)
        assert row.status == "failed" and row.error_code == "snapshot_failed"
        assert row.notification_status == "unknown"
        assert len(captured) == 1 and captured[0].status == "failed"
        assert row.config_snapshot["secrets_enc"] == {}
    finally:
        await manager.close()
        await runtime.stop()


async def test_cleanup_unknown_is_confirmed_and_retried_on_next_success(app, system_admin, monkeypatch):
    from app.services.backups.process import BackupAdapterError

    manager, runtime = await _configured(app, system_admin, keep=1)
    await runtime.start()
    original = LocalStore.delete
    failed = False

    def delete(store, locator):
        nonlocal failed
        if not failed:
            failed = True
            raise BackupAdapterError("cleanup_failed")
        return original(store, locator)

    monkeypatch.setattr(LocalStore, "delete", delete)
    try:
        first_id = await _accept(system_admin)
        assert (await _finished(app, first_id)).status == "succeeded"
        second_id = await _accept(system_admin)
        second = await _finished(app, second_id)
        assert second.status == "succeeded" and second.cleanup_status == "failed"
        async with app.state.sessionmaker() as db:
            first_artifact = await db.scalar(select(BackupArtifact).where(BackupArtifact.run_id == first_id))
            assert first_artifact.status == "unknown"
        third_id = await _accept(system_admin)
        third = await _finished(app, third_id)
        assert third.status == "succeeded" and third.cleanup_status == "succeeded"
        async with app.state.sessionmaker() as db:
            artifacts = list(await db.scalars(select(BackupArtifact).order_by(BackupArtifact.id)))
        assert [item.status for item in artifacts] == ["deleted", "deleted", "ready"]
    finally:
        await manager.close()
        await runtime.stop()


async def test_deleted_file_with_lost_ack_is_confirmed_as_deleted_next_time(app, system_admin, monkeypatch):
    from app.services.backups.process import BackupAdapterError

    manager, runtime = await _configured(app, system_admin, keep=1)
    await runtime.start()
    original = LocalStore.delete
    lost = False

    def delete_then_lose_ack(store, locator):
        nonlocal lost
        original(store, locator)
        if not lost:
            lost = True
            raise BackupAdapterError("cleanup_failed")

    monkeypatch.setattr(LocalStore, "delete", delete_then_lose_ack)
    try:
        first = await _accept(system_admin)
        assert (await _finished(app, first)).status == "succeeded"
        second = await _accept(system_admin)
        assert (await _finished(app, second)).cleanup_status == "failed"
        third = await _accept(system_admin)
        assert (await _finished(app, third)).cleanup_status == "succeeded"
        async with app.state.sessionmaker() as db:
            artifacts = list(await db.scalars(select(BackupArtifact).order_by(BackupArtifact.id)))
        assert [item.status for item in artifacts] == ["deleted", "deleted", "ready"]
    finally:
        await manager.close()
        await runtime.stop()


async def test_runtime_confirmed_commit_loss_does_not_repeat_or_release_lease(app, system_admin, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    manager, runtime = await _configured(app, system_admin)
    run_id = await _accept(system_admin)
    commit = AsyncSession.commit
    lost = False

    async def commit_then_lose_confirmation(session):
        nonlocal lost
        await commit(session)
        if not lost:
            lost = True
            raise OSError("do not expose private SQL parameters")

    monkeypatch.setattr(AsyncSession, "commit", commit_then_lose_confirmation)
    await runtime._write(BackupRun, run_id, status="running", started_at=datetime.now(UTC))
    assert not runtime._uncertain
    async with app.state.sessionmaker() as db:
        row = await db.get(BackupRun, run_id)
        assert row.status == "running" and row.active_slot == 1
    await manager.close()


async def test_runtime_unconfirmed_commit_keeps_durable_active_slot_and_blocks_new_work(
    app, system_admin, monkeypatch,
):
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.backups.service import BackupUnavailableError

    manager, runtime = await _configured(app, system_admin)
    run_id = await _accept(system_admin)

    async def unavailable(session):
        raise OSError("private connection secret")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", unavailable)
        with pytest.raises(BackupUnavailableError):
            await runtime._write(BackupRun, run_id, status="running", started_at=datetime.now(UTC))
    assert runtime._uncertain
    async with app.state.sessionmaker() as db:
        row = await db.get(BackupRun, run_id)
        assert row.status == "queued" and row.active_slot == 1
    response = await system_admin.post("/api/v1/admin/backups/runs", json={
        "request_id": str(uuid4()), "settings_revision": 1,
    })
    assert response.status_code == 400
    assert "待核实" in response.json()["detail"]
    await manager.close()


@pytest.mark.parametrize("path", ["/api/v1/admin/backups", "/api/v1/projects"])
async def test_backup_headers_middleware_preserves_request_task(path):
    from app.main import _BackupResponseHeaders

    origin = asyncio.current_task()
    messages = []

    async def endpoint(scope, receive, send):
        assert asyncio.current_task() is origin
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    await _BackupResponseHeaders(endpoint)({"type": "http", "path": path}, receive, send)
    headers = dict(messages[0]["headers"])
    if path.endswith("backups"):
        assert headers[b"cache-control"] == b"no-store"
    else:
        assert b"cache-control" not in headers


async def test_backup_unknown_exception_has_safe_uncached_response_and_log(caplog):
    from app.main import _BackupResponseHeaders

    messages = []

    async def endpoint(scope, receive, send):
        raise RuntimeError("very-private-connection-secret")

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    await _BackupResponseHeaders(endpoint)({"type": "http", "path": "/api/v1/admin/backups"}, receive, send)
    assert messages[0]["status"] == 500
    assert dict(messages[0]["headers"])[b"cache-control"] == b"no-store"
    assert b"very-private" not in messages[1]["body"]
    assert "very-private" not in caplog.text


async def test_backup_401_and_validation_errors_are_uncached(client, app, system_admin):
    response = await client.get("/api/v1/admin/backups")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    response = await system_admin.patch("/api/v1/admin/backups/settings", json={"revision": 0, "changes": {
        "backup_password": "secret-too-short",
        "s3_endpoint": "http://invalid.example.test",
    }})
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert "secret-too-short" not in response.text


async def test_recovery_verifies_known_completed_file_and_restores_immutable_identity(app, system_admin):
    manager, runtime = await _configured(app, system_admin)
    await runtime.start()
    run_id = await _accept(system_admin)
    assert (await _finished(app, run_id)).status == "succeeded"
    await manager.close()
    await runtime.stop()
    async with app.state.sessionmaker() as db:
        row = await db.get(BackupRun, run_id)
        row.status, row.active_slot, row.finished_at = "running", 1, None
        artifact = await db.scalar(select(BackupArtifact).where(BackupArtifact.run_id == run_id))
        artifact.status = "pending"
        artifact.locator = {key: value for key, value in artifact.locator.items() if key not in ("device", "inode")}
        await db.commit()
    restarted_manager = BackupManager(app.state.settings, app.state.cipher, app.state.sessionmaker)
    restarted = BackupRuntime(restarted_manager)
    app.state.backups, app.state.backup_runtime = restarted_manager, restarted
    restarted_manager.notify = restarted.notify
    restarted_manager.tool_readiness = restarted.tool_readiness
    await restarted.start()
    try:
        row = await _finished(app, run_id)
        assert row.status == "interrupted"
        async with app.state.sessionmaker() as db:
            artifact = await db.scalar(select(BackupArtifact).where(BackupArtifact.run_id == run_id))
            assert artifact.status == "ready"
            assert "inode" in artifact.locator and "device" in artifact.locator
        response = await system_admin.get(f"/api/v1/admin/backups/runs/{run_id}/download?destination=local")
        assert response.status_code == 200
    finally:
        await restarted_manager.close()
        await restarted.stop()


async def test_invalid_persisted_backup_configuration_pauses_only_backup_module(app, system_admin):
    manager, runtime = await _configured(app, system_admin)
    async with app.state.sessionmaker() as db:
        row = await db.get(BackupSettings, 1)
        row.configuration = {**row.configuration, "local_keep": 0}
        await db.commit()
    await runtime.start()
    try:
        assert runtime._uncertain and not manager.confirmed
        response = await system_admin.post("/api/v1/admin/backups/runs", json={
            "request_id": str(uuid4()), "settings_revision": 1,
        })
        assert response.status_code == 503
        assert (await system_admin.get("/api/v1/admin/system-settings")).status_code == 200
        assert app.state.task_engine is not None
        async with app.state.sessionmaker() as db:
            assert (await db.get(BackupSettings, 1)).configuration["local_keep"] == 0
            assert await db.scalar(select(BackupRun.id)) is None
    finally:
        await manager.close()
        await runtime.stop()
