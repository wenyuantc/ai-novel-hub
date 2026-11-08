from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.kb import SearchMode

MemoryKind = Literal["summary", "plot_point", "hook", "character", "foreshadow", "manual"]
MemoryOrigin = Literal["auto", "manual"]


def _clean_names(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    names = []
    for n in value:
        n = (n or "").strip()[:100]
        if n and n not in names:
            names.append(n)
    if len(names) > 20:
        raise ValueError("相关角色最多 20 个")
    return names


class MemoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    chapter_id: int | None
    chapter_no: int
    kind: MemoryKind
    content: str
    characters: list[str]
    importance: int
    origin: MemoryOrigin
    analysis_id: int | None
    # None 表示手动记忆或缺少可验证的分析来源，不能误判为新鲜或过期。
    source_stale: bool | None = None
    pinned: bool
    muted: bool
    created_at: datetime
    updated_at: datetime


class MemoryPage(BaseModel):
    items: list[MemoryOut]
    total: int
    page: int
    page_size: int


class MemoryCreate(BaseModel):
    chapter_no: int = Field(ge=1, le=100000)
    content: str = Field(min_length=1, max_length=1000)
    characters: list[str] = Field(default_factory=list)
    importance: int = Field(default=3, ge=1, le=5)
    pinned: bool = False

    @field_validator("content")
    @classmethod
    def _strip(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("记忆内容不能为空")
        return v.strip()

    @field_validator("characters")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        return _clean_names(v) or []


class MemoryUpdate(BaseModel):
    pinned: bool | None = None
    muted: bool | None = None
    # 以下字段只有手动记忆可以修改
    chapter_no: int | None = Field(default=None, ge=1, le=100000)
    content: str | None = Field(default=None, min_length=1, max_length=1000)
    characters: list[str] | None = None
    importance: int | None = Field(default=None, ge=1, le=5)

    @field_validator("content")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("记忆内容不能为空")
        return v.strip() if v is not None else None

    @field_validator("characters")
    @classmethod
    def _names(cls, v: list[str] | None) -> list[str] | None:
        return _clean_names(v)


class MemorySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    # 按第 N 章召回：只在第 N 章之前的记忆中检索（与生成时的范围一致，但不排除前情章节）
    before_chapter_no: int | None = Field(default=None, ge=1, le=100000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    min_score: float | None = Field(default=None, ge=0, le=1)
    include_muted: bool = False


class MemorySearchHit(BaseModel):
    memory: MemoryOut
    score: float
    vector_score: float | None
    keyword_score: float


class MemorySearchResult(BaseModel):
    hits: list[MemorySearchHit]
    mode: SearchMode
    warnings: list[str]
    candidates: int


class ContextPreview(BaseModel):
    """本次生成会注入的【参考资料】与【相关旧情节】（裁剪前；为空表示不注入）。"""

    knowledge: str
    memories: str
