"""全书字面搜索；位置为章节正文 Unicode 码点半开区间。"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.schemas.rich_document import TextOffset

PositiveId = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
MAX_HITS = 1000


def _valid_text(value: str) -> str:
    value.encode("utf-8")
    if "\x00" in value:
        raise ValueError("文本不能包含空字符")
    return value


CheckedText = Annotated[str, AfterValidator(_valid_text)]


class SearchScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: CheckedText = Field(min_length=1, max_length=512)
    start_chapter_id: PositiveId | None = None
    end_chapter_id: PositiveId | None = None

    @model_validator(mode="after")
    def complete_range(self):
        if (self.start_chapter_id is None) != (self.end_chapter_id is None):
            raise ValueError("请同时选择起止章节")
        return self


class SearchRequest(SearchScope):
    page: PositiveId = 1
    page_size: int = Field(default=20, strict=True, ge=1, le=100)
    snapshot: Digest | None = None

    @model_validator(mode="after")
    def pagination_snapshot(self):
        if self.page > 1 and self.snapshot is None:
            raise ValueError("续页必须携带首次搜索的快照")
        return self


class PreviewRequest(SearchRequest):
    replacement: CheckedText = Field(max_length=2048)


class SelectedHit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapter_id: PositiveId
    chapter_no: PositiveId
    chapter_title: CheckedText = Field(max_length=200)
    scene_id: PositiveId
    revision: TextOffset
    start: TextOffset
    end: TextOffset
    token: Digest


class ApplyRequest(SearchScope):
    replacement: CheckedText = Field(max_length=2048)
    request_id: UUID
    hits: list[SelectedHit] = Field(min_length=1, max_length=MAX_HITS)

    @model_validator(mode="after")
    def selected_hits(self):
        if self.query == self.replacement:
            raise ValueError("替换前后相同")
        keys = {(hit.chapter_id, hit.start) for hit in self.hits}
        if len(keys) != len(self.hits) or any(hit.end - hit.start != len(self.query) for hit in self.hits):
            raise ValueError("勾选的命中重复或范围无效")
        return self


class SearchHit(BaseModel):
    chapter_id: int
    chapter_no: int
    chapter_title: str
    revision: int
    content_hash: str
    start: int
    end: int
    before: str
    after: str
    scene_id: int | None = None
    unavailable_reason: str | None = None
    warning: str | None = None
    token: str | None = None


class SearchPage(BaseModel):
    items: list[SearchHit]
    total: int
    page: int
    page_size: int
    snapshot: str


class BatchSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    query: str
    replacement: str
    chapter_count: int
    hit_count: int
    status: Literal["applied", "undone"]
    created_at: datetime
    undone_at: datetime | None


class BatchPage(BaseModel):
    items: list[BatchSummary]
    total: int
    page: int
    page_size: int
