"""Agent 快照事务使用固定数据库视图，不将并发请求的修改误计为自身效果。"""

from sqlalchemy.exc import DBAPIError

from app.core.errors import ConflictError

CONCURRENT_CHANGE = "数据同时被其他操作修改，请刷新预览后重试"


def session(sessionmaker):
    bind = sessionmaker.kw["bind"]
    if bind.dialect.name == "postgresql":
        return sessionmaker(bind=bind.execution_options(isolation_level="REPEATABLE READ"))
    return sessionmaker()


def is_serialization_error(exc: DBAPIError) -> bool:
    original = exc.orig
    return getattr(original, "sqlstate", None) == "40001" or getattr(original, "pgcode", None) == "40001"


async def rollback_conflict(db, exc: DBAPIError) -> None:
    if is_serialization_error(exc):
        await db.rollback()
        raise ConflictError(CONCURRENT_CHANGE) from exc
    raise exc
