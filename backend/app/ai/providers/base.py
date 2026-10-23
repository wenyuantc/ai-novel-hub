from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Literal, TypedDict


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


# off = 不发送任何思考参数（默认会思考的模型按其默认行为）
ThinkingLevel = Literal["off", "low", "medium", "high"]


class ModelInfo(TypedDict):
    id: str
    name: str | None


class AIProviderError(Exception):
    """模型调用失败（网络、鉴权、配额等），message 为可直接展示给用户的中文说明。"""


class LLMProvider(ABC):
    # 禁止访问私网时由 build_provider 置为 False，防止重定向跳回内网
    follow_redirects: bool = True

    def __init__(self, api_key: str, base_url: str | None = None, timeout: float = 300.0):
        self.api_key = api_key
        self.base_url = base_url or None
        self.timeout = timeout

    @abstractmethod
    def generate_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking: ThinkingLevel = "off",
    ) -> AsyncIterator[str]:
        """流式返回增量正文文本（不含思考内容）。实现为 async generator。失败时抛 AIProviderError。"""

    async def list_models(self) -> list[ModelInfo]:
        """列出接口可用模型。失败时抛 AIProviderError。"""
        raise AIProviderError("该接口类型不支持获取模型列表")


def split_system(messages: list[ChatMessage]) -> tuple[str, list[ChatMessage]]:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [m for m in messages if m["role"] != "system"]
    return system, rest


def describe_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if len(text) > 500:
        text = text[:500] + "…"
    return f"模型调用失败：{text}"
