"""0048 在隔离 PostgreSQL schema 中保留旧日志与章节外键。"""

import asyncio
import os
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext import asyncio as async_sqlalchemy

from tests.test_setting_fields_migration import _config


@pytest.mark.parametrize("seeded", [False, True])
def test_0048_postgresql_model_source_migration_and_downgrade(tmp_path, monkeypatch, seeded):
    dedicated = os.environ.get("AINOVEL_GENERATION_LOG_POSTGRES_TEST_URL")
    url = dedicated or os.environ.get("AINOVEL_COLLAB_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("设置 AINOVEL_GENERATION_LOG_POSTGRES_TEST_URL 或 AINOVEL_COLLAB_POSTGRES_TEST_URL")
    database = "generation_log" if dedicated else "collab_test"
    parsed = make_url(url)
    if (parsed.drivername, parsed.host, parsed.database, parsed.username) != (
        "postgresql+asyncpg", "127.0.0.1", database, "collab_test",
    ):
        raise ValueError("生成日志迁移仅允许本机指定的隔离测试库")
    schema = f"generation_log_source_{uuid4().hex}"
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
        await conn.execute(text("INSERT INTO users(id,username,password_hash) "
                                "VALUES(1,'owner','hash'),(2,'config-owner','hash')"))
        await conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                                "VALUES(1,1,'旧书','','','','writing')"))
        await conn.execute(text("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                                "VALUES(1,1,1,'旧章','正文','done',2)"))
        await conn.execute(text("INSERT INTO ai_configs(id,user_id,name,provider,api_key_enc,model,is_default) "
                                "VALUES(1,2,'原配置','openai_compat','密文','model',false)"))
        await conn.exec_driver_sql("INSERT INTO generation_logs(id,project_id,actor_user_id,chapter_id,kind,"
                                   "ai_config_id,provider,model,params,messages,status,error,output_chars,duration_ms) "
                                   "VALUES(1,1,1,1,'generate',1,'openai_compat','model',"
                                   "'{\"_model_source\":\"owner\",\"model_config_owner_id\":2}',"
                                   "'[]','success','',3,5)")
        await conn.execute(text("INSERT INTO chapter_versions(id,chapter_id,seq,source,content,word_count,"
                                "generation_log_id) VALUES(1,1,1,'generate','旧正文',3,1)"))
        row = (await conn.execute(text("SELECT * FROM generation_logs WHERE id=1"))).mappings().one()
        fks = (await conn.execute(text(
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='generation_logs'::regclass AND contype='f' ORDER BY conname"
        ))).all()
        return dict(row), list(fks)

    async def check_upgrade(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0048"
        if not seeded:
            assert await conn.scalar(text("SELECT count(*) FROM generation_logs")) == 0
            return
        old_row, old_fks = before
        row = (await conn.execute(text("SELECT * FROM generation_logs WHERE id=1"))).mappings().one()
        assert {name: row[name] for name in old_row} == old_row
        assert row["model_source"] is None and row["model_config_owner_id"] is None
        fks = (await conn.execute(text(
            "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='generation_logs'::regclass AND contype='f' ORDER BY conname"
        ))).all()
        assert list(fks) == old_fks
        assert await conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1

    async def set_source(conn, source, owner_id):
        await conn.execute(text("UPDATE generation_logs SET model_source=:source,model_config_owner_id=:owner "
                                "WHERE id=1"), {"source": source, "owner": owner_id})

    async def retain_after_deletion(conn):
        await set_source(conn, "owner", 2)
        await conn.execute(text("DELETE FROM ai_configs WHERE id=1"))
        await conn.execute(text("DELETE FROM users WHERE id=2"))
        row = (await conn.execute(text("SELECT ai_config_id,model_source,model_config_owner_id "
                                       "FROM generation_logs WHERE id=1"))).one()
        assert tuple(row) == (None, "owner", 2)
        assert await conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1

    async def check_downgrade(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0047"
        columns = await conn.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_schema=:schema "
                                         "AND table_name='generation_logs' "
                                         "AND column_name IN ('model_source','model_config_owner_id')"),
                                    {"schema": schema})
        assert columns == 0
        if seeded:
            assert await conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1

    config = _config(tmp_path / "unused.db")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    created = False
    try:
        asyncio.run(run(create_schema, isolated=False))
        created = True
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", isolated_factory)
        command.upgrade(config, "0047")
        before = asyncio.run(run(seed)) if seeded else None
        command.upgrade(config, "0048")
        asyncio.run(run(check_upgrade))
        if seeded:
            for source, owner in (("owner", None), (None, 2), ("bogus", 2), ("actor", 0), ("owner", -1)):
                with pytest.raises(IntegrityError):
                    asyncio.run(run(lambda conn, s=source, o=owner: set_source(conn, s, o)))
            asyncio.run(run(retain_after_deletion))
            with pytest.raises(RuntimeError, match="降级"):
                command.downgrade(config, "0047")
            asyncio.run(run(lambda conn: set_source(conn, None, None)))
        command.downgrade(config, "0047")
        asyncio.run(run(check_downgrade))
    finally:
        if created:
            asyncio.run(run(drop_schema, isolated=False))
