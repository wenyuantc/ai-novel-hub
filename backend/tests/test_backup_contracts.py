"""整库备份配置/准入/下载授权的真实HTTP、事务与撤权回归。"""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminAuditLog, BackupArtifact, BackupRun, BackupSettings, User
from app.schemas.backup import BackupConfiguration, BackupSettingsUpdate
from app.services.authorization_service import AuthenticatedActor
from app.services.backups.service import BackupManager, next_schedule
from tests.helpers import register
from tests.rbac_helpers import assign_roles, create_role

BASE = "/api/v1/admin/backups"
SECRET = "isolated-backup-secret-123456"


@pytest.fixture
async def backup_manager(app):
    previous = getattr(app.state, "backups", None)
    manager = BackupManager(app.state.settings, app.state.cipher, app.state.sessionmaker)
    app.state.backups = manager
    await manager.load()
    try:
        yield manager
    finally:
        await manager.close()
        if previous is not None:
            app.state.backups = previous


async def save(admin, **changes):
    current = await admin.get(BASE + "/settings")
    assert current.status_code == 200, current.text
    response = await admin.patch(BASE + "/settings", json={"revision": current.json()["revision"], "changes": changes})
    assert response.status_code == 200, response.text
    return response.json()


async def manual(admin, *, revision=1, request_id=None):
    return await admin.post(
        BASE + "/runs",
        json={
            "request_id": request_id or str(uuid4()),
            "settings_revision": revision,
        },
    )


async def finish(app, run_id):
    async with app.state.sessionmaker() as db:
        await db.execute(
            update(BackupRun)
            .where(BackupRun.id == run_id)
            .values(
                status="failed",
                active_slot=None,
                finished_at=datetime.now(UTC),
            )
        )
        await db.commit()


async def audit_rows(app, action):
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == action)))


async def test_backup_defaults_configuration_secret_encryption_and_no_public_exposure(
    app,
    system_admin,
    backup_manager,
    client,
):
    current = (await system_admin.get(BASE + "/settings")).json()
    assert current["revision"] == 0 and current["schedule_time"] == "03:00"
    assert current["timezone"] == "Asia/Shanghai" and current["schedule_enabled"] is False
    assert current["local_enabled"] and not current["s3_enabled"] and current["encryption_enabled"]
    assert current["local_keep"] == current["s3_keep"] == 7
    overview = (await system_admin.get(BASE)).json()
    assert not overview["ready"] and not overview["notification_ready"]
    assert "smtp_host" not in overview and "s3_endpoint" not in overview
    assert (await manual(system_admin, revision=0)).status_code == 400

    saved = await save(
        system_admin, backup_password=SECRET, s3_secret_key="s3-secret-only-value", local_keep=3, s3_keep=9
    )
    assert saved["backup_password_configured"] and saved["s3_secret_key_configured"]
    assert SECRET not in json.dumps(saved) and "s3-secret-only-value" not in json.dumps(saved)
    async with app.state.sessionmaker() as db:
        row = await db.get(BackupSettings, 1)
        assert app.state.cipher.decrypt(row.secrets_enc["backup_password"]) == SECRET
        assert SECRET not in json.dumps(row.configuration) and SECRET not in json.dumps(row.secrets_enc)
        assert row.configuration["local_keep"] == 3 and row.configuration["s3_keep"] == 9
    old = (await backup_manager.snapshot()).secrets_enc["backup_password"]
    await save(system_admin, local_keep=4)
    assert (await backup_manager.snapshot()).secrets_enc["backup_password"] == old
    await save(system_admin, backup_password=None)
    assert not (await system_admin.get(BASE + "/settings")).json()["backup_password_configured"]
    assert not (await system_admin.get(BASE)).json()["ready"]
    public = await client.get("/api/v1/auth/config")
    assert "backups" not in public.text and "smtp" not in public.text
    audit = await audit_rows(app, "backup.settings.update")
    assert len(audit) == 3 and all(SECRET not in json.dumps(row.summary) for row in audit)
    assert set(audit[0].summary["backup_change"]["fields"]) == {
        "backup_password",
        "s3_secret_key",
        "local_keep",
        "s3_keep",
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"local_keep": True},
        {"local_keep": 0},
        {"s3_keep": 1001},
        {"encryption_enabled": "false"},
        {"schedule_time": "3:00"},
        {"local_enabled": None},
        {"backup_password": ""},
        {"backup_password": SECRET + "\n"},
        {"backup_password": "short"},
        {"backup_dir": "/tmp"},
        {"s3_endpoint": "http://public.example.test"},
        {"s3_endpoint": "https://user:secret@example.test"},
        {"s3_endpoint": "https://example.test/?secret=" + SECRET},
        {"s3_prefix": "../escape"},
        {"smtp_host": "host\r\nBcc:someone"},
        {"smtp_from_address": "bad\r\naddress"},
        {"smtp_security": "none"},
        {"smtp_recipients": ["not-an-address"]},
        {},
    ],
)
async def test_backup_patch_rejects_invalid_without_secret_echo(system_admin, backup_manager, changes):
    response = await system_admin.patch(BASE + "/settings", json={"revision": 0, "changes": changes})
    assert response.status_code == 422, response.text
    assert SECRET not in response.text
    assert (await system_admin.get(BASE + "/settings")).json()["revision"] == 0


async def test_configuration_cas_and_storage_and_schedule_readiness(system_admin, backup_manager):
    await save(system_admin, encryption_enabled=False, local_enabled=False)
    assert (await manual(system_admin)).status_code == 400
    response = await system_admin.patch(BASE + "/settings", json={"revision": 0, "changes": {"local_enabled": True}})
    assert response.status_code == 409 and response.json()["code"] == "settings_conflict"
    await save(system_admin, local_enabled=True)
    response = await system_admin.patch(BASE + "/settings", json={"revision": 2, "changes": {"schedule_enabled": True}})
    assert response.status_code == 400 and "邮件" in response.json()["detail"]
    assert not (await system_admin.get(BASE + "/settings")).json()["schedule_enabled"]
    await save(
        system_admin,
        schedule_enabled=True,
        smtp_host="mail.example.test",
        smtp_from_address="backup@example.com",
        smtp_recipients=["ops@example.com"],
    )
    overview = (await system_admin.get(BASE)).json()
    assert overview["ready"] and overview["notification_ready"] and overview["next_scheduled_at"]
    one = await save(system_admin, schedule_enabled=False)
    response = await manual(system_admin, revision=one["revision"])
    assert response.status_code == 202 and response.json()["source"] == "manual"


async def test_manual_idempotency_single_active_snapshot_and_safe_projection(app, system_admin, backup_manager):
    saved = await save(system_admin, backup_password=SECRET)
    request_id = str(uuid4())
    first, duplicate = await asyncio.gather(
        manual(system_admin, request_id=request_id),
        manual(system_admin, request_id=request_id),
    )
    assert first.status_code == duplicate.status_code == 202
    run_id = first.json()["id"]
    assert duplicate.json()["id"] == run_id
    assert first.json()["encrypted"] and first.json()["file_format"] == "sqlite3.gpg"
    assert first.json()["schema_revision"] is None  # create_all库没有迁移事实，不猜0030。
    assert "config_snapshot" not in first.json() and "locator" not in str(first.json())
    assert (await manual(system_admin)).json()["code"] == "backup_busy"
    assert len(await audit_rows(app, "backup.run.accept")) == 1
    await save(system_admin, encryption_enabled=False, local_keep=2)
    async with app.state.sessionmaker() as db:
        row = await db.get(BackupRun, run_id)
        assert row.config_snapshot["configuration"]["encryption_enabled"]
        assert row.config_snapshot["configuration"]["local_keep"] == 7
        assert row.settings_revision == saved["revision"]
        artifact = await db.scalar(select(BackupArtifact).where(BackupArtifact.run_id == run_id))
        artifact.size_bytes = 5 * 1024**3
        await db.commit()
    assert (await system_admin.get(BASE + f"/runs/{run_id}")).json()["artifacts"][0]["size_bytes"] == 5 * 1024**3
    assert (await manual(system_admin, revision=2, request_id=request_id)).json()["code"] == "request_conflict"
    assert (await manual(system_admin, revision=1, request_id=request_id)).json()["id"] == run_id
    await finish(app, run_id)
    new = await manual(system_admin, revision=2)
    assert new.status_code == 202 and not new.json()["encrypted"] and new.json()["file_format"] == "sqlite3"
    listing = await system_admin.get(BASE + "/runs", params={"status": "queued", "page_size": 1})
    assert listing.json()["total"] == 1 and listing.json()["items"][0]["id"] == new.json()["id"]


async def test_four_action_permissions_are_independent(app, system_admin, client, backup_manager):
    user = await register(client, "backup_reader")
    reader = await create_role(system_admin, "仅备份读取", ["backups.read"])
    await assign_roles(system_admin, user["id"], [reader["id"]])
    assert (await client.get(BASE)).status_code == 200
    assert (await client.get(BASE + "/runs")).status_code == 200
    assert (await client.get(BASE + "/settings")).status_code == 403
    assert (await manual(client)).status_code == 403
    assert (await client.get(BASE + "/runs/1/download", params={"destination": "local"})).status_code == 403
    for permission in ("backups.configure", "backups.run", "backups.download"):
        invalid = await system_admin.post("/api/v1/admin/roles", json={"name": permission, "permissions": [permission]})
        assert invalid.status_code == 422
    runner = await create_role(system_admin, "仅备份受理", ["backups.read", "backups.run"])
    await assign_roles(system_admin, user["id"], [runner["id"]])
    await save(system_admin, encryption_enabled=False)
    run = await manual(client)
    assert run.status_code == 202 and run.json()["actor_user_id"] == user["id"]
    assert (await client.get(BASE + "/settings")).status_code == 403
    assert (
        await client.get(BASE + f"/runs/{run.json()['id']}/download", params={"destination": "local"})
    ).status_code == 403
    await assign_roles(system_admin, user["id"], [])
    assert (await client.get(BASE)).status_code == 403
    assert (await manual(client)).status_code == 403


async def test_waiting_manager_write_rechecks_original_token(app, system_admin, backup_manager):
    current = (await system_admin.get("/api/v1/auth/me")).json()
    await backup_manager.lock.acquire()
    request = asyncio.create_task(
        system_admin.patch(
            BASE + "/settings",
            json={
                "revision": 0,
                "changes": {"encryption_enabled": False},
            },
        )
    )
    try:
        for _ in range(100):
            if backup_manager._writes:
                break
            await asyncio.sleep(0.005)
        assert backup_manager._writes
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == current["id"]).values(token_version=User.token_version + 1))
            await db.commit()
    finally:
        backup_manager.lock.release()
    assert (await request).status_code == 401
    async with app.state.sessionmaker() as db:
        assert await db.get(BackupSettings, 1) is None


async def test_commit_confirmation_loss_reloads_fact_and_does_not_record_opposite_failure(
    app,
    system_admin,
    backup_manager,
    monkeypatch,
    caplog,
):
    original = AsyncSession.commit
    lost = False

    async def lose_confirmation(db):
        nonlocal lost
        await original(db)
        if not lost:
            lost = True
            raise RuntimeError(SECRET)

    monkeypatch.setattr(AsyncSession, "commit", lose_confirmation)
    response = await system_admin.patch(
        BASE + "/settings",
        json={
            "revision": 0,
            "changes": {"backup_password": SECRET},
        },
    )
    assert response.status_code == 500 and SECRET not in response.text and SECRET not in caplog.text
    assert backup_manager.confirmed
    assert (await system_admin.get(BASE + "/settings")).json()["revision"] == 1
    logs = await audit_rows(app, "backup.settings.update")
    assert len(logs) == 1 and logs[0].result == "success"


async def test_audit_flush_failure_rolls_back_configuration_without_leaking_parameters(
    app,
    system_admin,
    backup_manager,
    monkeypatch,
    caplog,
):
    original = AsyncSession.flush

    async def reject_audit(db, *args, **kwargs):
        if any(isinstance(row, AdminAuditLog) and row.action == "backup.settings.update" for row in db.new):
            raise RuntimeError(SECRET)
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "flush", reject_audit)
    response = await system_admin.patch(
        BASE + "/settings",
        json={
            "revision": 0,
            "changes": {"backup_password": SECRET},
        },
    )
    assert response.status_code == 500 and SECRET not in response.text and SECRET not in caplog.text
    async with app.state.sessionmaker() as db:
        assert await db.get(BackupSettings, 1) is None


async def test_cancelled_caller_does_not_cancel_accepted_configuration_commit(app, system_admin, backup_manager):
    me = (await system_admin.get("/api/v1/auth/me")).json()
    async with app.state.sessionmaker() as db:
        actor = await db.get(User, me["id"])
        authenticated = AuthenticatedActor(actor.id, actor.token_version)
    await backup_manager.lock.acquire()
    caller = asyncio.create_task(
        backup_manager.update(
            authenticated,
            BackupSettingsUpdate.model_validate(
                {
                    "revision": 0,
                    "changes": {"backup_password": SECRET},
                }
            ),
        )
    )
    for _ in range(100):
        if backup_manager._writes:
            break
        await asyncio.sleep(0.005)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    pending = list(backup_manager._writes)
    backup_manager.lock.release()
    assert len(pending) == 1
    await asyncio.gather(*pending)
    assert (await backup_manager.load()).revision == 1
    assert len(await audit_rows(app, "backup.settings.update")) == 1


async def test_schedule_latest_slot_once_busy_wait_and_beijing_time(app, system_admin, backup_manager):
    configuration = BackupConfiguration(schedule_enabled=True)
    assert next_schedule(configuration, datetime(2026, 11, 1, 19, 0, tzinfo=UTC)) == datetime(
        2026, 11, 2, 19, 0, tzinfo=UTC
    )
    await save(
        system_admin,
        encryption_enabled=False,
        schedule_enabled=True,
        smtp_host="mail.example.test",
        smtp_from_address="backup@example.com",
        smtp_recipients=["ops@example.com"],
    )
    async with app.state.sessionmaker() as db:
        await db.execute(update(BackupSettings).values(next_scheduled_at=datetime(2030, 1, 1, 19, 0, tzinfo=UTC)))
        await db.commit()
    now = datetime(2030, 1, 4, 20, 0, tzinfo=UTC)
    first = await backup_manager.accept_scheduled(now)
    assert first is not None and first.source == "scheduled" and first.actor_user_id is None
    async with app.state.sessionmaker() as db:
        row = await db.get(BackupRun, first.id)
        assert row.scheduled_date == "2030-01-05"
    assert await backup_manager.accept_scheduled(now) is None
    assert await backup_manager.accept_scheduled(datetime(2030, 1, 5, 20, 0, tzinfo=UTC)) is None  # busy
    await finish(app, first.id)
    second = await backup_manager.accept_scheduled(datetime(2030, 1, 5, 20, 0, tzinfo=UTC))
    assert second.id != first.id


class DownloadResource:
    filename = "backup.sqlite3"
    size_bytes = 6
    closed = False

    async def iter_chunks(self):
        yield b"abc"
        yield b"def"

    async def close(self):
        self.closed = True


async def test_download_head_preflight_get_authorization_and_session_recheck(
    app,
    system_admin,
    backup_manager,
    monkeypatch,
):
    await save(system_admin, encryption_enabled=False)
    run = (await manual(system_admin)).json()
    await finish(app, run["id"])
    async with app.state.sessionmaker() as db:
        await db.execute(update(BackupArtifact).where(BackupArtifact.run_id == run["id"]).values(status="ready"))
        await db.commit()
    resource = DownloadResource()

    class Runtime:
        async def prepare_download(self, run_id, destination):
            assert run_id == run["id"] and destination == "local"
            return resource

    monkeypatch.setattr(app.state, "backup_runtime", Runtime(), raising=False)
    url = BASE + f"/runs/{run['id']}/download?destination=local"
    response = await system_admin.head(url)
    assert response.status_code == 200 and response.content == b"" and resource.closed
    assert not await audit_rows(app, "backup.download.authorize")
    resource.closed = False
    response = await system_admin.get(url)
    assert response.status_code == 200 and response.content == b"abcdef" and resource.closed
    assert response.headers["cache-control"] == "no-store" and response.headers["content-length"] == "6"
    assert len(await audit_rows(app, "backup.download.authorize")) == 1
    me = (await system_admin.get("/api/v1/auth/me")).json()

    async def revoked_during_open(run_id, destination):
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == me["id"]).values(token_version=User.token_version + 1))
            await db.commit()
        return resource

    resource.closed = False
    monkeypatch.setattr(app.state.backup_runtime, "prepare_download", revoked_during_open)
    assert (await system_admin.get(url)).status_code == 401
    assert resource.closed and len(await audit_rows(app, "backup.download.authorize")) == 1
