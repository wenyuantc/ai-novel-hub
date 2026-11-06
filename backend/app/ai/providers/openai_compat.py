from collections.abc import AsyncIterator

from httpx import AsyncClient
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from app.ai.endpoint_guard import ensure_endpoint_allowed
from app.ai.images import MAX_COVER_BYTES, TOO_LARGE, decode_image, validate_image
from app.ai.providers.base import (
    AIProviderError,
    ChatMessage,
    GeneratedImage,
    LLMProvider,
    ModelInfo,
    ThinkingLevel,
    describe_error,
    describe_tools_error,
)
from app.ai.providers.dispatch_hooks import current_dispatch_hooks
from app.ai.tools import AgentEvent, AgentMessage, ToolCall, ToolChoice, ToolSpec


def _tool_messages(messages: list[AgentMessage]) -> list[dict]:
    result = []
    for message in messages:
        item: dict = {"role": message["role"], "content": message.get("content", "")}
        if message["role"] == "assistant" and message.get("tool_calls"):
            item["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
                for call in message["tool_calls"]
            ]
        elif message["role"] == "tool":
            item["tool_call_id"] = message["tool_call_id"]
        result.append(item)
    return result


class OpenAICompatProvider(LLMProvider):
    """OpenAI 及兼容接口（中转、DeepSeek、Ollama、llama.cpp 等）。"""

    uses_dispatch_hooks = True

    def _client(self) -> AsyncOpenAI:
        # 本地 LLM 常无需 Key，但 SDK 要求非空
        hooks = current_dispatch_hooks()
        http_options = {"event_hooks": hooks.event_hooks} if hooks else {}
        # SDK 会把 request hook 的拒绝包装成连接错误；受控发送只在业务层显式重试。
        sdk_options = {"max_retries": 0} if hooks else {}
        return AsyncOpenAI(
            api_key=self.api_key or "EMPTY",
            base_url=self.base_url,
            timeout=self.timeout,
            http_client=DefaultAsyncHttpxClient(follow_redirects=self.follow_redirects, **http_options),
            **sdk_options,
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

    async def _download_image(self, url: str) -> bytes:
        if not self.follow_redirects:
            await ensure_endpoint_allowed(url)
        async with AsyncClient(timeout=self.timeout, follow_redirects=self.follow_redirects) as client:
            async with client.stream("GET", url) as response:
                # 即使允许私网也不把重定向响应体当成图片；关闭重定向时尤其不能跨过检查。
                if response.is_redirect:
                    raise AIProviderError("图片下载地址发生重定向，请更换图片接口后重试")
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_COVER_BYTES:
                        raise AIProviderError(TOO_LARGE)
                    body.extend(chunk)
                return bytes(body)

    async def generate_image(self, prompt: str, model: str) -> GeneratedImage:
        client = self._client()
        try:
            try:
                result = await client.images.generate(model=model, prompt=prompt, n=1, size="1024x1536")
            except Exception as exc:
                # 只有明确的尺寸参数拒绝才重试，避免配额/鉴权失败被重复计费。
                if getattr(exc, "status_code", None) != 400 or "size" not in str(exc).lower():
                    raise
                result = await client.images.generate(model=model, prompt=prompt, n=1)
            if not result.data:
                raise AIProviderError("图片模型未返回图片，请调整提示词或更换模型")
            image = result.data[0]
            if image.b64_json:
                data = decode_image(image.b64_json)
            elif image.url:
                data = await self._download_image(image.url)
            else:
                raise AIProviderError("图片模型未返回可读取的图片数据")
            return GeneratedImage(data=data, mime_type=validate_image(data))
        except AIProviderError:
            raise
        except Exception as exc:
            raise AIProviderError(describe_error(exc)) from exc
        finally:
            await client.close()

    async def chat_tools_stream(
        self,
        messages: list[AgentMessage],
        model: str,
        tools: list[ToolSpec],
        *,
        tool_choice: ToolChoice = "auto",
        max_tokens: int | None = None,
        thinking: ThinkingLevel = "off",
        temperature: float | None = None,
    ) -> AsyncIterator[AgentEvent]:
        client = None
        stream = None
        kwargs: dict = {"stream_options": {"include_usage": True}}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if thinking != "off":
            kwargs["reasoning_effort"] = thinking
        try:
            client = self._client()
            request = {
                "model": model,
                "messages": _tool_messages(messages),
                "tools": [{"type": "function", "function": tool} for tool in tools],
                "tool_choice": tool_choice,
                "stream": True,
            }
            try:
                stream = await client.chat.completions.create(**request, **kwargs)
            except Exception as exc:
                # 仅请求开始前兼容重试；流中途失败不能重放已经发出的文本/工具。
                if getattr(exc, "status_code", None) != 400 or "stream_options" not in str(exc).lower():
                    raise
                kwargs.pop("stream_options")
                stream = await client.chat.completions.create(**request, **kwargs)
            calls: dict[int, ToolCall] = {}
            reason = None
            async for chunk in stream:
                if chunk.usage is not None:
                    yield (
                        "usage",
                        {
                            "input_tokens": chunk.usage.prompt_tokens,
                            "output_tokens": chunk.usage.completion_tokens,
                        },
                    )
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta.content:
                    yield "text", {"text": delta.content}
                for piece in delta.tool_calls or []:
                    call = calls.setdefault(piece.index, ToolCall(id="", name="", arguments=""))
                    call["id"] += piece.id or ""
                    if piece.function:
                        call["name"] += piece.function.name or ""
                        call["arguments"] += piece.function.arguments or ""
                if choice.finish_reason:
                    reason = choice.finish_reason
            if reason is None:
                raise AIProviderError("模型工具调用流意外中断，请重试")
            if calls and reason not in ("tool_calls", "stop"):
                raise AIProviderError("模型工具调用未完整生成，请提高最大输出长度后重试")
            for _, call in sorted(calls.items()):
                if not call["id"] or not call["name"]:
                    raise AIProviderError("模型返回的工具调用缺少标识或名称，请重试")
                yield "tool_call", call
            yield "finish", {"reason": reason}
        except AIProviderError:
            raise
        except Exception as exc:
            raise AIProviderError(describe_tools_error(exc)) from exc
        finally:
            try:
                if stream is not None:
                    await stream.close()
            finally:
                if client is not None:
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
