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

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        """POST {base}/embeddings {model, input: [...]}；按返回的 index 还原顺序。"""
        client = self._client()
        try:
            # float：部分兼容接口不支持 SDK 默认的 base64 编码
            resp = await client.embeddings.create(model=model, input=texts, encoding_format="float")
            items = sorted(resp.data, key=lambda d: d.index)
            vectors = [list(d.embedding) for d in items]
        except AIProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AIProviderError(describe_error(exc)) from exc
        finally:
            await client.close()
        if len(vectors) != len(texts):
            raise AIProviderError(f"模型调用失败：向量数量不符（请求 {len(texts)} 条，返回 {len(vectors)} 条）")
        return vectors
