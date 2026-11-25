import asyncio
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from logging.config import fileConfig

from alembic import context
from sqlalchemy import event, pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, async_engine_from_config

from app.models import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
logger = logging.getLogger("alembic.runtime.migration")

# 固定项目专用键，同一个数据库的所有在线迁移使用同一把锁。
_MIGRATION_LOCK_KEY = 0x41494E4F56454C
_TRANSACTION_CONTROL = re.compile(r"^(BEGIN|COMMIT|END|ROLLBACK|ABORT|START|PREPARE)\b", re.IGNORECASE)
_SQLITE_FK_CHANGE = re.compile(r"^PRAGMA\s+(?:main\.)?foreign_keys\s*(?:=|\()", re.IGNORECASE)


def _database_url() -> str:
    # 优先使用调用方显式设置的 URL（测试用），其次环境变量
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL")
    if not url:
        try:
            from app.core.config import get_settings

            url = get_settings().database_url
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("未设置 DATABASE_URL，无法执行迁移") from exc
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _without_sql_prefix(statement: str, dialect: str) -> str:
    """按数据库的注释语义忽略前缀，仅检查首条实际语句。"""
    position = 0
    while position < len(statement):
        character = statement[position]
        if character.isspace() or character in ";\ufeff":
            position += 1
            continue
        if statement.startswith("--", position):
            position += 2
            # PostgreSQL 将单独的 CR 和 LF 都视为行结束；SQLite 只以 LF 结束注释。
            line_ends = "\r\n" if dialect == "postgresql" else "\n"
            while position < len(statement) and statement[position] not in line_ends:
                position += 1
            continue
        if statement.startswith("/*", position):
            position += 2
            depth = 1
            while position < len(statement) and depth:
                if dialect == "postgresql" and statement.startswith("/*", position):
                    depth += 1
                    position += 2
                elif statement.startswith("*/", position):
                    depth -= 1
                    position += 2
                else:
                    position += 1
            continue
        break
    return statement[position:]


def _is_transaction_control(sql: str, dialect: str) -> bool:
    match = _TRANSACTION_CONTROL.match(sql)
    if not match:
        return False
    if match.group(1).upper() in {"START", "PREPARE"}:
        # PREPARE 也能声明普通预备语句，只阻止修改事务边界的两种形式。
        tail = _without_sql_prefix(sql[match.end():], dialect)
        return bool(re.match(r"^TRANSACTION\b", tail, re.IGNORECASE))
    return True


@contextmanager
def _protect_transaction(connection: Connection):
    """迁移只能在调用方持有的事务中完成，不能中途提交或关闭事务。"""
    def reject_transaction(_connection):
        raise RuntimeError("迁移不能自行提交或回滚事务")

    def reject_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
        sql = _without_sql_prefix(statement, connection.dialect.name)
        if _is_transaction_control(sql, connection.dialect.name) or (
            connection.dialect.name == "sqlite" and _SQLITE_FK_CHANGE.match(sql)
        ):
            raise RuntimeError("迁移不能自行变更事务或 SQLite 外键开关")

    event.listen(connection, "commit", reject_transaction)
    event.listen(connection, "rollback", reject_transaction)
    event.listen(connection, "before_cursor_execute", reject_sql)
    try:
        yield
    finally:
        event.remove(connection, "before_cursor_execute", reject_sql)
        event.remove(connection, "rollback", reject_transaction)
        event.remove(connection, "commit", reject_transaction)


def _reject_autocommit_block():
    raise RuntimeError("迁移不能使用自动提交块；请先重新设计迁移原子性")


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, render_as_batch=True, transactional_ddl=True,
    )
    # 历史 SQLite 迁移仅在 FK 开启时进入自动提交块；迁移专用连接已在事务外关闭 FK。
    # 之后新增迁移若试图提交或进入自动提交块，必须在任何提交发生前失败。
    context.get_context().autocommit_block = _reject_autocommit_block
    with _protect_transaction(connection), context.begin_transaction():
        context.run_migrations()


def _sqlite_busy(exc: OperationalError) -> bool:
    return getattr(exc.orig, "sqlite_errorcode", 0) & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


async def _sqlite_begin(connection: AsyncConnection) -> None:
    while True:
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            return
        except OperationalError as exc:
            await connection.rollback()
            if not _sqlite_busy(exc):
                raise
            logger.info("等待 SQLite 迁移写锁")
            await asyncio.sleep(0.1)


async def _sqlite_migrations(connection: AsyncConnection) -> None:
    # SQLite 的 FK 开关不能在真实事务内变更；迁移引擎只用于此专用连接。
    await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if (await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one() != 0:
        raise RuntimeError("无法关闭迁移连接的 SQLite 外键开关")
    await connection.commit()  # 清除 SQLAlchemy 的隐式事务，随后显式建立真实事务。
    await _sqlite_begin(connection)
    try:
        await connection.run_sync(do_run_migrations)
        if (await connection.exec_driver_sql("PRAGMA foreign_key_check")).first() is not None:
            raise RuntimeError("迁移后的 SQLite 外键校验失败")
        await connection.commit()
    except BaseException:
        await connection.rollback()
        raise


async def _postgresql_migrations(connection: AsyncConnection) -> None:
    # 使用同一实际会话持锁、迁移和释放；事务池代理必须改为直连或会话池。
    lock = {"key": _MIGRATION_LOCK_KEY}
    logger.info("等待 PostgreSQL 迁移锁")
    await connection.execute(text("SELECT pg_advisory_lock(:key)"), lock)
    try:
        await connection.run_sync(do_run_migrations)
        await connection.commit()
    except BaseException:
        try:
            await connection.rollback()
            await connection.execute(text("SELECT pg_advisory_unlock(:key)"), lock)
            await connection.commit()
        except Exception:  # 关闭专用连接也会释放会话锁，保留原迁移错误。
            logger.warning("释放迁移锁失败，将关闭数据库连接")
        raise
    unlocked = (await connection.execute(text("SELECT pg_advisory_unlock(:key)"), lock)).scalar_one()
    if not unlocked:
        raise RuntimeError("无法确认 PostgreSQL 迁移锁已释放")
    await connection.commit()


async def run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            if connection.dialect.name == "sqlite":
                await _sqlite_migrations(connection)
            elif connection.dialect.name == "postgresql":
                await _postgresql_migrations(connection)
            else:
                raise RuntimeError("迁移仅支持 SQLite 和 PostgreSQL")
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
