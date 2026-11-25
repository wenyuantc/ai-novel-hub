"""SQLite 单文件快照冷恢复，包括初次引入独立租约库之前的备份。"""

import asyncio
import hashlib
import sqlite3
import threading
import time
from contextlib import closing
from uuid import uuid4

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import select

from app.main import create_app
from app.models import Task, TaskExecutionSlot, TaskStatistic, TaskWorker, TaskWorkRecord, User
from app.services import authorization_service, task_statistics_service
from app.services.backups.database import snapshot_database
from app.services.backups.sqlite_restore import restore_sqlite_snapshot
from app.services.task_engine.sqlite_lease_pair import SQLiteLeasePairError
from tests.helpers import make_settings
from tests.test_setting_fields_migration import _config


def _backup(source, snapshot):
    snapshot_database(f"sqlite+aiosqlite:///{source}", snapshot,
                      deadline=time.monotonic() + 20, stop_event=threading.Event())


@pytest.mark.parametrize("corrupt_side", [False, True], ids=["intact", "damaged-header"])
async def test_new_copy_rotates_lease_authority_and_recovers_saved_task(tmp_path, corrupt_side):
    live = tmp_path / "live.db"
    snapshot = tmp_path / "snapshot.sqlite3"
    config = _config(live)
    head = ScriptDirectory.from_config(config).get_current_head()
    await asyncio.to_thread(command.upgrade, config, "head")
    settings = make_settings(tmp_path, database_url=f"sqlite+aiosqlite:///{live}")
    original = create_app(settings, start_task_engine=False)
    old_engine = original.state.task_engine
    damaged = False
    try:
        old_worker = await old_engine.lease.register()
        previous_generation = old_engine.lease.sqlite_pair.generation
        execution, failure = str(uuid4()), str(uuid4())
        async with original.state.sessionmaker() as db:
            user = User(username="restore-test", password_hash="test")
            db.add(user)
            await db.flush()
            task = Task(user_id=user.id, kind="outline.continue", status="running",
                        worker_key=old_worker, execution_key=execution)
            db.add(task)
            await task_statistics_service.record_created(db, task)
            db.add(TaskExecutionSlot(execution_key=execution, task_id=task.id, worker_key=old_worker,
                                     statistics_key=task.statistics_key, queued_at=task.queued_at, user_id=user.id))
            db.add(TaskWorkRecord(work_key=failure, worker_key=old_worker, kind="cleanup", state="failed",
                                  task_id=task.id, task_key=task.statistics_key, queued_at=task.queued_at,
                                  evidence={"claim": execution}))
            await db.commit()
            task_id, task_key = task.id, task.statistics_key
        await asyncio.to_thread(_backup, live, snapshot)
        digest = hashlib.sha256(snapshot.read_bytes()).digest()
        with closing(sqlite3.connect(snapshot)) as saved:
            assert saved.execute("SELECT execution_key FROM task_execution_slots").fetchone() == (execution,)
        if corrupt_side:
            side = old_engine.lease.sqlite_pair.path
            assert await old_engine.lease.heartbeat()
            # 此处仅登记了执行者，后台心跳循环尚未启动。原位破坏完整的 16 字节 SQLite 文件头。
            with side.open("r+b") as output:
                assert output.read(16) == b"SQLite format 3\0"
                output.seek(0)
                output.write(b"damaged-header!!")
            damaged = True
            corrupt_digest = hashlib.sha256(side.read_bytes()).digest()
            assert not await old_engine.lease.heartbeat()
            peer = create_app(settings, start_task_engine=False)
            try:
                with pytest.raises(SQLiteLeasePairError):
                    await peer.state.task_engine.start()
                assert not peer.state.task_engine.running and peer.state.task_engine.lease.worker_key is None
                async with peer.state.sessionmaker() as db:
                    assert list(await db.scalars(select(TaskWorker.worker_key))) == [old_worker]
                    assert (await db.get(Task, task_id)).status == "running"
            finally:
                await peer.state.engine.dispose()
            assert hashlib.sha256(side.read_bytes()).digest() == corrupt_digest
        other = tmp_path / "rejected.db"
        with pytest.raises(SQLiteLeasePairError):
            await asyncio.to_thread(restore_sqlite_snapshot, snapshot, other)
        assert not other.exists()
    finally:
        try:
            if damaged:
                with pytest.raises(SQLiteLeasePairError):
                    await old_engine.lease.stop()
            else:
                await old_engine.lease.stop()
        finally:
            await original.state.engine.dispose()

    rollback = tmp_path / "rollback"
    rollback.mkdir()
    for old in tmp_path.glob("live.db*"):
        old.rename(rollback / old.name)
    previous_lock = rollback / "live.db.leases.lock"
    with closing(sqlite3.connect(previous_lock, isolation_level=None)) as busy:
        busy.execute("BEGIN")
        busy.execute("SELECT name FROM sqlite_schema").fetchall()
        with pytest.raises(SQLiteLeasePairError):
            await asyncio.to_thread(restore_sqlite_snapshot, snapshot, live, previous_lock=previous_lock)
        assert not live.exists()
    assert await asyncio.to_thread(restore_sqlite_snapshot, snapshot, live, previous_lock=previous_lock) == head
    assert hashlib.sha256(snapshot.read_bytes()).digest() == digest
    restored = create_app(settings, start_task_engine=False)
    new_engine = restored.state.task_engine
    try:
        async with restored.state.sessionmaker() as db:
            assert (await db.get(TaskWorker, old_worker)).fenced_at
            assert await db.get(TaskExecutionSlot, execution) is None
            retained = await db.get(TaskWorkRecord, failure)
            assert (retained.worker_key, retained.state) == (old_worker, "failed")
            assert retained.evidence == {"claim": execution}
            task = await db.get(Task, task_id)
            assert (task.status, task.worker_key, task.execution_key) == ("running", old_worker, execution)
        with closing(sqlite3.connect(f"{live}.leases.sqlite3")) as authority:
            assert authority.execute("SELECT count(*) FROM worker_leases").fetchone() == (0,)
        await authorization_service.initialize(restored.state.sessionmaker)
        await restored.state.system_settings.load()
        await new_engine.start()
        assert new_engine.lease.worker_key != old_worker
        assert new_engine.lease.sqlite_pair.generation != previous_generation
        async with restored.state.sessionmaker() as db:
            task = await db.get(Task, task_id)
            assert (task.status, task.execution_key) == ("interrupted", None)
            assert (await db.get(TaskStatistic, task_key)).status == "interrupted"
            assert await db.scalar(select(User.username)) == "restore-test"
    finally:
        await restored.state.system_settings.close()
        await new_engine.stop()
        await restored.state.engine.dispose()


async def test_pre_lease_snapshot_restores_before_upgrade(tmp_path, app):
    original = tmp_path / "old.db"
    snapshot = tmp_path / "old.sqlite3"
    restored = tmp_path / "restored.db"
    await asyncio.to_thread(command.upgrade, _config(original), "0065")
    with closing(sqlite3.connect(original)) as db:
        db.execute("INSERT INTO users(username, password_hash) VALUES ('legacy-user', 'fixture')")
        db.commit()
    await asyncio.to_thread(_backup, original, snapshot)
    previous_lock = tmp_path / "test.db.leases.lock"
    with pytest.raises(SQLiteLeasePairError):
        await asyncio.to_thread(restore_sqlite_snapshot, snapshot, restored, previous_lock=previous_lock)
    await app.state.task_engine.stop()
    # 调度器已退出，但仍提供 HTTP 的应用持有自己的请求租约，不能视为整站下线。
    with pytest.raises(SQLiteLeasePairError):
        await asyncio.to_thread(restore_sqlite_snapshot, snapshot, restored, previous_lock=previous_lock)
    await app.state.task_engine.request_worker.stop()
    assert await asyncio.to_thread(restore_sqlite_snapshot, snapshot, restored, previous_lock=previous_lock) == "0065"
    assert not (tmp_path / "restored.db.leases.sqlite3").exists()
    config = _config(restored)
    head = ScriptDirectory.from_config(config).get_current_head()
    await asyncio.to_thread(command.upgrade, config, "head")
    with closing(sqlite3.connect(restored)) as db:
        assert db.execute("SELECT username FROM users").fetchone() == ("legacy-user",)
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (head,)
        assert db.execute("SELECT count(*) FROM task_workers").fetchone() == (0,)


@pytest.mark.parametrize("revision", ["0066", "0067"])
def test_restore_requires_slot_table_to_match_snapshot_revision(tmp_path, revision):
    source, target = tmp_path / "snapshot.db", tmp_path / "restored.db"
    command.upgrade(_config(source), revision)
    with closing(sqlite3.connect(source)) as db:
        if revision == "0067":
            db.execute("DROP TABLE task_execution_slots")
        else:
            db.execute("CREATE TABLE task_execution_slots(execution_key TEXT)")
        db.commit()
    with pytest.raises(ValueError, match="任务占用表与迁移版本不一致"):
        restore_sqlite_snapshot(source, target)
    assert not target.exists()


@pytest.mark.parametrize("change", [
    "DROP TABLE task_work_records",
    "ALTER TABLE task_work_records DROP COLUMN evidence",
    "ALTER TABLE projects DROP COLUMN deletion_worker_key",
    "ALTER TABLE tasks DROP COLUMN cancel_requested_at",
    "ALTER TABLE agent_changes DROP COLUMN external_worker_key",
    "UPDATE alembic_version SET version_num='0067'",
])
def test_cold_restore_checks_work_identity_shape_before_installing_copy(tmp_path, change):
    source, target = tmp_path / "source.db", tmp_path / "restored.db"
    command.upgrade(_config(source), "0068")
    with closing(sqlite3.connect(source)) as db:
        db.execute(change)
        db.commit()
    with pytest.raises(ValueError, match="跨进程"):
        restore_sqlite_snapshot(source, target)
    assert not target.exists()


@pytest.mark.parametrize("suffix", ["", "-wal", ".leases.lock", ".leases.sqlite3-shm"])
def test_restore_leaves_occupied_destination_untouched(tmp_path, suffix):
    snapshot = tmp_path / "snapshot.sqlite3"
    snapshot.write_bytes(b"placeholder")
    restored = tmp_path / "restored.db"
    existing = tmp_path / (restored.name + suffix)
    existing.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        restore_sqlite_snapshot(snapshot, restored)
    assert existing.read_bytes() == b"keep"
    assert not suffix or not restored.exists()


def test_invalid_snapshot_leaves_no_partial_copy(tmp_path):
    snapshot = tmp_path / "snapshot.sqlite3"
    restored = tmp_path / "restored.db"
    snapshot.write_bytes(b"invalid backup")
    with pytest.raises(sqlite3.DatabaseError):
        restore_sqlite_snapshot(snapshot, restored)
    assert not restored.exists()


def test_failure_during_pairing_preserves_recovery_files_together(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot.sqlite3"
    command.upgrade(_config(snapshot), "head")
    target = tmp_path / "restored.db"
    guard = tmp_path / "restored.db.leases.lock"

    def fail_after_lock(path, **kwargs):
        assert path == target
        guard.touch(exist_ok=False)
        raise OSError("模拟配对时写入失败")

    monkeypatch.setattr("app.services.backups.sqlite_restore.initialize_cold_copy", fail_after_lock)
    with pytest.raises(OSError, match="模拟配对"):
        restore_sqlite_snapshot(snapshot, target)
    assert target.is_file() and guard.is_file()
    with pytest.raises(FileExistsError):
        restore_sqlite_snapshot(snapshot, target)
