from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

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
from app.ai.providers.dispatch_hooks import current_dispatch_hooks, model_client_event_hooks
from app.ai.providers.usage import ModelAttempt, may_request_usage_options, model_attempt, valid_usage_count
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


def _record_chat_usage(attempt: ModelAttempt, raw: object) -> dict[str, int]:
    if raw is None:
        return {}
    usage = {
        key: count
        for key, attribute in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"))
        if (count := valid_usage_count(getattr(raw, attribute, None))) is not None
    }
    details = getattr(raw, "prompt_tokens_details", None)
    cached = valid_usage_count(getattr(details, "cached_tokens", None))
    if cached is None:
        # DeepSeek 也通过 SDK 的附加字段返回缓存命中量。
        cached = valid_usage_count(getattr(raw, "prompt_cache_hit_tokens", None))
    attempt.set_usage(**usage, cached_input_tokens=cached)
    return usage


class OpenAICompatProvider(LLMProvider):
    """OpenAI 及兼容接口（中转、DeepSeek、Ollama、llama.cpp 等）。"""

    uses_dispatch_hooks = True
    reports_model_usage = True

    def _client(self) -> AsyncOpenAI:
        # 本地 LLM 常无需 Key，但 SDK 要求非空
        hooks = model_client_event_hooks()
        http_options = {"event_hooks": hooks} if hooks else {}
        # 受控发送只在业务层显式重试，裸 SDK 沿用其重试并由 HTTP 钩子记录尝试。
        sdk_options = {"max_retries": 0} if current_dispatch_hooks() is not None else {}
        return AsyncOpenAI(
            api_key=self.api_key or "EMPTY",
            base_url=self.base_url,
            timeout=self.timeout,
            http_client=DefaultAsyncHttpxClient(follow_redirects=self.follow_redirects, **http_options),
            **sdk_options,
        )

    @asynccontextmanager
    async def _chat_attempt(self, client, request: dict, options: dict):
        async with model_attempt() as attempt:
            stream = await attempt.invoke(client.chat.completions.create(**request, **options))
            attempt.mark_response()
            try:
                yield attempt, stream
            finally:
                await stream.close()

    @asynccontextmanager
    async def _chat_stream(self, client, request: dict, options: dict):
        async with AsyncExitStack() as attempts:
            try:
                active = await attempts.enter_async_context(self._chat_attempt(client, request, options))
            except Exception as exc:
                # 只重试建流时明确不兼容用量字段的请求。已经开始消费的流不能重放。
                if (
                    "stream_options" not in options
                    or getattr(exc, "status_code", None) != 400
                    or not any(field in str(exc).lower() for field in ("stream_options", "include_usage"))
                ):
                    raise
                without_usage = {key: value for key, value in options.items() if key != "stream_options"}
                active = await attempts.enter_async_context(self._chat_attempt(client, request, without_usage))
            yield active

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
        if may_request_usage_options():
            kwargs["stream_options"] = {"include_usage": True}
        try:
            request = {"model": model, "messages": messages, "stream": True}
            async with self._chat_stream(client, request, kwargs) as (attempt, stream):
                async for chunk in attempt.iterate(stream):
                    _record_chat_usage(attempt, getattr(chunk, "usage", None))
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
        # 图片 URL 是同一次生成的后续物理出站：每个下载/重定向也须重验原授权。
        hooks = current_dispatch_hooks()
        http_options = {"event_hooks": hooks.event_hooks} if hooks else {}
        async with AsyncClient(timeout=self.timeout, follow_redirects=self.follow_redirects, **http_options) as client:
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
                result = await self._request_image(client, model, prompt, size="1024x1536")
            except Exception as exc:
                # 只有明确的尺寸参数拒绝才重试，避免配额/鉴权失败被重复计费。
                if getattr(exc, "status_code", None) != 400 or "size" not in str(exc).lower():
                    raise
                result = await self._request_image(client, model, prompt)
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

    async def _request_image(self, client, model: str, prompt: str, **options):
        async with model_attempt() as attempt:
            result = await attempt.invoke(client.images.generate(model=model, prompt=prompt, n=1, **options))
            attempt.mark_response()
            usage = getattr(result, "usage", None)
            attempt.set_usage(
                input_tokens=getattr(usage, "input_tokens", None), output_tokens=getattr(usage, "output_tokens", None),
            )
            if result.data is not None:
                attempt.set_usage(image_count=sum(
                    bool(getattr(image, "b64_json", None) or getattr(image, "url", None)) for image in result.data
                ))
            return result

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
            calls: dict[int, ToolCall] = {}
            reason = None
            async with self._chat_stream(client, request, kwargs) as (attempt, stream):
                async for chunk in attempt.iterate(stream):
                    if usage := _record_chat_usage(attempt, getattr(chunk, "usage", None)):
                        yield "usage", usage
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
            if client is not None:
                await client.close()

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        """POST {base}/embeddings {model, input: [...]}；按返回的 index 还原顺序。"""
        client = self._client()
        try:
            async with model_attempt() as attempt:
                # float：部分兼容接口不支持 SDK 默认的 base64 编码
                resp = await attempt.invoke(client.embeddings.create(model=model, input=texts, encoding_format="float"))
                attempt.mark_response()
                usage = getattr(resp, "usage", None)
                attempt.set_usage(input_tokens=getattr(usage, "prompt_tokens", None))
                items = sorted(resp.data, key=lambda d: d.index)
                vectors = [list(d.embedding) for d in items]
                if len(vectors) != len(texts):
                    raise AIProviderError(f"模型调用失败：向量数量不符（请求 {len(texts)} 条，返回 {len(vectors)} 条）")
        except AIProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AIProviderError(describe_error(exc)) from exc
        finally:
            await client.close()
        return vectors
