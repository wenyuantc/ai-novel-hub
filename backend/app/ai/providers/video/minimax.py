from app.ai.providers.base import AIProviderError

from .base import RATIOS, VideoCapabilities, VideoProvider, VideoRequest, content_items, poll_result, task_id


class MiniMaxVideo(VideoProvider):
    default_base = "https://api.minimax.io"
    version_path = "/v2"
    reference_image_limits = (256, 5760, 2.5)

    @staticmethod
    def capabilities(model):
        name = model.lower()
        if name == "minimax-h3":
            resolutions = ("768p", "480p", "2k")
        elif name == "minimax-h3-mg":
            resolutions = ("768p", "2k")
        elif name in (
            "minimax-h3-highspeed",
            "minimax-h3-lite",
            "minimax-h3-max",
            "minimax-h3-max-turbo",
            "minimax-h3-nc",
        ):
            resolutions = ("768p", "480p")
        else:
            raise AIProviderError("MiniMax 视频接口需要 H3 系列模型")
        return VideoCapabilities(9, (5 if "-max" in name else 4, 15), resolutions, RATIOS, True, 3, 3)

    async def submit(self, request: VideoRequest):
        self.validate(request)
        payload = dict(
            model=request.model,
            content=content_items(request),
            resolution=request.resolution.upper(),
            ratio=request.aspect_ratio,
            duration=request.duration_seconds,
            generate_audio=request.generate_audio,
            response_format="task",
        )
        return task_id((await self._api("POST", "video_generation", json=payload)).get("task_id"))

    async def _task(self, external_id):
        response = await self._api("GET", f"query/video_generation/{task_id(external_id)}")
        return response.get("task") or response

    async def poll(self, external_id):
        task = await self._task(external_id)
        return poll_result(
            task.get("status"),
            url=(task.get("content") or {}).get("url"),
            duration=task.get("duration"),
            error=task.get("error"),
        )

    async def cancel(self, external_id):
        # MiniMax 同一路径也用于删除已结束任务；仅在排队时通知取消。
        if (await self._task(external_id)).get("status") == "queued":
            await self._api("DELETE", f"video_generation/{task_id(external_id)}", discard=True)
