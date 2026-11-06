import json
import re
from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from app.ai.providers.base import (
    AIProviderError,
    ChatMessage,
    LLMProvider,
    ModelInfo,
    ThinkingLevel,
    describe_error,
    describe_tools_error,
    split_system,
)
from app.ai.providers.dispatch_hooks import current_dispatch_hooks
from app.ai.tools import AgentEvent, AgentMessage, ToolCall, ToolChoice, ToolSpec, history_arguments, native_metadata

DEFAULT_MAX_TOKENS = 8192

# 4.5 及更早的模型不支持 adaptive thinking，只能用 budget_tokens
_LEGACY_THINKING_MODEL = re.compile(r"claude-3|claude-(opus|sonnet|haiku)-4(-[0-5])?(-\d{8})?$")
# max 档受 Opus 4 / 4.1 输出上限 32000 约束：budget + 正文预留 ≤ 32000
_LEGACY_THINKING_BUDGET = {"low": 2048, "medium": 8192, "high": 16384, "xhigh": 24576, "max": 27648}
# budget_tokens 必须小于 max_tokens，额外留给正文的空间
_LEGACY_ANSWER_RESERVE = 4096
# xhigh 自 4.7 起才有，4.6 降为 high
_NO_XHIGH_MODEL = re.compile(r"claude-(opus|sonnet|haiku)-4-6")


def thinking_kwargs(model: str, thinking: ThinkingLevel, max_tokens: int) -> tuple[dict, int]:
    """返回 (额外请求参数, 实际 max_tokens)。off 不发送任何思考参数。"""
    if thinking == "off":
        return {}, max_tokens
    if _LEGACY_THINKING_MODEL.search(model):
        budget = _LEGACY_THINKING_BUDGET[thinking]
        return (
            {"thinking": {"type": "enabled", "budget_tokens": budget}},
            max(max_tokens, budget + _LEGACY_ANSWER_RESERVE),
        )
    effort = "high" if thinking == "xhigh" and _NO_XHIGH_MODEL.search(model) else thinking
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}, max_tokens


def _tool_messages(messages: list[AgentMessage]) -> tuple[str, list[dict]]:
    system = "\n\n".join(m.get("content", "") for m in messages if m["role"] == "system")
    result: list[dict] = []
    for message in messages:
        if message["role"] == "system":
            continue
        role = "user" if message["role"] == "tool" else message["role"]
        blocks: list[dict] = []
        if message["role"] == "tool":
            blocks.append(
                {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": message.get("content", "")}
            )
        elif metadata := native_metadata(message, "anthropic"):
            blocks = metadata["content"]
        else:
            if message.get("content"):
                blocks.append({"type": "text", "text": message["content"]})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call["id"],
                    "name": call["name"],
                    "input": history_arguments(call["arguments"]),
                }
                for call in message.get("tool_calls", [])
            )
        if result and result[-1]["role"] == role:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": role, "content": list(blocks)})
    return system, result


class AnthropicProvider(LLMProvider):
    uses_dispatch_hooks = True

    def _client(self) -> AsyncAnthropic:
        hooks = current_dispatch_hooks()
        http_options = {"event_hooks": hooks.event_hooks} if hooks else {}
        sdk_options = {"max_retries": 0} if hooks else {}
        return AsyncAnthropic(
            api_key=self.api_key,
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
        system, rest = split_system(messages)
        kwargs, limit = thinking_kwargs(model, thinking, max_tokens or DEFAULT_MAX_TOKENS)
        if system:
            kwargs["system"] = system
        # 开启思考时不能自定义 temperature
        if temperature is not None and thinking == "off":
            kwargs["temperature"] = temperature
        try:
            async with client.messages.stream(
                model=model,
                max_tokens=limit,
                messages=[{"role": m["role"], "content": m["content"]} for m in rest],
                **kwargs,
            ) as stream:
                # text_stream 只包含正文，不含 thinking 块
                async for text in stream.text_stream:
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
            return [
                ModelInfo(id=m.id, name=getattr(m, "display_name", None) or None)
                async for m in client.models.list()
                if m.id
            ]
        except Exception as exc:  # noqa: BLE001
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
        try:
            client = self._client()
            system, rest = _tool_messages(messages)
            kwargs, limit = thinking_kwargs(model, thinking, max_tokens or DEFAULT_MAX_TOKENS)
            if system:
                kwargs["system"] = system
            if temperature is not None and thinking == "off":
                kwargs["temperature"] = temperature
            calls: dict[int, ToolCall] = {}
            initial_inputs: dict[int, dict] = {}
            completed: set[int] = set()
            stopped = False
            async with client.messages.stream(
                model=model,
                max_tokens=limit,
                messages=rest,
                tools=[
                    {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools
                ],
                tool_choice={"type": tool_choice},
                **kwargs,
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            calls[event.index] = ToolCall(id=block.id, name=block.name, arguments="")
                            initial_inputs[event.index] = block.input
                        elif block.type == "text" and block.text:
                            yield "text", {"text": block.text}
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield "text", {"text": event.delta.text}
                        elif event.delta.type == "input_json_delta" and event.index in calls:
                            calls[event.index]["arguments"] += event.delta.partial_json
                    elif event.type == "content_block_stop":
                        completed.add(event.index)
                    elif event.type == "message_stop":
                        stopped = True
                if not stopped or not calls.keys() <= completed:
                    raise AIProviderError("模型工具调用流意外中断，请重试")
                final = await stream.get_final_message()
            if calls and final.stop_reason not in ("tool_use", "end_turn"):
                raise AIProviderError("模型工具调用未完整生成，请提高最大输出长度后重试")
            yield (
                "usage",
                {
                    "input_tokens": final.usage.input_tokens
                    + (final.usage.cache_creation_input_tokens or 0)
                    + (final.usage.cache_read_input_tokens or 0),
                    "output_tokens": final.usage.output_tokens,
                },
            )
            for index, call in sorted(calls.items()):
                if not call["arguments"]:
                    call["arguments"] = json.dumps(initial_inputs[index], ensure_ascii=False)
                if index == min(calls):
                    call["provider_metadata"] = {
                        "provider": "anthropic",
                        "content": [block.model_dump(mode="json", exclude_none=True) for block in final.content],
                    }
                yield "tool_call", call
            yield "finish", {"reason": final.stop_reason or "end_turn"}
        except AIProviderError:
            raise
        except Exception as exc:
            raise AIProviderError(describe_tools_error(exc)) from exc
        finally:
            if client is not None:
                await client.close()
