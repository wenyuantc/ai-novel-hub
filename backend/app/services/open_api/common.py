"""开放读取共用的 SQL 分页和领域响应适配。"""

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def page_models[T](db: AsyncSession, stmt: Select[tuple[T]], page: int, page_size: int) -> tuple[list[T], int]:
    """先计算过滤后总数，再仅加载当前页；保留调用方的业务排序。"""
    total = int(await db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0)
    rows = list(await db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)))
    return rows, total


def contains_literal(value: str) -> str:
    """LIKE 的用户输入按字面量匹配，保留 SQL 参数绑定。"""
    escaped = value.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
