"""无用户、作品或生成记录时，0044 可在两种数据库中无损往返。"""

import asyncio
import os
import uuid

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext import asyncio as async_sqlalchemy

from tests.test_setting_fields_migration import _config, _foreign_keys_during_migrations


def test_0044_empty_sqlite_upgrade_and_downgrade(tmp_path, monkeypatch):
    _foreign_keys_during_migrations(monkeypatch)
    file = tmp_path / "empty-collaboration.db"
    config = _config(file)
    engine = create_engine(f"sqlite:///{file}")
    try:
        command.upgrade(config, "0043")
        with engine.connect() as conn:
            for table in ("users", "projects", "generation_logs"):
                assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        command.upgrade(config, "0044")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0044"
            for table in ("project_collaborators", "series_collaborators", "collaboration_audit_events"):
                assert conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
        command.downgrade(config, "0043")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0043"
            assert "project_collaborators" not in inspect(conn).get_table_names()
            assert "actor_user_id" not in {col["name"] for col in inspect(conn).get_columns("generation_logs")}
            assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    finally:
        engine.dispose()


def test_0044_empty_postgresql_upgrade_and_downgrade(tmp_path, monkeypatch):
    url = os.environ.get("AINOVEL_COLLAB_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("设置 AINOVEL_COLLAB_POSTGRES_TEST_URL 才运行隔离 PostgreSQL 协作验证")
    parsed = make_url(url)
    if (parsed.drivername, parsed.host, parsed.database, parsed.username) != (
        "postgresql+asyncpg", "127.0.0.1", "collab_test", "collab_test",
    ):
        raise ValueError("协作测试仅允许本机 collab_test 专用测试库")
    schema = f"collab_empty_{uuid.uuid4().hex}"
    original_factory = async_sqlalchemy.async_engine_from_config

    def isolated_factory(*args, **kwargs):
        return original_factory(*args, **kwargs, connect_args={"server_settings": {"search_path": schema}})

    async def run(action, *, isolated=True):
        engine = async_sqlalchemy.create_async_engine(url, connect_args={"server_settings": {
            "search_path": schema if isolated else "public",
        }})
        try:
            async with engine.begin() as conn:
                assert await conn.scalar(text("SELECT current_database()")) == "collab_test"
                await action(conn)
        finally:
            await engine.dispose()

    async def create_schema(conn):
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    async def drop_schema(conn):
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    async def check_empty(conn):
        for table in ("users", "projects", "generation_logs"):
            assert await conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0

    async def check_upgrade(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0044"
        for table in ("project_collaborators", "series_collaborators", "collaboration_audit_events"):
            assert await conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0

    async def check_downgrade(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0043"
        assert await conn.scalar(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema=:schema AND table_name='generation_logs' AND column_name='actor_user_id'"
        ), {"schema": schema}) == 0
        assert await conn.scalar(text("SELECT to_regclass('project_collaborators')")) is None

    config = _config(tmp_path / "unused.db")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    created = False
    try:
        asyncio.run(run(create_schema, isolated=False))
        created = True
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", isolated_factory)
        command.upgrade(config, "0043")
        asyncio.run(run(check_empty))
        command.upgrade(config, "0044")
        asyncio.run(run(check_upgrade))
        command.downgrade(config, "0043")
        asyncio.run(run(check_downgrade))
    finally:
        if created:
            asyncio.run(run(drop_schema, isolated=False))
