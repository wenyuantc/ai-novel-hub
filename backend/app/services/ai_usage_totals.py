"""预算和账单共用的整数金额运算。"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func

from app.models.ai_usage import NANOS_PER_UNIT
from app.schemas.ai_usage import BudgetState
from app.services.project_lifecycle import as_utc

BUDGET_TIMEZONE = ZoneInfo("Asia/Shanghai")


def monthly_budget_window(now: datetime) -> tuple[datetime, datetime]:
    local = as_utc(now).astimezone(BUDGET_TIMEZONE)
    first = datetime(local.year, local.month, 1, tzinfo=BUDGET_TIMEZONE)
    year, month = divmod(local.month, 12)
    next_first = datetime(local.year + year, month + 1, 1, tzinfo=BUDGET_TIMEZONE)
    return first.astimezone(UTC), next_first.astimezone(UTC)


def sum_cost(amount, prefix: str):
    # SQLite 的 SUM(int64) 可能溢出。先按整币和不足一币分开做整数加总，再在 Python 合并。
    unit = NANOS_PER_UNIT
    return (
        func.coalesce(func.sum(amount.op("/")(unit)), 0).label(f"{prefix}_whole"),
        func.coalesce(func.sum(amount % unit), 0).label(f"{prefix}_remainder"),
    )


def combined_cost(row, prefix: str) -> int:
    return int(row[f"{prefix}_whole"]) * NANOS_PER_UNIT + int(row[f"{prefix}_remainder"])


def budget_state(spent: int, limit: int, threshold_percent: int) -> BudgetState:
    if spent >= limit:
        return "exceeded"
    if spent * 100 >= limit * threshold_percent:
        return "near"
    return "normal"
