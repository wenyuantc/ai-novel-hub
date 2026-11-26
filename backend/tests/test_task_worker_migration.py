"""任务引擎迁移，包含 SQLite / PostgreSQL 原位升级、降级保护及往返。"""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command, op
from sqlalchemy import MetaData, Table, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext import asyncio as async_sqlalchemy
from sqlalchemy.schema import CreateSchema, DropSchema

from app.models import (
    AgentChange,
    McpPlugin,
    Project,
    TaskExecutionSlot,
    TaskLeaseStore,
    TaskWorker,
    TaskWorkRecord,
)
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


def _check_slot_schema(db):
    inspector = inspect(db)
    table = TaskExecutionSlot.__table__
    columns = {c["name"]: c for c in inspector.get_columns(table.name)}
    assert columns.keys() == set(table.columns.keys()) == {
        "execution_key", "worker_key", "task_id", "statistics_key", "queued_at", "user_id", "serial_key",
    }
    for column in table.columns:
        assert columns[column.name]["nullable"] == column.nullable == (column.name == "serial_key")
        assert columns[column.name]["type"].compile(db.dialect) == column.type.compile(db.dialect)
    assert inspector.get_pk_constraint(table.name)["constrained_columns"] == ["execution_key"]
    assert [c["column_names"] for c in inspector.get_unique_constraints(table.name)] == [["task_id"]]
    indexes = {tuple(i["column_names"]) for i in inspector.get_indexes(table.name)
               if not i.get("duplicates_constraint")}
    assert indexes == {("worker_key",), ("user_id",), ("serial_key",)}
    assert inspector.get_foreign_keys(table.name) == []


def _check_plugin_constraints(db):
    inspector = inspect(db)
    table = McpPlugin.__tablename__
    assert inspector.get_pk_constraint(table)["constrained_columns"] == ["id"]
    assert [(item["name"], item["column_names"]) for item in inspector.get_unique_constraints(table)] == [
        ("uq_mcp_plugins_user_id", ["user_id", "slug"]),
    ]
    assert [(item["name"], item["column_names"]) for item in inspector.get_indexes(table)
            if not item.get("duplicates_constraint")] == [("ix_mcp_plugins_user_id", ["user_id"])]
    owner, = inspector.get_foreign_keys(table)
    assert (owner["constrained_columns"], owner["referred_table"], owner["options"]["ondelete"]) == (
        ["user_id"], "users", "CASCADE",
    )


async def test_0067_preserves_legacy_task_and_guards_execution_slots(migration_db, monkeypatch):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0066")
    async with engine.begin() as db:
        await db.execute(text("INSERT INTO users(id, username, password_hash) VALUES(1,'slot-test','x')"))
        await db.execute(text("INSERT INTO tasks(id,user_id,kind,status,input) "
                              "VALUES(1,1,'chapter.generate','running','{}')"))
        plugins = await db.run_sync(lambda conn: Table("mcp_plugins", MetaData(), autoload_with=conn))
        plugin_row = dict(user_id=1, slug="existing", name="旧插件", transport="sse", url="https://old.example/mcp",
                          headers_encrypted="opaque-existing-ciphertext", enabled=False,
                          tools=[{"name": "example", "input_schema": {"type": "object"}}],
                          tool_prefs={"example": {"enabled": False}}, last_error="原错误",
                          tools_refreshed_at=datetime(2025, 1, 1, tzinfo=UTC),
                          last_tested_at=datetime(2025, 1, 2, tzinfo=UTC))
        original_id = await db.scalar(plugins.insert().values(**plugin_row).returning(plugins.c.id))
        await db.execute(Project.__table__.insert().values(id=1, user_id=1, title="旧作品"))
        conversations = await db.run_sync(lambda conn: Table("agent_conversations", MetaData(), autoload_with=conn))
        await db.execute(conversations.insert().values(id=1, user_id=1, project_id=1, mcp_plugin_ids=[original_id]))
        read_selection = select(conversations.c.mcp_plugin_ids).where(conversations.c.id == 1)
        read_plugin = select(plugins).where(plugins.c.id == original_id)
        original_plugin = (await db.execute(read_plugin)).mappings().one()
        plugin_ddl = text("SELECT sql FROM sqlite_master WHERE type='table' AND name='mcp_plugins'")

    create_table = op.create_table

    def fail_after_create(*args, **kwargs):
        create_table(*args, **kwargs)
        raise RuntimeError("模拟名额表创建中断")

    with monkeypatch.context() as patch:
        patch.setattr(op, "create_table", fail_after_create)
        with pytest.raises(RuntimeError, match="模拟名额表创建中断"):
            await asyncio.to_thread(command.upgrade, cfg, "0067")
    async with engine.connect() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0066"
        assert not await db.run_sync(lambda conn: inspect(conn).has_table("task_execution_slots"))
        assert (await db.execute(read_plugin)).mappings().one() == original_plugin
        if engine.dialect.name == "sqlite":
            assert "AUTOINCREMENT" not in await db.scalar(plugin_ddl)

    await asyncio.to_thread(command.upgrade, cfg, "0067")
    key, worker = str(uuid4()), str(uuid4())
    async with engine.begin() as db:
        await db.run_sync(_check_slot_schema)
        await db.run_sync(_check_plugin_constraints)
        assert (await db.execute(read_plugin)).mappings().one() == original_plugin
        inserted = plugins.insert().values(**{**plugin_row, "slug": "temporary"}).returning(plugins.c.id)
        first_id = await db.scalar(inserted)
        assert await db.scalar(read_selection) == [original_id]
        await db.execute(plugins.delete().where(plugins.c.id == first_id))
        assert await db.scalar(inserted) > first_id
        if engine.dialect.name == "sqlite":
            assert "AUTOINCREMENT" in await db.scalar(plugin_ddl)
        assert await db.scalar(text("SELECT count(*) FROM task_execution_slots")) == 0
        assert (await db.execute(text("SELECT status,worker_key FROM tasks WHERE id=1"))).one() == ("running", None)
        await db.execute(text("INSERT INTO task_execution_slots(execution_key,worker_key,task_id,statistics_key,"
                              "queued_at,user_id) VALUES(:key,:worker,1,:identity,CURRENT_TIMESTAMP,1)"),
                         {"key": key, "worker": worker, "identity": str(uuid4())})
    for existing_worker in (False, True):
        if existing_worker:
            async with engine.begin() as db:
                await db.execute(text("INSERT INTO task_workers(worker_key,hostname,pid) VALUES(:w,'active',1)"),
                                 {"w": worker})
        with pytest.raises(RuntimeError, match="拒绝降级"):
            await asyncio.to_thread(command.downgrade, cfg, "0066")
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0067"
            assert await db.scalar(text("SELECT execution_key FROM task_execution_slots")) == key

    async with engine.begin() as db:
        await db.execute(text("UPDATE task_workers SET stopped_at=CURRENT_TIMESTAMP WHERE worker_key=:w"),
                         {"w": worker})
    await asyncio.to_thread(command.downgrade, cfg, "0066")
    async with engine.connect() as db:
        assert not await db.run_sync(lambda conn: inspect(conn).has_table("task_execution_slots"))
        assert await db.scalar(text("SELECT status FROM tasks WHERE id=1")) == "running"
        await db.run_sync(_check_plugin_constraints)
        assert (await db.execute(read_plugin)).mappings().one() == original_plugin
        assert await db.scalar(read_selection) == [original_id]
        if engine.dialect.name == "sqlite":
            assert "AUTOINCREMENT" not in await db.scalar(plugin_ddl)
    await asyncio.to_thread(command.upgrade, cfg, "0067")
    async with engine.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM task_execution_slots")) == 0
    if engine.dialect.name == "sqlite":
        async with async_sqlalchemy.AsyncSession(engine) as db:
            pair = await open_pair(db)
        try:
            with pytest.raises(SQLiteLeasePairError, match="仍持有"):
                await asyncio.to_thread(command.downgrade, cfg, "0065")
        finally:
            pair.close()
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0067"
    await asyncio.to_thread(command.downgrade, cfg, "0065")


def _check_work_schema(db):
    inspector = inspect(db)
    table = TaskWorkRecord.__table__
    columns = {c["name"]: c for c in inspector.get_columns(table.name)}
    assert columns.keys() == set(table.columns.keys()) == {
        "work_key", "kind", "project_id", "worker_key", "task_id", "task_key", "queued_at",
        "execution_key", "operation_id", "state", "evidence",
    }
    for column in table.columns:
        assert columns[column.name]["nullable"] == column.nullable
        assert columns[column.name]["type"].compile(db.dialect) == column.type.compile(db.dialect)
    assert inspector.get_pk_constraint(table.name)["constrained_columns"] == ["work_key"]
    assert inspector.get_foreign_keys(table.name) == []
    assert {tuple(i["column_names"]) for i in inspector.get_indexes(table.name)} == {("project_id",), ("worker_key",)}
    for name, col in (("projects", "deletion_worker_key"), ("tasks", "cancel_requested_at"),
                      ("agent_changes", "external_worker_key")):
        assert next(c for c in inspector.get_columns(name) if c["name"] == col)["nullable"]


async def test_0068_preserves_legacy_claim_and_guards_work(migration_db, monkeypatch):
    cfg, engine = migration_db
    await asyncio.to_thread(command.upgrade, cfg, "0067")
    worker, execution, task_key, change_key, token = [str(uuid4()) for _ in range(5)]
    params = {"worker": worker, "execution": execution, "task_key": task_key, "token": token}
    legacy_result = {"_external_claim": str(uuid4()), "variant": "legacy"}
    projects = Project.__table__
    changes = AgentChange.__table__
    async with engine.begin() as db:
        await db.execute(text("INSERT INTO users(id, username, password_hash) VALUES(1, 'old-author', 'x')"))
        await db.execute(projects.insert().values(id=1, user_id=1, title="原作品", deletion_token=token,
                                                  deletion_started_at=datetime.now(UTC)))
        if db.dialect.name == "sqlite":
            await db.execute(projects.insert().values(id=20, user_id=1, title="曾存在的作品"))
            await db.execute(text("DELETE FROM projects WHERE id=20"))
        await db.execute(text("INSERT INTO agent_conversations (id, project_id, user_id) VALUES (1,1,1)"))
        await db.execute(changes.insert().values(id=1, project_id=1, conversation_id=1, tool_call_id="a",
                                                 tool_name="t", kind="external", status="executing",
                                                 change_key=change_key, result=legacy_result))
        await db.execute(text("INSERT INTO task_workers(worker_key,hostname,pid) VALUES(:worker,'old',1)"), params)
        await db.execute(text("INSERT INTO tasks(id,user_id,project_id,kind,status,input,execution_key,worker_key,"
                              "statistics_key,queued_at) VALUES(1,1,1,'outline.continue','running','{}',"
                              ":execution,:worker,:task_key,CURRENT_TIMESTAMP)"), params)
        await db.execute(text("INSERT INTO task_execution_slots(execution_key,worker_key,task_id,statistics_key,"
                              "queued_at,user_id) SELECT execution_key,worker_key,id,statistics_key,queued_at,user_id "
                              "FROM tasks WHERE id=1"))
    with pytest.raises(RuntimeError, match="未停止的执行者"):
        await asyncio.to_thread(command.upgrade, cfg, "0068")
    async with engine.begin() as db:
        await db.execute(text("UPDATE task_workers SET fenced_at=CURRENT_TIMESTAMP"))
        await db.execute(text("UPDATE task_execution_slots SET task_id=2"))
    with pytest.raises(RuntimeError, match="归属无法确认"):
        await asyncio.to_thread(command.upgrade, cfg, "0068")
    async with engine.begin() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0067"
        assert await db.scalar(text("SELECT deletion_token FROM projects")) == token
        assert not await db.run_sync(lambda c: inspect(c).has_table("task_work_records"))
        await db.execute(text("UPDATE task_execution_slots SET task_id=1"))

    create_index = op.create_index

    def interrupt(*args, **kwargs):
        create_index(*args, **kwargs)
        raise RuntimeError("原位 DDL 写入后中断")

    with monkeypatch.context() as patch:
        patch.setattr(op, "create_index", interrupt)
        with pytest.raises(RuntimeError, match="DDL"):
            await asyncio.to_thread(command.upgrade, cfg, "0068")
    async with engine.begin() as db:
        cols = await db.run_sync(lambda c: inspect(c).get_columns("projects"))
        assert "deletion_worker_key" not in {c["name"] for c in cols}
        assert await db.scalar(text("SELECT deletion_token FROM projects")) == token
    await asyncio.to_thread(command.upgrade, cfg, "0068")
    async with engine.begin() as db:
        await db.run_sync(_check_work_schema)
        intent = await db.execute(text("SELECT deletion_token,deletion_started_at,deletion_worker_key FROM projects"))
        assert intent.one() == (None, None, None)
        if db.dialect.name == "sqlite":
            assert await db.scalar(text("SELECT seq FROM sqlite_sequence WHERE name='projects'")) == 20
            assert not (await db.execute(text("PRAGMA foreign_key_check"))).all()
        legacy = (await db.execute(select(changes.c.result, changes.c.external_worker_key))).one()
        assert legacy == (legacy_result, None)
        assert await db.scalar(text("SELECT execution_key FROM task_execution_slots")) == execution
        assert await db.scalar(text("SELECT count(*) FROM task_work_records")) == 0

    # 每种新事实独立阻止降级，阻止后原版本、列和值均保持可读。
    cases = [
        ("task_execution_slots", None, "DELETE FROM task_execution_slots"),
        ("task_work_records",
         "INSERT INTO task_work_records(work_key,kind,state,project_id) VALUES(:execution,'cleanup','failed',1)",
         "DELETE FROM task_work_records"),
        ("tasks", None, "UPDATE tasks SET status='interrupted',worker_key=NULL,execution_key=NULL"),
        ("tasks", "UPDATE tasks SET cancel_requested_at=CURRENT_TIMESTAMP",
         "UPDATE tasks SET cancel_requested_at=NULL"),
        ("projects", "UPDATE projects SET deletion_started_at=CURRENT_TIMESTAMP,deletion_token=:token,"
         "deletion_worker_key=:worker", "UPDATE projects SET deletion_started_at=NULL,deletion_token=NULL,"
         "deletion_worker_key=NULL"),
        ("agent_changes", None, """UPDATE agent_changes SET status='failed',
         result='{"error":"unknown","_external_outcome_unknown": true}'"""),
        ("agent_changes", None, """UPDATE agent_changes SET status='failed',result='{"error":"not_sent"}'"""),
        ("agent_changes", "UPDATE agent_changes SET external_worker_key=:worker",
         "UPDATE agent_changes SET external_worker_key=NULL"),
    ]
    for table, insert, remove in cases:
        if insert:
            async with engine.begin() as db:
                await db.execute(text(insert), params)
        with pytest.raises(RuntimeError, match=table):
            await asyncio.to_thread(command.downgrade, cfg, "0067")
        async with engine.begin() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "0068"
            await db.execute(text(remove))
    await asyncio.to_thread(command.downgrade, cfg, "0067")
    async with engine.begin() as db:
        assert not await db.run_sync(lambda c: inspect(c).has_table("task_work_records"))
        assert (await db.execute(text("SELECT title,user_id FROM projects"))).one() == ("原作品", 1)
        assert await db.scalar(select(changes.c.result)) == {"error": "not_sent"}
        assert (await db.execute(text("SELECT statistics_key,status FROM tasks"))).one() == (task_key, "interrupted")
    await asyncio.to_thread(command.upgrade, cfg, "0068")
    async with engine.begin() as db:
        await db.run_sync(_check_work_schema)
