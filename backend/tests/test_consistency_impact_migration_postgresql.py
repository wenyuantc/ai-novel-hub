"""0059 在 PostgreSQL 中加列建表、拒绝有损降级，并在清空后安全往返。"""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text

pytest_plugins = ("tests.test_ai_config_instance_migration_postgresql",)

HASH = "a" * 64
PREV = {"prev_facts", "prev_source_hash", "prev_text_hash"}


async def _seed(conn):
    await conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash')"))
    await conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                            "VALUES(1,1,'作品','','','','planning')"))
    await conn.execute(text("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count) "
                            "VALUES(1,1,1,'第一章','佩剑已毁','draft',4),(2,1,2,'第二章','拔剑','draft',2)"))
    await conn.execute(text("INSERT INTO consistency_ledgers(project_id,chapter_id,source_hash,text_hash,facts) "
                            "VALUES(1,1,:h,:h,'[]')"), {"h": HASH})


async def _prev_columns(conn) -> set[str]:
    rows = await conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'consistency_ledgers' AND column_name LIKE 'prev_%'"
    ))
    return {row[0] for row in rows}


async def _has_impacts(conn) -> bool:
    return await conn.scalar(text(
        "SELECT count(*) FROM pg_tables WHERE schemaname = current_schema() AND tablename = 'consistency_impacts'"
    )) == 1


def test_0059_postgresql_adds_columns_table_and_rejects_lossy_downgrade(pg_instance_db):
    config, _, _, _, run, _, _ = pg_instance_db
    command.upgrade(config, "0058")
    asyncio.run(run(_seed))
    command.upgrade(config, "0059")

    async def add_impact(conn):
        assert await _prev_columns(conn) == PREV and await _has_impacts(conn)
        # 存量台账保留，上一版为空。
        row = (await conn.execute(text(
            "SELECT source_hash, prev_facts, prev_source_hash, prev_text_hash FROM consistency_ledgers"))).one()
        assert tuple(row) == (HASH, None, None, None)
        await conn.execute(text(
            "INSERT INTO consistency_impacts(project_id,chapter_id,changes,items,uncovered,acknowledged) "
            "VALUES(1,1,'[]','[]','[]','[]')"))
        # 默认值在 PostgreSQL 中生效。
        row = (await conn.execute(text(
            "SELECT revision, model, source_hash, assessed_at FROM consistency_impacts"))).one()
        assert tuple(row) == (1, "", None, None)

    asyncio.run(run(add_impact))
    with pytest.raises(RuntimeError, match="拒绝有损降级"):
        command.downgrade(config, "0058")

    async def only_previous_ledger(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0059"
        # 删章：影响记录级联删除；只剩上一版台账同样拒绝降级。
        await conn.execute(text("DELETE FROM chapters WHERE id=1"))
        assert await conn.scalar(text("SELECT count(*) FROM consistency_impacts")) == 0
        await conn.execute(text(
            "INSERT INTO consistency_ledgers(project_id,chapter_id,source_hash,text_hash,facts,prev_facts,"
            "prev_source_hash,prev_text_hash) VALUES(1,2,:h,:h,'[]','[]',:h,:h)"), {"h": HASH})

    asyncio.run(run(only_previous_ledger))
    with pytest.raises(RuntimeError, match="拒绝有损降级"):
        command.downgrade(config, "0058")

    async def clear(conn):
        await conn.execute(text("UPDATE consistency_ledgers SET prev_facts=NULL, prev_source_hash=NULL, "
                                "prev_text_hash=NULL"))

    asyncio.run(run(clear))
    command.downgrade(config, "0058")

    async def gone(conn):
        assert await _prev_columns(conn) == set() and not await _has_impacts(conn)
        # 当前台账不受降级影响。
        assert await conn.scalar(text("SELECT count(*) FROM consistency_ledgers")) == 1

    asyncio.run(run(gone))
    command.upgrade(config, "0059")

    async def back(conn):
        assert await _prev_columns(conn) == PREV and await _has_impacts(conn)

    asyncio.run(run(back))
