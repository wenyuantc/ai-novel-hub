"""PostgreSQL 16 的真实迁移进程竞争与连接中断。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_c7_migration_concurrency import (
    ALEMBIC,
    BACKEND,
    EMPTY_UPGRADES,
    HEAD,
    OLD_UPGRADES,
    _collect,
    _fault_script,
)

_RUNNER = """
import os, sys
from alembic import command
from alembic.config import Config
import sqlalchemy.ext.asyncio as aio
original = aio.async_engine_from_config
def isolated(section, **kwargs):
    kwargs['connect_args'] = {'server_settings': {
        'search_path': os.environ['C7_PG_SCHEMA'],
        'application_name': os.environ.get('C7_PG_APP', 'c7-migration-test'),
    }}
    return original(section, **kwargs)
aio.async_engine_from_config = isolated
cfg = Config(sys.argv[1])
if sys.argv[2] == 'current':
    command.current(cfg)
else:
    getattr(command, sys.argv[2])(cfg, sys.argv[3])
"""


async def _sql(raw: str, statement: str, parameters: dict | None = None) -> list[tuple]:
    engine = create_async_engine(raw)
    try:
        async with engine.begin() as db:
            result = await db.execute(text(statement), parameters or {})
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        await engine.dispose()


def _execute(raw: str, statement: str, parameters: dict | None = None) -> list[tuple]:
    return asyncio.run(_sql(raw, statement, parameters))


@pytest.fixture
def pg_schema():
    raw = os.getenv("AINOVEL_POSTGRES_TEST_URL")
    if not raw:
        pytest.skip("缺少隔离 PostgreSQL 测试库 AINOVEL_POSTGRES_TEST_URL")
    url = make_url(raw)
    if (url.drivername != "postgresql+asyncpg" or url.database != "ainovel_test"
            or url.host not in {"127.0.0.1", "localhost", "::1"} or url.query):
        raise ValueError("迁移测试须使用本机专用 ainovel_test 数据库")
    schema = f"c7_migration_{uuid4().hex}"
    _execute(raw, f'CREATE SCHEMA "{schema}"')
    try:
        yield raw, schema
    finally:
        _execute(raw, f'DROP SCHEMA "{schema}" CASCADE')


def _environment(raw: str, schema: str, **extra: str) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    env.update({
        "DATABASE_URL": raw,
        "C7_PG_SCHEMA": schema,
        "PYTHONPATH": str(BACKEND),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        **extra,
    })
    return env


def _spawn(
    raw: str, schema: str, config: Path = ALEMBIC, target: str = "head", action: str = "upgrade", **extra: str,
) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", _RUNNER, str(config), action, target], cwd=BACKEND,
        env=_environment(raw, schema, **extra), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _old_database(raw: str, schema: str) -> None:
    code, output = _collect([_spawn(raw, schema, target="0034")])[0]
    assert code == 0, output
    _execute(raw, f'INSERT INTO "{schema}".users(id,username,password_hash) '
                  "VALUES(1,'c7-migration-test','hash')")


def _state(raw: str, schema: str) -> tuple:
    # 比较所有关系/字段、索引、约束以及既有用户数据，而不只比较版本号。
    columns = _execute(raw, """
        SELECT c.relkind::text,c.relname,a.attname,format_type(a.atttypid,a.atttypmod),
               a.attnotnull,coalesce(pg_get_expr(d.adbin,d.adrelid),'')
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
        WHERE n.nspname=:schema ORDER BY c.relname,a.attnum
    """, {"schema": schema})
    constraints = _execute(raw, """
        SELECT c.relname,k.conname,pg_get_constraintdef(k.oid)
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=:schema ORDER BY c.relname,k.conname
    """, {"schema": schema})
    indexes = _execute(raw, "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=:schema ORDER BY indexname",
                       {"schema": schema})
    tables = {row[1] for row in columns if row[0] in {"r", "p"}}
    version = (_execute(raw, f'SELECT version_num FROM "{schema}".alembic_version')
               if "alembic_version" in tables else [])
    users = (_execute(raw, f'SELECT id,username,password_hash FROM "{schema}".users ORDER BY id')
             if "users" in tables else [])
    return columns, constraints, indexes, version, users


@pytest.mark.parametrize("old_version", [False, True], ids=["empty", "old-with-data"])
def test_pg_three_processes_run_each_migration_once(pg_schema, old_version):
    raw, schema = pg_schema
    if old_version:
        _old_database(raw, schema)
    results = _collect([_spawn(raw, schema) for _ in range(3)])
    assert all(code == 0 for code, _ in results), results
    expected = OLD_UPGRADES if old_version else EMPTY_UPGRADES
    assert sum(output.count("Running upgrade") for _, output in results) == expected
    _, _, _, version, users = _state(raw, schema)
    assert version == [(HEAD,)]
    if old_version:
        assert users == [(1, "c7-migration-test", "hash")]


@pytest.mark.parametrize("old_version", [False, True], ids=["empty", "old-with-data"])
def test_pg_three_failed_migrators_leave_schema_and_data_intact(pg_schema, tmp_path, old_version):
    raw, schema = pg_schema
    if old_version:
        _old_database(raw, schema)
    before = _state(raw, schema)
    config = _fault_script(tmp_path, '        raise RuntimeError("c7 injected migration failure")\n')
    results = _collect([_spawn(raw, schema, config) for _ in range(3)])
    assert all(code != 0 and "c7 injected migration failure" in output for code, output in results)
    assert _state(raw, schema) == before


def test_pg_rejects_autocommit_block_before_any_changes_commit(pg_schema, tmp_path):
    raw, schema = pg_schema
    _old_database(raw, schema)
    before = _state(raw, schema)
    config = _fault_script(tmp_path, '        with op.get_context().autocommit_block():\n            pass\n')
    code, output = _collect([_spawn(raw, schema, config)])[0]
    assert code != 0 and "迁移不能使用自动提交块" in output
    assert _state(raw, schema) == before


def test_pg_rejects_abort_before_it_breaks_migration_atomicity(pg_schema, tmp_path):
    raw, schema = pg_schema
    _old_database(raw, schema)
    before = _state(raw, schema)
    injection = '''        op.execute("ABORT")
        op.execute("CREATE TABLE c7_escaped_transaction (id INTEGER)")
        raise RuntimeError("c7 failure after abort")
'''
    code, output = _collect([_spawn(raw, schema, _fault_script(tmp_path, injection))])[0]
    assert _state(raw, schema) == before
    assert code != 0 and "迁移不能自行变更事务" in output


async def _rollback_prepared_if_present(raw: str, gid: str) -> bool:
    # 若守卫回归且测试服务器启用了两阶段提交，也只清理本测试生成的事务。
    engine = create_async_engine(raw, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as db:
            found = (await db.execute(text(
                "SELECT 1 FROM pg_prepared_xacts WHERE database=current_database() AND gid=:gid"
            ), {"gid": gid})).first() is not None
            if found:
                await db.exec_driver_sql(f"ROLLBACK PREPARED '{gid}'")
            return found
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "statement",
    [
        "START TRANSACTION",
        "START /* comment */ TRANSACTION",
        "PREPARE TRANSACTION '{gid}'",
        "PREPARE /* outer /* inner */ outer */ TRANSACTION '{gid}'",
    ],
    ids=["start", "start-with-comment", "prepare", "prepare-with-nested-comment"],
)
def test_pg_rejects_start_or_prepare_transaction(pg_schema, tmp_path, statement):
    raw, schema = pg_schema
    _old_database(raw, schema)
    before = _state(raw, schema)
    gid = f"c7_{uuid4().hex}"
    sql = statement.format(gid=gid)
    injection = (
        f'        op.execute({sql!r})\n'
        '        op.execute("CREATE TABLE c7_escaped_transaction (id INTEGER)")\n'
        '        raise RuntimeError("c7 failure after transaction command")\n'
    )
    try:
        code, output = _collect([_spawn(raw, schema, _fault_script(tmp_path, injection))])[0]
    finally:
        prepared = asyncio.run(_rollback_prepared_if_present(raw, gid))
    assert not prepared, "迁移留下已准备但尚未提交的事务"
    assert _state(raw, schema) == before
    assert code != 0 and "迁移不能自行变更事务" in output


def test_pg_allows_regular_prepared_statement(pg_schema, tmp_path):
    raw, schema = pg_schema
    _old_database(raw, schema)
    injection = '''        if table == _ENTRIES[0]:
            op.execute("PREPARE c7_statement AS SELECT 1")
            op.execute("DEALLOCATE c7_statement")
'''
    code, output = _collect([_spawn(raw, schema, _fault_script(tmp_path, injection), target="0037")])[0]
    assert code == 0, output


@pytest.mark.parametrize(
    "statement",
    [";COMMIT", "/* lead */ ; COMMIT", "\ufeffCOMMIT", " \ufeff ; /* lead */ ; -- note\n COMMIT"],
    ids=["semicolon", "comment-semicolon", "bom", "mixed"],
)
def test_pg_rejects_transaction_escape_with_sql_prefix(pg_schema, tmp_path, statement):
    raw, schema = pg_schema
    _old_database(raw, schema)
    before = _state(raw, schema)
    injection = f'        op.execute({statement!r})\n        raise RuntimeError("c7 failure after commit")\n'
    code, output = _collect([_spawn(raw, schema, _fault_script(tmp_path, injection))])[0]
    assert code != 0 and "迁移不能自行变更事务" in output
    assert _state(raw, schema) == before


@pytest.mark.parametrize(
    "statement",
    [
        "/* outer /* inner */ outer */ COMMIT",
        "/* a /* b /* c */ b */ a */ COMMIT",
        "-- before\n /* outer /* inner */ outer */ COMMIT",
        "-- before\rCOMMIT",
    ],
    ids=["two-levels", "three-levels", "line-and-nested", "carriage-return"],
)
def test_pg_rejects_transaction_escape_after_nested_comments(pg_schema, tmp_path, statement):
    raw, schema = pg_schema
    _old_database(raw, schema)
    before = _state(raw, schema)
    injection = f'        op.execute({statement!r})\n        raise RuntimeError("c7 failure after commit")\n'
    code, output = _collect([_spawn(raw, schema, _fault_script(tmp_path, injection))])[0]
    assert code != 0
    assert _state(raw, schema) == before
    assert "迁移不能自行变更事务" in output


def test_pg_allows_ddl_with_nested_comment_prefix(pg_schema, tmp_path):
    raw, schema = pg_schema
    _old_database(raw, schema)
    injection = '''        if table == _ENTRIES[0]:
            op.execute("/* outer /* inner */ outer */ CREATE TABLE c7_nested_ddl (id INTEGER)")
'''
    code, output = _collect([_spawn(raw, schema, _fault_script(tmp_path, injection), target="0037")])[0]
    assert code == 0, output
    assert _execute(raw, "SELECT to_regclass(:name)", {"name": f"{schema}.c7_nested_ddl"})[0][0] is not None


def test_pg_killed_migrator_releases_lock_after_waiting_over_five_seconds(pg_schema, tmp_path):
    raw, schema = pg_schema
    _old_database(raw, schema)
    config = _fault_script(tmp_path, '''        if (marker := __import__("os").environ.get("C7_TEST_READY")):
            __import__("pathlib").Path(marker).write_text("ready")
            __import__("time").sleep(120)
''')
    marker = tmp_path / "ready"
    app_name = f"c7-waiter-{uuid4().hex}"
    leader = _spawn(raw, schema, config, C7_TEST_READY=str(marker))
    waiters = []
    try:
        deadline = time.monotonic() + 45
        while not marker.exists():
            assert leader.poll() is None, leader.communicate()[1]
            assert time.monotonic() < deadline, "迁移没有到达加列后的屏障"
            time.sleep(0.05)
        # 不能在 DDL 持有 ACCESS EXCLUSIVE 锁时枚举完整结构；先观察版本行仍为旧值。
        assert _execute(raw, f'SELECT version_num FROM "{schema}".alembic_version') == [("0034",)]
        waiters = [_spawn(raw, schema, config, C7_PG_APP=app_name) for _ in range(2)]
        deadline = time.monotonic() + 20
        while True:
            count, = _execute(raw, """
                SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid
                WHERE a.application_name=:name AND l.locktype='advisory' AND NOT l.granted
            """, {"name": app_name})[0]
            if count == 2:
                break
            assert all(process.poll() is None for process in waiters)
            assert time.monotonic() < deadline, "后续进程没有实际等待 advisory lock"
            time.sleep(0.1)
        time.sleep(6.2)
        assert all(process.poll() is None for process in waiters)
        leader.kill()
        leader.communicate(timeout=10)
        results = _collect(waiters)
        assert all(code == 0 for code, _ in results), results
        assert sum(output.count("Running upgrade") for _, output in results) == OLD_UPGRADES
        assert _state(raw, schema)[3:] == ([(HEAD,)], [(1, "c7-migration-test", "hash")])
    finally:
        for process in [leader, *waiters]:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_pg_current_stamp_and_downgrade(pg_schema):
    raw, schema = pg_schema
    code, output = _collect([_spawn(raw, schema, target="0034", action="stamp")])[0]
    assert code == 0, output
    assert _state(raw, schema)[3] == [("0034",)]
    code, output = _collect([_spawn(raw, schema, action="current")])[0]
    assert code == 0 and "0034" in output
    _execute(raw, f'DROP SCHEMA "{schema}" CASCADE')
    _execute(raw, f'CREATE SCHEMA "{schema}"')
    _old_database(raw, schema)
    code, output = _collect([_spawn(raw, schema, target="0033", action="downgrade")])[0]
    assert code == 0, output
    assert _state(raw, schema)[3:] == ([("0033",)], [(1, "c7-migration-test", "hash")])
