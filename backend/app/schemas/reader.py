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
    content_hash: str
    # 无排版时不膨胀为逐行 JSON；阅读器沿用原纯文本渲染。
    blocks: list[ReaderBlock] | None


ReaderEmotion = Literal["neutral", "happy", "angry", "afraid", "sad", "whisper"]


class CastingRange(BaseModel):
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, gt=0)


class CastingVoice(BaseModel):
    id: str = Field(min_length=1, max_length=1024)
    name: str = Field(min_length=1, max_length=160)
    lang: str = Field(max_length=40, pattern=r"(?i)^(zh|cmn|yue)([-_][a-z0-9]+)*$")


class ReaderCastingRequest(BaseModel):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cues: list[CastingRange] = Field(max_length=400)
    voices: list[CastingVoice] = Field(min_length=1, max_length=128)
    ai_config_id: int | None = Field(None, gt=0, le=2_147_483_647, strict=True)


class CastCue(BaseModel):
    start: int
    speaker: str | None
    emotion: ReaderEmotion


class CastVoice(BaseModel):
    speaker: str
    voice_id: str


class ReaderCastingResult(BaseModel):
    content_hash: str
    cues: list[CastCue]
    voices: list[CastVoice]
