import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy.ext.asyncio as sa_async
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def test_pg_0040_migration_roundtrip_and_guard(monkeypatch):
    url = os.environ['AINOVEL_SCENES_POSTGRES_TEST_URL']
    schema = f'rich_early_{uuid4().hex}'
    async def sql(statements):
        engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
        try:
            async with engine.begin() as conn:
                results = []
                for statement, args in statements:
                    result = await conn.execute(text(statement), args)
                    results.append(result.scalar() if result.returns_rows else None)
                return results
        finally:
            await engine.dispose()
    async def schema_sql(query):
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(query))
        finally:
            await engine.dispose()
    asyncio.run(schema_sql(f'CREATE SCHEMA "{schema}"'))
    original_factory = sa_async.async_engine_from_config
    def isolated_factory(*args, **kwargs):
        return original_factory(*args, **kwargs, connect_args={'server_settings': {'search_path': schema}})
    monkeypatch.setattr(sa_async, 'async_engine_from_config', isolated_factory)
    backend = Path.cwd()
    config = Config(str(backend / 'alembic.ini'))
    config.set_main_option('script_location', str(backend / 'alembic'))
    config.set_main_option('sqlalchemy.url', url.replace('%', '%%'))
    config.attributes['configure_logger'] = False
    body = ' 甲😀\r\n\n乙e\u0301\r尾 '
    try:
        command.upgrade(config, '0039')
        asyncio.run(sql([
            ("INSERT INTO users(id,username,password_hash) VALUES(1,'pgwriter','hash')", {}),
            ("INSERT INTO projects(id,user_id,title,genre,premise,style,status) VALUES(1,1,'旧书','','','','planning')", {}),
            ("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count,content_revision) VALUES(1,1,1,'旧章',:body,'draft',8,7)", {'body': body}),
            ("INSERT INTO scenes(id,chapter_id,content,sort,active) VALUES(1,1,:body,0,true)", {'body': body}),
        ]))
        command.upgrade(config, '0040')
        got=asyncio.run(sql([("SELECT content FROM scenes WHERE id=1", {}), ("SELECT content_revision FROM chapters WHERE id=1", {}), ("SELECT count(*) FROM scene_documents", {})]))
        assert got == [body,7,0]
        command.downgrade(config, '0039')
        command.upgrade(config, '0040')
        asyncio.run(sql([("INSERT INTO scene_sections(scene_id,logical_key,anchor,sort,title,content) VALUES(1,'645d44c0-cc16-4949-973b-542022da8421',1,0,'备选','保留')", {})]))
        with pytest.raises(RuntimeError, match='不能无损降级'):
            command.downgrade(config, '0039')
        got=asyncio.run(sql([("SELECT content FROM scene_sections WHERE scene_id=1", {}), ("SELECT version_num FROM alembic_version", {})]))
        assert got == ['保留','0040']
    finally:
        asyncio.run(schema_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
