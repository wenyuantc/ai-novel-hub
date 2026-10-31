"""管理员首页只读传输类型；缺失的历史观测与有效的零分别表示。"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

DashboardDays = Literal[7, 30, 90]


class DashboardTotals(BaseModel):
    users: int = Field(ge=0)
    projects: int = Field(ge=0)
    chapters: int = Field(ge=0)
    total_words: int = Field(ge=0)
    pending_submissions: int = Field(ge=0)


class DashboardMetadata(BaseModel):
    as_of: datetime
    tracking_since: datetime
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"


class AdminDashboardOverview(DashboardMetadata, DashboardTotals):
    pass


class AdminDashboardTasks(DashboardMetadata):
    baseline_tasks: int = Field(ge=0)
    new_tasks: int = Field(ge=0)
    queued: int = Field(ge=0)
    running: int = Field(ge=0)
    paused: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    interrupted: int = Field(ge=0)
    failed_24h: int = Field(ge=0)
    interrupted_24h: int = Field(ge=0)
    removed_unfinished: int = Field(ge=0)


class DashboardTaskDay(BaseModel):
    created: int = Field(default=0, ge=0)
    succeeded: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    cancelled: int = Field(default=0, ge=0)
    interrupted: int = Field(default=0, ge=0)


class DashboardHistoryPoint(BaseModel):
    day: date
    captured_at: datetime | None
    totals: DashboardTotals | None
    tasks: DashboardTaskDay | None
    partial: bool


class DashboardComparison(BaseModel):
    from_day: date
    to_day: date
    users: int | None
    projects: int | None
    chapters: int | None
    total_words: int | None


class AdminDashboardHistory(DashboardMetadata):
    days: DashboardDays
    start_date: date
    end_date: date
    points: list[DashboardHistoryPoint]
    comparison: DashboardComparison
