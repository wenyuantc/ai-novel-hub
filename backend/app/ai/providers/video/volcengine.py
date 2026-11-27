from app.ai.providers.base import AIProviderError

from .base import RATIOS, VideoCapabilities, VideoProvider, VideoRequest, content_items, poll_result, task_id

_TASKS = "contents/generations/tasks"


def _is_25(model):
    return "2.5" in model or "2-5" in model


class VolcengineVideo(VideoProvider):
    default_base = "https://ark.cn-beijing.volces.com"
    version_path = "/api/v3"
    reference_image_limits = (300, 6000, 2.5)

    @staticmethod
    def capabilities(model):
        name = model.lower()
        resolutions = (
            ("480p", "720p") if any(word in name for word in ("fast", "flash", "mini")) else ("480p", "720p", "1080p")
        )
        if _is_25(name):
            images, clips = (10, 5) if "flash" in name else (30, 10)
            return VideoCapabilities(images, (4, 30), resolutions, RATIOS, True, clips, clips)
        return VideoCapabilities(9, (4, 15), resolutions, RATIOS, True, 3, 3)

    def validate(self, request):
        super().validate(request)
        if request.audio_urls and not (request.images or request.video_urls):
            if not _is_25(request.model):
                raise AIProviderError("Seedance 2.0 的参考音频需要搭配参考图或参考视频")

    async def submit(self, request: VideoRequest):
        self.validate(request)
        payload = dict(
            model=request.model,
            content=content_items(request),
            ratio=request.aspect_ratio,
            resolution=request.resolution.lower(),
            duration=request.duration_seconds,
            generate_audio=request.generate_audio,
        )
        response = await self._api("POST", _TASKS, json=payload)
        return task_id(response.get("id"))

    async def poll(self, external_id):
        response = await self._api("GET", f"{_TASKS}/{task_id(external_id)}")
        return poll_result(
            response.get("status"),
            url=(response.get("content") or {}).get("video_url"),
            error=response.get("error"),
            duration=response.get("duration"),
        )

    async def cancel(self, external_id):
        path = f"{_TASKS}/{task_id(external_id)}"
        if (await self._api("GET", path)).get("status") == "queued":
            await self._api("DELETE", path, discard=True)
