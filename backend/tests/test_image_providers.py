"""用真实 SDK + 假 HTTP 验证图片生成协议，下载与 SSRF 不访问外网。"""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from google import genai
from google.genai import types
from openai import AsyncOpenAI

from app.ai.images import MAX_COVER_BYTES
from app.ai.providers.anthropic import AnthropicProvider
from app.ai.providers.base import AIProviderError
from app.ai.providers.gemini import GeminiProvider
from app.ai.providers.openai_compat import OpenAICompatProvider
from tests.cover_fixtures import PNG


def openai_provider(monkeypatch, responses):
    calls = []
    pending = list(responses)

    def handle(request):
        calls.append(json.loads(request.content))
        status, data = pending.pop(0)
        return httpx.Response(status, json=data)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client = AsyncOpenAI(api_key="test-key", http_client=http, max_retries=0)
    provider = OpenAICompatProvider(api_key="test-key")
    monkeypatch.setattr(provider, "_client", lambda: client)
    return provider, calls, http


def image_response():
    return {"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode()}]}


async def test_openai_image_base64_and_sdk_close(monkeypatch):
    provider, calls, http = openai_provider(monkeypatch, [(200, image_response())])
    image = await provider.generate_image("雨夜书封", "gpt-image-1")
    assert image.data == PNG and image.mime_type == "image/png"
    assert calls == [{"model": "gpt-image-1", "prompt": "雨夜书封", "n": 1, "size": "1024x1536"}]
    assert http.is_closed


async def test_openai_size_retries_once_without_response_format(monkeypatch):
    provider, calls, http = openai_provider(monkeypatch, [
        (400, {"error": {"message": "Invalid size: 1024x1536 is not supported", "type": "invalid_request_error"}}),
        (200, image_response()),
    ])
    assert (await provider.generate_image("封面", "dall-e-3")).data == PNG
    assert len(calls) == 2 and "size" in calls[0] and "size" not in calls[1]
    assert all("response_format" not in c for c in calls) and http.is_closed


@pytest.mark.parametrize("status", [400, 401, 429])
async def test_openai_non_size_error_is_not_retried(monkeypatch, status):
    provider, calls, http = openai_provider(monkeypatch, [
        (status, {"error": {"message": "quota unavailable", "type": "invalid_request_error"}}),
    ])
    with pytest.raises(AIProviderError, match="模型调用失败"):
        await provider.generate_image("封面", "image")
    assert len(calls) == 1 and http.is_closed


@pytest.mark.parametrize("data", [[], [{}], [{"b64_json": "%%%"}], [{"b64_json": "dGV4dA=="}]])
async def test_openai_empty_invalid_or_non_image_response(monkeypatch, data):
    provider, _, http = openai_provider(monkeypatch, [(200, {"created": 1, "data": data})])
    with pytest.raises(AIProviderError):
        await provider.generate_image("封面", "image")
    assert http.is_closed


async def test_openai_url_download_has_no_api_credentials(monkeypatch):
    provider, _, sdk_http = openai_provider(monkeypatch, [(200, {"created": 1, "data": [{
        "url": "https://images.example.com/cover.png"
    }]})])
    provider.follow_redirects = False
    downloads = []
    original_client = httpx.AsyncClient
    download_clients = []

    def handle(request):
        downloads.append(request)
        return httpx.Response(200, content=PNG)

    def make_client(**kwargs):
        http = original_client(transport=httpx.MockTransport(handle), **kwargs)
        download_clients.append(http)
        return http

    monkeypatch.setattr("app.ai.providers.openai_compat.AsyncClient", make_client)
    image = await provider.generate_image("封面", "image")
    assert image.data == PNG and image.mime_type == "image/png"
    assert len(downloads) == 1 and "authorization" not in downloads[0].headers
    assert sdk_http.is_closed and all(c.is_closed for c in download_clients)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/cover", "http://169.254.169.254/cover", "https://rebind.example.test/cover", "file:///tmp/x"
])
async def test_openai_url_download_rejects_private_urls(monkeypatch, url):
    provider, _, http = openai_provider(monkeypatch, [(200, {"created": 1, "data": [{"url": url}]})])
    provider.follow_redirects = False
    with pytest.raises(AIProviderError):
        await provider.generate_image("封面", "image")
    assert http.is_closed


@pytest.mark.parametrize("redirect", [True, False])
async def test_openai_download_redirect_and_size_limit(monkeypatch, redirect):
    provider = OpenAICompatProvider(api_key="secret")
    provider.follow_redirects = False
    original_client = httpx.AsyncClient
    calls = []

    def handle(request):
        calls.append(request)
        if redirect:
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
        return httpx.Response(200, content=b"x" * (MAX_COVER_BYTES + 1))

    monkeypatch.setattr(
        "app.ai.providers.openai_compat.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises((AIProviderError, httpx.HTTPStatusError)):
        await provider._download_image("https://images.example.com/cover")
    assert len(calls) == 1


async def test_anthropic_image_generation_is_explicitly_unsupported():
    with pytest.raises(AIProviderError, match="不支持图片生成"):
        await AnthropicProvider(api_key="test").generate_image("封面", "claude")


def gemini_provider(monkeypatch, response):
    calls = []

    def handle(request):
        calls.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json=response)

    client = genai.Client(api_key="test-key", http_options=types.HttpOptions(
        async_client_args={"transport": httpx.MockTransport(handle)}, retry_options={"attempts": 1}
    ))
    provider = GeminiProvider(api_key="test-key")
    monkeypatch.setattr(provider, "_client", lambda: client)
    return provider, calls, client._api_client._async_httpx_client


async def test_gemini_native_image_protocol_and_close(monkeypatch):
    response = {"candidates": [{"content": {"role": "model", "parts": [
        {"text": "这是生成的封面"},
        {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}},
    ]}}]}
    provider, calls, http = gemini_provider(monkeypatch, response)
    image = await provider.generate_image("星海", "gemini-2.5-flash-image")
    assert image.data == PNG and image.mime_type == "image/png" and http.is_closed
    assert calls[0][1]["generationConfig"]["responseModalities"] == ["IMAGE"]
    assert "gemini-2.5-flash-image:generateContent" in calls[0][0]


async def test_gemini_imagen_compatible_sdk_branch(monkeypatch):
    generate = AsyncMock(return_value=SimpleNamespace(generated_images=[SimpleNamespace(
        image=types.Image(image_bytes=PNG, mime_type="image/png")
    )]))
    close = AsyncMock()
    provider = GeminiProvider(api_key="test")
    monkeypatch.setattr(provider, "_client", lambda: SimpleNamespace(aio=SimpleNamespace(
        models=SimpleNamespace(generate_images=generate), aclose=close
    )))
    image = await provider.generate_image("星海", "imagen-test")
    assert image.data == PNG and image.mime_type == "image/png"
    assert generate.await_args.kwargs["config"].number_of_images == 1
    assert generate.await_args.kwargs["config"].aspect_ratio == "3:4"
    close.assert_awaited_once()


async def test_gemini_current_sdk_imagen_unavailable_is_chinese(monkeypatch):
    provider, calls, http = gemini_provider(monkeypatch, {})
    with pytest.raises(AIProviderError, match="当前接口或 SDK 不支持 Imagen.*Gemini 原生"):
        await provider.generate_image("星海", "imagen-4.0-generate-001")
    assert calls == [] and http.is_closed


@pytest.mark.parametrize("response", [
    {}, {"candidates": []}, {"candidates": [{"content": {"parts": [{"text": "拒绝"}]}}]}
])
async def test_gemini_empty_or_text_only_result(monkeypatch, response):
    provider, _, http = gemini_provider(monkeypatch, response)
    with pytest.raises(AIProviderError, match="未返回图片"):
        await provider.generate_image("星海", "gemini-image")
    assert http.is_closed
