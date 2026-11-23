"""0068 的离线、并发和 SQLite 身份边界。"""

import asyncio
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.main import create_app
from app.services.backups.sqlite_restore import restore_sqlite_snapshot
from app.services.task_engine import sqlite_worker_leases
from app.services.task_engine.sqlite_lease_pair import SQLiteLeasePairError, open_pair
from tests import test_task_worker_migration
from tests.helpers import make_settings
from tests.test_backup_sqlite_restore import _backup
from tests.test_setting_fields_migration import _config

migration_db = test_task_worker_migration.migration_db


def test_0068_cold_copy_releases_only_confirmed_execution_work(tmp_path):
    source, snapshot, restored = (tmp_path / name for name in ("live.db", "snapshot.db", "restored.db"))
    command.upgrade(_config(source), "head")
    credentials = {"username": "cold-owner", "password": "local-test-credential"}
    old_app = create_app(make_settings(tmp_path, database_url=f"sqlite+aiosqlite:///{source}"), start_task_engine=False)
    with TestClient(old_app) as client:
        assert client.post("/api/v1/auth/register", json=credentials).status_code == 201
        created = client.post("/api/v1/projects", json={"title": "冷恢复作品", "genre": "历史", "premise": "测试"})
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]

    owner, unknown = str(uuid4()), str(uuid4())
    rows = [
        (str(uuid4()), "execution", owner, "active"),
        (str(uuid4()), "request_cleanup", owner, "active"),
        (str(uuid4()), "abort_failure", owner, "failed"),
        (str(uuid4()), "execution", unknown, "active"),
    ]
    with closing(sqlite3.connect(source)) as db:
        db.execute("INSERT INTO task_workers(worker_key,hostname,pid) VALUES(?,'cold-test',1)", (owner,))
        db.executemany("INSERT INTO task_work_records(work_key,kind,worker_key,state,project_id) VALUES (?,?,?,?,?)",
                       [(*row, project_id) for row in rows])
        db.commit()
    _backup(source, snapshot)
    with closing(sqlite3.connect(snapshot)) as db:
        assert db.execute("SELECT count(*) FROM task_work_records").fetchone() == (4,)

    assert restore_sqlite_snapshot(snapshot, restored) == "0068"
    with closing(sqlite3.connect(restored)) as db:
        assert db.execute("SELECT fenced_at FROM task_workers WHERE worker_key=?", (owner,)).fetchone()[0]
        assert set(db.execute("SELECT work_key,kind,worker_key,state FROM task_work_records")) == set(rows[1:])
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    with closing(sqlite3.connect(snapshot)) as db:
        assert db.execute("SELECT fenced_at FROM task_workers WHERE worker_key=?", (owner,)).fetchone() == (None,)
        assert set(db.execute("SELECT work_key,kind,worker_key,state FROM task_work_records")) == set(rows)

    new_settings = make_settings(tmp_path, database_url=f"sqlite+aiosqlite:///{restored}")
    new_app = create_app(new_settings, start_task_engine=False)
    with TestClient(new_app) as client:
        assert client.post("/api/v1/auth/login", json=credentials).status_code == 200
        response = client.delete(f"/api/v1/projects/{project_id}")
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "project_tasks_not_stopped"
        assert client.get(f"/api/v1/projects/{project_id}").status_code == 200


def test_0068_roundtrip_preserves_project_high_water_and_children(tmp_path):
    path = tmp_path / "old.db"
    config = _config(path)
    command.upgrade(config, "0067")
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'test','test')")
        for project_id in (1, 50):
            db.execute("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                       "VALUES(?,1,'已保存的作品','','','','writing')", (project_id,))
        db.execute("INSERT INTO tasks(id,user_id,project_id,kind,status,input) "
                   "VALUES(9,1,1,'book.import','queued','{}')")
        db.execute("DELETE FROM projects WHERE id=50")
        db.commit()
    for revision, migrate in (("0068", command.upgrade), ("0067", command.downgrade)):
        migrate(config, revision)
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='projects'").fetchone() == (50,)
            assert db.execute("SELECT project_id FROM tasks WHERE id=9").fetchone() == (1,)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    with closing(sqlite3.connect(path)) as db:
        new = db.execute("INSERT INTO projects(user_id,title,genre,premise,style,status) "
                         "VALUES(1,'新作品','','','','writing')")
        assert new.lastrowid == 51


async def test_0068_downgrade_observes_concurrent_committed_work(migration_db):
    cfg, db_engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "head")
    dialect = db_engine.dialect.name
    loop = asyncio.get_running_loop()
    entering = asyncio.Event()
    prefix = "UPDATE alembic_version SET version_num=version_num" if dialect == "sqlite" else "LOCK TABLE task_workers"

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(prefix):
            loop.call_soon_threadsafe(entering.set)

    evidence = str(uuid4())
    async with db_engine.connect() as writer:
        await writer.execute(text("INSERT INTO task_work_records(work_key,kind,state) "
                                  "VALUES(:k,'abort_failure','failed')"), {"k": evidence})
        event.listen(Engine, "before_cursor_execute", before_cursor_execute)
        migration = asyncio.create_task(asyncio.to_thread(command.downgrade, cfg, "0067"))
        try:
            await asyncio.wait_for(entering.wait(), 10)
            assert not migration.done()
            if dialect == "postgresql":
                writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
                async with db_engine.connect() as observer, asyncio.timeout(5):
                    while not await observer.scalar(text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE :pid=ANY(pg_blocking_pids(pid)))"
                    ), {"pid": writer_pid}):
                        await observer.rollback()
                        await asyncio.sleep(0.01)
            await writer.commit()
            with pytest.raises(RuntimeError, match="task_work_records"):
                await migration
        finally:
            event.remove(Engine, "before_cursor_execute", before_cursor_execute)
            await writer.rollback()
            await asyncio.gather(migration, return_exceptions=True)
        assert await writer.scalar(text("SELECT version_num FROM alembic_version")) == "0068"
        stored = (await writer.execute(text("SELECT work_key,state FROM task_work_records"))).one()
        assert stored == (evidence, "failed")


async def test_0068_named_memory_migrations_require_an_offline_pair(tmp_path):
    uri = f"file:c3-migration-{uuid4().hex}?mode=memory&cache=shared"
    url = f"sqlite+aiosqlite:///{uri}&uri=true"
    config = _config(tmp_path / "unused.db")
    config.set_main_option("sqlalchemy.url", url)
    engine = create_async_engine(url, poolclass=StaticPool)
    sessions = async_sessionmaker(engine)
    with closing(sqlite3.connect(uri, uri=True)) as keeper:
        try:
            await asyncio.to_thread(command.upgrade, config, "0067")
            for previous, target, migrate in (("0067", "0068", command.upgrade), ("0068", "0067", command.downgrade)):
                async with sessions() as db:
                    pair = await open_pair(db)
                key = str(uuid4())
                try:
                    with pytest.raises(SQLiteLeasePairError, match="仍持有租约锁"):
                        await asyncio.to_thread(migrate, config, target)
                    await sqlite_worker_leases.register(pair, key)
                finally:
                    pair.close()
                # 模拟启动者已登记旁库、主库仍无 worker，且原进程此时崩溃。
                with pytest.raises(SQLiteLeasePairError, match="未停止的执行者"):
                    await asyncio.to_thread(migrate, config, target)
                assert keeper.execute("SELECT version_num FROM alembic_version").fetchone() == (previous,)
                async with sessions() as db:
                    pair = await open_pair(db)
                try:
                    await sqlite_worker_leases.stop(pair, key)
                finally:
                    pair.close()
                await asyncio.to_thread(migrate, config, target)
                assert keeper.execute("SELECT version_num FROM alembic_version").fetchone() == (target,)
        finally:
            await engine.dispose()
