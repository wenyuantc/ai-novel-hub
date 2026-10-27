from pydantic import BaseModel, Field


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
