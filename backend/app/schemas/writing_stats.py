"""作品内个人码字目标和按北京自然日汇总的写作量。"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, model_validator

from app.schemas.open_api import UtcTime
from app.schemas.serialization import WriteRequest
from app.schemas.setting_field_value import PositiveDbInt

Period = Literal["day", "week"]


class GoalUpdate(WriteRequest):
    period: Period | None
    target_words: PositiveDbInt | None

    @model_validator(mode="after")
    def check_goal(self):
        if (self.period is None) != (self.target_words is None):
            raise ValueError("目标周期和字数需要同时设置或同时清除")
        return self


class GoalOut(BaseModel):
    period: Period | None
    target_words: int | None
    revision: int


class DailyWords(BaseModel):
    date: date
    manual_words: int | None
    ai_words: int | None
    partial: bool


class Overview(BaseModel):
    tracking_started_at: UtcTime
    today: date
    start_date: date
    end_date: date
    today_manual_words: int
    today_ai_words: int
    goal: GoalOut
    period_manual_words: int
    streak_days: int
    days: list[DailyWords]
