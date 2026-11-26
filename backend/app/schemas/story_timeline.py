"""作品历法与章景日期的读写契约。"""

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator


def _text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("名称不能包含空字符")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("名称包含无效字符") from exc
    return value


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64), AfterValidator(_text)]
MonthId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
Positive = Annotated[int, Field(strict=True, ge=1, le=999999)]
Kind = Literal["chapter", "scene"]


class StoryMonth(BaseModel):
    id: MonthId
    name: Name
    days: Positive


class StoryDate(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    year: Positive
    month_id: MonthId
    day: Positive


class CalendarOut(BaseModel):
    revision: int
    era_name: str | None
    months: list[StoryMonth]


class CalendarWrite(BaseModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    era_name: Name
    months: list[StoryMonth] = Field(min_length=1, max_length=128)
    confirm_reorder: bool = False

    @model_validator(mode="after")
    def unique_months(self) -> Self:
        if len({m.id for m in self.months}) != len(self.months):
            raise ValueError("月份标识不得重复")
        return self


class DateWrite(BaseModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    date: StoryDate | None


class TimelineItem(BaseModel):
    kind: Kind
    chapter_id: int
    chapter_no: int
    chapter_title: str
    scene_id: int | None
    scene_sort: int | None
    scene_title: str | None
    active: bool
    explicit_date: StoryDate | None
    effective_date: StoryDate | None
    source: Literal["chapter", "scene", "inherited", "unknown"]
    date_label: str | None
    revision: int


class CalendarImpact(BaseModel):
    blocked: list[TimelineItem]
    reordered: list[TimelineItem]


class TimelinePage(BaseModel):
    total: int
    items: list[TimelineItem]
    revision: int


class CalendarGenerateRequest(BaseModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    instruction: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000), AfterValidator(_text)] = ""
    ai_config_id: int | None = Field(None, gt=0, le=2_147_483_647, strict=True)
