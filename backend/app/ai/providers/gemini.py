import json
import math
import re
from collections.abc import AsyncIterator
from uuid import uuid4

from google import genai
from google.genai import types

from app.ai.images import validate_image
from app.ai.providers.base import (
    AIProviderError,
    ChatMessage,
    GeneratedImage,
    LLMProvider,
    ModelInfo,
    ThinkingLevel,
    describe_error,
    describe_tools_error,
    split_system,
)
from app.ai.tools import AgentEvent, AgentMessage, ToolCall, ToolChoice, ToolSpec, history_arguments, native_metadata

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


def _gemini_schema(schema: dict) -> dict:
    """展开 Pydantic 本地引用并移除 Gemini 不支持的关键字，保留同名业务属性。"""
    unsupported = {"additionalProperties", "$schema", "$id", "$defs", "definitions", "title", "default", "examples"}

    def clean(value: dict, refs: tuple[str, ...] = ()) -> dict:
        if ref := value.get("$ref"):
            if not ref.startswith("#/") or ref in refs:
                raise AIProviderError("工具参数结构包含不支持的循环或外部引用")
            target = schema
            for segment in ref[2:].split("/"):
                target = target[segment.replace("~1", "/").replace("~0", "~")]
            value = {**target, **{k: v for k, v in value.items() if k != "$ref"}}
            refs = (*refs, ref)
        result = {k: v for k, v in value.items() if k not in unsupported}
        # Gemini Schema 没有 exclusiveMinimum/Maximum；整数边界可无损转换，
        # 非整数边界使用包含边界的提示，执行前仍由原 Pydantic schema 严格校验。
        if "exclusiveMinimum" in result:
            bound = result.pop("exclusiveMinimum")
            minimum = math.floor(bound) + 1 if result.get("type") == "integer" else bound
            result["minimum"] = max(minimum, result.get("minimum", minimum))
        if "exclusiveMaximum" in result:
            bound = result.pop("exclusiveMaximum")
            maximum = math.ceil(bound) - 1 if result.get("type") == "integer" else bound
            result["maximum"] = min(maximum, result.get("maximum", maximum))
        if "properties" in result:
            result["properties"] = {key: clean(prop, refs) for key, prop in result["properties"].items()}
        if isinstance(result.get("items"), dict):
            result["items"] = clean(result["items"], refs)
        if "anyOf" in result:
            variants = result["anyOf"]
            non_null = [item for item in variants if item.get("type") != "null"]
            if len(non_null) != len(variants):
                result["nullable"] = True
                if len(non_null) == 1:
                    result.pop("anyOf")
                    result.update(clean(non_null[0], refs))
                else:
                    result["anyOf"] = [clean(item, refs) for item in non_null]
            else:
                result["anyOf"] = [clean(item, refs) for item in variants]
        return result

    return clean(schema)


def _tool_contents(messages: list[AgentMessage]) -> tuple[str, list[types.Content]]:
    system = "\n\n".join(m.get("content", "") for m in messages if m["role"] == "system")
    contents: list[types.Content] = []
    native_ids: dict[str, str | None] = {}
    for message in messages:
        if message["role"] == "system":
            continue
        role = "model" if message["role"] == "assistant" else "user"
        parts: list[types.Part] = []
        if message["role"] == "tool":
            try:
                response = json.loads(message.get("content", ""))
            except ValueError:
                response = message.get("content", "")
            if not isinstance(response, dict):
                response = {"result": response}
            parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=native_ids.get(message["tool_call_id"]), name=message["name"], response=response
                    )
                )
            )
        elif metadata := native_metadata(message, "gemini"):
            parts = [types.Part.model_validate(part) for part in metadata["parts"]]
            native_calls = [part.function_call for part in parts if part.function_call]
            for call, native_call in zip(message.get("tool_calls", []), native_calls, strict=True):
                native_ids[call["id"]] = native_call.id
        else:
            if message.get("content"):
                parts.append(types.Part(text=message["content"]))
            for call in message.get("tool_calls", []):
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(name=call["name"], args=history_arguments(call["arguments"]))
                    )
                )
                native_ids[call["id"]] = None
        if contents and contents[-1].role == role:
            contents[-1].parts.extend(parts)
        else:
            contents.append(types.Content(role=role, parts=parts))
    return system, contents


class GeminiProvider(LLMProvider):
    def _client(self) -> genai.Client:
        http_options = types.HttpOptions(
            base_url=self.base_url,
            timeout=int(self.timeout * 1000),
            async_client_args={"follow_redirects": self.follow_redirects},
        )
        return genai.Client(api_key=self.api_key, http_options=http_options)

    async def generate_image(self, prompt: str, model: str) -> GeneratedImage:
        client = None
        try:
            client = self._client()
            if "imagen" in model.lower():
                try:
                    result = await client.aio.models.generate_images(
                        model=model,
                        prompt=prompt,
                        config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio="3:4"),
                    )
                except ValueError as exc:
                    if "not in gemini developer api mode" not in str(exc).lower():
                        raise
                    raise AIProviderError(
                        "当前接口或 SDK 不支持 Imagen 图片生成，请在「模型配置」中换用 Gemini 原生生图模型"
                    ) from exc
                images = result.generated_images or []
                if images and images[0].image is not None and images[0].image.image_bytes:
                    data = images[0].image.image_bytes
                    return GeneratedImage(data=data, mime_type=validate_image(data))
            else:
                result = await client.aio.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
                )
                for candidate in result.candidates or []:
                    for part in candidate.content.parts if candidate.content and candidate.content.parts else []:
                        if part.inline_data is not None and part.inline_data.data:
                            data = part.inline_data.data
                            return GeneratedImage(data=data, mime_type=validate_image(data))
            raise AIProviderError("图片模型未返回图片，可能被内容策略拦截，请调整提示词或更换模型")
        except AIProviderError:
            raise
        except Exception as exc:
            raise AIProviderError(describe_error(exc)) from exc
        finally:
            if client is not None:
                await client.aio.aclose()

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
        try:
            system, contents = _tool_contents(messages)
            config = types.GenerateContentConfig(
                system_instruction=system or None,
                temperature=temperature,
                max_output_tokens=max_tokens,
                thinking_config=thinking_config(model, thinking),
                tools=[
                    types.Tool(
                        function_declarations=[
                            types.FunctionDeclaration(
                                name=tool["name"],
                                description=tool["description"],
                                parameters=_gemini_schema(tool["parameters"])
                                if tool["parameters"].get("properties")
                                else None,
                            )
                            for tool in tools
                        ]
                    )
                ],
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode=tool_choice.upper())
                ),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                automatic_continuation=False,
            )
            # 历史可能已被摘要压缩，不能仅靠历史最大序号保证跨轮次唯一。
            request_id = uuid4().hex
            client = self._client()
            stream = await client.aio.models.generate_content_stream(model=model, contents=contents, config=config)
            parts: list[dict] = []
            calls: list[ToolCall] = []
            reason = None
            usage = None
            async for chunk in stream:
                if chunk.usage_metadata is not None:
                    usage = chunk.usage_metadata
                if not chunk.candidates:
                    continue
                candidate = chunk.candidates[0]
                for part in candidate.content.parts or [] if candidate.content else []:
                    # JSON 模式将 bytes 签名编码；model_validate 能无损恢复，不能手工拼接/删除 thought 块。
                    parts.append(part.model_dump(mode="json", exclude_none=True))
                    if part.text and not part.thought:
                        yield "text", {"text": part.text}
                    if function := part.function_call:
                        if function.will_continue or function.partial_args:
                            raise AIProviderError("当前 Gemini 接口返回了不完整的工具参数，请重试")
                        call_id = function.id or f"call_{request_id}_{len(calls)}"
                        if not function.name:
                            raise AIProviderError("模型返回的工具调用缺少名称，请重试")
                        calls.append(
                            ToolCall(
                                id=call_id,
                                name=function.name,
                                arguments=json.dumps(function.args or {}, ensure_ascii=False),
                            )
                        )
                if candidate.finish_reason:
                    reason = candidate.finish_reason.value
            if reason is None:
                raise AIProviderError("模型工具调用流意外中断，请重试")
            if calls and reason != "STOP":
                raise AIProviderError("模型工具调用未完整生成，请提高最大输出长度后重试")
            if usage:
                yield (
                    "usage",
                    {
                        "input_tokens": usage.prompt_token_count or 0,
                        "output_tokens": (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0),
                    },
                )
            if calls:
                calls[0]["provider_metadata"] = {"provider": "gemini", "parts": parts}
            for call in calls:
                yield "tool_call", call
            yield "finish", {"reason": reason}
        except AIProviderError:
            raise
        except Exception as exc:
            raise AIProviderError(describe_tools_error(exc)) from exc
        finally:
            try:
                if stream is not None:
                    await stream.aclose()
            finally:
                if client is not None:
                    await client.aio.aclose()
                    client.close()

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        """多条文本一次请求（Gemini API 走 batchEmbedContents）。"""
        try:
            client = self._client()
            resp = await client.aio.models.embed_content(model=model, contents=list(texts))
            vectors = [list(e.values or []) for e in (resp.embeddings or [])]
        except AIProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AIProviderError(describe_error(exc)) from exc
        if len(vectors) != len(texts):
            raise AIProviderError(f"模型调用失败：向量数量不符（请求 {len(texts)} 条，返回 {len(vectors)} 条）")
        return vectors
