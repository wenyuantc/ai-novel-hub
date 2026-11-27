from app.ai.providers.base import AIProviderError

from .base import RATIOS, VideoCapabilities, VideoProvider, VideoRequest, poll_result, reference_urls, task_id


class WanVideo(VideoProvider):
    # 万相 3.0 的地域端点包含账号的 WorkspaceId。
    version_path = "/api/v1"
    reference_image_limits = (240, 8000, 8)

    @staticmethod
    def capabilities(model):
        if model.lower() not in ("wan3.0-video", "wan3.0-video-prime"):
            raise AIProviderError("请使用万相 3.0 视频模型（wan3.0-video 或 wan3.0-video-prime）")
        return VideoCapabilities(10, (2, 30), ("480p", "720p", "1080p"), RATIOS, True, 5, 5)

    async def submit(self, request: VideoRequest):
        self.validate(request)
        inputs = {"prompt": request.prompt}
        media = [{"type": f"reference_{kind}", "url": url} for kind, url in reference_urls(request)]
        if media:
            inputs["media"] = media
        payload = dict(
            model=request.model,
            input=inputs,
            parameters=dict(
                resolution=request.resolution.upper(),
                duration=request.duration_seconds,
                ratio=request.aspect_ratio,
                audio=request.generate_audio,
            ),
        )
        response = await self._api(
            "POST",
            "services/aigc/video-generation/video-synthesis",
            json=payload,
            headers={"X-DashScope-Async": "enable"},
        )
        return task_id((response.get("output") or {}).get("task_id"))

    async def poll(self, external_id):
        response = await self._api("GET", f"tasks/{task_id(external_id)}")
        output = response.get("output") or {}
        return poll_result(
            output.get("task_status"),
            url=output.get("video_url"),
            duration=(response.get("usage") or {}).get("output_video_duration"),
            error=output.get("code") or output.get("message"),
        )

    async def cancel(self, external_id):
        await self._api("POST", f"tasks/{task_id(external_id)}/cancel", discard=True)
