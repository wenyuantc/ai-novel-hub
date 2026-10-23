from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ProviderName = Literal["openai_compat", "anthropic", "gemini"]


class AIConfigCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    provider: ProviderName
    base_url: str | None = Field(default=None, max_length=500)
    # openai_compat 指向本地 LLM（Ollama 等）时可为空
    api_key: str = Field(default="", max_length=1000)
    model: str = Field(min_length=1, max_length=200)
    is_default: bool = False


class AIConfigUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    provider: ProviderName | None = None
    base_url: str | None = Field(default=None, max_length=500)
    # 省略或空字符串 = 保持原 Key 不变
    api_key: str | None = Field(default=None, max_length=1000)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    is_default: bool | None = None


class AIConfigOut(BaseModel):
    id: int
    name: str
    provider: ProviderName
    base_url: str | None
    model: str
    api_key_masked: str
    is_default: bool
    created_at: datetime
    updated_at: datetime


class AIConfigTestResult(BaseModel):
    ok: bool
    message: str
    latency_ms: int
