from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

VersionSource = Literal["manual", "baseline", "generate", "polish", "rewrite", "restore"]
GenerationKind = Literal["generate", "polish", "rewrite", "expand", "condense", "deslop", "continue"]
GenerationStatus = Literal["success", "error", "cancelled"]
RewriteOp = Literal["rewrite", "expand", "condense", "deslop", "continue"]


class ChapterVersionBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    chapter_id: int
    seq: int
    source: VersionSource
    word_count: int
    starred: bool
    note: str
    generation_log_id: int | None
    created_at: datetime
    updated_at: datetime


class ChapterVersionOut(ChapterVersionBrief):
    content: str


class ChapterVersionUpdate(BaseModel):
    # 省略或 null = 不变
    starred: bool | None = None
    note: str | None = Field(default=None, max_length=200)


class GenerationLogBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    chapter_id: int | None
    kind: GenerationKind
    ai_config_id: int | None
    provider: str
    model: str
    params: dict[str, Any]
    status: GenerationStatus
    error: str
    output_chars: int
    duration_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    created_at: datetime


class GenerationLogOut(GenerationLogBrief):
    messages: list[dict[str, Any]]


class RewriteRequest(BaseModel):
    ai_config_id: int | None = None
    op: RewriteOp
    # 码点偏移，范围 [0, len(content)]；op=continue 时 start == end 表示光标位置
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    instruction: str = Field(default="", max_length=2000)


class RewriteApply(BaseModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    # 预览时的选区原文，用于乐观并发校验
    original: str
    # 预览时选区（光标）前最多 200 码点的原文，取自预览 done 载荷的 before；传入时一并校验。
    # 光标续写 original 为空，必须靠它发现前文变化
    before: str | None = Field(default=None, max_length=200)
    replacement: str = Field(max_length=100_000)
    log_id: int | None = None
