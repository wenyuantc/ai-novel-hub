import asyncio
import io

from PIL import Image, ImageOps

from app.ai.providers.base import AIProviderError

from .base import VideoCapabilities, VideoPoll, VideoProvider, VideoRequest, poll_result, task_id


def _first_frame(data, size):
    try:
        with Image.open(io.BytesIO(data)) as source:
            frame = ImageOps.pad(source.convert("RGB"), size, method=Image.Resampling.LANCZOS)
            output = io.BytesIO()
            frame.save(output, format="JPEG", quality=90)
            return output.getvalue()
    except (ValueError, OSError, Image.DecompressionBombError) as exc:
        raise AIProviderError("无法读取视频首帧参考图") from exc


class OpenAIVideo(VideoProvider):
    version_path = "/v1"

    @staticmethod
    def capabilities(model):
        resolutions = ("720p", "1080p") if "pro" in model.lower() else ("720p",)
        return VideoCapabilities(1, (4, 20), resolutions, ("16:9", "9:16"), False)

    def validate(self, request):
        super().validate(request)
        if request.duration_seconds not in (4, 8, 12, 16, 20):
            raise AIProviderError("兼容视频接口的时长须为 4、8、12、16 或 20 秒")
        if not request.generate_audio:
            raise AIProviderError("该兼容视频接口不支持关闭音轨")

    async def submit(self, request: VideoRequest):
        self.validate(request)
        width, height = (1920, 1080) if request.resolution.lower() == "1080p" else (1280, 720)
        if request.aspect_ratio == "9:16":
            width, height = height, width
        # HTTPX 通过 files 中的 (None, str) 生成无文件名的 multipart 字段。
        fields = {
            "model": (None, request.model),
            "prompt": (None, request.prompt),
            "size": (None, f"{width}x{height}"),
            "seconds": (None, str(request.duration_seconds)),
        }
        if request.images:
            data = await asyncio.to_thread(_first_frame, request.images[0].data, (width, height))
            fields["input_reference"] = ("reference.jpg", data, "image/jpeg")
        response = await self._api("POST", "videos", files=fields)
        return task_id(response.get("id"))

    async def poll(self, external_id):
        external_id = task_id(external_id)
        response = await self._api("GET", f"videos/{external_id}")
        return poll_result(
            response.get("status"),
            content_id=external_id,
            duration=response.get("seconds"),
            progress=response.get("progress"),
            error=response.get("error"),
        )

    def _download_target(self, poll: VideoPoll):
        # 只有使用本站已配置 endpoint 拼出的内容路径可以附带作者的 key。
        return f"{self.base_url}/videos/{task_id(poll.external_id)}/content", True
