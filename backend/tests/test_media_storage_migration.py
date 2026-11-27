import asyncio
import sqlite3
import threading
import time
from contextlib import closing

import pytest
from alembic import command
from sqlalchemy import UniqueConstraint, delete, inspect, select, text

from app.models import Base, MediaObject, Role
from app.services.backups.database import snapshot_database
from app.services.backups.sqlite_restore import restore_sqlite_snapshot
from tests.test_task_worker_migration import migration_db as migration_db

TABLES = {"media_objects", "media_storage_locations", "media_storage_settings", "media_volume_reports"}


def _verify_schema(connection):
    inspector = inspect(connection)
    for name in TABLES:
        table = Base.metadata.tables[name]
        columns = inspector.get_columns(name)
        assert {c["name"]: c["nullable"] for c in columns} == {c.name: c.nullable for c in table.columns}
        assert {i["name"] for i in inspector.get_indexes(name) if not i.get("duplicates_constraint")} == {
            i.name for i in table.indexes
        }
        assert {u["name"] for u in inspector.get_unique_constraints(name)} == {
            u.name for u in table.constraints if isinstance(u, UniqueConstraint)
        }
        assert {c["name"]: c["type"].compile(connection.dialect) for c in columns} == {
            c.name: c.type.compile(connection.dialect) for c in table.columns
        }
        assert {fk["name"] for fk in inspector.get_foreign_keys(name)} == {
            fk.name for fk in table.foreign_key_constraints
        }


async def test_media_schema_and_backup_roundtrip(migration_db, tmp_path):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0080")
    permissions = select(Role.permissions).where(Role.system_key == "super_admin")
    async with engine.connect() as db:
        previous = await db.scalar(permissions)
    await asyncio.to_thread(command.upgrade, cfg, "0081")
    async with engine.connect() as db:
        await db.run_sync(_verify_schema)
        assert (await db.execute(text("SELECT kind,state FROM media_storage_locations"))).one() == ("local", "active")
        assert (await db.execute(text("SELECT id,revision,default_quota_bytes FROM media_storage_settings"))).one() == (
            1,
            1,
            5 * 1024**3,
        )
        current = await db.scalar(permissions)
        expected = previous.copy()
        at = expected.index("settings.read")
        expected[at:at] = ["media_storage.read", "media_storage.configure"]
        assert current == expected

    async with engine.begin() as db:
        location = await db.scalar(text("SELECT id FROM media_storage_locations"))
        await db.execute(
            MediaObject.__table__.insert().values(
                id="reference",
                location_id=location,
                storage_key="81/reference.png",
                scope="user",
                kind="image",
                source="generated",
                mime="image/png",
                size_bytes=4096,
            )
        )
    with pytest.raises(RuntimeError, match="仍有媒体文件"):
        await asyncio.to_thread(command.downgrade, cfg, "0080")

    if engine.dialect.name == "sqlite":
        snapshot, restored = tmp_path / "snapshot", tmp_path / "restored.db"
        await asyncio.to_thread(
            snapshot_database, str(engine.url), snapshot, deadline=time.monotonic() + 60, stop_event=threading.Event()
        )
        assert await asyncio.to_thread(restore_sqlite_snapshot, snapshot, restored) == "0081"
        with closing(sqlite3.connect(restored)) as copy:
            assert TABLES <= {r[0] for r in copy.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            assert copy.execute("SELECT storage_key,source,size_bytes FROM media_objects").fetchall() == [
                ("81/reference.png", "generated", 4096)
            ]
    async with engine.begin() as db:
        await db.execute(delete(MediaObject))
    await asyncio.to_thread(command.downgrade, cfg, "0080")
    async with engine.connect() as db:
        assert not TABLES.intersection(await db.run_sync(lambda c: inspect(c).get_table_names()))
        assert await db.scalar(permissions) == previous


async def test_downgrade_preserves_media_configuration(migration_db):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0081")
    async with engine.begin() as db:
        await db.execute(text("UPDATE media_storage_settings SET revision=2"))
    with pytest.raises(RuntimeError, match="媒体"):
        await asyncio.to_thread(command.downgrade, cfg, "0080")
