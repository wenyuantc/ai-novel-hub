"""拆书导入：解析记录、章节预览与校正、预估与开始导入。章节序号（index）与范围一律从 1 起、两端包含。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.task import TaskOut

BookKind = Literal["txt", "md", "docx", "epub"]
SplitMode = Literal["auto", "regex"]
ChapterWarning = Literal["short", "long", "dup_title", "gap"]
BookOp = Literal["rename", "merge_next", "split", "delete", "start_volume", "rename_volume", "clear_volume"]
# [起, 止]，从 1 起、两端包含
Range = tuple[int, int]


class BookImportBrief(BaseModel):
    id: int
    filename: str
    book_title: str
    chapter_count: int
    total_chars: int
    expires_at: datetime
    updated_at: datetime


class BookChapterView(BaseModel):
    index: int
    title: str
    # 0 = 不归属任何卷（排在第一个卷之前）；没有任何分卷时全部为 1、卷名为空
    volume_no: int
    volume_title: str
    # 从标题解析出的原始序号（只用于序号断档提示）
    source_no: int | None
    chars: int
    first_line: str
    warnings: list[ChapterWarning]


class WarningCounts(BaseModel):
    short: int = 0
    long: int = 0
    dup_title: int = 0
    gap: int = 0


class BookImportOut(BaseModel):
    id: int
    filename: str
    kind: BookKind
    size: int
    book_title: str
    split_mode: SplitMode
    split_pattern: str | None
    revision: int
    notices: list[str]
    chapters: list[BookChapterView]
    warning_counts: WarningCounts
    chapter_count: int
    total_chars: int
    # 章节数超过 max_import_chapters：导入范围必须缩小
    too_many: bool
    max_import_chapters: int
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class BookChapterText(BaseModel):
    index: int
    title: str
    # 正文按行切分（行号 = 下标 + 1，拆分时传这个行号）；segments 拼接处是一个空行
    lines: list[str]


class ResplitRequest(BaseModel):
    mode: SplitMode
    pattern: str | None = Field(default=None, max_length=100)
    revision: int


class BookOpRequest(BaseModel):
    op: BookOp
    index: int = Field(ge=1)
    # rename / start_volume / rename_volume 必填（1–200 字）
    title: str | None = Field(default=None, max_length=200)
    # split 必填：章内行号（从 1 起，不能是第 1 行）
    line: int | None = Field(default=None, ge=1)
    revision: int


class EstimateRequest(BaseModel):
    import_range: Range
    reverse_range: Range
    extract_style: bool = True
    deep_analyze: bool = False


class EstimateOut(BaseModel):
    import_chapters: int
    import_chars: int
    reverse_chars: int
    batches: int
    digest_input: int
    synthesize_input: int
    style_input: int
    analyze_input: int
    total_input: int


class StartRequest(BaseModel):
    revision: int
    title: str = Field(min_length=1, max_length=200)
    genre: str = Field(default="", max_length=100)
    import_range: Range
    reverse_range: Range
    extract_style: bool = True
    deep_analyze: bool = False
    ai_config_id: int | None = None


class StartResult(BaseModel):
    project_id: int
    task: TaskOut
