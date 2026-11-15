import asyncio
from contextlib import suppress

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.authorization_runtime import AuthorizationRuntime


def create_engine(database_url: str) -> AsyncEngine:
    kwargs: dict = {"pool_pre_ping": True}
    engine = create_async_engine(database_url, **kwargs)
    if engine.dialect.name == "sqlite":
        # SQLite 默认不启用外键约束，开启后 ON DELETE CASCADE / SET NULL 才生效
        @event.listens_for(engine.sync_engine, "connect")
        def _fk_pragma(dbapi_conn, _record):  # pragma: no cover - 简单钩子
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(engine.sync_engine, "handle_error")
        def _release_cancelled(context):
            _release_cancelled_sqlite(context)

    return engine


def _release_cancelled_sqlite(context) -> None:
    """取消打断 aiosqlite 语句时，先在驱动线程 finalize 该游标并回滚，再让 SQLAlchemy 作废连接。

    SQLAlchemy 把 CancelledError 视为退出异常：既不关闭游标也不做 reset 回滚，直接作废并 terminate。
    被打断的语句仍在驱动线程里执行完，其游标被异常回溯引用而未 finalize；sqlite3 关闭带未
    finalize 语句的连接只会变成「僵尸连接」，库锁一直保留到游标被 GC 回收（只回滚仍保留读锁，
    其他连接无法提交），占用回退、任务收尾因此反复 database is locked、任务永久停在 running。
    两个操作按序排在被打断语句之后、关闭之前执行；只撤销本就要随连接作废丢弃的未提交事务。
    """
    if not isinstance(context.original_exception, asyncio.CancelledError) or context.connection is None:
        return
    # 处于 greenlet 内：操作同步入队到 aiosqlite 线程队列（先于随后的 terminate/close）；
    # 再次取消时已入队的操作仍会在线程中执行，CancelledError 原样向上传递。
    # ExceptionContextImpl 声明了 cursor 却未赋值；被打断的游标挂在执行上下文上。
    cursor = getattr(context.execution_context, "cursor", None)
    if cursor is not None:
        with suppress(Exception):
            cursor.close()
    try:
        dbapi_connection = context.connection.connection.dbapi_connection
    except Exception:  # noqa: BLE001
        return
    if dbapi_connection is not None:
        with suppress(Exception):
            dbapi_connection.rollback()


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False：提交后仍可读取属性，避免流式响应期间触发隐式查询
    return async_sessionmaker(engine, expire_on_commit=False, info={"authorization_runtime": AuthorizationRuntime()})
