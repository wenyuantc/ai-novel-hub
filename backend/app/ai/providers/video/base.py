"""短视频生成协议。只在提交时传素材，轮询与下载仅需外部任务号或结果地址。"""

from __future__ import annotations

import base64
import math
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import httpx
from PIL import Image

from app.ai.endpoint_guard import ensure_endpoint_allowed
from app.ai.providers.base import AIProviderError
from app.ai.providers.dispatch_hooks import model_client_event_hooks

if TYPE_CHECKING:
    from app.ai.providers.base import ImageInput

RATIOS = ("16:9", "9:16", "1:1", "4:3", "3:4")


@dataclass(frozen=True)
class VideoRequest:
    prompt: str
    model: str
    duration_seconds: int
    aspect_ratio: str
    resolution: str
    images: tuple[ImageInput, ...] = ()
    generate_audio: bool = True
    video_urls: tuple[str, ...] = ()
    audio_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class VideoCapabilities:
    max_images: int
    durations: tuple[int, int]
    resolutions: tuple[str, ...]
    aspect_ratios: tuple[str, ...]
    can_cancel: bool
    max_videos: int = 0
    max_audios: int = 0


@dataclass(frozen=True)
class VideoPoll:
    state: Literal["running", "succeeded", "failed"]
    progress: int | None = None
    result_url: str | None = None
    error_message: str | None = None
    duration_seconds: int | None = None
    external_id: str | None = None
    mime_type: str = "video/mp4"


class VideoTransientError(AIProviderError):
    """短暂传输/限流故障。仅可安全重试查询。"""


def task_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,254}", value):
        raise AIProviderError("视频服务返回了无效任务编号")
    return value


def http_url(value: str) -> str:
    try:
        url = httpx.URL(value)
        if url.scheme in ("https", "http") and url.host and not url.userinfo and not url.fragment:
            return str(url)
    except (httpx.InvalidURL, TypeError):
        pass
    raise AIProviderError("视频服务地址须为有效的 HTTP 或 HTTPS 地址")


def safe_failure(code: object, status: int | None = None) -> str:
    # 厂商消息可能回显提示词、签名链接或 key；只投影已知错误类别。
    value = str(code).lower()
    if any(key in value for key in ("policy", "sensitive", "inspection", "moderation", "safety", "审核")):
        return "视频未通过厂商内容审核"
    if status in (401, 403):
        return "视频服务认证失败，请检查密钥与模型权限"
    if status == 402 or any(key in value for key in ("quota", "balance", "credit", "余额")):
        return "视频服务额度不足"
    if status in (400, 422) or "invalid" in value:
        return "视频服务拒绝了参数，请检查模型、参考素材、时长和画幅"
    if status == 404:
        return "视频服务未找到接口、模型或任务，请检查配置"
    if "expir" in value or "timeout" in value:
        return "厂商视频任务已超时"
    if "cancel" in value:
        return "厂商视频任务已取消"
    return "视频服务生成失败，请检查模型配置及厂商控制台"


def poll_result(status: object, *, url=None, error=None, duration=None, progress=None, content_id=None) -> VideoPoll:
    state = str(status).lower()
    if state in ("succeeded", "completed"):
        if not content_id and not isinstance(url, str):
            raise AIProviderError("视频服务未返回下载地址")
        try:
            seconds = math.ceil(float(duration))
        except (TypeError, ValueError, OverflowError):
            seconds = 0
        return VideoPoll(
            "succeeded", 100, url, duration_seconds=seconds if seconds > 0 else None, external_id=content_id
        )
    if state in ("queued", "pending", "running", "in_progress"):
        return VideoPoll("running", progress if type(progress) is int and 0 <= progress <= 100 else None)
    if state in ("failed", "expired", "cancelled", "canceled", "unknown"):
        return VideoPoll("failed", error_message=safe_failure(error or state))
    raise AIProviderError("视频服务返回了无法识别的任务状态")


def reference_urls(request: VideoRequest):
    for item in request.images:
        encoded = base64.b64encode(item.data).decode("ascii")
        yield "image", f"data:{item.mime_type};base64,{encoded}"
    for url in request.video_urls:
        yield "video", http_url(url)
    for url in request.audio_urls:
        yield "audio", http_url(url)


def content_items(request: VideoRequest) -> list[dict]:
    items = [{"type": "text", "text": request.prompt}]
    for kind, url in reference_urls(request):
        field = f"{kind}_url"
        items.append({"type": field, field: {"url": url}, "role": f"reference_{kind}"})
    return items


class VideoProvider(ABC):
    default_base = ""
    version_path = ""
    max_download_bytes = 1024**3
    reference_image_limits: tuple[int, int, float] | None = None
    _transport: httpx.AsyncBaseTransport | None = None

    def __init__(self, api_key: str, base_url: str | None, timeout: float, *, allow_private: bool):
        if not base_url and not self.default_base:
            raise AIProviderError("请填写视频服务的 Base URL")
        base = http_url(base_url or self.default_base).rstrip("/")
        if urlsplit(base).query:
            raise AIProviderError("视频服务 Base URL 不能带查询参数")
        if not urlsplit(base).path.strip("/"):
            base += self.version_path
        self.base_url = base
        self.api_key = api_key
        self.timeout = timeout
        self.allow_private = allow_private

    @staticmethod
    @abstractmethod
    def capabilities(model: str) -> VideoCapabilities: ...

    def validate(self, request: VideoRequest) -> None:
        cap = self.capabilities(request.model)
        if not request.model.strip() or not request.prompt.strip():
            raise AIProviderError("视频模型和提示词均不能为空")
        if (
            type(request.duration_seconds) is not int
            or not cap.durations[0] <= request.duration_seconds <= cap.durations[1]
        ):
            raise AIProviderError(f"视频时长须在 {cap.durations[0]} 至 {cap.durations[1]} 秒之间")
        if request.aspect_ratio not in cap.aspect_ratios or request.resolution.lower() not in cap.resolutions:
            raise AIProviderError("该模型不支持所选视频画幅或分辨率")
        if len(request.images) > cap.max_images:
            raise AIProviderError(f"该模型最多支持 {cap.max_images} 张参考图")
        if self.reference_image_limits:
            min_edge, max_edge, max_ratio = self.reference_image_limits
            for reference in request.images:
                try:
                    with Image.open(BytesIO(reference.data)) as image:
                        shortest, longest = sorted(image.size)
                except (OSError, ValueError, Image.DecompressionBombError) as exc:
                    raise AIProviderError("无法读取视频参考图") from exc
                if shortest < min_edge or longest > max_edge or longest > max_ratio * shortest:
                    raise AIProviderError(
                        f"该视频模型要求参考图每边为 {min_edge} 至 {max_edge} 像素，长短边比例不超过 {max_ratio:g}:1"
                    )
        if len(request.video_urls) > cap.max_videos or len(request.audio_urls) > cap.max_audios:
            raise AIProviderError("参考视频或参考音频的数量超过该模型上限")
        for url in (*request.video_urls, *request.audio_urls):
            http_url(url)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
            event_hooks=model_client_event_hooks(),
            transport=self._transport,
        )

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _status(response: httpx.Response, code=None) -> None:
        if response.is_success:
            return
        if response.is_redirect:
            raise AIProviderError("视频服务重定向已拒绝，请检查接口地址")
        if response.status_code in (408, 425, 429) or response.status_code >= 500:
            raise VideoTransientError("视频服务暂时不可用或受到限流")
        raise AIProviderError(safe_failure(code, response.status_code))

    async def _api(self, method: str, path: str, *, discard=False, headers=None, **kwargs) -> dict:
        url = f"{self.base_url}/{path}"
        if not self.allow_private:
            await ensure_endpoint_allowed(url)
        try:
            async with self._client() as client:
                response = await client.request(method, url, headers=self._headers() | (headers or {}), **kwargs)
        except httpx.RequestError as exc:
            raise VideoTransientError("视频服务网络连接失败") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = None
        self._status(response, payload)
        if discard:
            return {}
        if not isinstance(payload, dict):
            raise AIProviderError("视频服务返回了无法读取的响应")
        return payload

    @abstractmethod
    async def submit(self, request: VideoRequest) -> str: ...

    @abstractmethod
    async def poll(self, external_id: str) -> VideoPoll: ...

    async def cancel(self, external_id: str) -> None:
        """没有取消协议的服务仅在本地停止等待。"""
        return None

    def _download_target(self, poll: VideoPoll) -> tuple[str, bool]:
        return http_url(poll.result_url or ""), False

    async def download(self, poll: VideoPoll) -> AsyncIterator[bytes]:
        url, authenticated = self._download_target(poll)
        if not authenticated or not self.allow_private:
            await ensure_endpoint_allowed(url)
        headers = self._headers() if authenticated else {}
        try:
            async with self._client() as client, client.stream("GET", url, headers=headers) as response:
                self._status(response)
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > self.max_download_bytes:
                    raise AIProviderError("结果视频超过大小限制")
                total = 0
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > self.max_download_bytes:
                        raise AIProviderError("结果视频超过大小限制")
                    yield chunk
        except httpx.RequestError as exc:
            raise VideoTransientError("结果视频下载中断") from exc
