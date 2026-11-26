"""作品连载排期、定稿及手动发布的传输契约。"""

import re
from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from app.schemas.chapter import ChapterStatus
from app.schemas.open_api import UtcTime
from app.schemas.reader import ReaderBlock
from app.schemas.setting_field_value import PositiveDbInt

Stage = Literal["revision", "proofreading", "final"]
Format = Literal["txt", "md", "docx"]
Revision = Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)]


def _calendar_date(value: object) -> object:
    if type(value) is date or (isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)):
        return value
    raise ValueError("日期需要 YYYY-MM-DD 格式")


CalendarDate = Annotated[date, BeforeValidator(_calendar_date)]


class WriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: Revision


class GoalUpdate(WriteRequest):
    daily_goal: PositiveDbInt | None


class EntryUpdate(WriteRequest):
    expected_scene_id: PositiveDbInt
    stage: Literal["revision", "proofreading"] | None = None
    planned_date: CalendarDate | None = None

    @model_validator(mode="after")
    def require_change(self):
        if "stage" in self.model_fields_set and self.stage is None:
            raise ValueError("编辑状态不能为空")
        if not self.model_fields_set.intersection({"stage", "planned_date"}):
            raise ValueError("请选择编辑状态或计划日期")
        return self


class PreflightWriteRequest(WriteRequest):
    confirmation_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class FinalizeRequest(PreflightWriteRequest):
    expected_scene_id: PositiveDbInt
    expected_content_revision: Revision
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class PublishRequest(PreflightWriteRequest):
    edition_id: PositiveDbInt
    channel: str = Field(min_length=1, max_length=100)
    published_at: AwareDatetime
    request_id: UUID

    @field_validator("channel", mode="before")
    @classmethod
    def normalize_channel(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if "\x00" in value:
                raise ValueError("发布渠道含无效字符")
            value.encode("utf-8")
        return value

    @field_validator("published_at", mode="before")
    @classmethod
    def require_timestamp(cls, value):
        if isinstance(value, datetime) or (isinstance(value, str) and "T" in value):
            return value
        raise ValueError("实际发布时间需要带时区的日期时间")

    @field_validator("published_at")
    @classmethod
    def utc_publication_time(cls, value: datetime) -> datetime:
        try:
            return value.astimezone(UTC)
        except OverflowError as exc:
            raise ValueError("实际发布时间超出可记录范围") from exc


class GoalOut(BaseModel):
    daily_goal: int | None
    revision: int


class EntryMutation(BaseModel):
    revision: int


class PublicationReceipt(BaseModel):
    id: int


class CalendarCounts(BaseModel):
    date: date
    planned_count: int
    first_published_count: int


class Overview(GoalOut):
    stock_count: int
    support_days: int | None
    remaining_chapters: int | None
    days: list[CalendarCounts]


class Page[T](BaseModel):
    items: list[T]
    total: int
    offset: int
    limit: int


class ChapterRow(BaseModel):
    chapter_id: int
    chapter_no: int
    title: str
    status: ChapterStatus
    stage: Stage
    planned_date: date | None
    revision: int
    is_published: bool
    latest_edition_id: int | None


class EditionBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    seq: int
    chapter_no: int
    title: str
    created_at: UtcTime


class EditionOut(EditionBrief):
    content: str
    blocks: list[ReaderBlock] | None


class Draft(BaseModel):
    chapter_no: int
    title: str
    content: str
    blocks: list[ReaderBlock] | None
    content_revision: int
    fingerprint: str


class ChapterDetail(BaseModel):
    scene_id: PositiveDbInt
    chapter: ChapterRow
    draft: Draft | None
    edition: EditionOut | None
    editions: list[EditionBrief]
    draft_changed: bool


class PublicationRow(BaseModel):
    id: int
    chapter_id: int | None
    edition_id: int
    edition_seq: int
    chapter_no: int
    title: str
    channel: str
    published_at: UtcTime
    created_at: UtcTime


class PublicationDetail(BaseModel):
    publication: PublicationRow
    edition: EditionOut
    draft: Draft | None
    draft_changed: bool


class DayDetail(BaseModel):
    planned: Page[ChapterRow]
    first_published: Page[PublicationRow]
