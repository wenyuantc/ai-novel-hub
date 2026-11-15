"""0045 PostgreSQL 原位迁移与索引占用交错；仅允许本机隔离测试库。"""

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


def test_0045_postgresql_preserves_children_and_refuses_lossy_downgrade(tmp_path, monkeypatch):
    dedicated = os.environ.get("AINOVEL_COLLAB_INDEX_POSTGRES_TEST_URL")
    url = dedicated or os.environ.get("AINOVEL_COLLAB_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("设置 AINOVEL_COLLAB_INDEX_POSTGRES_TEST_URL 或 AINOVEL_COLLAB_POSTGRES_TEST_URL")
    database = "collab_index" if dedicated else "collab_test"
    parsed = make_url(url)
    if (parsed.drivername, parsed.host, parsed.database, parsed.username) != (
        "postgresql+asyncpg", "127.0.0.1", database, "collab_test",
    ):
        raise ValueError("索引迁移测试仅允许本机指定的隔离数据库")
    schema = f"collab_index_migration_{uuid4().hex}"
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
                await action(conn)
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
        await conn.execute(text("INSERT INTO kb_documents(id,project_id,filename,sha256,text) "
                                "VALUES(1,1,'旧文件',:sha,'文档原文')"), {"sha": "a" * 64})
        await conn.execute(text("INSERT INTO kb_chunks(id,document_id,seq,content) VALUES(1,1,1,'分块原文')"))
        await conn.execute(text("INSERT INTO tasks(id,user_id,project_id,kind,status,input) "
                                "VALUES(1,1,1,'kb.index','queued','{}')"))

    async def check_upgrade(conn):
        row = (await conn.execute(text("SELECT index_task_key,index_task_queued_at,index_claim_key "
                                       "FROM kb_documents WHERE id=1"))).one()
        assert tuple(row) == (None, None, None)
        assert await conn.scalar(text("SELECT execution_key FROM tasks WHERE id=1")) is None
        assert await conn.scalar(text("SELECT content FROM kb_chunks WHERE id=1")) == "分块原文"
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0045"

    async def violate_shape(conn):
        await conn.execute(text("UPDATE kb_documents SET index_task_key=:key WHERE id=1"), {"key": str(uuid4())})

    async def add_claim(conn):
        await conn.execute(text("UPDATE kb_documents SET index_task_key=:key, "
                                "index_task_queued_at=now(),index_claim_key=:claim WHERE id=1"),
                           {"key": str(uuid4()), "claim": str(uuid4())})
        await conn.execute(text("UPDATE tasks SET execution_key=:key WHERE id=1"), {"key": str(uuid4())})

    async def clear_document(conn):
        await conn.execute(text("UPDATE kb_documents SET index_task_key=NULL,index_task_queued_at=NULL,"
                                "index_claim_key=NULL WHERE id=1"))

    async def clear_task(conn):
        await conn.execute(text("UPDATE tasks SET execution_key=NULL WHERE id=1"))

    async def check_downgrade(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0044"
        assert await conn.scalar(text("SELECT text FROM kb_documents WHERE id=1")) == "文档原文"
        assert await conn.scalar(text("SELECT content FROM kb_chunks WHERE id=1")) == "分块原文"
        assert await conn.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_schema=:schema "
                                      "AND table_name='tasks' AND column_name='execution_key'"),
                                 {"schema": schema}) == 0

    config = _config(tmp_path / "unused.db")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    created = False
    try:
        asyncio.run(run(create_schema, isolated=False))
        created = True
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", isolated_factory)
        command.upgrade(config, "0044")
        asyncio.run(run(seed))
        command.upgrade(config, "0045")
        asyncio.run(run(check_upgrade))
        with pytest.raises(IntegrityError):
            asyncio.run(run(violate_shape))
        asyncio.run(run(add_claim))
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0044")
        asyncio.run(run(clear_document))
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0044")
        asyncio.run(run(clear_task))
        command.downgrade(config, "0044")
        asyncio.run(run(check_downgrade))
    finally:
        if created:
            asyncio.run(run(drop_schema, isolated=False))
