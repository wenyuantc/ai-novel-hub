"""结算与预算变更共用事务锁，归档启用预算各周期的累计进度。"""

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, User
from app.models.ai_usage import AiBudgetPeriod, AiUsageBudget, AiUsageCall
from app.services.ai_usage_totals import BUDGET_TIMEZONE, budget_state, combined_cost, monthly_budget_window, sum_cost


async def lock_budget_scope(db: AsyncSession, *, user_id: int | None, project_id: int | None) -> None:
    # 锁定账号／作品本身才能覆盖尚无预算的情况，并与修改预算的授权锁保持相同顺序。
    # SQLite 的入账条件 UPDATE / 配置 UPSERT 已串行化写者。
    if db.get_bind().dialect.name == "postgresql":
        scope = select(User.id).where(User.id == user_id) if user_id is not None else (
            select(Project.id).where(Project.id == project_id)
        )
        await db.execute(scope.with_for_update(key_share=True))  # FOR NO KEY UPDATE，兼容普通外键引用。


async def _period_progress(
    db: AsyncSession, budget: AiUsageBudget, payer_id: int, started_at: datetime, *, exclude_call: str | None = None,
) -> AiBudgetPeriod:
    if budget.project_id is None:
        start, end = monthly_budget_window(started_at)
        period = start.astimezone(BUDGET_TIMEZONE).strftime("%Y-%m")
        scope = (AiUsageCall.started_at >= start, AiUsageCall.started_at < end)
    else:
        period = "all"
        scope = (AiUsageCall.project_id == budget.project_id, AiUsageCall.author_paid.is_(True))
    row = await db.get(AiBudgetPeriod, (budget.id, period), populate_existing=True)
    if row is None:
        stmt = select(*sum_cost(AiUsageCall.amount_nano, "spent")).where(
            *scope, AiUsageCall.payer_id == payer_id, AiUsageCall.currency == budget.currency,
            AiUsageCall.amount_nano.is_not(None),
        )
        if exclude_call is not None:
            stmt = stmt.where(AiUsageCall.id != exclude_call)
        spent = combined_cost((await db.execute(stmt)).mappings().one(), "spent")
        row = AiBudgetPeriod(budget_id=budget.id, period=period, spent_nano=min(spent, budget.limit_nano))
        db.add(row)
    return row


async def reset_progress(db: AsyncSession, budget: AiUsageBudget) -> None:
    await db.execute(delete(AiBudgetPeriod).where(AiBudgetPeriod.budget_id == budget.id))
    payer_id = budget.user_id
    if payer_id is None:
        payer_id = await db.scalar(select(Project.user_id).where(Project.id == budget.project_id))
    if payer_id is not None:
        await _period_progress(db, budget, payer_id, datetime.now(UTC))
        await db.flush()


async def record_settlement(db: AsyncSession, call: AiUsageCall, amount: int) -> None:
    from app.services.notification_service import deliver

    if not amount or call.payer_id is None or call.currency is None:
        return
    scopes = [(call.payer_id, None)]
    if call.author_paid and call.project_id is not None:
        scopes.append((None, call.project_id))
    # 所有结算先锁账号再锁作品。当前调用及这两个预算的累计与提醒同事务提交。
    for user_id, project_id in scopes:
        await lock_budget_scope(db, user_id=user_id, project_id=project_id)
        scope = AiUsageBudget.user_id == user_id if user_id is not None else AiUsageBudget.project_id == project_id
        budget = await db.scalar(select(AiUsageBudget).where(
            scope, AiUsageBudget.currency == call.currency,
        ).execution_options(populate_existing=True))
        if budget is None:
            continue
        row = await _period_progress(db, budget, call.payer_id, call.started_at, exclude_call=call.id)
        previous = budget_state(row.spent_nano, budget.limit_nano, budget.threshold_percent)
        row.spent_nano = min(row.spent_nano + amount, budget.limit_nano)
        current = budget_state(row.spent_nano, budget.limit_nano, budget.threshold_percent)
        if previous != current:
            await deliver(
                db, recipient_id=call.payer_id, kind="budget", variant=current, project_id=project_id,
                budget_id=budget.id, budget_key=budget.instance_key, currency=budget.currency,
                event_key=f"budget:{budget.id}:{budget.instance_key}:{row.period}:{current}",
            )
