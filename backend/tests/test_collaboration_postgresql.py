"""只在专用本机 PostgreSQL 隔离 schema 中运行真实 0044 升降级。"""

import asyncio
import os
import uuid

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext import asyncio as async_sqlalchemy

from tests.test_collaboration_migration import _seed_legacy
from tests.test_setting_fields_migration import _config


def test_0044_postgresql_upgrade_downgrade_and_live_foreign_keys(tmp_path, monkeypatch):
    database_url = os.environ.get("AINOVEL_COLLAB_POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("设置 AINOVEL_COLLAB_POSTGRES_TEST_URL 才运行隔离 PostgreSQL 协作验证")
    parsed = make_url(database_url)
    if (parsed.drivername, parsed.host, parsed.database, parsed.username) != (
        "postgresql+asyncpg", "127.0.0.1", "collab_test", "collab_test",
    ):
        raise ValueError("协作测试仅允许本机 collab_test 专用测试库")
    schema = f"collab_test_{uuid.uuid4().hex}"
    original_factory = async_sqlalchemy.async_engine_from_config

    def isolated_factory(*args, **kwargs):
        return original_factory(*args, **kwargs, connect_args={"server_settings": {"search_path": schema}})

    async def run(action, *, isolated=True):
        engine = async_sqlalchemy.create_async_engine(database_url, connect_args={"server_settings": {
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

    async def seed(conn):
        await conn.run_sync(_seed_legacy)

    async def verify_upgrade(conn):
        assert await conn.scalar(text("SELECT actor_user_id FROM generation_logs WHERE id=1")) == 1
        assert await conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1
        await conn.execute(text(
            "INSERT INTO project_collaborators(project_id,user_id,role,status,invitation_id,revision) "
            "VALUES(1,2,'editor','accepted',:invitation,1)"
        ), {"invitation": str(uuid.uuid4())})
        await conn.execute(text("UPDATE generation_logs SET actor_user_id=2 WHERE id=1"))

    async def verify_cascade(conn):
        await conn.execute(text(
            "INSERT INTO project_collaborators(project_id,user_id,role,status,invitation_id,revision) "
            "VALUES(1,2,'editor','accepted',:invitation,1)"
        ), {"invitation": str(uuid.uuid4())})
        assert await conn.scalar(text("SELECT count(*) FROM project_collaborators")) == 1
        await conn.execute(text(
            "INSERT INTO collaboration_audit_events(operation_id,scope,resource_id,action,result,summary,"
            "actor_user_id,actor_username,target_user_id,target_username) "
            "VALUES(:operation,'project',1,'invite','success','{}',1,'owner',2,'guest')"
        ), {"operation": str(uuid.uuid4())})
        await conn.execute(text("DELETE FROM users WHERE id=2"))
        assert await conn.scalar(text("SELECT count(*) FROM project_collaborators")) == 0
        assert await conn.scalar(text("SELECT actor_user_id FROM generation_logs WHERE id=1")) is None
        assert await conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1
        assert (await conn.execute(text(
            "SELECT actor_user_id,actor_username,target_user_id,target_username FROM collaboration_audit_events"
        ))).one() == (1, "owner", 2, "guest")
        # 清理测试事件，以独立验证未知 actor 日志的降级拒绝。
        await conn.execute(text("DELETE FROM collaboration_audit_events"))

    async def clear_project_member(conn):
        await conn.execute(text("DELETE FROM project_collaborators"))

    async def add_series_member(conn):
        await conn.execute(text("INSERT INTO series(id,owner_id,name) VALUES(1,1,'系列')"))
        await conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(3,'series-guest','hash')"))
        await conn.execute(text(
            "INSERT INTO series_collaborators(series_id,user_id,role,status,invitation_id,revision) "
            "VALUES(1,3,'editor','accepted',:invitation,1)"
        ), {"invitation": str(uuid.uuid4())})

    async def clear_series_member_add_audit(conn):
        await conn.execute(text("DELETE FROM series_collaborators"))
        await conn.execute(text(
            "INSERT INTO collaboration_audit_events(operation_id,scope,resource_id,action,result,summary) "
            "VALUES(:operation,'project',1,'invite','success','{}')"
        ), {"operation": str(uuid.uuid4())})

    async def clear_audit(conn):
        await conn.execute(text("DELETE FROM collaboration_audit_events"))

    async def restore_actor(conn):
        await conn.execute(text("UPDATE generation_logs SET actor_user_id=1 WHERE id=1"))

    async def verify_downgrade(conn):
        assert await conn.scalar(text("SELECT generation_log_id FROM chapter_versions WHERE id=1")) == 1
        assert await conn.scalar(text("SELECT count(*) FROM information_schema.columns "
                                      "WHERE table_schema=:schema AND table_name='generation_logs' "
                                      "AND column_name='actor_user_id'"), {"schema": schema}) == 0

    config = _config(tmp_path / "unused.db")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    created = False
    try:
        asyncio.run(run(create_schema, isolated=False))
        created = True
        monkeypatch.setattr(async_sqlalchemy, "async_engine_from_config", isolated_factory)
        command.upgrade(config, "0043")
        asyncio.run(run(seed))
        command.upgrade(config, "0044")
        asyncio.run(run(verify_upgrade))
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0043")
        asyncio.run(run(clear_project_member))
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0043")  # 仍有非作者日志
        asyncio.run(run(verify_cascade))
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0043")
        asyncio.run(run(restore_actor))
        asyncio.run(run(add_series_member))
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0043")
        asyncio.run(run(clear_series_member_add_audit))
        with pytest.raises(RuntimeError, match="降级"):
            command.downgrade(config, "0043")
        asyncio.run(run(clear_audit))
        command.downgrade(config, "0043")
        asyncio.run(run(verify_downgrade))
    finally:
        if created:
            asyncio.run(run(drop_schema, isolated=False))
