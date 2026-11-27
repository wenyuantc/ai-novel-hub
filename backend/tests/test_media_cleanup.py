import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.media import service
from app.media.backends import LocalBackend
from app.models.media import MediaObject
from tests.helpers import create_project, register
from tests.test_media_service import VIDEO, chunks
from tests.test_recycle_bin import PROJECTS, RECYCLE, item


async def _store(db, user, pid=None):
    return await service.store(db, owner_id=user, scope="project" if pid else "user", project_id=pid,
                               kind="video", mime="video/mp4", source=chunks(VIDEO), max_bytes=2048)


async def test_purge_after_rollback_restore_then_physical_delete(app, client, recycle_admin):
    user = (await register(client))["id"]
    pid = (await create_project(client))["id"]
    async with app.state.sessionmaker() as db:
        obj = await _store(db, user, pid)
        await service.mark_deleted(db, [obj.id])
        await db.rollback()
    async with app.state.sessionmaker() as db:
        obj = await db.scalar(select(MediaObject))
        media = Path(app.state.settings.media_dir) / obj.storage_key
    assert (await client.delete(f"{PROJECTS}/{pid}")).status_code == 204
    deleted = await item(recycle_admin, pid)
    assert media.read_bytes() == VIDEO
    body = {"deleted_at": deleted["deleted_at"], "confirm": True}
    assert (await recycle_admin.post(f"{RECYCLE}/{pid}/restore", json=body)).status_code == 204
    assert media.read_bytes() == VIDEO
    assert (await client.delete(f"{PROJECTS}/{pid}")).status_code == 204
    deleted = await item(recycle_admin, pid)
    body.update(deleted_at=deleted["deleted_at"], title=deleted["title"])
    assert (await recycle_admin.post(f"{RECYCLE}/{pid}/purge", json=body)).status_code == 204
    assert not media.exists()
    async with app.state.sessionmaker() as db:
        assert await db.get(MediaObject, obj.id) is None


async def test_sweep_retries_without_erasing_files_on_other_volumes(app, client, monkeypatch):
    factory = app.state.sessionmaker
    user = (await register(client))["id"]
    now = datetime.now(UTC)
    async with factory() as db:
        objects = [await _store(db, user) for _ in range(6)]
        ready, stale, orphan, ownerless, marked, foreign = objects
        stale.status, stale.created_at = "pending", now - timedelta(hours=25)
        orphan.scope = "project"
        ownerless.owner_user_id = None
        await service.mark_deleted(db, [marked.id, foreign.id])
        foreign.volume_id = "other-volume"
        await db.commit()
    deleted = []
    original = LocalBackend.delete

    async def flaky(backend, key):
        if key == marked.storage_key and key not in deleted:
            deleted.append(key)
            raise OSError("temporary")
        deleted.append(key)
        await original(backend, key)

    monkeypatch.setattr(LocalBackend, "delete", flaky)
    await service.sweep(factory, now=now)
    async with factory() as db:
        remaining = set(await db.scalars(select(MediaObject.id)))
    assert remaining == {ready.id, marked.id, foreign.id}
    await asyncio.gather(service.sweep(factory, now=now), service.sweep(factory, now=now))
    assert deleted.count(marked.storage_key) == 2
    assert foreign.storage_key not in deleted
    async with factory() as db:
        assert set(await db.scalars(select(MediaObject.id))) == {ready.id, foreign.id}


async def test_mark_and_sweep_original_also_removes_separately_stored_poster(app, client):
    from io import BytesIO

    import pytest
    from PIL import Image

    from app.core.errors import ConflictError

    user = (await register(client))["id"]
    factory = app.state.sessionmaker
    png = BytesIO()
    Image.new("RGB", (10, 10)).save(png, "PNG")
    async with factory() as db:
        video = await _store(db, user)
        poster = await service.store_derived(db, video, "poster", "image/png", png.getvalue())
        assert poster.parent_id == video.id
        assert (await service.store_derived(db, video, "poster", "image/png", png.getvalue())).id == poster.id
        other = BytesIO()
        Image.new("RGB", (11, 10)).save(other, "PNG")
        with pytest.raises(ConflictError) as changed:
            await service.store_derived(db, video, "poster", "image/png", other.getvalue())
        assert changed.value.extra["code"] == "media_variant_conflict"
        # 发生冲突的事务已回滚，重读对象。
        video = await db.scalar(select(MediaObject).where(MediaObject.parent_id.is_(None)))
        poster = await db.scalar(select(MediaObject).where(MediaObject.parent_id == video.id))
        await service.mark_deleted(db, [video.id])
        await db.commit()
    await service.sweep(factory, object_ids=[video.id])
    async with factory() as db:
        assert await db.scalar(select(MediaObject)) is None
    for key in (video.storage_key, poster.storage_key):
        assert not (Path(app.state.settings.media_dir) / key).exists()
