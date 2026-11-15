"""0047 在隔离 PostgreSQL schema 中原位回填并拒绝有损降级。"""

import asyncio
import os
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext import asyncio as async_sqlalchemy

from tests.test_setting_fields_migration import _config


@pytest.mark.parametrize("seeded", [False, True])
def test_0047_postgresql_identity_migration_and_lossless_downgrade(tmp_path, monkeypatch, seeded):
    dedicated = os.environ.get("AINOVEL_AGENT_IDENTITY_POSTGRES_TEST_URL")
    url = dedicated or os.environ.get("AINOVEL_COLLAB_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("设置 AINOVEL_AGENT_IDENTITY_POSTGRES_TEST_URL 或 AINOVEL_COLLAB_POSTGRES_TEST_URL")
    database = "agent_identity" if dedicated else "collab_test"
    parsed = make_url(url)
    if (parsed.drivername, parsed.host, parsed.database, parsed.username) != (
        "postgresql+asyncpg", "127.0.0.1", database, "collab_test",
    ):
        raise ValueError("Agent 身份迁移测试仅允许本机指定隔离数据库")
    schema = f"agent_identity_{uuid4().hex}"
    original = async_sqlalchemy.async_engine_from_config

    def isolated_factory(*args, **kwargs):
        return original(*args, **kwargs, connect_args={"server_settings": {"search_path": schema}})

    async def run(action, *, isolated=True):
        engine = async_sqlalchemy.create_async_engine(url, connect_args={"server_settings": {
            "search_path": schema if isolated else "public",
        }})
        try:
            async with engine.begin() as conn:
                assert await conn.scalar(text("SELECT current_database()")) == database
                return await action(conn)
        finally:
            await engine.dispose()

    async def create_schema(conn):
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    async def drop_schema(conn):
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    async def seed(conn):
        await conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash')"))
        await conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                                "VALUES(1,1,'旧书','','','','writing')"))
        await conn.execute(text("INSERT INTO agent_conversations(id,user_id,project_id,title) VALUES(1,1,1,'旧会话')"))
        await conn.execute(text("INSERT INTO tasks(id,user_id,project_id,kind,status,input,statistics_key) "
                                "VALUES(1,1,1,'agent.turn','paused','{}',:key)"), {"key": task_key})
        await conn.execute(text("INSERT INTO agent_messages(id,conversation_id,turn_task_id,role,content) "
                                "VALUES(5,1,1,'assistant','旧消息')"))
        await conn.execute(text("INSERT INTO agent_changes(id,conversation_id,project_id,turn_task_id,tool_call_id,"
                                "tool_name,arguments,kind,preview,status,seq) "
                                "VALUES(7,1,1,1,'call-a','edit','{}','small','{}','proposed',1),"
                                "(19,1,1,1,'call-b','edit','{}','small','{}','executed',2)"))
        await conn.execute(text("CREATE TABLE agent_change_references (id INTEGER PRIMARY KEY, "
                                "change_id INTEGER REFERENCES agent_changes(id) ON DELETE CASCADE)"))
        await conn.execute(text("INSERT INTO agent_change_references(id,change_id) VALUES(1,7)"))
        rows = (await conn.execute(text("SELECT * FROM agent_changes ORDER BY id"))).mappings().all()
        fks = (await conn.execute(text(
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='agent_changes'::regclass AND contype='f' ORDER BY conname"
        ))).all()
        return [dict(row) for row in rows], list(fks)

    async def check_upgrade(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0047"
        rows = (await conn.execute(text("SELECT * FROM agent_changes ORDER BY id"))).mappings().all()
        if not seeded:
            assert not rows
            return []
        old_rows, old_fks = before
        assert [{name: row[name] for name in old_rows[0]} for row in rows] == old_rows
        keys = [row["change_key"] for row in rows]
        assert len(set(keys)) == 2
        assert all(str(UUID(key)) == key and UUID(key).version == 4 for key in keys)
        assert all(row["turn_task_key"] is None for row in rows)
        assert await conn.scalar(text("SELECT turn_task_key FROM agent_messages WHERE id=5")) is None
        fks = (await conn.execute(text(
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='agent_changes'::regclass AND contype='f' ORDER BY conname"
        ))).all()
        assert list(fks) == old_fks
        assert await conn.scalar(text("SELECT change_id FROM agent_change_references WHERE id=1")) == 7
        return keys

    async def set_change_key(conn, key):
        await conn.execute(text("UPDATE agent_changes SET change_key=:key WHERE id=7"), {"key": key})

    async def missing_change_key(conn):
        await conn.execute(text("INSERT INTO agent_changes(conversation_id,project_id,turn_task_id,tool_call_id,"
                                "tool_name,arguments,kind,preview) VALUES(1,1,1,'missing','edit','{}','small','{}')"))

    async def invalid_task_key(conn, table, row_id, value):
        await conn.execute(text(f"UPDATE {table} SET turn_task_key=:key WHERE id=:id"), {"key": value, "id": row_id})

    task_key = str(uuid4())

    async def retain_task_key(conn):
        for table, row_id in (("agent_messages", 5), ("agent_changes", 7)):
            await conn.execute(text(f"UPDATE {table} SET turn_task_key=:key WHERE id=:id"),
                               {"key": task_key, "id": row_id})
        await conn.execute(text("DELETE FROM tasks WHERE id=1"))
        for table, row_id in (("agent_messages", 5), ("agent_changes", 7)):
            row = (await conn.execute(text(f"SELECT turn_task_id,turn_task_key FROM {table} WHERE id=:id"),
                                      {"id": row_id})).one()
            assert tuple(row) == (None, task_key)
        await conn.execute(text("INSERT INTO tasks(id,user_id,project_id,kind,status,input,statistics_key) "
                                "VALUES(1,1,1,'agent.turn','paused','{}',:key)"), {"key": str(uuid4())})
        for table, row_id in (("agent_messages", 5), ("agent_changes", 7)):
            row = (await conn.execute(text(f"SELECT turn_task_id,turn_task_key FROM {table} WHERE id=:id"),
                                      {"id": row_id})).one()
            assert tuple(row) == (None, task_key)

    async def clear_changes(conn):
        await conn.execute(text("DELETE FROM agent_changes"))
        assert await conn.scalar(text("SELECT count(*) FROM agent_change_references")) == 0

    async def clear_message_key(conn):
        assert await conn.scalar(text("SELECT turn_task_key FROM agent_messages WHERE id=5")) == task_key
        await conn.execute(text("UPDATE agent_messages SET turn_task_key=NULL WHERE id=5"))

    async def check_downgrade(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0046"
        columns = await conn.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_schema=:schema "
                                         "AND table_name IN ('agent_messages','agent_changes') "
                                         "AND column_name IN ('turn_task_key','change_key')"), {"schema": schema})
        assert columns == 0
        if seeded:
            assert await conn.scalar(text("SELECT content FROM agent_messages WHERE id=5")) == "旧消息"

    config = _config(tmp_path / "unused.db")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    created = False
    try:
        asyncio.run(run(create_schema, isolated=False))
        created = True
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", isolated_factory)
        command.upgrade(config, "0046")
        before = asyncio.run(run(seed)) if seeded else None
        command.upgrade(config, "0047")
        keys = asyncio.run(run(check_upgrade))
        if seeded:
            for invalid in (None, "", "short", str(uuid4()).upper(), keys[1]):
                with pytest.raises(IntegrityError):
                    asyncio.run(run(lambda conn, value=invalid: set_change_key(conn, value)))
            with pytest.raises(IntegrityError):
                asyncio.run(run(missing_change_key))
            for table, row_id in (("agent_messages", 5), ("agent_changes", 7)):
                for invalid in ("", "short", str(uuid4()).upper()):
                    with pytest.raises(IntegrityError):
                        asyncio.run(run(lambda conn, t=table, row=row_id, value=invalid:
                                        invalid_task_key(conn, t, row, value)))
            asyncio.run(run(retain_task_key))
            with pytest.raises(RuntimeError, match="降级"):
                command.downgrade(config, "0046")
            asyncio.run(run(clear_changes))
            with pytest.raises(RuntimeError, match="降级"):
                command.downgrade(config, "0046")
            asyncio.run(run(clear_message_key))
        command.downgrade(config, "0046")
        asyncio.run(run(check_downgrade))
    finally:
        if created:
            asyncio.run(run(drop_schema, isolated=False))
