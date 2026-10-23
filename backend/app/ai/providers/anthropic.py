from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from app.ai.providers.base import (
    AIProviderError,
    ChatMessage,
    LLMProvider,
    describe_error,
    split_system,
)

DEFAULT_MAX_TOKENS = 8192


class AnthropicProvider(LLMProvider):
    async def generate_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        client = AsyncAnthropic(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            http_client=DefaultAsyncHttpxClient(follow_redirects=self.follow_redirects),
        )
        system, rest = split_system(messages)
        kwargs: dict = {}
        if system:
            kwargs["system"] = system
        if temperature is not None:
            kwargs["temperature"] = temperature
        try:
            async with client.messages.stream(
                model=model,
                max_tokens=max_tokens or DEFAULT_MAX_TOKENS,
                messages=[{"role": m["role"], "content": m["content"]} for m in rest],
                **kwargs,
            ) as stream:
                async for text in stream.text_stream:
                    if text:
                        yield text
        except AIProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AIProviderError(describe_error(exc)) from exc
        finally:
            await client.close()
