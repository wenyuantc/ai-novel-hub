from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.task import TaskOut

KbDocumentStatus = Literal["pending", "parsing", "indexing", "ready", "ready_keyword_only", "failed"]
SearchMode = Literal["hybrid", "keyword"]


class KbSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    auto_inject: bool
    top_k: int
    min_score: float
    max_chars: int
    memory_top_k: int
    memory_max_chars: int
    memory_min_score: float


class KbSettingsUpdate(BaseModel):
    auto_inject: bool | None = None
    top_k: int | None = Field(default=None, ge=1, le=20)
    min_score: float | None = Field(default=None, ge=0, le=1)
    max_chars: int | None = Field(default=None, ge=500, le=8000)
    memory_top_k: int | None = Field(default=None, ge=1, le=20)
    memory_max_chars: int | None = Field(default=None, ge=300, le=6000)
    memory_min_score: float | None = Field(default=None, ge=0, le=1)


class KbDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    filename: str
    mime: str
    size: int
    sha256: str
    status: KbDocumentStatus
    error: str
    warnings: list[str]
    char_count: int
    chunk_count: int
    inject: bool
    created_at: datetime
    updated_at: datetime


class KbUploadResult(BaseModel):
    document: KbDocumentOut
    task: TaskOut


class KbDocumentUpdate(BaseModel):
    inject: bool


class KbChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    seq: int
    heading_path: str
    content: str
    start_offset: int
    end_offset: int


class KbChunkPage(BaseModel):
    items: list[KbChunkOut]
    total: int
    page: int
    page_size: int


class KbSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=50)
    min_score: float | None = Field(default=None, ge=0, le=1)


class KbSearchHit(BaseModel):
    chunk_id: int
    document_id: int
    filename: str
    heading_path: str
    content: str
    score: float
    vector_score: float | None
    keyword_score: float


class KbSearchResult(BaseModel):
    hits: list[KbSearchHit]
    mode: SearchMode
    warnings: list[str]
    candidates: int


class StaleCounts(BaseModel):
    kb: int = 0
    memory: int = 0


class IndexStatus(BaseModel):
    # 当前默认向量模型（配置名 · 模型名）；未配置时为 null（只用关键词检索）
    embedding_model: str | None
    # 存在缺少当前向量模型向量的检索单元（换了模型 / 生成失败 / 尚未生成）
    needs_rebuild: bool
    stale_count: int
    stale: StaleCounts
    # 项目有排队 / 运行中的建索引任务（横幅可据此提示「正在建索引」而不是「需要重建」）
    indexing: bool
