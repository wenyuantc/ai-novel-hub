from collections.abc import AsyncIterator

from google import genai
from google.genai import types

from app.ai.providers.base import (
    AIProviderError,
    ChatMessage,
    LLMProvider,
    describe_error,
    split_system,
)


class GeminiProvider(LLMProvider):
    async def generate_stream(
        self,
        messages: list[ChatMessage],
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        http_options = types.HttpOptions(
            base_url=self.base_url,
            timeout=int(self.timeout * 1000),
            async_client_args={"follow_redirects": self.follow_redirects},
        )
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
        )
        try:
            client = genai.Client(api_key=self.api_key, http_options=http_options)
            stream = await client.aio.models.generate_content_stream(model=model, contents=contents, config=config)
            async for chunk in stream:
                text = chunk.text
                if text:
                    yield text
        except AIProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AIProviderError(describe_error(exc)) from exc
