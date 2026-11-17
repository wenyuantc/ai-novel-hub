"""备份请求不跨作品回收／恢复复用旧的生命周期代次。"""

import asyncio
from tempfile import SpooledTemporaryFile

import httpx
import pytest

from app.api.v1.transfer import _validated_backup_stream
from app.core.errors import ConflictError
from app.models import Project, User
from app.schemas.transfer import BackupOptions
from app.services.project_transfer import exporter
from tests.helpers import create_project, register, wait_until
from tests.test_collaboration_stream_running_e5c1 import _serve


async def _exercise_recycled_then_restored(client, app, phase):
    owner = await register(client)
    project = await create_project(client)
    project_id = project["id"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as admin:
        admin_user = await register(admin, "backup-admin")
        async with app.state.sessionmaker() as db:
            (await db.get(User, admin_user["id"])).is_admin = True
            await db.commit()
        async with app.state.sessionmaker() as db:
            plan = await exporter.prepare_export(db, owner["id"], project_id, BackupOptions())
        if phase == "staging":
            stream = exporter.stream_export(app.state.sessionmaker, plan)
        else:
            backing = SpooledTemporaryFile(max_size=8, mode="w+b")
            backing.write(b'{"safe":true}')
            backing.seek(0)
            stream = _validated_backup_stream(backing, 13, app.state.sessionmaker, plan)
        try:
            assert await anext(stream)
            assert (await client.delete(f"/api/v1/projects/{project_id}")).status_code == 204
            listing = await admin.get("/api/v1/admin/recycle-bin/projects")
            assert listing.status_code == 200, listing.text
            recycled = next(row for row in listing.json()["items"] if row["id"] == project_id)
            restored = await admin.post(f"/api/v1/admin/recycle-bin/projects/{project_id}/restore", json={
                "deleted_at": recycled["deleted_at"], "confirm": True,
            })
            assert restored.status_code == 204, restored.text
            with pytest.raises(ConflictError) as exc:
                async for _ in stream:
                    pass
            assert exc.value.extra["code"] == "project_lifecycle_changed"
        finally:
            await stream.aclose()
        assert (await client.get(f"/api/v1/projects/{project_id}/backup")).status_code == 200


@pytest.mark.parametrize("phase", ["staging", "sending"])
async def test_backup_does_not_resume_after_recycle_restore(client, app, phase):
    await _exercise_recycled_then_restored(client, app, phase)


async def test_backup_real_http_disconnect_closes_spooled_file(client, app, monkeypatch):
    from app.api.v1 import transfer

    await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        (await db.get(Project, project["id"])).premise = "材料" * 300_000
        await db.commit()

    opened = []
    original_spool = transfer.SpooledTemporaryFile
    original_stream = transfer._validated_backup_stream
    reached, release = asyncio.Event(), asyncio.Event()

    def spool(*args, **kwargs):
        file = original_spool(*args, **kwargs)
        opened.append(file)
        return file

    async def gated_stream(*args):
        async for chunk in original_stream(*args):
            yield chunk
            reached.set()
            await release.wait()

    monkeypatch.setattr(transfer, "SpooledTemporaryFile", spool)
    monkeypatch.setattr(transfer, "_validated_backup_stream", gated_stream)
    cookies = {cookie.name: cookie.value for cookie in client.cookies.jar}
    async with _serve(app) as base:
        try:
            async with httpx.AsyncClient(base_url=base, cookies=cookies) as network:
                async with network.stream("GET", f"/api/v1/projects/{project['id']}/backup") as response:
                    assert response.status_code == 200
                    assert await anext(response.aiter_bytes())
                    await asyncio.wait_for(reached.wait(), 5)
                    assert opened and opened[-1]._rolled and not opened[-1].closed
                await wait_until(lambda: opened[-1].closed, within=5)
        finally:
            release.set()
