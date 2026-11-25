"""迁移在真实独立进程间互斥，并在异常或持有者死亡后保持数据库完整。"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).resolve().parents[1]
ALEMBIC = BACKEND / "alembic.ini"
_SCRIPT = ScriptDirectory.from_config(Config(str(ALEMBIC)))
HEAD = _SCRIPT.get_current_head()
EMPTY_UPGRADES = len(list(_SCRIPT.iterate_revisions("head", "base")))
OLD_UPGRADES = len(list(_SCRIPT.iterate_revisions("head", "0034")))


def _environment(database: Path, **extra: str) -> dict[str, str]:
    # 不继承开发机的 DATABASE_URL、密钥或其他业务配置。
    env = {key: os.environ[key] for key in ("PATH", "TMPDIR", "SYSTEMROOT") if key in os.environ}
    env.update({
        "DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "PYTHONPATH": str(BACKEND),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        **extra,
    })
    return env


def _command(config: Path, target: str = "head") -> list[str]:
    return [sys.executable, "-m", "alembic", "-c", str(config), "upgrade", target]


def _run(database: Path, config: Path = ALEMBIC, target: str = "head") -> subprocess.CompletedProcess:
    return subprocess.run(
        _command(config, target), cwd=BACKEND, env=_environment(database),
        capture_output=True, text=True, timeout=120, check=False,
    )


def _invoke(database: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC), *arguments], cwd=BACKEND,
        env=_environment(database), capture_output=True, text=True, timeout=120, check=False,
    )


def _spawn(database: Path, config: Path = ALEMBIC, **extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        _command(config), cwd=BACKEND, env=_environment(database, **extra),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _collect(processes: list[subprocess.Popen]) -> list[tuple[int, str]]:
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=120)
            results.append((process.returncode, stdout + stderr))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    return results


def _old_database(database: Path) -> None:
    result = _run(database, target="0034")
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'c7-migration-test','hash')")


def _state(database: Path) -> tuple[list[tuple], list[tuple], list[tuple]]:
    with sqlite3.connect(database) as conn:
        schema = conn.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        version = conn.execute("SELECT version_num FROM alembic_version").fetchall() if any(
            row[1] == "alembic_version" for row in schema
        ) else []
        users = conn.execute("SELECT id,username,password_hash FROM users ORDER BY id").fetchall() if any(
            row[1] == "users" for row in schema
        ) else []
        return schema, version, users


def _fault_script(tmp_path: Path, injection: str) -> Path:
    """只在测试临时副本中，在真实历史迁移的首条 DDL 之后注入故障。"""
    script = tmp_path / "alembic-copy"
    shutil.copytree(BACKEND / "alembic", script, ignore=shutil.ignore_patterns("__pycache__"))
    migration = script / "versions" / "0037_setting_ai_visibility.py"
    source = migration.read_text()
    line = '        op.add_column(table, sa.Column("ai_mode", sa.String(16), nullable=True))\n'
    assert source.count(line) == 1
    migration.write_text(source.replace(line, line + injection))
    config = tmp_path / "alembic.ini"
    config.write_text(ALEMBIC.read_text().replace("script_location = %(here)s/alembic", f"script_location = {script}"))
    return config


@pytest.mark.parametrize("old_version", [False, True], ids=["empty", "old-with-data"])
def test_sqlite_failure_rolls_back_entire_upgrade(tmp_path, old_version):
    database = tmp_path / "main.db"
    if old_version:
        _old_database(database)
    before = _state(database)
    config = _fault_script(tmp_path, '        raise RuntimeError("c7 injected migration failure")\n')
    results = _collect([_spawn(database, config) for _ in range(3)])
    assert all(code != 0 and "c7 injected migration failure" in output for code, output in results)
    assert _state(database) == before


@pytest.mark.parametrize(
    ("injection", "message"),
    [
        ('        op.get_bind().commit()\n', "迁移不能自行提交或回滚事务"),
        ('        op.execute("/* comment */ COMMIT")\n', "迁移不能自行变更事务"),
        ('        with op.get_context().autocommit_block():\n            pass\n', "迁移不能使用自动提交块"),
    ],
    ids=["connection-commit", "sql-commit", "autocommit-block"],
)
def test_sqlite_rejects_transaction_escape_before_committing(tmp_path, injection, message):
    database = tmp_path / "main.db"
    _old_database(database)
    before = _state(database)
    result = _run(database, _fault_script(tmp_path, injection))
    assert result.returncode != 0 and message in result.stderr
    assert _state(database) == before


@pytest.mark.parametrize(
    "statement",
    [
        ";COMMIT",
        "/* lead */ ; COMMIT",
        "\ufeffCOMMIT",
        " \ufeff ; /* lead */ ; -- note\n COMMIT",
        ";/* lead */;\ufeffCOMMIT",
        "\ufeff;/* lead */ COMMIT",
        "/* outer /* inner */ ; COMMIT",
        "/* outer /* inner */ ; /* next */ COMMIT",
    ],
    ids=["semicolon", "comment-semicolon", "bom", "mixed", "semicolon-comment-bom", "bom-comment",
         "sqlite-comment-boundary", "sqlite-comment-then-comment"],
)
def test_sqlite_rejects_transaction_escape_with_sql_prefix(tmp_path, statement):
    database = tmp_path / "main.db"
    _old_database(database)
    before = _state(database)
    injection = f'        op.execute({statement!r})\n        raise RuntimeError("c7 failure after commit")\n'
    result = _run(database, _fault_script(tmp_path, injection))
    assert result.returncode != 0
    assert _state(database) == before
    assert "迁移不能自行变更事务" in result.stderr


def test_sqlite_allows_trigger_body_with_transaction_keywords(tmp_path):
    database = tmp_path / "main.db"
    _old_database(database)
    config = _fault_script(tmp_path, '''        if table == _ENTRIES[0]:
            op.execute("-- harmless\\rCOMMIT")
            op.execute("; /* lead */ CREATE TABLE c7_trigger_log (id INTEGER)")
            op.execute("; /* lead */ CREATE TRIGGER c7_trigger AFTER INSERT ON c7_trigger_log "
                       "BEGIN UPDATE c7_trigger_log SET id = id + 1 WHERE id = NEW.id; END")
''')
    result = _run(database, config)
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO c7_trigger_log(id) VALUES(7)")
        assert conn.execute("SELECT id FROM c7_trigger_log").fetchall() == [(8,)]


def test_sqlite_foreign_key_validation_rolls_back_all_migrations(tmp_path):
    database = tmp_path / "main.db"
    setup = _run(database, target="0069")
    assert setup.returncode == 0, setup.stderr
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'c7-migration-test','hash')")
    before = _state(database)
    config = _fault_script(tmp_path, "")
    migration = tmp_path / "alembic-copy/versions/0070_task_drain_setting.py"
    source = migration.read_text()
    assert source.count("    pass\n") == 1
    injection = '''    op.execute("CREATE TABLE c7_parent (id INTEGER PRIMARY KEY)")
    op.execute("CREATE TABLE c7_child (parent_id INTEGER REFERENCES c7_parent(id))")
    op.execute("INSERT INTO c7_child(parent_id) VALUES (123)")
'''
    migration.write_text(source.replace("    pass\n", injection))
    result = _run(database, config, target="0070")
    assert result.returncode != 0 and "迁移后的 SQLite 外键校验失败" in result.stderr
    assert _state(database) == before


@pytest.mark.parametrize("old_version", [False, True], ids=["empty", "old-with-data"])
def test_sqlite_three_processes_run_each_migration_once(tmp_path, old_version):
    database = tmp_path / "main.db"
    if old_version:
        _old_database(database)
    results = _collect([_spawn(database) for _ in range(3)])
    assert all(code == 0 for code, _ in results), results
    expected = OLD_UPGRADES if old_version else EMPTY_UPGRADES
    assert sum(output.count("Running upgrade") for _, output in results) == expected
    _, version, users = _state(database)
    assert version == [(HEAD,)]
    if old_version:
        assert users == [(1, "c7-migration-test", "hash")]
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_sqlite_killed_migrator_rolls_back_before_waiters_resume(tmp_path):
    database = tmp_path / "main.db"
    _old_database(database)
    before = _state(database)
    config = _fault_script(tmp_path, '''        if (marker := __import__("os").environ.get("C7_TEST_READY")):
            __import__("pathlib").Path(marker).write_text("ready")
            __import__("time").sleep(120)
''')
    marker = tmp_path / "ready"
    leader = _spawn(database, config, C7_TEST_READY=str(marker))
    waiters = []
    try:
        deadline = time.monotonic() + 45
        while not marker.exists():
            assert leader.poll() is None, leader.communicate()[1]
            assert time.monotonic() < deadline, "迁移没有到达加列后的屏障"
            time.sleep(0.05)
        assert _state(database) == before
        waiters = [_spawn(database, config) for _ in range(2)]
        leader.kill()
        leader.communicate(timeout=10)
        results = _collect(waiters)
        assert all(code == 0 for code, _ in results), results
        assert sum(output.count("Running upgrade") for _, output in results) == OLD_UPGRADES
        assert _state(database)[1:] == ([(HEAD,)], [(1, "c7-migration-test", "hash")])
        with sqlite3.connect(database) as conn:
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        for process in [leader, *waiters]:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_sqlite_waits_for_migration_lock_longer_than_five_seconds(tmp_path):
    database = tmp_path / "locked.db"
    lock = sqlite3.connect(database)
    lock.execute("BEGIN IMMEDIATE")
    process = _spawn(database)
    try:
        time.sleep(6.2)
        assert process.poll() is None, "迁移在等待锁时提前退出"
    finally:
        lock.rollback()
        lock.close()
    try:
        stdout, stderr = process.communicate(timeout=120)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert process.returncode == 0, stderr + stdout
    assert _state(database)[1] == [(HEAD,)]


def test_sqlite_current_stamp_downgrade_and_offline_sql(tmp_path):
    database = tmp_path / "online.db"
    assert _run(database, target="0034").returncode == 0
    current = _invoke(database, "current")
    assert current.returncode == 0 and "0034" in current.stdout
    downgrade = _invoke(database, "downgrade", "0033")
    assert downgrade.returncode == 0, downgrade.stderr
    assert _state(database)[1] == [("0033",)]

    stamped = tmp_path / "stamp.db"
    stamp = _invoke(stamped, "stamp", "0034")
    assert stamp.returncode == 0, stamp.stderr
    assert _state(stamped)[1] == [("0034",)]
    current = _invoke(stamped, "current")
    assert current.returncode == 0 and "0034" in current.stdout

    absent = tmp_path / "no-connection.db"
    offline = _invoke(absent, "upgrade", "0001", "--sql")
    assert offline.returncode == 0, offline.stderr
    assert "CREATE TABLE users" in offline.stdout
    assert not absent.exists()


def test_postgresql_offline_sql_does_not_connect(tmp_path):
    # 不存在的端口确保离线生成 SQL 没有意外打开数据库连接。
    env = _environment(tmp_path / "unused.db", DATABASE_URL="postgresql+asyncpg://fake:fake@127.0.0.1:1/ainovel_test")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC), "upgrade", "0001", "--sql"], cwd=BACKEND,
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE users" in result.stdout
