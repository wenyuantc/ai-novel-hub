"""全文提及的公共合同；所有文本位置均为原始 Unicode 码点半开区间。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

EntityKind = Literal["character", "world"]
EntityCategory = Literal["character", "place", "item"]
SourceType = Literal["chapter", "chat", "note", "knowledge"]
SOURCE_TYPES: tuple[SourceType, ...] = ("chapter", "chat", "note", "knowledge")


class EntityRef(BaseModel):
    kind: EntityKind
    id: int
    name: str
    category: EntityCategory
    aliases: list[str]


class MentionSpan(BaseModel):
    start: int
    end: int
    text: str
    entities: list[EntityRef]


class MentionCatalog(BaseModel):
    entities: list[EntityRef]
    dictionary_hash: str
    available_source_types: list[SourceType]


class MentionPreviewRequest(BaseModel):
    text: str = Field(max_length=500_000)


class MentionPreview(BaseModel):
    spans: list[MentionSpan]
    content_hash: str
    dictionary_hash: str


class SourceRef(BaseModel):
    type: SourceType
    id: int
    title: str
    chapter_no: int | None = None
    conversation_id: int | None = None
    created_at: datetime | None = None


class Occurrence(MentionSpan):
    source: SourceRef
    content_hash: str
    context_before: str
    context_after: str


class MentionPage(BaseModel):
    items: list[Occurrence]
    total: int
    page: int
    page_size: int
    source_counts: dict[SourceType, int]
    dictionary_hash: str


class SummaryChapter(BaseModel):
    id: int
    chapter_no: int
    title: str
    has_content: bool


class ChapterMentionCount(BaseModel):
    chapter_id: int
    count: int
    ambiguous_count: int


class MentionSummaryEntry(BaseModel):
    entity: EntityRef
    mention_count: int
    ambiguous_count: int
    chapter_count: int
    last_chapter_no: int | None
    gap_chapters: int | None
    counts: list[ChapterMentionCount]


class MentionSummary(BaseModel):
    chapters: list[SummaryChapter]
    entries: list[MentionSummaryEntry]
    total: int
    page: int
    page_size: int


class SourceWindow(BaseModel):
    source: SourceRef
    content_hash: str
    stale: bool
    window_start: int
    text: str
    spans: list[MentionSpan]
    target_start: int | None
    target_end: int | None
