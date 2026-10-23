from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChapterStatus = Literal["draft", "generating", "done"]


class ChapterCreate(BaseModel):
    outline_id: int | None = None
    chapter_no: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=200)
    content: str = ""


class ChapterUpdate(BaseModel):
    outline_id: int | None = None
    chapter_no: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=200)
    content: str | None = None
    # 不允许手动设为 generating；可用于把卡住的 generating 章节复位
    status: Literal["draft", "done"] | None = None


class ChaptersFromOutlines(BaseModel):
    # 为空表示为项目中所有尚未建章的大纲条目创建章节
    outline_ids: list[int] | None = None


class ChapterBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    outline_id: int | None
    chapter_no: int
    title: str
    status: ChapterStatus
    word_count: int
    updated_at: datetime


class ChapterOut(ChapterBrief):
    content: str


class GenerateChapterRequest(BaseModel):
    ai_config_id: int | None = None
    instruction: str = Field(default="", max_length=2000)
    target_words: int = Field(default=3000, ge=300, le=20000)
    # 字数低于目标 85% 时自动续写（最多 2 轮）。接口默认关闭以兼容旧客户端，编辑器默认显式开启
    auto_continue: bool = False


class PolishChapterRequest(BaseModel):
    ai_config_id: int | None = None
    instruction: str = Field(default="", max_length=2000)


class WizardRequest(BaseModel):
    ai_config_id: int | None = None
    instruction: str = Field(default="", max_length=2000)
    # 仅 outline 使用
    volume_count: int = Field(default=2, ge=1, le=20)
    chapters_per_volume: int = Field(default=10, ge=1, le=100)
    # 仅 characters 使用
    character_count: int = Field(default=5, ge=3, le=30)
