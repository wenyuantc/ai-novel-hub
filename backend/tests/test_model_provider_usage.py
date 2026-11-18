"""用本地 SDK 传输验证真实模型尝试与计量，不访问外部端点。"""

import json
from contextlib import aclosing, asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest

from app.ai.providers import openai_compat
from app.ai.providers.base import AIProviderError, RequestPolicyError
from app.ai.providers.dispatch_hooks import DispatchHooks, model_client_event_hooks, use_dispatch_hooks
from app.ai.providers.openai_compat import OpenAICompatProvider
from app.ai.providers.usage import ModelUsage, model_attempt, observe_model_usage
from tests.test_agent_providers import MESSAGES, SDKTransport, anthropic_stream, gemini_chunk, openai_chunk


class UsageObserver:
    def __init__(self):
        self.pending: list[tuple[str, datetime]] = []
        self.finished: list[ModelUsage] = []

    async def begin(self, attempt_id, started_at):
        self.pending.append((attempt_id, started_at))

    async def finish(self, snapshot):
        self.finished.append(snapshot)


def _anthropic_response(*, use_tool=False):
    blocks = [
        ({"type": "text", "text": ""}, [{"type": "text_delta", "text": "正文"}]),
    ]
    if use_tool:
        blocks.append(({"type": "tool_use", "id": "read-1", "name": "read_chapter", "input": {}}, []))
    events = anthropic_stream(blocks, reason="tool_use" if use_tool else "end_turn")
    events[0]["message"]["usage"].update(cache_creation_input_tokens=2, cache_read_input_tokens=3)
    return events


@pytest.mark.parametrize(("provider", "frames", "expected"), [
    ("openai", [
        openai_chunk({"content": "正文"}, finish="stop"),
        openai_chunk(usage={
            "prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 4},
        }),
    ], (8, 4, 4)),
    ("anthropic", _anthropic_response(), (16, 15, 3)),
    ("gemini", [
        gemini_chunk([{"text": "思考", "thought": True}], usage={
            "promptTokenCount": 7, "candidatesTokenCount": 0, "cachedContentTokenCount": 3,
        }),
        gemini_chunk([{"text": "正文"}], finish="STOP", usage={"candidatesTokenCount": 3, "thoughtsTokenCount": 8}),
    ], (7, 11, 3)),
])
async def test_text_stream_keeps_existing_output_and_reports_final_usage(monkeypatch, provider, frames, expected):
    sdk = SDKTransport(monkeypatch, provider, [frames])
    observer = UsageObserver()
    with observe_model_usage(observer):
        assert [text async for text in sdk.provider.generate_stream(MESSAGES, "test-model")] == ["正文"]
    assert len(sdk.requests) == len(observer.pending) == len(observer.finished) == 1
    if provider == "openai":
        assert sdk.requests[0]["stream_options"] == {"include_usage": True}
    [result] = observer.finished
    assert (result.input_tokens, result.output_tokens, result.cached_input_tokens) == expected
    assert result.status == "completed" and result.sent is True
    assert result.image_count is None and result.dispatched_at is not None
    assert observer.pending == [(result.attempt_id, result.started_at)]
    assert UUID(result.attempt_id).version == 4 and result.started_at.tzinfo is UTC
    assert all(client.is_closed for client in sdk.clients)


@pytest.mark.parametrize(("provider", "frames", "expected"), [
    ("openai", [
        openai_chunk({"content": "正文"}),
        openai_chunk({"tool_calls": [{
            "index": 0, "id": "read-1", "function": {"name": "read_chapter", "arguments": "{}"},
        }]}, finish="tool_calls"),
        openai_chunk(usage={
            "prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12,
            "prompt_cache_hit_tokens": 4, "prompt_cache_miss_tokens": 4,
        }),
    ], (8, 4, 4)),
    ("anthropic", _anthropic_response(use_tool=True), (16, 15, 3)),
    ("gemini", [
        gemini_chunk(
            [{"text": "正文"}, {"functionCall": {"id": "read-1", "name": "read_chapter", "args": {}}}],
            finish="STOP",
            usage={"promptTokenCount": 7, "candidatesTokenCount": 11, "cachedContentTokenCount": 3},
        ),
    ], (7, 11, 3)),
])
async def test_tool_stream_records_cache_hits_and_keeps_event_format(monkeypatch, provider, frames, expected):
    sdk = SDKTransport(monkeypatch, provider, [frames])
    observer = UsageObserver()
    with observe_model_usage(observer):
        events = await sdk.run()
    [result] = observer.finished
    inputs, outputs, cached = expected
    assert (result.input_tokens, result.output_tokens, result.cached_input_tokens) == (inputs, outputs, cached)
    assert ("usage", {"input_tokens": inputs, "output_tokens": outputs}) in events
    assert ("text", {"text": "正文"}) in events
    [tool_call] = [data for event, data in events if event == "tool_call"]
    assert tool_call["name"] == "read_chapter" and tool_call["arguments"] == "{}"


@pytest.mark.parametrize(("cache_fields", "expected"), [
    pytest.param({}, None, id="missing"),
    pytest.param({"prompt_tokens_details": {"cached_tokens": 0}}, 0, id="openai-zero"),
    pytest.param({"prompt_cache_hit_tokens": 0}, 0, id="deepseek-zero"),
    pytest.param({"prompt_tokens_details": {"cached_tokens": 4}, "prompt_cache_hit_tokens": 4}, 4, id="both"),
    pytest.param({"prompt_tokens_details": {"cached_tokens": -1}}, None, id="negative"),
    pytest.param({"prompt_cache_hit_tokens": 9}, None, id="above-total"),
])
async def test_openai_cache_usage_distinguishes_zeros_and_unknowns(monkeypatch, cache_fields, expected):
    frames = [
        openai_chunk({"content": "正文"}, finish="stop"),
        openai_chunk(usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12, **cache_fields}),
    ]
    sdk = SDKTransport(monkeypatch, "openai", [frames])
    observer = UsageObserver()
    with observe_model_usage(observer):
        assert [text async for text in sdk.provider.generate_stream(MESSAGES, "test-model")] == ["正文"]
    assert observer.finished[0].cached_input_tokens == expected


def _mock_openai(monkeypatch, handler):
    def http_client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(openai_compat, "DefaultAsyncHttpxClient", http_client)
    return OpenAICompatProvider(api_key="test-key")


async def test_openai_usage_option_rejection_is_an_independent_attempt(monkeypatch):
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if "stream_options" in body:
            return httpx.Response(400, json={"error": {"message": "Unknown parameter: stream_options"}})
        frame = json.dumps(openai_chunk({"content": "text"}, finish="stop"))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=f"data: {frame}\n\n")

    provider = _mock_openai(monkeypatch, handle)
    observer = UsageObserver()
    with observe_model_usage(observer):
        assert [text async for text in provider.generate_stream(MESSAGES, "test-model")] == ["text"]
    assert len(requests) == len(observer.pending) == 2
    first, second = observer.finished
    assert first.attempt_id != second.attempt_id
    assert first.sent is True and second.sent is True
    assert first.status == "failed" and second.status == "completed"
    assert first.input_tokens is None and second.input_tokens is None
    assert all(result.started_at <= result.dispatched_at for result in (first, second))


async def test_one_shot_stream_does_not_request_usage_but_records_spontaneous_counts(monkeypatch):
    sdk = SDKTransport(monkeypatch, "openai", [[
        openai_chunk({"content": "正文"}, finish="stop"),
        openai_chunk(usage={"prompt_tokens": 0, "completion_tokens": 3, "total_tokens": 3}),
    ]])
    observer = UsageObserver()
    observer.request_usage_options = False
    with observe_model_usage(observer):
        assert [text async for text in sdk.provider.generate_stream(MESSAGES, "test-model")] == ["正文"]
    assert len(sdk.requests) == 1 and "stream_options" not in sdk.requests[0]
    assert (observer.finished[0].input_tokens, observer.finished[0].output_tokens) == (0, 3)


async def test_early_close_keeps_partial_usage_and_one_attempt(monkeypatch):
    events = [
        openai_chunk(usage={
            "prompt_tokens": 9, "completion_tokens": 0, "total_tokens": 9,
            "prompt_tokens_details": {"cached_tokens": 3},
        }),
        openai_chunk({"content": "开头"}),
    ]
    sdk = SDKTransport(monkeypatch, "openai", [events])
    observer = UsageObserver()
    with observe_model_usage(observer):
        async with aclosing(sdk.provider.generate_stream(MESSAGES, "test-model")) as stream:
            assert await anext(stream) == "开头"
    [result] = observer.finished
    assert result.status == "cancelled" and result.sent is True
    assert (result.input_tokens, result.output_tokens, result.cached_input_tokens) == (9, 0, 3)
    assert sdk.clients[0].is_closed


async def test_unanswered_model_dispatch_is_ambiguous_and_not_implicitly_retried(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        raise httpx.ReadError("upstream closed", request=request)

    provider = _mock_openai(monkeypatch, handle)
    observer = UsageObserver()

    @asynccontextmanager
    async def lease():
        yield

    hooks = DispatchHooks(lease)
    try:
        with observe_model_usage(observer), use_dispatch_hooks(hooks), pytest.raises(AIProviderError):
            await provider.embed(["文字"], "test-model")
    finally:
        await hooks.aclose()
    assert len(requests) == len(observer.pending) == len(observer.finished) == 1
    [result] = observer.finished
    assert result.status == "failed" and result.sent is None and result.dispatched_at is not None


@pytest.mark.parametrize("disconnect", [False, True])
async def test_unguarded_sdk_keeps_internal_retries_and_records_each_attempt(monkeypatch, disconnect):
    retries = []

    def handle(request):
        retries.append(request.headers["x-stainless-retry-count"])
        if len(retries) == 1:
            if disconnect:
                raise httpx.ReadError("upstream closed", request=request)
            return httpx.Response(503, json={"error": {"message": "Unavailable"}})
        return httpx.Response(200, json={
            "data": [{"index": 0, "embedding": [0.75]}],
            "usage": {"prompt_tokens": 7, "total_tokens": 7},
        })

    provider = _mock_openai(monkeypatch, handle)
    observer = UsageObserver()
    with observe_model_usage(observer):
        assert await provider.embed(["文字"], "test-model") == [[0.75]]
    previous, final = observer.finished
    assert retries == ["0", "1"] and len(observer.pending) == 2
    assert previous.attempt_id != final.attempt_id
    assert previous.status == "failed" and previous.sent is (None if disconnect else True)
    assert previous.input_tokens is None and final.input_tokens == 7 and final.sent is True


async def test_passive_hooks_count_retries_without_sdk_header_but_not_redirects():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if calls == ["/model"]:
            return httpx.Response(307, headers={"location": "/continued-model"})
        return httpx.Response(503 if request.url.path == "/continued-model" else 200)

    observer = UsageObserver()
    with observe_model_usage(observer):
        hooks = model_client_event_hooks()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True, event_hooks=hooks,
        ) as client, model_attempt() as attempt:
            assert (await attempt.invoke(client.post("https://test.example/model"))).status_code == 503
            attempt.set_usage(input_tokens=7, cached_input_tokens=3)
            assert (await attempt.invoke(client.post("https://test.example/model"))).status_code == 200
            attempt.set_usage(input_tokens=9)
    assert calls == ["/model", "/continued-model", "/model"]
    assert len(observer.pending) == 2
    first, last = observer.finished
    assert (first.status, first.sent, first.input_tokens, first.cached_input_tokens) == ("failed", True, 7, 3)
    assert (last.status, last.sent, last.input_tokens, last.cached_input_tokens) == ("completed", True, 9, None)


async def test_denied_request_records_proven_unsent_without_consuming_upstream(monkeypatch):
    @asynccontextmanager
    async def acquire():
        raise RequestPolicyError("本地发送策略拒绝")
        yield

    def reject_transport(_request):
        pytest.fail("发送前拒绝不应触达上游")

    provider = _mock_openai(monkeypatch, reject_transport)
    observer = UsageObserver()
    hooks = DispatchHooks(acquire)
    with pytest.raises(RequestPolicyError, match="本地发送策略拒绝"):
        with observe_model_usage(observer), use_dispatch_hooks(hooks):
            await anext(provider.generate_stream(MESSAGES, "test-model"))
    await hooks.aclose()
    [result] = observer.finished
    assert result.sent is False and result.status == "failed" and result.dispatched_at is None


@pytest.mark.parametrize("tokens", [0, None])
async def test_openai_embedding_usage_distinguishes_reported_zero_and_missing(monkeypatch, tokens):
    def handle(request):
        assert request.url.path.endswith("/embeddings")
        return httpx.Response(200, json={
            "data": [{"index": 0, "embedding": [0.75, 0.25]}],
            **({"usage": {"prompt_tokens": tokens, "total_tokens": tokens}} if tokens is not None else {}),
        })

    provider = _mock_openai(monkeypatch, handle)
    observer = UsageObserver()
    with observe_model_usage(observer):
        assert await provider.embed(["文字"], "test-model") == [[0.75, 0.25]]
    [result] = observer.finished
    assert result.input_tokens == tokens and result.output_tokens is None and result.sent is True


async def test_image_asset_download_failure_keeps_count_and_does_not_record_another_call(monkeypatch):
    def handle(request):
        assert request.method == "POST"
        return httpx.Response(200, json={
            "data": [{"url": "https://assets.example/cover.png"}],
            "usage": {"input_tokens": 0, "output_tokens": 4, "total_tokens": 4},
        })

    def download_client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404)), **kwargs)

    monkeypatch.setattr(openai_compat, "AsyncClient", download_client)
    provider = _mock_openai(monkeypatch, handle)
    observer = UsageObserver()
    with observe_model_usage(observer), pytest.raises(AIProviderError):
        await provider.generate_image("封面", "test-model")
    [result] = observer.finished
    assert result.image_count == 1 and result.sent is True and result.status == "completed"
    assert (result.input_tokens, result.output_tokens) == (0, 4)


async def test_hook_completion_callbacks_run_after_an_unanswered_lease_closes():
    actions = []

    @asynccontextmanager
    async def lease():
        try:
            yield
        finally:
            actions.append("lease closed")

    async def finalize():
        actions.append("finalized")

    def fail(_request):
        raise httpx.ReadError("connection ended")

    hooks = DispatchHooks(lease)
    hooks.after_close(finalize)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail), event_hooks=hooks.event_hooks) as client:
        with pytest.raises(httpx.ReadError):
            await client.get("https://test.example")
    await hooks.aclose()
    await hooks.aclose()
    assert actions == ["lease closed", "finalized"]
