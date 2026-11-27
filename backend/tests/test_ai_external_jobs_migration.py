import asyncio
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import AiExternalJob, AiUsageCall, Base, MediaObject
from app.services.backups.database import snapshot_database
from app.services.backups.sqlite_restore import restore_sqlite_snapshot
from app.services.project_transfer.registry import NOT_EXPORTED
from tests.test_annotation_native_backup import _postgres_pair, _restore
from tests.test_setting_fields_migration import _config
from tests.test_task_worker_migration import migration_db as migration_db


def _schema(connection):
    inspector = inspect(connection)
    for name in ("ai_external_jobs", "ai_model_prices", "ai_usage_calls"):
        table = Base.metadata.tables[name]
        assert {
            c["name"]: (c["type"].compile(connection.dialect), c["nullable"]) for c in inspector.get_columns(name)
        } == {c.name: (c.type.compile(connection.dialect), c.nullable) for c in table.columns}
        expected_checks = {c.name for c in table.constraints if isinstance(c, CheckConstraint)}
        expected_checks.update(
            c.name for col in table.columns for c in col.constraints if isinstance(c, CheckConstraint)
        )
        assert {c["name"] for c in inspector.get_check_constraints(name)} == expected_checks
        assert {c["name"] for c in inspector.get_unique_constraints(name)} == {
            c.name for c in table.constraints if isinstance(c, UniqueConstraint)
        }
        assert {c["name"] for c in inspector.get_indexes(name) if not c.get("duplicates_constraint")} == {
            c.name for c in table.indexes
        }
        assert {f["name"]: f["options"].get("ondelete") for f in inspector.get_foreign_keys(name)} == {
            f.name: f.ondelete for f in table.foreign_key_constraints
        }


async def test_external_jobs_upgrade_restore_and_downgrade(migration_db, tmp_path):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0081")
    legacy = str(uuid4())
    parent, thumbnail, audio = (str(uuid4()) for _ in range(3))
    media = dict(location_id=1, scope="user", kind="image", mime="image/png", source="upload", size_bytes=10)
    links = text("SELECT id,parent_id FROM media_objects ORDER BY storage_key")

    async with engine.begin() as db:
        await db.execute(MediaObject.__table__.insert().values(id=parent, storage_key="a", **media))
        await db.execute(
            MediaObject.__table__.insert().values(
                id=thumbnail, storage_key="b", parent_id=parent, variant="thumb", **media
            )
        )
        await db.execute(
            text(
                "INSERT INTO ai_usage_calls (id, started_at, provider, model, model_type, kind) "
                "VALUES (:id, CURRENT_TIMESTAMP, 'existing', 'example', 'image', 'cover')"
            ),
            {"id": legacy},
        )
    await asyncio.to_thread(command.upgrade, cfg, "0082")
    async with engine.begin() as db:
        await db.run_sync(_schema)
        assert (await db.execute(links)).all() == [(parent, None), (thumbnail, parent)]

        assert (await db.execute(text("SELECT id,video_seconds,price_video_nano FROM ai_usage_calls"))).one() == (
            legacy,
            None,
            None,
        )
        job_id = str(uuid4())
        await db.execute(
            Base.metadata.tables["ai_external_jobs"]
            .insert()
            .values(
                id=job_id,
                config_id=1,
                config_instance_key=str(uuid4()),
                provider="minimax",
                model="test",
                request={},
                external_id="example-remote-id",
                state="submitted",
            )
        )
    assert "ai_external_jobs" in NOT_EXPORTED
    with pytest.raises(RuntimeError, match="视频"):
        await asyncio.to_thread(command.downgrade, cfg, "0081")
    if engine.dialect.name == "sqlite":
        snapshot, restored = tmp_path / "snapshot", tmp_path / "restored.db"
        await asyncio.to_thread(
            snapshot_database, str(engine.url), snapshot, deadline=time.monotonic() + 60, stop_event=threading.Event()
        )
        assert await asyncio.to_thread(restore_sqlite_snapshot, snapshot, restored) == "0082"
        with closing(sqlite3.connect(restored)) as copy:
            assert copy.execute("SELECT id,external_id FROM ai_external_jobs").fetchall() == [
                (job_id, "example-remote-id")
            ]
    async with engine.begin() as db:
        await db.execute(text("DELETE FROM ai_external_jobs"))
        await db.execute(
            MediaObject.__table__.insert().values(
                **(media | {"id": audio, "storage_key": "voice", "kind": "audio", "mime": "audio/wav"})
            )
        )
    with pytest.raises(RuntimeError, match="音频"):
        await asyncio.to_thread(command.downgrade, cfg, "0081")
    async with engine.begin() as db:
        await db.execute(text("DELETE FROM media_objects WHERE id=:id"), {"id": audio})
    await asyncio.to_thread(command.downgrade, cfg, "0081")
    async with engine.connect() as db:
        assert "ai_external_jobs" not in await db.run_sync(lambda c: inspect(c).get_table_names())
        assert (await db.execute(links)).all() == [(parent, None), (thumbnail, parent)]
        assert await db.scalar(text("SELECT id FROM ai_usage_calls")) == legacy
        columns = await db.run_sync(lambda c: inspect(c).get_columns("ai_usage_calls"))
        assert {"video_seconds", "price_video_nano"}.isdisjoint(c["name"] for c in columns)


async def test_postgres_backup_preserves_vendor_id_and_video_accounting(tmp_path):
    """数据库原生备份包括厂商任务号及结算时使用的价格，恢复后仍可按同一账本对账。"""
    async with _postgres_pair() as (source, target):
        cfg = _config(tmp_path / "unused.db")
        cfg.set_main_option("sqlalchemy.url", source.replace("%", "%%"))
        await asyncio.to_thread(command.upgrade, cfg, "0082")
        engine = create_async_engine(source)
        key = str(uuid4())
        try:
            async with engine.begin() as db:
                finished = datetime.now(UTC)
                await db.execute(
                    AiUsageCall.__table__.insert().values(
                        id=key,
                        provider="minimax",
                        model="sample",
                        model_type="video",
                        kind="ai.video_probe",
                        status="succeeded",
                        sent=True,
                        started_at=finished,
                        finished_at=finished,
                        currency="CNY",
                        price_video_nano=50000,
                        video_seconds=6,
                        amount_nano=300000,
                        pricing_status="priced",
                    )
                )
                await db.execute(
                    AiExternalJob.__table__.insert().values(
                        id=key,
                        external_id="remote-42",
                        config_id=1,
                        config_instance_key=str(uuid4()),
                        provider="minimax",
                        model="sample",
                        request={},
                        state="succeeded",
                        usage_call_id=key,
                    )
                )
            snapshot = tmp_path / "native.dump"
            await asyncio.to_thread(
                snapshot_database, source, snapshot, deadline=time.monotonic() + 60, stop_event=threading.Event()
            )
            await _restore(snapshot, target)
        finally:
            await engine.dispose()

        restored = create_async_engine(target)
        try:
            async with restored.connect() as db:
                assert (
                    await db.execute(
                        text(
                            "SELECT j.id,j.external_id,c.video_seconds,c.price_video_nano,c.amount_nano "
                            "FROM ai_external_jobs j "
                            "JOIN ai_usage_calls c ON j.usage_call_id=c.id"
                        )
                    )
                ).one() == (key, "remote-42", 6, 50000, 300000)
        finally:
            await restored.dispose()
