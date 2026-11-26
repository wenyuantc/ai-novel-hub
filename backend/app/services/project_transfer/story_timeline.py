"""导入前核对固定历法和显式日期。未标注的场景在新书中继续继承。"""

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from app.schemas.story_timeline import CalendarWrite, StoryDate

TIMELINE_TABLES = ("story_calendars", "chapter_story_dates", "scene_story_dates")


def validate_timeline_tables(
    tables: dict[str, list[dict[str, Any]]], project_id: int, error: Callable[[str], None],
) -> None:
    calendars = tables["story_calendars"]
    if len(calendars) > 1:
        error("每部作品只能有一份故事历法")
        return
    days_by_month = {}
    if calendars:
        row = calendars[0]
        source_id = row.get("project_id", project_id)
        if type(source_id) is not int or source_id != project_id:
            error("故事历法属于另一部作品")
            return
        try:
            calendar = CalendarWrite(expected_revision=0, era_name=row["era_name"], months=row["months"])
        except ValidationError:
            error("故事历法名称或月份设置无效")
            return
        days_by_month = {month.id: month.days for month in calendar.months}
        # 与正常编辑入口一致，导入只保存规范化字段。
        row["era_name"] = calendar.era_name
        row["months"] = [month.model_dump() for month in calendar.months]
    for table, key in (("chapter_story_dates", "chapter_id"), ("scene_story_dates", "scene_id")):
        seen = set()
        for i, row in enumerate(tables[table], start=1):
            if row[key] in seen:
                error(f"{table} 第 {i} 行重复登记 {key}")
            seen.add(row[key])
            try:
                date = StoryDate.model_validate(row)
            except ValidationError:
                error(f"{table} 第 {i} 行的年月日无效")
                continue
            if date.day > days_by_month.get(date.month_id, 0):
                error(f"{table} 第 {i} 行的故事日期与历法不符")
