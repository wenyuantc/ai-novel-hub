"""0050 在 PostgreSQL 中保留来源并串行化降级与新来源。"""

import asyncio
from contextlib import suppress
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.test_collaboration_access_postgresql import _wait_for_blocker

pytest_plugins = ("tests.test_ai_config_instance_migration_postgresql",)


async def _seed(conn):
    await conn.execute(text(
        "INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash'),(2,'editor','hash')"
    ))
    await conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                            "VALUES(1,1,'作品','','','','planning')"))
    await conn.execute(text("INSERT INTO inspiration_sessions(id,user_id,project_id,title,state) "
                            "VALUES(1,2,1,'会话','{}')"))


def test_0050_postgresql_keeps_purged_origin_and_rejects_downgrade(pg_instance_db):
    config, _, _, _, run, _, _ = pg_instance_db
    command.upgrade(config, "0049")
    asyncio.run(run(_seed))
    command.upgrade(config, "0050")
    key = str(uuid4())

    async def add_then_purge(conn):
        await conn.execute(text("INSERT INTO inspiration_origins(session_id,project_id,invitation_id,origin_key) "
                                "VALUES(1,1,:invitation,:identity)"),
                           {"invitation": str(uuid4()), "identity": key})
        await conn.execute(text("DELETE FROM projects WHERE id=1"))
        assert await conn.scalar(text("SELECT project_id FROM inspiration_sessions WHERE id=1")) is None
        assert await conn.scalar(text("SELECT origin_key FROM inspiration_origins WHERE session_id=1")) == key

    asyncio.run(run(add_then_purge))
    with pytest.raises(RuntimeError, match="不能安全降级"):
        command.downgrade(config, "0049")

    async def delete_session(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0050"
        await conn.execute(text("DELETE FROM inspiration_sessions WHERE id=1"))
        assert await conn.scalar(text("SELECT count(*) FROM inspiration_origins")) == 0

    asyncio.run(run(delete_session))
    command.downgrade(config, "0049")


def test_0050_postgresql_origin_commit_before_waiting_downgrade(pg_instance_db):
    config, url, _, schema, run, _, _ = pg_instance_db
    command.upgrade(config, "0049")
    asyncio.run(run(_seed))
    command.upgrade(config, "0050")

    async def race():
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        maker = async_sessionmaker(engine, expire_on_commit=False)
        pending = None
        try:
            async with engine.connect() as writer:
                transaction = await writer.begin()
                await writer.execute(
                    text("INSERT INTO inspiration_origins(session_id,project_id,invitation_id,origin_key) "
                         "VALUES(1,1,:invitation,:identity)"),
                    {"invitation": str(uuid4()), "identity": str(uuid4())},
                )
                pid = await writer.scalar(text("SELECT pg_backend_pid()"))
                pending = asyncio.create_task(asyncio.to_thread(command.downgrade, config, "0049"))
                await asyncio.wait_for(_wait_for_blocker(maker, pid), 15)
                await transaction.commit()
                with pytest.raises(RuntimeError, match="不能安全降级"):
                    await asyncio.wait_for(pending, 15)
            async with engine.connect() as verify:
                assert await verify.scalar(text("SELECT version_num FROM alembic_version")) == "0050"
                assert await verify.scalar(text("SELECT count(*) FROM inspiration_origins")) == 1
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
            await engine.dispose()

    asyncio.run(race())
