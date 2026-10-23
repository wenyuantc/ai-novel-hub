from collections.abc import AsyncIterator

from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from app.ai.providers.base import (
    AIProviderError,
    ChatMessage,
    LLMProvider,
    ModelInfo,
    ThinkingLevel,
    describe_error,
)


class OpenAICompatProvider(LLMProvider):
    """OpenAI 及兼容接口（中转、DeepSeek、Ollama、llama.cpp 等）。"""

    def _client(self) -> AsyncOpenAI:
        # 本地 LLM 常无需 Key，但 SDK 要求非空
        return AsyncOpenAI(
            api_key=self.api_key or "EMPTY",
            base_url=self.base_url,
            timeout=self.timeout,
            http_client=DefaultAsyncHttpxClient(follow_redirects=self.follow_redirects),
        )

    async def generate_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking: ThinkingLevel = "off",
    ) -> AsyncIterator[str]:
        client = self._client()
        kwargs: dict = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if thinking != "off":
            kwargs["reasoning_effort"] = thinking
        try:
            stream = await client.chat.completions.create(model=model, messages=messages, stream=True, **kwargs)
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                # 只取正文；中转返回的 reasoning_content 不读取
                text = getattr(delta, "content", None) if delta else None
                if text:
                    yield text
        except AIProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AIProviderError(describe_error(exc)) from exc
        finally:
            await client.close()

    async def list_models(self) -> list[ModelInfo]:
        client = self._client()
        try:
            return [ModelInfo(id=m.id, name=None) async for m in client.models.list() if m.id]
        except Exception as exc:  # noqa: BLE001
            raise AIProviderError(describe_error(exc)) from exc
        finally:
            await client.close()
