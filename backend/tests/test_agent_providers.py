"""通过 SDK + 假 HTTP/SSE 验证原生协议；不访问外部模型。"""

import asyncio
import json

import httpx
import httpx2
import pytest
from anthropic import AsyncAnthropic
from google import genai
from google.genai import types
from openai import AsyncOpenAI

from app.ai.providers.anthropic import AnthropicProvider
from app.ai.providers.base import TOOLS_UNSUPPORTED, AIProviderError, LLMProvider
from app.ai.providers.gemini import GeminiProvider, _gemini_schema
from app.ai.providers.openai_compat import OpenAICompatProvider
from tests.helpers import FakeLLM, Gate

TOOLS = [{"name": "read_chapter", "description": "读取章节", "parameters": {"type": "object", "properties": {}}}]
MESSAGES = [{"role": "system", "content": "系统约束"}, {"role": "user", "content": "读取章节"}]


class SDKTransport:
    def __init__(self, monkeypatch, kind, responses):
        self.requests = []
        self.clients = []
        self.responses = list(responses)
        self.kind = kind
        self.http = httpx2 if kind == "anthropic" else httpx
        cls = {"openai": OpenAICompatProvider, "anthropic": AnthropicProvider, "gemini": GeminiProvider}[kind]
        self.provider = cls(api_key="test-key")
        monkeypatch.setattr(self.provider, "_client", self.make_client)

    def handle(self, request):
        self.requests.append(json.loads(request.content))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            status, message = response
            return self.http.Response(
                status, json={"error": {"message": message, "code": status, "type": "invalid_request"}}
            )
        frames = []
        for chunk in response:
            prefix = f"event: {chunk['type']}\n" if self.kind == "anthropic" else ""
            frames.append(f"{prefix}data: {json.dumps(chunk)}\n\n")
        if self.kind == "openai":
            frames.append("data: [DONE]\n\n")
        return self.http.Response(200, headers={"content-type": "text/event-stream"}, content="".join(frames).encode())

    def make_client(self):
        if self.kind == "gemini":
            client = genai.Client(
                api_key="test-key",
                http_options=types.HttpOptions(
                    async_client_args={"transport": httpx.MockTransport(self.handle)}, retry_options={"attempts": 1}
                ),
            )
            self.clients.append(client._api_client._async_httpx_client)
            return client
        http = self.http.AsyncClient(transport=self.http.MockTransport(self.handle))
        self.clients.append(http)
        if self.kind == "openai":
            return AsyncOpenAI(api_key="test-key", http_client=http, max_retries=0)
        if self.kind == "anthropic":
            return AsyncAnthropic(api_key="test-key", http_client=http, max_retries=0)
        raise AssertionError(self.kind)

    async def run(self, messages=None, **kwargs):
        return [
            event
            async for event in self.provider.chat_tools_stream(messages or MESSAGES, "test-model", TOOLS, **kwargs)
        ]


def openai_chunk(delta=None, finish=None, usage=None):
    return {
        "id": "chat-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}] if usage is None else [],
        "usage": usage,
    }


def anthropic_stream(blocks, *, reason="tool_use", stop=True):
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "model": "test-model",
                "content": [],
                "usage": {"input_tokens": 11, "output_tokens": 1},
            },
        }
    ]
    for index, (block, deltas) in enumerate(blocks):
        events.append({"type": "content_block_start", "index": index, "content_block": block})
        events.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas)
        events.append({"type": "content_block_stop", "index": index})
    events.append({"type": "message_delta", "delta": {"stop_reason": reason}, "usage": {"output_tokens": 15}})
    if stop:
        events.append({"type": "message_stop"})
    return events


def gemini_chunk(parts, finish=None, usage=None):
    return {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
        **({"usageMetadata": usage} if usage else {}),
    }


def tool_calls(events):
    return [data for event, data in events if event == "tool_call"]


async def test_openai_interleaved_calls_usage_and_history(monkeypatch):
    sdk = SDKTransport(
        monkeypatch,
        "openai",
        [
            [
                openai_chunk({"content": "正在读取"}),
                openai_chunk(
                    {"tool_calls": [{"index": 1, "id": "two", "function": {"name": "read_chapter", "arguments": "{"}}]}
                ),
                openai_chunk(
                    {"tool_calls": [{"index": 0, "id": "one", "function": {"name": "read_", "arguments": '{"id":'}}]}
                ),
                openai_chunk(
                    {
                        "content": "两章",
                        "tool_calls": [{"index": 0, "function": {"name": "chapter", "arguments": "1}"}}],
                    }
                ),
                openai_chunk({"tool_calls": [{"index": 1, "function": {"arguments": '"id":2}'}}]}, "tool_calls"),
                openai_chunk(usage={"prompt_tokens": 12, "completion_tokens": 9, "total_tokens": 21}),
            ],
            [openai_chunk({"content": "完成"}, "stop")],
        ],
    )
    events = await sdk.run(thinking="high", max_tokens=123, temperature=0.4)
    calls = tool_calls(events)
    assert calls == [
        {"id": "one", "name": "read_chapter", "arguments": '{"id":1}'},
        {"id": "two", "name": "read_chapter", "arguments": '{"id":2}'},
    ]
    assert [data["text"] for event, data in events if event == "text"] == ["正在读取", "两章"]
    assert ("usage", {"input_tokens": 12, "output_tokens": 9}) in events
    assert events[-1] == ("finish", {"reason": "tool_calls"})
    calls[0]["provider_metadata"] = {"provider": "gemini", "parts": [{"text": "不能传给 OpenAI"}]}
    history = MESSAGES + [
        {"role": "assistant", "content": "正在读取两章", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "one", "name": "read_chapter", "content": "第一章"},
        {"role": "tool", "tool_call_id": "two", "name": "read_chapter", "content": "第二章"},
    ]
    await sdk.run(history, tool_choice="none")
    request = sdk.requests[0]
    assert request["stream_options"] == {"include_usage": True}
    assert request["tools"][0] == {"type": "function", "function": TOOLS[0]}
    assert (request["reasoning_effort"], request["max_tokens"], request["temperature"]) == ("high", 123, 0.4)
    second = sdk.requests[1]
    assert second["tool_choice"] == "none"
    assert second["messages"][-2] == {"role": "tool", "tool_call_id": "one", "content": "第一章"}
    assert second["messages"][-3]["tool_calls"][0]["function"]["arguments"] == '{"id":1}'
    assert "provider_metadata" not in json.dumps(second)
    assert all(client.is_closed for client in sdk.clients)


async def test_openai_stream_options_retry_once(monkeypatch):
    sdk = SDKTransport(
        monkeypatch, "openai", [(400, "unsupported stream_options"), [openai_chunk({"content": "ok"}, "stop")]]
    )
    assert ("text", {"text": "ok"}) in await sdk.run()
    assert "stream_options" in sdk.requests[0] and "stream_options" not in sdk.requests[1]
    twice = SDKTransport(
        monkeypatch, "openai", [(400, "unsupported stream_options"), (400, "unsupported stream_options")]
    )
    with pytest.raises(AIProviderError, match="stream_options"):
        await twice.run()
    assert len(twice.requests) == 2


async def test_anthropic_tool_results_and_signed_thinking_roundtrip(monkeypatch):
    blocks = [
        (
            {"type": "thinking", "thinking": "", "signature": ""},
            [
                {"type": "thinking_delta", "thinking": "私有思考"},
                {"type": "signature_delta", "signature": "signed-thinking"},
            ],
        ),
        ({"type": "redacted_thinking", "data": "opaque"}, []),
        ({"type": "text", "text": ""}, [{"type": "text_delta", "text": "正在读取"}]),
        (
            {"type": "tool_use", "id": "one", "name": "read_chapter", "input": {}},
            [
                {"type": "input_json_delta", "partial_json": '{"id":'},
                {"type": "input_json_delta", "partial_json": "1}"},
            ],
        ),
        ({"type": "tool_use", "id": "two", "name": "read_chapter", "input": {}}, []),
    ]
    sdk = SDKTransport(
        monkeypatch,
        "anthropic",
        [anthropic_stream(blocks), anthropic_stream([({"type": "text", "text": "完成"}, [])], reason="end_turn")],
    )
    events = await sdk.run(thinking="high", temperature=0.2)
    calls = tool_calls(events)
    assert [call["arguments"] for call in calls] == ['{"id":1}', "{}"]
    assert ("usage", {"input_tokens": 11, "output_tokens": 15}) in events
    assert [data for event, data in events if event == "text"] == [{"text": "正在读取"}]
    calls = json.loads(json.dumps(calls))
    native = calls[0]["provider_metadata"]["content"]
    assert native[0] == {"type": "thinking", "thinking": "私有思考", "signature": "signed-thinking"}
    assert native[1] == {"type": "redacted_thinking", "data": "opaque"}
    history = MESSAGES + [
        {"role": "assistant", "content": "正在读取", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "one", "name": "read_chapter", "content": "第一章"},
        {"role": "tool", "tool_call_id": "two", "name": "read_chapter", "content": "第二章"},
        {"role": "user", "content": "继续"},
    ]
    await sdk.run(history, tool_choice="none")
    assert sdk.requests[0]["thinking"] == {"type": "adaptive"}
    assert "temperature" not in sdk.requests[0]
    assert sdk.requests[0]["tools"][0]["input_schema"] == TOOLS[0]["parameters"]
    second = sdk.requests[1]
    assert second["system"] == "系统约束"
    assert second["tool_choice"] == {"type": "none"}
    assert second["messages"][-2]["content"] == native
    assert second["messages"][-1] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "one", "content": "第一章"},
            {"type": "tool_result", "tool_use_id": "two", "content": "第二章"},
            {"type": "text", "text": "继续"},
        ],
    }
    assert all(client.is_closed for client in sdk.clients)


async def test_anthropic_usage_includes_cached_input_tokens(monkeypatch):
    response = anthropic_stream([({"type": "text", "text": "完成"}, [])], reason="end_turn")
    response[0]["message"]["usage"].update(cache_creation_input_tokens=20, cache_read_input_tokens=30)
    response[-2]["usage"].update(input_tokens=12, cache_creation_input_tokens=21, cache_read_input_tokens=31)
    sdk = SDKTransport(monkeypatch, "anthropic", [response])
    events = await sdk.run()
    assert ("usage", {"input_tokens": 64, "output_tokens": 15}) in events


async def test_gemini_signatures_usage_and_function_ids_roundtrip(monkeypatch):
    parts = [
        {"text": "思考", "thought": True, "thoughtSignature": "/wBzaWduZWQ="},
        {"text": "正在读取"},
        {"functionCall": {"name": "read_chapter", "args": {"id": 1}}, "thoughtSignature": "c2lnbmVk"},
        {"functionCall": {"name": "read_chapter", "id": "native-2", "args": {"id": 2}}},
    ]
    sdk = SDKTransport(
        monkeypatch,
        "gemini",
        [
            [gemini_chunk([part]) for part in parts]
            + [gemini_chunk([], "STOP", {"promptTokenCount": 21, "candidatesTokenCount": 13, "thoughtsTokenCount": 7})],
            [gemini_chunk([{"functionCall": {"name": "read_chapter", "args": {}}}], "STOP")],
        ],
    )
    events = await sdk.run(max_tokens=123, thinking="high", temperature=0.4)
    calls = tool_calls(events)
    assert calls[0]["id"].startswith("call_") and calls[1]["id"] == "native-2"
    assert ("usage", {"input_tokens": 21, "output_tokens": 20}) in events
    assert [data for event, data in events if event == "text"] == [{"text": "正在读取"}]
    calls = json.loads(json.dumps(calls))
    native = calls[0]["provider_metadata"]["parts"]
    assert types.Part.model_validate(native[0]).thought_signature == b"\xff\x00signed"
    history = MESSAGES + [
        {"role": "assistant", "content": "正在读取", "tool_calls": calls},
        {
            "role": "tool",
            "tool_call_id": calls[0]["id"],
            "name": "read_chapter",
            "content": '{"untrusted":true,"data":"第一章"}',
        },
        {"role": "tool", "tool_call_id": "native-2", "name": "read_chapter", "content": "第二章"},
    ]
    next_events = await sdk.run(history, tool_choice="none")
    assert tool_calls(next_events)[0]["id"] not in {call["id"] for call in calls}
    request = sdk.requests[1]
    assert request["toolConfig"]["functionCallingConfig"]["mode"] == "NONE"
    assert sdk.requests[0]["generationConfig"]["maxOutputTokens"] == 123
    restored = request["contents"][-2]["parts"]
    assert len(restored) == len(parts)
    assert types.Part.model_validate(restored[0]).thought_signature == b"\xff\x00signed"
    assert restored[2]["thoughtSignature"] == "c2lnbmVk"
    results = request["contents"][-1]["parts"]
    assert results[0]["functionResponse"] == {"name": "read_chapter", "response": {"untrusted": True, "data": "第一章"}}
    assert results[1]["functionResponse"] == {
        "name": "read_chapter",
        "id": "native-2",
        "response": {"result": "第二章"},
    }
    assert all(client.is_closed for client in sdk.clients)


def test_gemini_schema_resolves_refs_and_preserves_property_names():
    schema = {
        "$schema": "schema",
        "$defs": {
            "Entry": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"title": {"type": "string", "default": "a"}},
            }
        },
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "additionalProperties": {"type": "string"},
            "rows": {"type": "array", "items": {"$ref": "#/$defs/Entry"}},
            "optional": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
            "id": {"type": "integer", "exclusiveMinimum": 0, "exclusiveMaximum": 10},
        },
    }
    result = _gemini_schema(schema)
    assert "additionalProperties" not in result and "$defs" not in result
    assert result["properties"]["additionalProperties"] == {"type": "string"}
    assert result["properties"]["rows"]["items"] == {"type": "object", "properties": {"title": {"type": "string"}}}
    assert result["properties"]["optional"] == {"type": "integer", "nullable": True}
    assert result["properties"]["id"] == {"type": "integer", "minimum": 1, "maximum": 9}
    types.Schema.model_validate(result)
    assert schema["additionalProperties"] is False


def test_all_registered_tool_schemas_are_accepted_by_gemini_sdk():
    from app.services.agent.tools import list_tools

    for tool in list_tools():
        types.FunctionDeclaration(
            name=tool.name, description=tool.description, parameters=_gemini_schema(tool.parameters)
        )


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
@pytest.mark.parametrize(
    ("status", "message", "unsupported"),
    [
        (400, "tools unsupported for this model", True),
        (404, "function calling not available", True),
        (400, "Unrecognized request argument supplied: tools", True),
        (400, "Unknown parameter: tools", True),
        (400, "Invalid schema for function read_chapter: required field missing", False),
        (400, "Unsupported schema keyword in tools: anyOf", False),
        (400, "Unknown tool_call_id in tool result", False),
        (422, "Tool result must follow tool use", False),
        (401, "invalid token for tools request", False),
        (429, "function quota exhausted", False),
    ],
)
async def test_provider_error_mapping(monkeypatch, kind, status, message, unsupported):
    sdk = SDKTransport(monkeypatch, kind, [(status, message)])
    with pytest.raises(AIProviderError) as caught:
        await sdk.run()
    if unsupported:
        assert str(caught.value) == TOOLS_UNSUPPORTED
    else:
        assert str(caught.value).startswith("模型调用失败：")
        assert message in str(caught.value)
    assert all(client.is_closed for client in sdk.clients)


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
async def test_truncated_stream_never_exposes_tool_calls(monkeypatch, kind):
    streams = {
        "openai": [
            openai_chunk(
                {"tool_calls": [{"index": 0, "id": "one", "function": {"name": "read_chapter", "arguments": "{"}}]}
            )
        ],
        "anthropic": anthropic_stream(
            [({"type": "tool_use", "id": "one", "name": "read_chapter", "input": {}}, [])], stop=False
        ),
        "gemini": [gemini_chunk([{"functionCall": {"name": "read_chapter", "args": {}}}])],
    }
    sdk = SDKTransport(monkeypatch, kind, [streams[kind]])
    seen = []
    with pytest.raises(AIProviderError, match="中断"):
        async for event in sdk.provider.chat_tools_stream(MESSAGES, "test-model", TOOLS):
            seen.append(event)
    assert not tool_calls(seen)
    assert all(client.is_closed for client in sdk.clients)


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
async def test_output_limit_never_exposes_partial_tool_calls(monkeypatch, kind):
    streams = {
        "openai": [
            openai_chunk(
                {"tool_calls": [{"index": 0, "id": "one", "function": {"name": "read_chapter", "arguments": "{"}}]},
                "length",
            )
        ],
        "anthropic": anthropic_stream(
            [({"type": "tool_use", "id": "one", "name": "read_chapter", "input": {}}, [])], reason="max_tokens"
        ),
        "gemini": [gemini_chunk([{"functionCall": {"name": "read_chapter", "args": {}}}], "MAX_TOKENS")],
    }
    sdk = SDKTransport(monkeypatch, kind, [streams[kind]])
    seen = []
    with pytest.raises(AIProviderError, match="未完整生成"):
        async for event in sdk.provider.chat_tools_stream(MESSAGES, "test-model", TOOLS):
            seen.append(event)
    assert not tool_calls(seen)


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
async def test_closing_stream_releases_client(monkeypatch, kind):
    streams = {
        "openai": [openai_chunk({"content": "正文"}), openai_chunk(finish="stop")],
        "anthropic": anthropic_stream([({"type": "text", "text": "正文"}, [])], reason="end_turn"),
        "gemini": [gemini_chunk([{"text": "正文"}], "STOP")],
    }
    sdk = SDKTransport(monkeypatch, kind, [streams[kind]])
    stream = sdk.provider.chat_tools_stream(MESSAGES, "test-model", TOOLS)
    assert await anext(stream) == ("text", {"text": "正文"})
    await stream.aclose()
    assert all(client.is_closed for client in sdk.clients)


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
async def test_client_setup_error_is_wrapped(monkeypatch, kind):
    sdk = SDKTransport(monkeypatch, kind, [])

    def fail():
        raise ValueError("无效地址")

    monkeypatch.setattr(sdk.provider, "_client", fail)
    with pytest.raises(AIProviderError, match="模型调用失败：无效地址"):
        await sdk.run()


async def test_base_provider_does_not_fall_back_to_text():
    class TextOnly(LLMProvider):
        async def generate_stream(self, messages, model, **kwargs):
            yield "不应调用"

    with pytest.raises(AIProviderError, match="不支持工具调用"):
        _ = [event async for event in TextOnly("key").chat_tools_stream(MESSAGES, "model", TOOLS)]


async def test_fake_llm_tool_script_records_immutable_history_and_cancellation():
    fake = FakeLLM()
    gate = Gate()
    fake.tool_script = [
        [("text", {"text": "读取"}), ("tool_call", {"id": "one", "name": "read_chapter", "arguments": "{}"})],
        [gate],
    ]
    provider = fake.factory("openai_compat", "key", None, 30)
    messages = [{"role": "user", "content": "原始消息"}]
    events = [event async for event in provider.chat_tools_stream(messages, "model", TOOLS, tool_choice="none")]
    messages[0]["content"] = "修改过的消息"
    assert fake.tool_calls[0]["messages"][0]["content"] == "原始消息"
    assert fake.tool_calls[0]["tools"] == TOOLS and fake.tool_calls[0]["tool_choice"] == "none"
    assert fake.calls == fake.tool_calls
    assert events[-1] == ("finish", {"reason": "stop"})
    pending = asyncio.create_task(anext(provider.chat_tools_stream(messages, "model", TOOLS)))
    await gate.wait_reached()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert fake.aborted == 1


def test_cross_provider_history_tolerates_invalid_tool_arguments():
    # OpenAI 兼容模型产生的非法参数原文：换用 Anthropic / Gemini 后历史仍能转换，不让会话永久失败。
    from app.ai.providers.anthropic import _tool_messages
    from app.ai.providers.gemini import _tool_contents

    history = [
        {"role": "user", "content": "读取"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "read_chapter", "arguments": "{bad"}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_chapter", "content": '{"error": "参数错误"}'},
    ]
    _, anthropic_messages = _tool_messages(history)
    assert anthropic_messages[1]["content"][0] == {"type": "tool_use", "id": "c1", "name": "read_chapter", "input": {}}
    _, gemini_contents = _tool_contents(history)
    assert gemini_contents[1].parts[0].function_call.args == {}
