import re
from collections.abc import AsyncIterator

from google import genai
from google.genai import types

from app.ai.providers.base import (
    AIProviderError,
    ChatMessage,
    LLMProvider,
    ModelInfo,
    ThinkingLevel,
    describe_error,
    split_system,
)

# Gemini 1.x / 2.x 用 thinking_budget（-1 = 动态，由模型在自身上限内决定）；3 及以后用 thinking_level
_BUDGET_MODEL = re.compile(r"gemini-[12](\.|-|$)")
_THINKING_BUDGET = {"low": 2048, "medium": 8192, "high": 16384, "xhigh": 24576, "max": -1}
# Gemini 最高只有 HIGH，xhigh / max 统一取 HIGH
_THINKING_LEVEL = {
    "low": types.ThinkingLevel.LOW,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
    "xhigh": types.ThinkingLevel.HIGH,
    "max": types.ThinkingLevel.HIGH,
}


def thinking_config(model: str, thinking: ThinkingLevel) -> types.ThinkingConfig | None:
    """off 不发送思考配置。"""
    if thinking == "off":
        return None
    if _BUDGET_MODEL.search(model):
        return types.ThinkingConfig(thinking_budget=_THINKING_BUDGET[thinking])
    return types.ThinkingConfig(thinking_level=_THINKING_LEVEL[thinking])


class GeminiProvider(LLMProvider):
    def _client(self) -> genai.Client:
        http_options = types.HttpOptions(
            base_url=self.base_url,
            timeout=int(self.timeout * 1000),
            async_client_args={"follow_redirects": self.follow_redirects},
        )
        return genai.Client(api_key=self.api_key, http_options=http_options)

    async def generate_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking: ThinkingLevel = "off",
    ) -> AsyncIterator[str]:
        system, rest = split_system(messages)
        contents = [
            types.Content(
                role="model" if m["role"] == "assistant" else "user",
                parts=[types.Part(text=m["content"])],
            )
            for m in rest
        ]
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            temperature=temperature,
            max_output_tokens=max_tokens,
            thinking_config=thinking_config(model, thinking),
        )
        try:
            client = self._client()
            stream = await client.aio.models.generate_content_stream(model=model, contents=contents, config=config)
            async for chunk in stream:
                # chunk.text 已排除 thought 分片
                text = chunk.text
                if text:
                    yield text
        except AIProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AIProviderError(describe_error(exc)) from exc

    async def list_models(self) -> list[ModelInfo]:
        try:
            client = self._client()
            result: list[ModelInfo] = []
            async for m in await client.aio.models.list():
                name = (m.name or "").removeprefix("models/")
                actions = m.supported_actions
                if not name or (actions is not None and "generateContent" not in actions):
                    continue
                result.append(ModelInfo(id=name, name=m.display_name or None))
            return result
        except Exception as exc:  # noqa: BLE001
            raise AIProviderError(describe_error(exc)) from exc
