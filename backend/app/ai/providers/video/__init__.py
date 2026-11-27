from app.ai.providers.base import AIProviderError

from .aliyun_wan import WanVideo
from .base import VideoCapabilities, VideoPoll, VideoProvider, VideoRequest, VideoTransientError
from .minimax import MiniMaxVideo
from .openai_video import OpenAIVideo
from .volcengine import VolcengineVideo

__all__ = [
    "VideoCapabilities",
    "VideoPoll",
    "VideoProvider",
    "VideoRequest",
    "VideoTransientError",
    "build_video_provider",
    "probe_defaults",
]

_PROVIDERS = {
    "volcengine": VolcengineVideo,
    "openai_compat": OpenAIVideo,
    "minimax": MiniMaxVideo,
    "aliyun_wan": WanVideo,
}


def _provider_type(provider: str) -> type[VideoProvider]:
    try:
        return _PROVIDERS[provider]
    except KeyError as exc:
        raise AIProviderError("该厂商不支持视频生成") from exc


def build_video_provider(
    provider: str, api_key: str, base_url: str | None = None, timeout: float = 60.0, *, allow_private: bool = False
) -> VideoProvider:
    return _provider_type(provider)(api_key, base_url, timeout, allow_private=allow_private)


def probe_defaults(provider: str, model: str, with_image: bool = False) -> dict:
    cap = _provider_type(provider).capabilities(model)
    if with_image and not cap.max_images:
        raise AIProviderError("该模型不支持参考图")
    return dict(duration_seconds=cap.durations[0], resolution=cap.resolutions[0], aspect_ratio=cap.aspect_ratios[0])
