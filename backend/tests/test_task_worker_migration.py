"""0066 原位升级、租约降级保护与 SQLite / PostgreSQL 往返。"""

import asyncio
import os
from uuid import uuid4

import pytest
from alembic import command, op
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext import asyncio as async_sqlalchemy
from sqlalchemy.schema import CreateSchema, DropSchema

from app.models import TaskLeaseStore, TaskWorker
from app.services.task_engine.sqlite_lease_pair import SQLiteLeasePairError, open_pair
from tests.test_setting_fields_migration import _config


@pytest.fixture(params=["sqlite", "postgresql"])
async def migration_db(request, tmp_path, monkeypatch):
    cfg = _config(tmp_path / "workers.db")
    if request.param == "sqlite":
        engine = async_sqlalchemy.create_async_engine(cfg.get_main_option("sqlalchemy.url"))
        try:
            yield cfg, engine
        finally:
            await engine.dispose()
        return

    raw = os.getenv("AINOVEL_POSTGRES_TEST_URL")
    if not raw:
        pytest.skip("缺少隔离 PostgreSQL 测试库 AINOVEL_POSTGRES_TEST_URL")
    url = make_url(raw)
    if (url.drivername != "postgresql+asyncpg" or url.database != "ainovel_test"
            or url.host not in {"127.0.0.1", "localhost", "::1"} or url.query):
        raise ValueError("迁移测试须使用本机专用 ainovel_test 数据库")
    schema = f"task_worker_migration_{uuid4().hex}"
    admin = async_sqlalchemy.create_async_engine(url)
    options = {"server_settings": {"search_path": schema}}
    engine = async_sqlalchemy.create_async_engine(url, connect_args=options)
    original = async_sqlalchemy.async_engine_from_config

    def scoped_engine(config, **kwargs):
        assert make_url(config["sqlalchemy.url"]) == url
        return original(config, connect_args=options, **kwargs)

    cfg.set_main_option("sqlalchemy.url", raw.replace("%", "%%"))
    monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", scoped_engine)
    try:
        async with admin.begin() as db:
            await db.execute(CreateSchema(schema))
        try:
            yield cfg, engine
        finally:
            async with admin.begin() as db:
                await db.execute(DropSchema(schema, cascade=True))
    finally:
        await engine.dispose()
        await admin.dispose()


def _check_schema(db):
    inspector = inspect(db)
    table = TaskWorker.__table__
    columns = {c["name"]: c for c in inspector.get_columns(table.name)}
    assert columns.keys() == set(table.columns.keys()) == {
        "worker_key", "started_at", "heartbeat_at", "stopped_at", "fenced_at", "hostname", "pid", "peer_url",
    }
    for column in table.columns:
        assert columns[column.name]["nullable"] == column.nullable
        assert columns[column.name]["type"].compile(db.dialect) == column.type.compile(db.dialect)
    assert inspector.get_pk_constraint(table.name)["constrained_columns"] == ["worker_key"]
    assert inspector.get_foreign_keys(table.name) == []
    task_columns = {c["name"]: c for c in inspector.get_columns("tasks")}
    assert task_columns["worker_key"]["nullable"] and task_columns["recovered_at"]["nullable"]
    assert all("worker_key" not in fk["constrained_columns"] for fk in inspector.get_foreign_keys("tasks"))
    pair_table = TaskLeaseStore.__table__
    assert {c["name"] for c in inspector.get_columns(pair_table.name)} == set(pair_table.columns.keys()) == {
        "id", "generation", "main_path", "main_file", "lock_file", "lease_file",
    }
    assert inspector.get_pk_constraint(pair_table.name)["constrained_columns"] == ["id"]


async def test_0066_upgrade_downgrade_guards_and_roundtrip(migration_db, monkeypatch):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0065")
    async with engine.begin() as db:
        await db.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'worker-test','x')"))
        await db.execute(text("INSERT INTO tasks(id,user_id,kind,status,input) "
                              "VALUES(1,1,'chapter.generate','running','{}')"))
        await db.execute(text("INSERT INTO tasks(id,user_id,kind,status,input,retry_of_id) "
                              "VALUES(2,1,'chapter.generate','queued','{}',1)"))
    add_column = op.add_column

    def fail_after_first_column(table, column):
        add_column(table, column)
        raise RuntimeError("模拟加列后连接中断")

    with monkeypatch.context() as patch:
        patch.setattr(op, "add_column", fail_after_first_column)
        with pytest.raises(RuntimeError, match="模拟加列"):
            await asyncio.to_thread(command.upgrade, cfg, "0066")
    async with engine.connect() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0065"
        assert not await db.run_sync(lambda conn: inspect(conn).has_table("task_workers"))
        assert not await db.run_sync(lambda conn: inspect(conn).has_table("task_lease_store"))
        cols = await db.run_sync(lambda conn: inspect(conn).get_columns("tasks"))
        assert not {"worker_key", "recovered_at"} & {c["name"] for c in cols}
    await asyncio.to_thread(command.upgrade, cfg, "0066")
    key = str(uuid4())
    async with engine.begin() as db:
        await db.run_sync(_check_schema)
        assert (await db.execute(text("SELECT status,worker_key,recovered_at FROM tasks WHERE id=1"))).one() == (
            "running", None, None,
        )
        assert await db.scalar(text("SELECT retry_of_id FROM tasks WHERE id=2")) == 1
        await db.execute(text("INSERT INTO task_workers(worker_key,hostname,pid) VALUES(:key,'test',42)"), {"key": key})

    with pytest.raises(RuntimeError, match="未停止的执行者"):
        await asyncio.to_thread(command.downgrade, cfg, "0065")
    async with engine.begin() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0066"
        assert (await db.execute(text("SELECT worker_key,stopped_at,fenced_at FROM task_workers"))).one() == (
            key, None, None,
        )
        assert await db.scalar(text("SELECT worker_key FROM tasks WHERE id=1")) is None
        await db.execute(
            text("UPDATE task_workers SET stopped_at=CURRENT_TIMESTAMP WHERE worker_key=:key"), {"key": key},
        )
        await db.execute(text("UPDATE tasks SET worker_key=:key WHERE id=1"), {"key": key})
    with pytest.raises(RuntimeError, match="运行中任务"):
        await asyncio.to_thread(command.downgrade, cfg, "0065")
    async with engine.begin() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0066"
        assert (await db.execute(text("SELECT status,worker_key FROM tasks WHERE id=1"))).one() == ("running", key)
        assert await db.scalar(text("SELECT count(*) FROM task_workers WHERE stopped_at IS NOT NULL")) == 1
        await db.execute(text("UPDATE tasks SET status='interrupted' WHERE id=1"))

    if engine.dialect.name == "sqlite":
        async with async_sqlalchemy.AsyncSession(engine) as session:
            pair = await open_pair(session)
        try:
            # 已经拿到有效配对、主库尚未登记的进程也须阻止降级。
            with pytest.raises(SQLiteLeasePairError, match="仍持有"):
                await asyncio.to_thread(command.downgrade, cfg, "0065")
        finally:
            pair.close()
    await asyncio.to_thread(command.downgrade, cfg, "0065")
    async with engine.begin() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0065"
        assert await db.scalar(text("SELECT retry_of_id FROM tasks WHERE id=2")) == 1
        assert not await db.run_sync(lambda conn: inspect(conn).has_table("task_workers"))
        assert not await db.run_sync(lambda conn: inspect(conn).has_table("task_lease_store"))
        cols = await db.run_sync(lambda conn: inspect(conn).get_columns("tasks"))
        assert not {"worker_key", "recovered_at"} & {c["name"] for c in cols}
        await db.execute(text("DELETE FROM tasks"))
        await db.execute(text("DELETE FROM users WHERE id=1"))
    await asyncio.to_thread(command.upgrade, cfg, "0066")
    if engine.dialect.name == "sqlite":
        async with async_sqlalchemy.AsyncSession(engine) as session:
            reopened = await open_pair(session)
        try:
            assert reopened.generation == pair.generation
        finally:
            reopened.close()
    await asyncio.to_thread(command.downgrade, cfg, "base")
    async with engine.connect() as db:
        assert await db.run_sync(lambda conn: set(inspect(conn).get_table_names())) == {"alembic_version"}
