"""一致性检查的模型调用：只负责把一次流式响应收成全文，不做任何授权或落库。"""

from dataclasses import dataclass
from typing import Any

from app.ai.providers.base import ChatMessage, LLMProvider, ThinkingLevel


@dataclass(frozen=True)
class ModelTarget:
    """一次调用使用的模型。provider 由调用方构建：任务处理器传入已绑定出站门禁的受控 provider。"""

    provider: LLMProvider | Any
    model: str
    max_tokens: int | None = None
    thinking: ThinkingLevel = "off"


async def complete(target: ModelTarget, messages: list[ChatMessage]) -> str:
    """调用模型并返回全文；失败抛 AIProviderError。模型原文不进入任务输出，由调用方决定如何处理。"""
    pieces: list[str] = []
    async for piece in target.provider.generate_stream(
        messages, target.model, max_tokens=target.max_tokens, thinking=target.thinking
    ):
        pieces.append(piece)
    return "".join(pieces)
