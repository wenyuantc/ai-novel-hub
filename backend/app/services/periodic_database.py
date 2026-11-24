"""对允许重复、且每次使用新事务的后台维护操作处理短时写入竞争。"""

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import DBAPIError, OperationalError


async def retry_contended_write[Result](write: Callable[[], Awaitable[Result]]) -> Result:
    from app.services.ai_dispatch import is_sqlite_busy

    for delay in (0.025, 0.1):
        try:
            return await write()
        except DBAPIError as exc:
            state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
            if state not in ("40001", "40P01") and not (isinstance(exc, OperationalError) and is_sqlite_busy(exc)):
                raise
            await asyncio.sleep(delay)
    return await write()
