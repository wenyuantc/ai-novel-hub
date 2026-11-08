from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.chapter import ChapterOut


class ReadingProgressIn(BaseModel):
    chapter_id: int
    ratio: float = Field(ge=0, le=1, allow_inf_nan=False)


class TocItem(BaseModel):
    """阅读器目录条目（只含已写章节）；没有关联大纲时卷信息为 null。"""

    id: int
    chapter_no: int
    title: str
    word_count: int
    volume_no: int | None = None
    volume_title: str | None = None


class ReaderRun(BaseModel):
    """正文码点坐标；只暴露读者可见的排版。"""

    start: int
    end: int
    bold: bool = False
    italic: bool = False


class ReaderBlock(BaseModel):
    kind: Literal["paragraph", "heading", "bullet_list", "ordered_list", "blockquote"]
    start: int
    end: int
    level: int | None = None
    runs: list[ReaderRun]


class ReaderChapter(BaseModel):
    chapter: ChapterOut
    # 无排版时不膨胀为逐行 JSON；阅读器沿用原纯文本渲染。
    blocks: list[ReaderBlock] | None
