"""连载工作台中与登录者绑定的写作量、连续天数及个人目标。"""

from datetime import UTC, date, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Project
from app.models.writing_stats import WritingDailyStat, WritingGoal, WritingStatsTracking
from app.schemas.writing_stats import DailyWords, GoalOut, GoalUpdate, Overview
from app.services import project_access
from app.services.audit_writer import AuditOperation
from app.services.project_lifecycle import as_utc
from app.services.serialization_read import BEIJING, workbench_read_snapshot


def _now() -> datetime:
    return datetime.now(UTC)


def _goal(row: WritingGoal | None) -> GoalOut:
    if row is None:
        return GoalOut(period=None, target_words=None, revision=0)
    return GoalOut.model_validate(row, from_attributes=True)


def _range(start: date | None, end: date | None, today: date) -> tuple[date, date]:
    end = end or today
    start = start or date.fromordinal(max(date.min.toordinal(), end.toordinal() - 29))
    if start > end or end > today or (end - start).days >= 366:
        raise HTTPException(422, "日期范围须按先后排列，最多 366 天，且不能包含未来日期")
    return start, end


async def _streak(db: AsyncSession, user_id: int, project_id: int, today: date) -> int:
    days = await db.stream_scalars(select(WritingDailyStat.day).where(
        WritingDailyStat.user_id == user_id, WritingDailyStat.project_id == project_id,
        WritingDailyStat.day <= today, WritingDailyStat.manual_words > 0,
    ).order_by(WritingDailyStat.day.desc()).execution_options(yield_per=64))
    expected = today.toordinal()
    count = 0
    try:
        async for day in days:
            if count == 0 and day.toordinal() == expected - 1:
                expected -= 1
            if day.toordinal() != expected:
                break
            count += 1
            expected -= 1
    finally:
        await days.close()
    return count


async def overview(
    db: AsyncSession, user_id: int, project_id: int, start_date: date | None, end_date: date | None,
) -> Overview:
    async with workbench_read_snapshot(db, user_id, project_id) as reader:
        today = _now().astimezone(BEIJING).date()
        start, end = _range(start_date, end_date, today)
        dates = (await reader.execute(
            select(WritingStatsTracking.started_at, Project.created_at, Project.writing_stats_started_at)
            .join_from(WritingStatsTracking, Project, Project.id == project_id)
            .where(WritingStatsTracking.id == 1)
        )).one()
        started_at = max(as_utc(dates.started_at), as_utc(dates.writing_stats_started_at or dates.created_at))
        first = started_at.astimezone(BEIJING).date()
        goal = _goal(await reader.get(WritingGoal, (user_id, project_id)))
        period_start = today - timedelta(days=today.weekday()) if goal.period == "week" else today
        # 只读筛选区间和当前周期，查看很早的趋势也不加载两段之间的历史。
        rows = await reader.execute(select(
            WritingDailyStat.day, WritingDailyStat.manual_words, WritingDailyStat.ai_words,
        ).where(
            WritingDailyStat.user_id == user_id, WritingDailyStat.project_id == project_id,
            WritingDailyStat.day.between(start, end) | WritingDailyStat.day.between(period_start, today),
        ))
        totals = {row.day: (row.manual_words, row.ai_words) for row in rows}
        days = []
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            manual, ai = totals.get(day, (0, 0)) if day >= first else (None, None)
            days.append(DailyWords(date=day, manual_words=manual, ai_words=ai, partial=day in (first, today)))
        manual_today, ai_today = totals.get(today, (0, 0))
        return Overview(
            tracking_started_at=started_at, today=today, start_date=start, end_date=end, days=days, goal=goal,
            today_manual_words=manual_today, today_ai_words=ai_today,
            period_manual_words=sum(count for day, (count, _) in totals.items() if period_start <= day <= today),
            streak_days=await _streak(reader, user_id, project_id, today),
        )


async def set_goal(db: AsyncSession, user_id: int, project_id: int, body: GoalUpdate) -> GoalOut:
    async with AuditOperation(db, user_id, "project.update", "project", project_id, project_id=project_id) as audit:
        project = await project_access.require_project(db, user_id, project_id, "content.edit", for_update=True)
        audit.target(project, label="设置个人码字目标")
        row = await db.get(WritingGoal, (user_id, project_id), populate_existing=True)
        goal = _goal(row)
        if goal.revision != body.expected_revision:
            raise ConflictError("码字目标已变化，请刷新后重试", extra={
                "code": "stale_writing_goal", "current_revision": goal.revision,
            })
        if (goal.period, goal.target_words) != (body.period, body.target_words):
            if row is None:
                row = WritingGoal(user_id=user_id, project_id=project_id)
                db.add(row)
            row.period, row.target_words, row.revision = body.period, body.target_words, goal.revision + 1
        result = _goal(row)
        await audit.commit()
        return result
