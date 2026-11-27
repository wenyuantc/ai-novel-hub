import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.media import locations
from app.models import AdminAuditLog
from app.models.media import MediaStorageLocation, MediaVolumeReport
from tests.helpers import register
from tests.rbac_helpers import assign_roles, create_role

URL = "/api/v1/admin/media-storage"
S3 = {
    "kind": "s3",
    "endpoint": "https://storage.example.com",
    "bucket": "media-assets",
    "access_key": "access-example",
    "secret_key": "secret-example",
}


async def test_switch_probe_secrets_and_revision(app, system_admin, monkeypatch):
    page = (await system_admin.get(URL)).json()
    active = next(row for row in page["locations"] if row["state"] == "active")
    assert active["kind"] == "local"
    rev = page["revision"]
    probed = []

    async def check(self):
        probed.append(self)

    monkeypatch.setattr(locations.S3Backend, "check", check)
    assert (await system_admin.post(URL + "/test", json=S3)).json() == {"ok": True}
    assert (await system_admin.get(URL)).json() == page
    change = await system_admin.patch(URL, json={"revision": rev, "location": S3})
    assert change.status_code == 200, change.text
    assert "secret-example" not in change.text and "access-example" not in change.text
    assert len(probed) == 2
    old = next(row for row in change.json()["locations"] if row["id"] == active["id"])
    new = next(row for row in change.json()["locations"] if row["state"] == "active")
    assert old["state"] == "readonly" and new["has_access_key"] and new["has_secret_key"]
    async with app.state.sessionmaker() as db:
        stored = await db.get(MediaStorageLocation, new["id"])
        assert stored.secrets_enc["secret_key"] != S3["secret_key"]
        assert app.state.cipher.decrypt(stored.secrets_enc["secret_key"]) == S3["secret_key"]
        audit = (await db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "media_storage.update"))).one()
        assert audit.result == "success" and S3["secret_key"] not in str(audit.summary)
    assert (await system_admin.post(URL + "/test", json={"location_id": old["id"]})).status_code == 200
    same = {k: v for k, v in S3.items() if k not in ("access_key", "secret_key")}
    assert (await system_admin.post(URL + "/test", json=same)).status_code == 200
    # 切换目的端点时必须重新输入凭据，防止持有配置权限的账号将已存密钥转发给其他服务。
    assert (
        await system_admin.post(URL + "/test", json=same | {"endpoint": "https://new.example.com"})
    ).status_code == 400
    conflict = await system_admin.patch(URL, json={"revision": rev, "default_quota_bytes": 128})
    assert conflict.status_code == 409 and conflict.json()["code"] == "stale_revision"

    async def fail(self):
        raise OSError("secret-example")

    monkeypatch.setattr(locations.S3Backend, "check", fail)
    failed = await system_admin.patch(URL, json={"revision": rev + 1, "location": same})
    assert failed.status_code == 400 and "secret-example" not in failed.text
    assert (await system_admin.get(URL)).json() == change.json()


async def test_read_only_and_concurrent_quota(client, system_admin):
    user = await register(client)
    role = await create_role(system_admin, "媒体存储查看者", ["media_storage.read"])
    await assign_roles(system_admin, user["id"], [role["id"]])
    page = await client.get(URL)
    assert page.status_code == 200
    patch = {"revision": page.json()["revision"], "default_quota_bytes": 1000}
    assert (await client.patch(URL, json=patch)).status_code == 403
    assert (await client.post(URL + "/test", json={"kind": "local"})).status_code == 403
    changes = await asyncio.gather(*(system_admin.patch(URL, json=patch) for _ in range(2)))
    assert sorted(result.status_code for result in changes) == [200, 409]


async def test_volume_reports_compare_fresh_instances(app, system_admin, tmp_path):
    async with app.state.sessionmaker() as db:
        db.info["media_settings"] = db.info["media_settings"].model_copy(update={"media_dir": str(tmp_path / "first")})
        await locations.report_volume(db, "worker-a")
        await locations.report_volume(db, "worker-b")
        await db.commit()
        assert not await locations.volumes_mismatch(db)
        db.info["media_settings"] = db.info["media_settings"].model_copy(update={"media_dir": str(tmp_path / "other")})
        await locations.report_volume(db, "worker-b")
        await db.commit()
        assert await locations.volumes_mismatch(db)
    assert (await system_admin.get(URL)).json()["volume_mismatch"]
    async with app.state.sessionmaker() as db:
        (await db.get(MediaVolumeReport, "worker-a")).seen_at = datetime.now(UTC) - timedelta(days=1)
        await db.commit()
    assert not (await system_admin.get(URL)).json()["volume_mismatch"]


async def test_switch_routes_new_files_and_keeps_previous_location_readable(
    app, client, system_admin, monkeypatch, tmp_path
):
    from app.media import service

    owner = (await register(client))["id"]
    video = b"\x00\x00\x00\x18ftypmp42" + bytes(64)

    async def chunks(data):
        yield data

    def test_s3(**kwargs):
        return locations.LocalBackend(tmp_path / "object-store")

    monkeypatch.setattr(locations, "S3Backend", test_s3)
    async with app.state.sessionmaker() as db:
        before = await service.store(
            db, owner_id=owner, scope="user", kind="video", mime="video/mp4", source=chunks(video), max_bytes=1024
        )
    page = (await system_admin.get(URL)).json()
    assert (await system_admin.patch(URL, json={"revision": page["revision"], "location": S3})).status_code == 200
    async with app.state.sessionmaker() as db:
        after = await service.store(
            db,
            owner_id=owner,
            scope="user",
            kind="video",
            mime="video/mp4",
            source=chunks(video + b"new"),
            max_bytes=1024,
        )
        assert before.location_id != after.location_id
        for media, expected in ((before, video), (after, video + b"new")):
            stream = await service.open_range(db, media, None, None)
            assert b"".join([part async for part in stream]) == expected
    page = (await system_admin.get(URL)).json()
    assert page["total_bytes"] == 2 * len(video) + 3
    assert sorted((item["file_count"], item["size_bytes"]) for item in page["locations"]) == [
        (1, len(video)),
        (1, len(video) + 3),
    ]
