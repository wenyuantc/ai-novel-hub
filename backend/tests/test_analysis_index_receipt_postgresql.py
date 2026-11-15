"""真实 PostgreSQL 迁移、来源去重和降级写屏障；仅使用隔离 schema。"""

import asyncio
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext import asyncio as async_sqlalchemy

from app.models import Base
from tests.test_analysis_index_receipt_migration import _insert, _receipt, _seed
from tests.test_collaboration_access_postgresql import _wait_for_blocker

pytest_plugins = ("tests.test_ai_config_instance_migration_postgresql",)


async def _seed_postgresql(conn):
    return await conn.run_sync(_seed)


async def _insert_postgresql(conn, values):
    await conn.run_sync(lambda sync_conn: _insert(sync_conn, values))


def test_0051_postgresql_create_all_matches_receipt_integrity(pg_instance_db):
    _, _, _, _, run, _, _ = pg_instance_db

    async def setup(conn):
        await conn.run_sync(Base.metadata.create_all)
        parent, child = await _seed_postgresql(conn)
        await _insert_postgresql(conn, _receipt(parent, child))
        await _insert_postgresql(conn, _receipt(parent, None, outcome="no_work", memory_refs="[]"))
        return parent

    parent = asyncio.run(run(setup))
    source_key = str(uuid4())
    for invalid in (
        _receipt(parent, source_key, source_key=source_key),
        _receipt(parent, str(uuid4()), memory_refs="{}"),
        _receipt(parent, str(uuid4()), memory_refs="[]"),
        _receipt(parent, None, outcome="queued"),
        _receipt(parent, str(uuid4()), outcome="no_work"),
    ):
        with pytest.raises(IntegrityError):
            asyncio.run(run(lambda conn, values=invalid: _insert_postgresql(conn, values)))

    async def modify(conn):
        await conn.execute(text("UPDATE analysis_index_receipts SET actor_id=3"))

    with pytest.raises(IntegrityError):
        asyncio.run(run(modify))

    async def verify(conn):
        assert await conn.scalar(text("SELECT count(*) FROM analysis_index_receipts")) == 2
        assert await conn.scalar(text("SELECT count(*) FROM analysis_index_receipts WHERE outcome='no_work' "
                                      "AND child_task_key IS NULL")) == 1

    asyncio.run(run(verify))


def test_0051_postgresql_migrates_preserves_receipts_and_blocks_lossy_downgrade(pg_instance_db):
    config, _, _, schema, run, _, _ = pg_instance_db
    command.upgrade(config, "0050")
    parent, child = asyncio.run(run(_seed_postgresql))
    command.upgrade(config, "0051")
    receipt = _receipt(parent, child)

    async def insert_and_delete(conn):
        assert await conn.scalar(text("SELECT count(*) FROM analysis_index_receipts")) == 0
        await _insert_postgresql(conn, receipt)
        await conn.execute(text("DELETE FROM tasks WHERE id IN (1,2)"))
        await conn.execute(text("DELETE FROM users WHERE id=2"))
        row = (await conn.execute(text(
            "SELECT actor_id,parent_task_key,child_task_key FROM analysis_index_receipts"
        ))).one()
        assert tuple(row) == (2, parent, child)
        assert await conn.scalar(text("SELECT count(*) FROM information_schema.table_constraints "
                                      "WHERE table_schema=:schema AND table_name='analysis_index_receipts' "
                                      "AND constraint_type='FOREIGN KEY'"), {"schema": schema}) == 1

    asyncio.run(run(insert_and_delete))

    async def alter(conn):
        await conn.execute(text("UPDATE analysis_index_receipts SET actor_id=3"))

    with pytest.raises(IntegrityError):
        asyncio.run(run(alter))
    same_source_and_child = str(uuid4())
    for invalid in (
        _receipt(parent, same_source_and_child, source_key=same_source_and_child),
        _receipt(parent, None, outcome="queued"),
    ):
        with pytest.raises(IntegrityError):
            asyncio.run(run(lambda conn, values=invalid: _insert_postgresql(conn, values)))
    with pytest.raises(RuntimeError, match="不能安全降级"):
        command.downgrade(config, "0050")

    async def delete_project(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0051"
        await conn.execute(text("DELETE FROM projects WHERE id=1"))
        assert await conn.scalar(text("SELECT count(*) FROM analysis_index_receipts")) == 0

    asyncio.run(run(delete_project))
    command.downgrade(config, "0050")


@pytest.mark.parametrize("outcome", ["queued", "no_work"])
def test_0051_postgresql_concurrent_duplicate_source_is_atomic_after_task_pruning(pg_instance_db, outcome):
    config, url, _, schema, run, _, _ = pg_instance_db
    command.upgrade(config, "0051")
    parent, child = asyncio.run(run(_seed_postgresql))

    async def remove_placeholder(conn):
        await conn.execute(text("DELETE FROM tasks WHERE id=2"))

    asyncio.run(run(remove_placeholder))
    first = _receipt(parent, child if outcome == "queued" else None, outcome=outcome)
    second_child = str(uuid4())
    second = _receipt(parent, second_child, source_key=first["source_key"])

    async def compete():
        engine = async_sqlalchemy.create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        maker = async_sqlalchemy.async_sessionmaker(engine)
        try:
            async with engine.connect() as winner, engine.connect() as loser:
                transaction = await winner.begin()
                if outcome == "queued":
                    await winner.execute(text(
                        "INSERT INTO tasks(id,statistics_key,user_id,project_id,kind,status,input) "
                        "VALUES(2,:key,2,1,'kb.index','queued','{}')"
                    ), {"key": child})
                    await winner.execute(text(
                        "INSERT INTO admin_task_statistics(key,kind,origin,status) "
                        "VALUES(:key,'kb.index','new','queued')"
                    ), {"key": child})
                await _insert_postgresql(winner, first)
                holder = await winner.scalar(text("SELECT pg_backend_pid()"))

                async def duplicate():
                    async with loser.begin():
                        await loser.execute(text(
                            "INSERT INTO tasks(id,statistics_key,user_id,project_id,kind,status,input) "
                            "VALUES(3,:key,2,1,'kb.index','queued','{}')"
                        ), {"key": second_child})
                        await loser.execute(text(
                            "INSERT INTO admin_task_statistics(key,kind,origin,status) "
                            "VALUES(:key,'kb.index','new','queued')"
                        ), {"key": second_child})
                        await _insert_postgresql(loser, second)

                pending = asyncio.create_task(duplicate())
                try:
                    await asyncio.wait_for(_wait_for_blocker(maker, holder), 15)
                    assert not pending.done()
                    await transaction.commit()
                    with pytest.raises(IntegrityError):
                        await asyncio.wait_for(pending, 15)
                finally:
                    if transaction.is_active:
                        await transaction.rollback()
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        finally:
            await engine.dispose()

    asyncio.run(compete())

    async def verify(conn):
        assert await conn.scalar(text("SELECT count(*) FROM analysis_index_receipts")) == 1
        assert await conn.scalar(text("SELECT count(*) FROM tasks WHERE statistics_key=:key"), {"key": child}) == (
            1 if outcome == "queued" else 0
        )
        assert await conn.scalar(
            text("SELECT count(*) FROM tasks WHERE statistics_key=:key"), {"key": second_child},
        ) == 0
        assert await conn.scalar(text("SELECT count(*) FROM admin_task_statistics WHERE key=:key"),
                                 {"key": second_child}) == 0
        await conn.execute(text("DELETE FROM tasks WHERE statistics_key=:key"), {"key": child})
        assert await conn.scalar(text("SELECT child_task_key FROM analysis_index_receipts")) == (
            child if outcome == "queued" else None
        )

    asyncio.run(run(verify))
    with pytest.raises(IntegrityError):
        asyncio.run(run(lambda conn: _insert_postgresql(conn, second)))


def test_0051_postgresql_receipt_commit_serializes_before_downgrade(pg_instance_db):
    config, url, _, schema, run, _, _ = pg_instance_db
    command.upgrade(config, "0051")
    parent, child = asyncio.run(run(_seed_postgresql))
    receipt = _receipt(parent, child)

    async def compete():
        engine = async_sqlalchemy.create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        maker = async_sqlalchemy.async_sessionmaker(engine)
        try:
            async with engine.connect() as writer:
                transaction = await writer.begin()
                # 建立与业务一致的 Task → 收据顺序，确保迁移拿到表锁后不漏并发写入。
                await writer.execute(text("UPDATE tasks SET stage=stage WHERE id=1"))
                await _insert_postgresql(writer, receipt)
                holder = await writer.scalar(text("SELECT pg_backend_pid()"))
                migration = asyncio.create_task(asyncio.to_thread(command.downgrade, config, "0050"))
                try:
                    await asyncio.wait_for(_wait_for_blocker(maker, holder), 15)
                    assert not migration.done()
                    await transaction.commit()
                    with pytest.raises(RuntimeError, match="不能安全降级"):
                        await asyncio.wait_for(migration, 15)
                finally:
                    if transaction.is_active:
                        await transaction.rollback()
                    await asyncio.gather(migration, return_exceptions=True)
        finally:
            await engine.dispose()

    asyncio.run(compete())

    async def verify(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0051"
        assert await conn.scalar(text("SELECT receipt_key FROM analysis_index_receipts")) == receipt["receipt_key"]

    asyncio.run(run(verify))
