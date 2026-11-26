import asyncio
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.errors import ForbiddenError
from app.models import AdminAuditLog, User
from app.models.two_step import LoginChallenge, RecoveryCode, TwoStepFactor
from app.services.audit_writer import AuditOperation, AuditOperationError
from app.services.two_step_recovery import recover_on_host
from tests.helpers import register
from tests.rbac_helpers import assign_roles, create_role

BACKEND_DIR = Path(__file__).resolve().parents[1]


async def enrolled(app, uid):
    async with app.state.sessionmaker() as db:
        user = await db.get(User, uid)
        db.add(TwoStepFactor(user_id=uid, enabled=True, secret_enc="never-expose", setup_version=user.token_version,
                             setup_expires=0, last_step=-1, window_start=0, failures=0))
        await db.flush()
        db.add(RecoveryCode(user_id=uid, digest="test-recovery-digest"))
        db.add(LoginChallenge(user_id=uid, version=user.token_version, expires=2100000000, digest="test-challenge"))
        await db.commit()
        return user.token_version


async def check_state(app, uid, version, *, reset):
    async with app.state.sessionmaker() as db:
        assert ((await db.get(TwoStepFactor, uid)) is None) is reset
        assert ((await db.get(LoginChallenge, "test-challenge")) is None) is reset
        assert ((await db.get(RecoveryCode, "test-recovery-digest")) is None) is reset
        assert (await db.get(User, uid)).token_version == version + int(reset)


async def test_verified_admin_reset_and_audit(app, client, system_admin, make_client):
    uid = (await register(client, "author"))["id"]
    admin_id = (await system_admin.get("/api/v1/auth/me")).json()["id"]
    version = await enrolled(app, uid)
    endpoint = f"/api/v1/admin/users/{uid}/reset-two-step"
    assert (await system_admin.get(f"/api/v1/admin/users/{uid}")).json()["two_step_enabled"]
    delegate = make_client()
    delegate_id = (await register(delegate, "operator"))["id"]
    role = await create_role(system_admin, "用户管理", ["users.read", "users.reset_password"])
    await assign_roles(system_admin, delegate_id, [role["id"]])
    assert (await delegate.post(endpoint, json={"identity_verified": True})).status_code == 403
    assert (await system_admin.post(endpoint, json={"identity_verified": False})).status_code == 400
    await check_state(app, uid, version, reset=False)
    assert (await system_admin.post(endpoint, json={"identity_verified": True})).status_code == 204
    await check_state(app, uid, version, reset=True)
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    assert not (await system_admin.get(f"/api/v1/admin/users/{uid}")).json()["two_step_enabled"]
    result = await system_admin.get("/api/v1/admin/audit-logs", params={"action": "user.two_step.reset"})
    assert result.status_code == 200
    events = result.json()["items"]
    assert [(e["actor_user_id"], e["target_id"], e["result"]) for e in events] == [
        (admin_id, uid, "success"), (admin_id, uid, "failure")]
    assert "never-expose" not in result.text


async def test_local_cli_only_sole_enabled_admin(app, client, system_admin):
    uid = (await system_admin.get("/api/v1/auth/me")).json()["id"]
    other_id = (await register(client, "another_user"))["id"]
    version = await enrolled(app, uid)
    async with app.state.sessionmaker() as db:
        with pytest.raises(ForbiddenError):
            await recover_on_host(db, other_id)
        other = await db.get(User, other_id)
        other.is_admin = True
        await db.commit()
        with pytest.raises(ForbiddenError):
            await recover_on_host(db, uid)
        await db.refresh(other)
        other.is_admin = False
        await db.commit()
    await check_state(app, uid, version, reset=False)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.recover_two_step", "--user-id", str(uid), "--confirm",
        env={**os.environ, "DATABASE_URL": app.state.test_deployment.database_url}, cwd=BACKEND_DIR,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    assert "本机恢复审计" in out.decode()
    await check_state(app, uid, version, reset=True)
    async with app.state.sessionmaker() as db:
        log = await db.scalar(select(AdminAuditLog).where(
            AdminAuditLog.action == "user.two_step.local_recovery", AdminAuditLog.result == "success",
        ))
        assert log.target_id == uid and log.actor_user_id is None and log.actor_username == "部署者本机恢复"


@pytest.mark.parametrize("local", [False, True])
async def test_audit_failure_rolls_back_credentials(app, client, system_admin, monkeypatch, local):
    if local:
        uid = (await system_admin.get("/api/v1/auth/me")).json()["id"]
    else:
        uid = (await register(client, "author"))["id"]
    version = await enrolled(app, uid)
    original = AuditOperation._event

    def reject_success(self, result, **kwargs):
        if result == "success":
            raise RuntimeError("injected audit failure")
        return original(self, result, **kwargs)

    monkeypatch.setattr(AuditOperation, "_event", reject_success)
    if local:
        async with app.state.sessionmaker() as db:
            with pytest.raises(AuditOperationError):
                await recover_on_host(db, uid)
    else:
        resp = await system_admin.post(f"/api/v1/admin/users/{uid}/reset-two-step", json={"identity_verified": True})
        assert resp.status_code == 500
    await check_state(app, uid, version, reset=False)
