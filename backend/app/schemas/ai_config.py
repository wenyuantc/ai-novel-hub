from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

ProviderName = Literal["openai_compat", "anthropic", "gemini"]
# embedding = 向量模型（知识库与剧情记忆的语义检索），Anthropic 不提供
ModelType = Literal["text", "image", "embedding"]
ThinkingLevel = Literal["off", "low", "medium", "high", "xhigh", "max"]

# 单位 K（×1000 tokens）
ContextWindowK = Field(default=None, ge=1, le=2000)
MaxOutputK = Field(default=None, ge=1, le=256)


def check_output_within_context(context_window_k: int | None, max_output_k: int | None) -> None:
    if context_window_k is not None and max_output_k is not None and max_output_k >= context_window_k:
        raise ValueError("最大输出必须小于上下文大小")


EMBEDDING_UNSUPPORTED_PROVIDERS = ("anthropic",)
EMBEDDING_UNSUPPORTED = "该提供方不支持向量模型"


def check_embedding_provider(provider: str | None, model_type: str | None) -> None:
    if model_type == "embedding" and provider in EMBEDDING_UNSUPPORTED_PROVIDERS:
        raise ValueError(EMBEDDING_UNSUPPORTED)


class AIConfigCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: ProviderName
    base_url: str | None = Field(default=None, max_length=500)
    # openai_compat 指向本地 LLM（Ollama 等）时可为空
    api_key: str = Field(default="", max_length=1000)
    model: str = Field(min_length=1, max_length=200)
    model_type: ModelType = "text"
    is_default: bool = False
    thinking_level: ThinkingLevel = "off"
    context_window_k: int | None = ContextWindowK
    max_output_k: int | None = MaxOutputK

    @model_validator(mode="after")
    def _check_limits(self) -> Self:
        check_output_within_context(self.context_window_k, self.max_output_k)
        check_embedding_provider(self.provider, self.model_type)
        return self


class AIConfigUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    provider: ProviderName | None = None
    base_url: str | None = Field(default=None, max_length=500)
    # 省略或空字符串 = 保持原 Key 不变
    api_key: str | None = Field(default=None, max_length=1000)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    model_type: ModelType | None = None
    is_default: bool | None = None
    thinking_level: ThinkingLevel | None = None
    # 显式传 null = 清空（恢复默认）；省略 = 不修改
    context_window_k: int | None = ContextWindowK
    max_output_k: int | None = MaxOutputK


class AIConfigOut(BaseModel):
    id: int
    name: str
    provider: ProviderName
    base_url: str | None
    model: str
    model_type: ModelType
    api_key_masked: str
    is_default: bool
    thinking_level: ThinkingLevel
    context_window_k: int | None
    max_output_k: int | None
    created_at: datetime
    updated_at: datetime


class AIConfigTestResult(BaseModel):
    ok: bool
    message: str
    latency_ms: int


class ModelListRequest(BaseModel):
    provider: ProviderName
    base_url: str | None = Field(default=None, max_length=500)
    # 为空时若提供 config_id 则使用该配置已保存的 Key
    api_key: str | None = Field(default=None, max_length=1000)
    config_id: int | None = None


class ModelInfo(BaseModel):
    id: str
    name: str | None = None


class ModelListResult(BaseModel):
    models: list[ModelInfo]
