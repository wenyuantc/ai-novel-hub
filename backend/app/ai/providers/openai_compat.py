from collections.abc import AsyncIterator

from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from app.ai.providers.base import AIProviderError, ChatMessage, LLMProvider, describe_error


class OpenAICompatProvider(LLMProvider):
    """OpenAI 及兼容接口（中转、DeepSeek、Ollama、llama.cpp 等）。"""

    async def generate_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        # 本地 LLM 常无需 Key，但 SDK 要求非空
        client = AsyncOpenAI(
            api_key=self.api_key or "EMPTY",
            base_url=self.base_url,
            timeout=self.timeout,
            http_client=DefaultAsyncHttpxClient(follow_redirects=self.follow_redirects),
        )
        kwargs: dict = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        try:
            stream = await client.chat.completions.create(model=model, messages=messages, stream=True, **kwargs)
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None) if delta else None
                if text:
                    yield text
        except AIProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AIProviderError(describe_error(exc)) from exc
        finally:
            await client.close()
