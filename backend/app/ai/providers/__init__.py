from collections.abc import Callable

from app.ai.providers.anthropic import AnthropicProvider
from app.ai.providers.base import AIProviderError, ChatMessage, LLMProvider
from app.ai.providers.gemini import GeminiProvider
from app.ai.providers.openai_compat import OpenAICompatProvider

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "openai_compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}

# 工厂签名：(provider, api_key, base_url, timeout) -> LLMProvider；测试中可替换为 mock 工厂
ProviderFactory = Callable[[str, str, str | None, float], LLMProvider]


def create_provider(provider: str, api_key: str, base_url: str | None, timeout: float = 300.0) -> LLMProvider:
    cls = _PROVIDERS.get(provider)
    if cls is None:
        raise AIProviderError(f"不支持的模型提供方：{provider}")
    return cls(api_key=api_key, base_url=base_url, timeout=timeout)


__all__ = [
    "AIProviderError",
    "AnthropicProvider",
    "ChatMessage",
    "GeminiProvider",
    "LLMProvider",
    "OpenAICompatProvider",
    "ProviderFactory",
    "create_provider",
]
