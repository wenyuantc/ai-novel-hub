"""独立审查补充：跨账号幂等身份与提交确认丢失的隔离边界。"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BackupArtifact, BackupRun, BackupSettings
from app.services.backups.runtime import DownloadResource
from tests.helpers import register
from tests.rbac_helpers import assign_roles, create_role
from tests.test_backup_contracts import (
    BASE,
    SECRET,
    audit_rows,
    finish,
    manual,
    save,
)
from tests.test_backup_contracts import (
    backup_manager as backup_manager,
)


async def test_download_get_and_head_have_distinct_openapi_operation_ids(app):
    operations = app.openapi()["paths"][BASE + "/runs/{run_id}/download"]
    assert {"get", "head"} <= operations.keys()
    assert operations["get"]["operationId"] != operations["head"]["operationId"]


async def test_other_actor_cannot_reuse_accepted_request_id(app, system_admin, client, backup_manager):
    await save(system_admin, encryption_enabled=False)
    request_id = str(uuid4())
    accepted = await manual(system_admin, request_id=request_id)
    assert accepted.status_code == 202
    user = await register(client, "independent_backup_runner")
    runner = await create_role(system_admin, "独立备份受理员", ["backups.read", "backups.run"])
    await assign_roles(system_admin, user["id"], [runner["id"]])
    rejected = await manual(client, request_id=request_id)
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "request_conflict"
    assert "请求标识" in rejected.json()["detail"]
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(BackupRun)) == 1
    assert len(await audit_rows(app, "backup.run.accept")) == 1


async def test_manual_commit_confirmation_loss_reuses_durable_acceptance(
    app, system_admin, backup_manager, monkeypatch, caplog,
):
    await save(system_admin, encryption_enabled=False)
    original = AsyncSession.commit
    lost = False

    async def lose_confirmation(db):
        nonlocal lost
        await original(db)
        if not lost:
            lost = True
            raise RuntimeError(SECRET)

    monkeypatch.setattr(AsyncSession, "commit", lose_confirmation)
    request_id = str(uuid4())
    response = await manual(system_admin, request_id=request_id)
    assert response.status_code == 500
    assert SECRET not in response.text and SECRET not in caplog.text
    assert backup_manager.confirmed
    retry = await manual(system_admin, request_id=request_id)
    assert retry.status_code == 202
    async with app.state.sessionmaker() as db:
        runs = list(await db.scalars(select(BackupRun)))
        assert len(runs) == 1
        assert runs[0].id == retry.json()["id"] and runs[0].active_slot == 1
    logs = await audit_rows(app, "backup.run.accept")
    assert len(logs) == 1 and logs[0].result == "success"


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
async def test_download_header_disconnect_closes_resource_before_first_chunk(
    app, system_admin, backup_manager, monkeypatch, spec_version, tmp_path, request,
):
    await save(system_admin, encryption_enabled=False)
    run = (await manual(system_admin)).json()
    await finish(app, run["id"])
    async with app.state.sessionmaker() as db:
        artifact = await db.scalar(select(BackupArtifact).where(BackupArtifact.run_id == run["id"]))
        artifact.status = "ready"
        await db.commit()

    class OpenResource(DownloadResource):
        iterated = False

        async def iter_chunks(self):
            self.iterated = True
            async for chunk in super().iter_chunks():
                yield chunk

    path = tmp_path / "isolated.sqlite3"
    path.write_bytes(b"abcdef")
    source = path.open("rb")
    request.addfinalizer(source.close)
    resource = OpenResource("isolated.sqlite3", 6, source)

    class Runtime:
        async def prepare_download(self, run_id, destination):
            return resource

    monkeypatch.setattr(app.state, "backup_runtime", Runtime())
    headers_started = asyncio.Event()

    async def receive():
        await headers_started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            headers_started.set()
            if spec_version == "2.4":
                raise asyncio.CancelledError
            # 旧ASGI通过独立disconnect监听取消流；此时迭代器同样尚未开始。
            await asyncio.sleep(10)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1", "scheme": "http", "method": "GET",
        "path": f"/api/v1/admin/backups/runs/{run['id']}/download",
        "raw_path": f"/api/v1/admin/backups/runs/{run['id']}/download".encode(),
        "query_string": b"destination=local", "root_path": "",
        "headers": [(b"cookie", "; ".join(f"{k}={v}" for k, v in system_admin.cookies.items()).encode())],
        "client": ("127.0.0.1", 43210), "server": ("testserver", 80),
    }
    if spec_version == "2.4":
        with pytest.raises(asyncio.CancelledError):
            await app(scope, receive, send)
    else:
        await app(scope, receive, send)
    assert headers_started.is_set() and not resource.iterated
    assert source.closed


async def test_unconfirmed_backup_commit_only_pauses_backup_module(
    app, system_admin, backup_manager, monkeypatch, caplog,
):
    original_commit = AsyncSession.commit
    original_reload = backup_manager._reload
    committed = False
    lost = False
    baseline_budget = app.state.settings.context_char_budget
    assert backup_manager.authorization.settings_confirmed

    async def lose_confirmation(db):
        nonlocal committed, lost
        await original_commit(db)
        committed = True
        if not lost:
            lost = True
            raise RuntimeError(SECRET)

    async def fail_confirmation_read():
        if committed:
            raise RuntimeError(SECRET)
        return await original_reload()

    monkeypatch.setattr(AsyncSession, "commit", lose_confirmation)
    monkeypatch.setattr(backup_manager, "_reload", fail_confirmation_read)
    response = await system_admin.patch(
        BASE + "/settings", json={"revision": 0, "changes": {"backup_password": SECRET}},
    )
    assert response.status_code == 500
    assert not backup_manager.confirmed
    assert backup_manager.authorization.settings_confirmed
    assert app.state.settings.context_char_budget == baseline_budget
    blocked = await manual(system_admin)
    assert blocked.status_code == 503
    assert SECRET not in response.text + blocked.text + caplog.text
    async with app.state.sessionmaker() as db:
        persisted = await db.get(BackupSettings, 1)
        assert persisted.revision == 1
        assert await db.scalar(select(func.count()).select_from(BackupRun)) == 0
    monkeypatch.setattr(backup_manager, "_reload", original_reload)
    restored = await backup_manager.load()
    assert backup_manager.confirmed and restored.revision == 1
    assert restored.backup_password_configured
    logs = await audit_rows(app, "backup.settings.update")
    assert len(logs) == 1 and logs[0].result == "success"


@pytest.mark.parametrize("method,path,body", [
    ("GET", BASE, None),
    ("GET", BASE + "/settings", None),
    ("PATCH", BASE + "/settings", {"revision": 1, "changes": {"local_keep": 2}}),
    ("POST", BASE + "/runs", {"settings_revision": 1}),
])
async def test_invalid_persisted_configuration_has_one_safe_unavailable_boundary(
    app, system_admin, backup_manager, method, path, body,
):
    await save(system_admin, encryption_enabled=False)
    async with app.state.sessionmaker() as db:
        row = await db.get(BackupSettings, 1)
        row.configuration = {**row.configuration, "local_keep": 0}
        await db.commit()
    payload = {**body, "request_id": str(uuid4())} if method == "POST" else body
    response = await system_admin.request(method, path, json=payload)
    assert response.status_code == 503
    assert response.json() == {"detail": "备份持久状态暂时无法确认，请稍后刷新核实"}
    assert not backup_manager.confirmed
    assert backup_manager.authorization.settings_confirmed
    assert (await system_admin.get("/api/v1/admin/system-settings")).status_code == 200
    # 配置损坏只暂停依赖它的新准入；历史安全记录仍能读取。
    assert (await system_admin.get(BASE + "/runs")).status_code == 200
    async with app.state.sessionmaker() as db:
        persisted = await db.get(BackupSettings, 1)
        assert persisted.revision == 1 and persisted.configuration["local_keep"] == 0
        assert await db.scalar(select(func.count()).select_from(BackupRun)) == 0
