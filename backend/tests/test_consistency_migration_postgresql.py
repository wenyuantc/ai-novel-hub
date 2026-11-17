"""0058 在 PostgreSQL 中建表、拒绝有损降级，并在清空后安全往返。"""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text

pytest_plugins = ("tests.test_ai_config_instance_migration_postgresql",)

TABLES = ("consistency_ledgers", "consistency_runs", "consistency_issues")


async def _seed(conn):
    await conn.execute(text("INSERT INTO users(id,username,password_hash) VALUES(1,'owner','hash')"))
    await conn.execute(text("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
                            "VALUES(1,1,'作品','','','','planning')"))


async def _tables(conn) -> set[str]:
    rows = await conn.execute(text(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() AND tablename LIKE 'consistency_%'"
    ))
    return {row[0] for row in rows}


def test_0058_postgresql_creates_tables_and_rejects_lossy_downgrade(pg_instance_db):
    config, _, _, _, run, _, _ = pg_instance_db
    command.upgrade(config, "0057")
    asyncio.run(run(_seed))
    command.upgrade(config, "0058")

    async def add_issue(conn):
        assert await _tables(conn) == set(TABLES)
        await conn.execute(text("INSERT INTO consistency_runs(project_id,scope,stats) VALUES(1,'{}','{}')"))
        await conn.execute(text(
            "INSERT INTO consistency_issues(project_id,category,title,severity,identity_key,evidence,first_run_id) "
            "VALUES(1,'item','佩剑去向冲突','high',:key,'[]',(SELECT max(id) FROM consistency_runs))"
        ), {"key": "a" * 64})
        # 默认值与约束在 PostgreSQL 中生效。
        row = (await conn.execute(text(
            "SELECT status, revision, reappeared, note FROM consistency_issues"
        ))).one()
        assert tuple(row) == ("open", 1, False, "")

    asyncio.run(run(add_issue))
    with pytest.raises(RuntimeError, match="拒绝有损降级"):
        command.downgrade(config, "0057")

    async def unique_then_clear(conn):
        assert await conn.scalar(text("SELECT version_num FROM alembic_version")) == "0058"
        # 删除运行记录后问题保留、引用置空。
        await conn.execute(text("DELETE FROM consistency_runs"))
        assert await conn.scalar(text("SELECT first_run_id FROM consistency_issues")) is None
        # 删除作品级联清空。
        await conn.execute(text("DELETE FROM projects WHERE id=1"))
        for table in TABLES:
            assert await conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0

    asyncio.run(run(unique_then_clear))
    command.downgrade(config, "0057")

    async def gone(conn):
        assert await _tables(conn) == set()

    asyncio.run(run(gone))
    command.upgrade(config, "0058")

    async def back(conn):
        assert await _tables(conn) == set(TABLES)

    asyncio.run(run(back))
