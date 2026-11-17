"""0049 使用本机隔离 PostgreSQL schema 验证迁移、真实备份与跨连接竞争。"""

import asyncio
import os
import shutil
import subprocess
import threading
import time
from uuid import UUID, uuid1, uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext import asyncio as async_sqlalchemy

from app.services.backups.database import snapshot_database
from tests.schema_helpers import assert_sqlite_chapter_plan_fk_intact, create_isolated_schema
from tests.test_collaboration_access_postgresql import _wait_for_blocker
from tests.test_setting_fields_migration import _config


@pytest.fixture
def pg_instance_db(tmp_path, monkeypatch):
    dedicated = os.environ.get("AINOVEL_AI_CONFIG_INSTANCE_POSTGRES_TEST_URL")
    url = dedicated or os.environ.get("AINOVEL_COLLAB_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("设置 AINOVEL_AI_CONFIG_INSTANCE_POSTGRES_TEST_URL 或 AINOVEL_COLLAB_POSTGRES_TEST_URL")
    database = "ai_config_instance" if dedicated else "collab_test"
    parsed = make_url(url)
    if (parsed.drivername, parsed.host, parsed.database, parsed.username) != (
        "postgresql+asyncpg", "127.0.0.1", database, "collab_test",
    ):
        raise ValueError("模型配置身份迁移仅允许本机指定的隔离测试库")
    schema = f"ai_config_instance_{uuid4().hex}"
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

    config = _config(tmp_path / "unused.db")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    asyncio.run(run(create_schema, isolated=False))
    monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", isolated_factory)
    try:
        yield config, url, parsed, schema, run, create_schema, drop_schema
    finally:
        asyncio.run(run(drop_schema, isolated=False))


async def _seed(conn):
    await conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash')"))
    await conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                            "VALUES(1,1,'旧书','','','','writing')"))
    await conn.execute(text("INSERT INTO ai_configs(id,user_id,name,provider,api_key_enc,model,is_default) "
                            "VALUES(1,1,'原配置','openai_compat','密文','model',false),"
                            "(2,1,'同内容配置','openai_compat','密文','model',false)"))
    await conn.execute(text("CREATE TABLE config_references (id INTEGER PRIMARY KEY, "
                            "config_id INTEGER REFERENCES ai_configs(id) ON DELETE CASCADE)"))
    await conn.execute(text("INSERT INTO config_references(id,config_id) VALUES(1,1)"))
    await conn.execute(text("INSERT INTO tasks(id,user_id,project_id,kind,status,input) "
                            "VALUES(1,1,1,'chapter.generate','succeeded','{}')"))


def _canonical(value):
    assert str(UUID(value)) == value and UUID(value).version == 4


@pytest.mark.parametrize("seeded", [False, True])
def test_0049_postgresql_migrate_constraints_and_safe_downgrade(pg_instance_db, seeded):
    config, _, _, schema, run, _, _ = pg_instance_db
    command.upgrade(config, "0048")
    if seeded:
        asyncio.run(run(_seed))
    command.upgrade(config, "0049")

    async def check(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0049"
        assert await conn.scalar(text("SELECT is_nullable FROM information_schema.columns "
                                      "WHERE table_schema=:schema AND table_name='ai_configs' "
                                      "AND column_name='instance_key'"), {"schema": schema}) == "NO"
        keys = list((await conn.execute(text("SELECT instance_key FROM ai_configs ORDER BY id"))).scalars())
        if seeded:
            assert len(keys) == len(set(keys)) == 2
            assert await conn.scalar(text("SELECT config_id FROM config_references WHERE id=1")) == 1
            for key in keys:
                _canonical(key)
        return keys

    keys = asyncio.run(run(check))
    if seeded:
        async def change_key(conn, key):
            await conn.execute(text("UPDATE ai_configs SET instance_key=:key WHERE id=1"), {"key": key})

        for invalid in (None, "", "bad", str(uuid1()), str(uuid4()).upper(), str(uuid4()), keys[1]):
            with pytest.raises(IntegrityError):
                asyncio.run(run(lambda conn, value=invalid: change_key(conn, value)))

        async def insert_direct(conn):
            await conn.execute(text("INSERT INTO ai_configs(id,user_id,name,provider,api_key_enc,model,is_default) "
                                    "VALUES(3,1,'直接一','openai_compat','密文','model',false),"
                                    "(4,1,'直接二','openai_compat','密文','model',false)"))
            saved = list((await conn.execute(text("SELECT instance_key FROM ai_configs ORDER BY id"))).scalars())
            assert len(set(saved)) == len(saved) == 4
            for key in saved:
                _canonical(key)
        asyncio.run(run(insert_direct))
    command.downgrade(config, "0048")

    async def check_downgrade(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0048"
        assert await conn.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_schema=:schema "
                                      "AND table_name='ai_configs' AND column_name='instance_key'"),
                                 {"schema": schema}) == 0
        if seeded:
            assert await conn.scalar(text("SELECT config_id FROM config_references WHERE id=1")) == 1
            assert await conn.scalar(text("SELECT count(*) FROM ai_configs")) == 4
    asyncio.run(run(check_downgrade))


def test_0049_postgresql_create_all_matches_direct_sql_and_immutability(pg_instance_db):
    _, _, _, _, run, _, _ = pg_instance_db

    async def create_and_test(conn):
        await conn.run_sync(create_isolated_schema)
        await conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash')"))
        await conn.execute(text("INSERT INTO ai_configs(user_id,name,provider,api_key_enc,model,is_default) "
                                "VALUES(1,'直接一','openai_compat','密文','model',false),"
                                "(1,'直接二','openai_compat','密文','model',false)"))
        keys = list((await conn.execute(text("SELECT instance_key FROM ai_configs ORDER BY id"))).scalars())
        assert len(set(keys)) == len(keys) == 2
        for key in keys:
            _canonical(key)

    asyncio.run(run(create_and_test))
    async def change(conn):
        await conn.execute(text("UPDATE ai_configs SET instance_key=:key WHERE id=1"), {"key": str(uuid4())})
    with pytest.raises(IntegrityError):
        asyncio.run(run(change))
    assert_sqlite_chapter_plan_fk_intact()


def test_0049_postgresql_concurrent_binding_commit_blocks_downgrade(pg_instance_db):
    config, url, _, schema, run, _, _ = pg_instance_db
    command.upgrade(config, "0048")
    asyncio.run(run(_seed))
    command.upgrade(config, "0049")

    async def compete():
        engine = async_sqlalchemy.create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        maker = async_sqlalchemy.async_sessionmaker(engine)
        try:
            async with engine.connect() as writer:
                transaction = await writer.begin()
                await writer.execute(text("UPDATE tasks SET input=:input WHERE id=1"), {
                    "input": '{"_project_model":{"version":2,"config_instance_key":"new-instance"}}',
                })
                holder = await writer.scalar(text("SELECT pg_backend_pid()"))
                migration = asyncio.create_task(asyncio.to_thread(command.downgrade, config, "0048"))
                try:
                    await asyncio.wait_for(_wait_for_blocker(maker, holder), 15)
                    assert not migration.done()
                    await transaction.commit()
                    with pytest.raises(RuntimeError, match="降级"):
                        await asyncio.wait_for(migration, 15)
                finally:
                    if transaction.is_active:
                        await transaction.rollback()
                    if not migration.done():
                        migration.cancel()
                    await asyncio.gather(migration, return_exceptions=True)
        finally:
            await engine.dispose()
    asyncio.run(compete())

    async def verify(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0049"
        assert "config_instance_key" in str(await conn.scalar(text("SELECT input FROM tasks WHERE id=1")))
    asyncio.run(run(verify))


def test_0049_postgresql_full_backup_restores_instance_keys(pg_instance_db, tmp_path):
    config, url, parsed, schema, run, create_schema, drop_schema = pg_instance_db
    command.upgrade(config, "0048")
    asyncio.run(run(_seed))
    command.upgrade(config, "0049")

    async def keys(conn):
        return list((await conn.execute(text("SELECT instance_key FROM ai_configs ORDER BY id"))).scalars())

    original = asyncio.run(run(keys))
    dump_tool, restore_tool = shutil.which("pg_dump"), shutil.which("pg_restore")
    if not dump_tool or not restore_tool:
        pytest.fail("真实 PostgreSQL 备份测试需要 PATH 中的 pg_dump 与 pg_restore")
    dump = tmp_path / "whole-database.dump"
    info = snapshot_database(url, dump, pg_dump_path=dump_tool, deadline=time.monotonic() + 90,
                             stop_event=threading.Event())
    assert info.engine == "postgresql"
    asyncio.run(run(drop_schema, isolated=False))
    asyncio.run(run(create_schema, isolated=False))
    env = os.environ.copy()
    env["PGPASSWORD"] = parsed.password or ""
    completed = subprocess.run([
        restore_tool, "--no-password", "--exit-on-error", "--no-owner", "--no-privileges",
        "--host", parsed.host, "--port", str(parsed.port or 5432), "--username", parsed.username,
        "--dbname", parsed.database, "--schema", schema, str(dump),
    ], env=env, capture_output=True, timeout=90, check=False)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert asyncio.run(run(keys)) == original

    async def change(conn):
        await conn.execute(text("UPDATE ai_configs SET instance_key=:key WHERE id=1"), {"key": str(uuid4())})
    with pytest.raises(IntegrityError):
        asyncio.run(run(change))
