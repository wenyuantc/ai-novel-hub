import io
import json
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from PIL import Image

from app.ai.providers.base import AIProviderError, ImageInput
from app.ai.providers.video import VideoPoll, VideoRequest, VideoTransientError, build_video_provider, probe_defaults

CASES = [
    (
        "volcengine",
        "doubao-seedance-2-0",
        "/api/v3/contents/generations/tasks",
        {"id": "task1"},
        {"status": "queued"},
        {"status": "succeeded", "content": {"video_url": "https://cdn.example/result"}, "duration": 5},
        {"status": "failed", "error": {"code": "InputImageSensitiveContentDetected"}},
    ),
    (
        "minimax",
        "MiniMax-H3",
        "/v2/video_generation",
        {"task_id": "task1"},
        {"task": {"status": "running"}},
        {"task": {"status": "succeeded", "content": {"url": "https://cdn.example/result"}, "duration": 5}},
        {"task": {"status": "failed", "error": {"code": "content_policy"}}},
    ),
    (
        "aliyun_wan",
        "wan3.0-video",
        "/api/v1/services/aigc/video-generation/video-synthesis",
        {"output": {"task_id": "task1"}},
        {"output": {"task_status": "PENDING"}},
        {"output": {"task_status": "SUCCEEDED", "video_url": "https://cdn.example/result"}},
        {"output": {"task_status": "FAILED", "code": "DataInspectionFailed"}},
    ),
    (
        "openai_compat",
        "sora-2",
        "/v1/videos",
        {"id": "task1"},
        {"status": "in_progress"},
        {"status": "completed", "seconds": "4"},
        {"status": "failed", "error": {"code": "moderation_blocked"}},
    ),
]


@pytest.mark.parametrize("name,model,path,submitted,pending,success,failed", CASES)
async def test_video_lifecycle(name, model, path, submitted, pending, success, failed, monkeypatch):
    calls = []
    replies = iter([submitted, pending, success, failed])

    def respond(req):
        calls.append(req)
        return httpx.Response(200, json=next(replies))

    provider = build_video_provider(name, "secret", "https://8.8.8.8")
    monkeypatch.setattr(provider, "_transport", httpx.MockTransport(respond))
    frame = io.BytesIO()
    Image.new("RGB", (320, 320)).save(frame, format="JPEG")
    request = VideoRequest(
        "湖边", model, images=(ImageInput(frame.getvalue(), "image/jpeg"),), **probe_defaults(name, model, True)
    )
    assert await provider.submit(request) == "task1"
    assert calls[0].url.path == path
    assert calls[0].headers["Authorization"] == "Bearer secret"
    if name == "openai_compat":
        assert b'name="input_reference"' in calls[0].content
        assert b"1280x720" in calls[0].content
    elif name == "aliyun_wan":
        assert calls[0].headers["X-DashScope-Async"] == "enable"
        assert json.loads(calls[0].content)["input"]["media"][0]["type"] == "reference_image"
    else:
        assert json.loads(calls[0].content)["content"][1]["role"] == "reference_image"
    assert (await provider.poll("task1")).state == "running"
    result = await provider.poll("task1")
    assert result.state == "succeeded"
    assert "审核" in (await provider.poll("task1")).error_message
    with pytest.raises(AIProviderError):
        await provider.submit(VideoRequest("bad", model, 35, "16:9", "720p"))
    assert len(calls) == 4


async def test_external_download_guard_limits_and_auth(monkeypatch):
    provider = build_video_provider("minimax", "secret", allow_private=True)
    requests = []

    def respond(req):
        requests.append(req)
        return httpx.Response(200, content=b"ab")

    monkeypatch.setattr(provider, "_transport", httpx.MockTransport(respond))
    with pytest.raises(AIProviderError, match="内网"):
        _ = [c async for c in provider.download(VideoPoll("succeeded", result_url="http://127.0.0.1/a"))]
    assert not requests
    assert [c async for c in provider.download(VideoPoll("succeeded", result_url="https://8.8.8.8/a"))] == [b"ab"]
    assert "Authorization" not in requests[0].headers
    monkeypatch.setattr(provider, "max_download_bytes", 1)
    with pytest.raises(AIProviderError, match="大小"):
        _ = [c async for c in provider.download(VideoPoll("succeeded", result_url="https://8.8.8.8/a"))]


async def test_transient_poll_and_no_redirect(monkeypatch):
    provider = build_video_provider("openai_compat", "secret", "https://8.8.8.8/v1")
    responses = iter([httpx.Response(503), httpx.Response(302, headers={"Location": "http://127.0.0.1/x"})])
    monkeypatch.setattr(provider, "_transport", httpx.MockTransport(lambda _: next(responses)))
    with pytest.raises(VideoTransientError):
        await provider.poll("id")
    with pytest.raises(AIProviderError, match="重定向"):
        await provider.poll("id")


@pytest.mark.parametrize(
    "name,path,query",
    [
        ("volcengine", "/api/v3/contents/generations/tasks/job", {"status": "queued"}),
        ("minimax", "/v2/video_generation/job", {"task": {"status": "queued"}}),
        ("aliyun_wan", "/api/v1/tasks/job/cancel", None),
        ("openai_compat", None, None),
    ],
)
async def test_cancel_semantics(name, path, query, monkeypatch):
    calls = []

    def respond(req):
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json=query if req.method == "GET" else "accepted")

    provider = build_video_provider(name, "secret", "https://8.8.8.8")
    monkeypatch.setattr(provider, "_transport", httpx.MockTransport(respond))
    await provider.cancel("job")
    if path is None:
        assert calls == []
    elif name == "aliyun_wan":
        assert calls == [("POST", path)]
    else:
        assert calls[-1] == ("DELETE", path)
        assert calls[0][0] == "GET"


@pytest.mark.parametrize(
    "name,model", [("volcengine", "seedance-2-0"), ("minimax", "MiniMax-H3"), ("aliyun_wan", "wan3.0-video")]
)
async def test_signed_reference_media(name, model, monkeypatch):
    calls = []

    def respond(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"task_id": "t", "id": "t", "output": {"task_id": "t"}})

    provider = build_video_provider(name, "secret", "https://8.8.8.8")
    monkeypatch.setattr(provider, "_transport", httpx.MockTransport(respond))
    await provider.submit(
        VideoRequest(
            "跳舞",
            model,
            **probe_defaults(name, model),
            video_urls=("https://cdn.example/v?sign=abc",),
            audio_urls=("https://cdn.example/a?sign=abc",),
        )
    )
    if name == "aliyun_wan":
        assert calls[0]["input"]["media"] == [
            {"type": "reference_video", "url": "https://cdn.example/v?sign=abc"},
            {"type": "reference_audio", "url": "https://cdn.example/a?sign=abc"},
        ]
    else:
        assert calls[0]["content"][1:] == [
            {"type": "video_url", "role": "reference_video", "video_url": {"url": "https://cdn.example/v?sign=abc"}},
            {"type": "audio_url", "role": "reference_audio", "audio_url": {"url": "https://cdn.example/a?sign=abc"}},
        ]


async def test_only_configured_content_path_receives_key(monkeypatch):
    provider = build_video_provider("openai_compat", "secret", "https://8.8.8.8/provider/v1")
    calls = []

    def respond(req):
        calls.append(req)
        return httpx.Response(200, content=b"mp4")

    monkeypatch.setattr(provider, "_transport", httpx.MockTransport(respond))
    result = VideoPoll("succeeded", result_url="https://unrelated.example/steal", external_id="safe-id")
    assert [c async for c in provider.download(result)] == [b"mp4"]
    assert str(calls[0].url) == "https://8.8.8.8/provider/v1/videos/safe-id/content"
    assert calls[0].headers["Authorization"] == "Bearer secret"
    with pytest.raises(AIProviderError, match="编号"):
        await provider.poll("../unrelated")
    assert len(calls) == 1


async def test_private_api_rejected_before_network(monkeypatch):
    def unexpected(_):
        pytest.fail("不应发出内网请求")

    provider = build_video_provider("minimax", "secret", "http://127.0.0.1/v2")
    monkeypatch.setattr(provider, "_transport", httpx.MockTransport(unexpected))
    with pytest.raises(AIProviderError, match="内网"):
        await provider.poll("job")


def test_wan_requires_workspace_endpoint_and_checks_reference_dimensions():
    with pytest.raises(AIProviderError, match="Base URL"):
        build_video_provider("aliyun_wan", "secret")

    provider = build_video_provider("aliyun_wan", "secret", "https://example.cn-beijing.maas.aliyuncs.com")
    assert provider.base_url == "https://example.cn-beijing.maas.aliyuncs.com/api/v1"
    for size in [(239, 720), (240, 1921)]:
        frame = io.BytesIO()
        Image.new("RGB", size).save(frame, format="JPEG")
        req = VideoRequest(
            "测试",
            "wan3.0-video",
            2,
            "16:9",
            "480p",
            images=(ImageInput(frame.getvalue(), "image/jpeg"),),
        )
        with pytest.raises(AIProviderError, match="像素"):
            provider.validate(req)


async def test_openai_reference_preserves_frame_edges_and_advertised_size(monkeypatch):
    body = io.BytesIO()
    image = Image.new("RGB", (250, 250), "blue")
    image.paste("red", (0, 0, 250, 30))
    image.save(body, format="PNG")

    def respond(request):
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content
        )
        reference = next(
            part
            for part in message.iter_parts()
            if part.get_param("name", header="content-disposition") == "input_reference"
        )
        with Image.open(io.BytesIO(reference.get_payload(decode=True))) as frame:
            assert frame.size == (1280, 720)
            red, green, blue = frame.getpixel((640, 25))
            assert red > 200 and green < 30 and blue < 30
        return httpx.Response(200, json={"id": "job"})

    provider = build_video_provider("openai_compat", "secret", "https://8.8.8.8")
    monkeypatch.setattr(provider, "_transport", httpx.MockTransport(respond))
    request = VideoRequest("首帧", "sora-2", 4, "16:9", "720p", images=(ImageInput(body.getvalue(), "image/png"),))
    assert await provider.submit(request) == "job"


def test_seedance_versions_validate_audio_only_and_counts():
    provider = build_video_provider("volcengine", "secret")
    audio = ("https://cdn.example.com/voice",)
    with pytest.raises(AIProviderError, match="需要搭配"):
        provider.validate(VideoRequest("雨声", "doubao-seedance-2-0", 4, "16:9", "720p", audio_urls=audio))
    provider.validate(VideoRequest("雨声", "doubao-seedance-2-5", 4, "16:9", "720p", audio_urls=audio))
    assert provider.capabilities("doubao-seedance-2-5").max_images == 30
    assert provider.capabilities("doubao-seedance-2-5-flash").max_images == 10
