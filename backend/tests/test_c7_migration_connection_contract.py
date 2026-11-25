"""迁移的 DDL 必须沿用 Alembic 连接。

这是一项静态约束：禁止迁移直接调用已知的连接、引擎或会话创建入口。
它不能证明动态查找、任意辅助函数或独立进程不会访问数据库，评审仍须检查这些路径。
已审查的 SQLite 租约生命周期守卫是例外：它们只读主库，并锁住配对的生命周期文件。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
_SQLALCHEMY_FACTORIES = frozenset({
    "create_engine", "engine_from_config", "create_mock_engine", "create_async_engine",
    "async_engine_from_config", "create_pool_from_url", "sessionmaker", "async_sessionmaker",
    "Session", "AsyncSession",
})
_DRIVER_FACTORIES = frozenset({
    "connect", "Connection", "AsyncConnection", "create_pool", "ConnectionPool", "AsyncConnectionPool",
    "SimpleConnectionPool", "ThreadedConnectionPool",
})
_DRIVER_MODULES = frozenset({"sqlite3", "aiosqlite", "asyncpg", "psycopg", "psycopg2", "psycopg_pool", "pg8000"})
_CONNECTION_ATTRIBUTES = frozenset({"connect", "raw_connection", "begin"})
_LEASE_MODULE = "app.services.task_engine.sqlite_lease_pair"
_LEASE_GUARDS = frozenset({"guard_downgrade", "guard_memory_migration"})


def _qualified_name(node: ast.AST, imports: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return imports.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value, imports)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _connection_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    imports: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                imports[item.asname or item.name.split(".")[0]] = item.name if item.asname else item.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                imports[item.asname or item.name] = f"{node.module}.{item.name}"

    errors = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _CONNECTION_ATTRIBUTES:
            errors.append(f"{node.lineno}: 不能直接获取另一连接或启动独立事务: {node.attr}")
            continue
        if not isinstance(node, ast.Name | ast.Attribute) or not isinstance(node.ctx, ast.Load):
            continue
        name = _qualified_name(node, imports)
        if name is None:
            continue
        module, _, member = name.rpartition(".")
        root = name.split(".", 1)[0]
        if (root == "sqlalchemy" and member in _SQLALCHEMY_FACTORIES) or (
            root in _DRIVER_MODULES and member in _DRIVER_FACTORIES
        ):
            errors.append(f"{node.lineno}: 不能创建数据库连接、引擎或会话: {name}")
        elif module == _LEASE_MODULE and member not in _LEASE_GUARDS:
            errors.append(f"{node.lineno}: SQLite 租约辅助调用尚未纳入审查: {name}")
    return errors


def test_all_migrations_use_the_alembic_connection():
    paths = sorted(_VERSIONS.glob("*.py"))
    assert paths, "没有找到 Alembic 迁移文件"
    errors = {
        path.name: issues for path in paths
        if (issues := _connection_violations(path.read_text(encoding="utf-8")))
    }
    assert not errors, errors


@pytest.mark.parametrize(
    "source",
    [
        "import sqlalchemy as sa\nsa.create_engine('sqlite:///db')",
        "import sqlalchemy as sa\nfactory = sa.create_engine\nfactory('sqlite:///db')",
        "from sqlalchemy import create_engine as factory\nfactory('sqlite:///db')",
        "from sqlalchemy.ext.asyncio import create_async_engine as factory\nfactory('sqlite+aiosqlite:///db')",
        "from sqlalchemy.ext.asyncio import async_engine_from_config as factory\nfactory({})",
        "import sqlite3 as driver\ndriver.connect('db')",
        "from sqlite3 import connect as factory\nfactory('db')",
        "from asyncpg import connect as factory\nfactory('postgresql://db')",
        "from psycopg_pool import ConnectionPool as factory\nfactory('postgresql://db')",
        "from sqlalchemy.orm import sessionmaker as factory\nfactory()",
        "conn = op.get_bind().engine.connect()",
        "factory = op.get_bind().engine.connect\nconn = factory()",
        "conn = op.get_bind().engine.raw_connection()",
        "with op.get_bind().engine.begin():\n    pass",
        "from app.services.task_engine.sqlite_lease_pair import _connect as factory\nfactory(path, 'rw')",
        "from app.services.task_engine import sqlite_lease_pair as pair\npair._connect(path, 'rw')",
    ],
    ids=["sqlalchemy", "factory-assignment", "sqlalchemy-alias", "async-engine", "async-config", "sqlite",
         "sqlite-alias", "asyncpg", "driver-pool", "session", "engine-connect", "engine-connect-alias",
         "raw-connection", "engine-begin", "lease-private-alias", "lease-private-module"],
)
def test_direct_connection_creation_is_rejected(source):
    assert _connection_violations(source)


def test_alembic_connection_and_reviewed_sqlite_lease_guards_are_allowed():
    source = '''
from alembic import op
import sqlalchemy as sa
from app.services.task_engine.sqlite_lease_pair import guard_downgrade as guard
from app.services.task_engine import sqlite_lease_pair as pair
db = op.get_bind()
db.execute(sa.text("CREATE TABLE example (id INTEGER)"))
guards = guard(path)
sa.event.listen(db.engine.pool, "checkin", lambda *_args: guards.close(), once=True)
with pair.guard_memory_migration(db):
    pass
'''
    assert _connection_violations(source) == []
