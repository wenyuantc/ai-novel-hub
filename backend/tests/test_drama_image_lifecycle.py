"""造型图片归属、单书备份边界与从业务删除到文件回收的完整流程。"""

import asyncio
import sqlite3
import threading
import time
from contextlib import closing
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from PIL import Image
from sqlalchemy import CheckConstraint, UniqueConstraint, delete, inspect, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.media import cleanup, service
from app.models import MediaObject, Project, Task
from app.plugins.drama.models import DramaAssetImage, DramaCharacter, DramaEpisode, DramaLook, DramaSeries
from app.services.backups.database import snapshot_database
from app.services.backups.sqlite_restore import restore_sqlite_snapshot
from tests.helpers import create_project, purge_trashed_project
from tests.test_annotation_native_backup import _postgres_pair, _restore
from tests.test_drama_assets import BASE
from tests.test_drama_assets import library as library
from tests.test_drama_transfer import _seed_native
from tests.test_media_service import chunks
from tests.test_project_transfer import _backup, _import
from tests.test_recycle_bin import RECYCLE, item
from tests.test_serialization_concurrency import settings_overrides as settings_overrides
from tests.test_setting_fields_migration import _config
from tests.test_task_worker_migration import migration_db as migration_db


async def _seed_image_snapshot(conn):
    await _seed_native(conn)
    await conn.execute(DramaCharacter.__table__.insert().values(id=1, series_id=1, name="人物"))
    await conn.execute(DramaLook.__table__.insert().values(id=1, character_id=1, name="常服", is_default=True))
    root_id, thumb_id = str(uuid4()), str(uuid4())
    common = dict(
        location_id=1, project_id=1, owner_user_id=1, plugin_id="drama", scope="project", kind="image", status="ready"
    )
    await conn.execute(
        MediaObject.__table__.insert().values(id=root_id, storage_key="source.png", mime="image/png", **common)
    )
    await conn.execute(
        MediaObject.__table__.insert().values(
            id=thumb_id, parent_id=root_id, variant="thumb", storage_key="thumb.webp", mime="image/webp", **common
        )
    )
    await conn.execute(
        DramaAssetImage.__table__.insert().values(
            series_id=1,
            owner_type="look",
            owner_id=1,
            status="ready",
            media_object_id=root_id,
            is_selected=True,
            prompt="常服立绘",
        )
    )
    return root_id, thumb_id


async def test_asset_image_migration(migration_db, tmp_path):
    config, engine = migration_db
    await asyncio.to_thread(command.upgrade, config, "0084")

    def schema(conn):
        expected = DramaAssetImage.__table__
        inspector = inspect(conn)
        assert {
            c["name"]: (c["type"].compile(conn.dialect), c["nullable"]) for c in inspector.get_columns(expected.name)
        } == {c.name: (c.type.compile(conn.dialect), c.nullable) for c in expected.columns}
        assert {c["name"] for c in inspector.get_check_constraints(expected.name)} == {
            c.name for c in expected.constraints if isinstance(c, CheckConstraint)
        }
        assert {c["name"] for c in inspector.get_unique_constraints(expected.name)} == {
            c.name for c in expected.constraints if isinstance(c, UniqueConstraint)
        }
        assert {c["name"] for c in inspector.get_indexes(expected.name) if not c.get("duplicates_constraint")} == {
            i.name for i in expected.indexes
        }
        assert {f["name"]: f["options"].get("ondelete") for f in inspector.get_foreign_keys(expected.name)} == {
            f.name: f.ondelete for f in expected.foreign_key_constraints
        }

    async with engine.begin() as conn:
        await conn.run_sync(schema)
        original, thumb = await _seed_image_snapshot(conn)
    with pytest.raises(RuntimeError, match="资产图"):
        await asyncio.to_thread(command.downgrade, config, "0083")
    if engine.dialect.name == "sqlite":
        snapshot, copy = tmp_path / "snapshot", tmp_path / "restored.db"
        await asyncio.to_thread(
            snapshot_database, str(engine.url), snapshot, deadline=time.monotonic() + 60, stop_event=threading.Event()
        )
        assert await asyncio.to_thread(restore_sqlite_snapshot, snapshot, copy) == "0084"
        with closing(sqlite3.connect(copy)) as db:
            assert db.execute("SELECT prompt,media_object_id,is_selected FROM drama_asset_images").fetchall() == [
                ("常服立绘", original, 1)
            ]
            assert db.execute("SELECT id FROM media_objects WHERE parent_id=?", (original,)).fetchone() == (thumb,)
        with closing(sqlite3.connect(snapshot)) as db:
            db.execute("DROP TABLE drama_asset_images")
        with pytest.raises(ValueError, match="短剧资产图"):
            await asyncio.to_thread(restore_sqlite_snapshot, snapshot, tmp_path / "broken.db")
    async with engine.begin() as conn:
        await conn.execute(delete(DramaAssetImage))
    await asyncio.to_thread(command.downgrade, config, "0083")
    async with engine.connect() as conn:
        assert not await conn.run_sync(lambda c: inspect(c).has_table("drama_asset_images"))


@pytest.mark.parametrize("parent", ["look", "character", "series", "project"])
async def test_asset_delete_marks_only_owned_media_and_reclaims_files(app, client, library, system_admin, parent):
    sid, eid, pid = library
    character = (await client.post(f"{BASE}/series/{sid}/characters", json={"name": "守钟人"})).json()
    look = (await client.post(f"{BASE}/characters/{character['id']}/looks", json={"name": "冬装"})).json()
    buf = BytesIO()
    Image.new("RGB", (60, 90)).save(buf, "PNG")
    async with app.state.sessionmaker() as db:
        owner = (await db.get(Project, pid)).user_id

        async def add_image(look_id):
            root = await service.store(
                db,
                owner_id=owner,
                scope="project",
                kind="image",
                mime="image/png",
                source=chunks(buf.getvalue()),
                max_bytes=4096,
                plugin_id="drama",
                project_id=pid,
            )
            thumb = await db.scalar(select(MediaObject).where(MediaObject.parent_id == root.id))
            image = DramaAssetImage(
                series_id=sid,
                owner_type="look",
                owner_id=look_id,
                media_object_id=root.id,
                status="ready",
                is_selected=True,
                prompt="冬季",
            )
            db.add(image)
            await db.commit()
            return (
                image.id,
                [root.id, thumb.id],
                [Path(app.state.settings.media_dir) / obj.storage_key for obj in (root, thumb)],
            )

        image_id, media_ids, paths = await add_image(look["id"])
        other_id, other_media, other_files = await add_image(character["looks"][0]["id"])

    # 删除一集不能移除全剧共享的素材或资产图。
    async with app.state.sessionmaker() as db:
        revision = (await db.get(DramaEpisode, eid)).revision
    assert (await client.delete(f"{BASE}/episodes/{eid}", params={"revision": revision})).is_success
    assert all(path.is_file() for path in paths)
    if parent == "project":
        async with app.state.sessionmaker.begin() as db:
            task = Task(user_id=owner, project_id=pid, kind="drama.image", status="succeeded")
            db.add(task)
            await db.flush()
            (await db.get(DramaAssetImage, image_id)).task_id = task.id
            task_id, key = task.id, task.statistics_key
        result = await client.delete(f"/api/v1/tasks/{task_id}", params={"task_key": key})
        assert result.is_success, result.text
        async with app.state.sessionmaker() as db:
            linked = await db.get(DramaAssetImage, image_id)
            assert linked.task_id is None and linked.media_object_id == media_ids[0]

        backup = await _backup(client, pid)
        assert "drama_asset_images" not in backup["tables"] and "media_objects" not in backup["tables"]
        imported = (await _import(client, backup))["project"]["id"]
        async with app.state.sessionmaker() as db:
            assert (
                await db.scalar(select(DramaAssetImage.id).join(DramaSeries).where(DramaSeries.project_id == imported))
                is None
            )
        assert (await client.delete(f"/api/v1/projects/{pid}")).is_success
        assert all(path.is_file() for path in paths)
        trashed = await item(system_admin, pid)
        restored = await system_admin.post(
            f"{RECYCLE}/{pid}/restore", json={"deleted_at": trashed["deleted_at"], "confirm": True}
        )
        assert restored.is_success, restored.text
        async with app.state.sessionmaker() as db:
            assert (await db.get(DramaAssetImage, image_id)).media_object_id == media_ids[0]
            assert (await db.get(MediaObject, media_ids[0])).status == "ready"
        assert all(path.is_file() for path in paths)
        assert (await client.delete(f"/api/v1/projects/{pid}")).is_success
        await purge_trashed_project(system_admin, pid)
    else:
        if parent == "series":
            entity = (await client.get(f"{BASE}/series/{sid}")).json()
            url = f"{BASE}/series/{sid}"
        elif parent == "character":
            (entity,) = (await client.get(f"{BASE}/series/{sid}/assets")).json()["characters"]
            url = f"{BASE}/characters/{entity['id']}"
        else:
            entity = look
            url = f"{BASE}/looks/{look['id']}"
            other_project = (await create_project(client, title="其他作品"))["id"]
            async with app.state.sessionmaker.begin() as db:
                (await db.get(MediaObject, media_ids[1])).project_id = other_project
            blocked = await client.delete(url, params={"revision": entity["revision"]})
            assert blocked.status_code == 409 and blocked.json()["code"] == "drama_image_reference_invalid"
            async with app.state.sessionmaker.begin() as db:
                assert (await db.get(DramaAssetImage, image_id)).is_selected
                assert (await db.get(MediaObject, media_ids[0])).status == "ready"
                (await db.get(MediaObject, media_ids[1])).project_id = pid
        result = await client.delete(url, params={"revision": entity["revision"]})
        assert result.is_success, result.text
        async with app.state.sessionmaker() as db:
            assert all(
                obj.status == "deleting"
                for obj in await db.scalars(select(MediaObject).where(MediaObject.id.in_(media_ids)))
            )
        await cleanup.sweep(app.state.sessionmaker)
    async with app.state.sessionmaker() as db:
        assert await db.get(DramaAssetImage, image_id) is None
        assert not any(path.exists() for path in paths)
        if parent == "look":
            assert await db.get(DramaAssetImage, other_id) is not None
            assert all(path.is_file() for path in other_files)
            assert all(
                obj.status == "ready"
                for obj in await db.scalars(select(MediaObject).where(MediaObject.id.in_(other_media)))
            )
        else:
            assert await db.get(DramaAssetImage, other_id) is None
            assert not any(path.exists() for path in other_files)


async def test_asset_image_postgres_native_snapshot(tmp_path):
    async with _postgres_pair() as (source, target):
        cfg = _config(tmp_path / "unused.db")
        cfg.set_main_option("sqlalchemy.url", source.replace("%", "%%"))
        await asyncio.to_thread(command.upgrade, cfg, "0084")
        engine = create_async_engine(source)
        try:
            async with engine.begin() as conn:
                root, thumb = await _seed_image_snapshot(conn)
        finally:
            await engine.dispose()
        snapshot = tmp_path / "snapshot"
        await asyncio.to_thread(
            snapshot_database, source, snapshot, deadline=time.monotonic() + 60, stop_event=threading.Event()
        )
        await _restore(snapshot, target)
        restored = create_async_engine(target)
        try:
            async with restored.connect() as conn:
                assert (
                    await conn.execute(
                        select(DramaAssetImage.media_object_id, DramaAssetImage.prompt, DramaAssetImage.is_selected)
                    )
                ).all() == [(root, "常服立绘", True)]
                assert (await conn.scalars(select(MediaObject.id).where(MediaObject.parent_id == root))).all() == [
                    thumb
                ]
        finally:
            await restored.dispose()
