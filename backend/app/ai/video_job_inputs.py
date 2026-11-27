"""台账仅保存素材 UUID。服务端按执行者权限读取图片或签发有期限的音视频外链。"""

import asyncio
from contextlib import aclosing
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.ai.images import MAX_COVER_BYTES
from app.ai.providers.base import ImageInput
from app.ai.providers.video import VideoRequest
from app.ai.references import normalize_images
from app.core.errors import BadRequestError
from app.media import access, service, signing

ObjectId = Annotated[str, StringConstraints(min_length=36, max_length=36)]


class VideoInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: str = Field(min_length=1, max_length=32000)
    duration_seconds: int = Field(gt=0, le=600)
    resolution: str = Field(min_length=1, max_length=16)
    aspect_ratio: str = Field(min_length=1, max_length=16)
    image_ids: list[ObjectId] = Field(default_factory=list, max_length=16)
    video_ids: list[ObjectId] = Field(default_factory=list, max_length=16)
    audio_ids: list[ObjectId] = Field(default_factory=list, max_length=16)
    generate_audio: bool = True

    @classmethod
    def parse(cls, value):
        try:
            return cls.model_validate(value)
        except ValidationError:
            raise BadRequestError("视频生成参数或参考素材列表无效") from None

    async def references(self, db, actor, project, provider):
        audio_ms = 0
        video_ms = self.duration_seconds * 1000
        for kind, ids in (("image", self.image_ids), ("video", self.video_ids), ("audio", self.audio_ids)):
            for oid in dict.fromkeys(ids):
                obj = await access.resolve(db, oid, actor_id=actor)
                if obj.kind != kind or obj.parent_id or (obj.scope == "project" and obj.project_id != project):
                    raise access.missing()
                if kind == "audio" and provider in ("aliyun_wan", "minimax"):
                    audio_ms += obj.duration_ms or 0
                    if obj.size_bytes > 15 * 1024**2 or audio_ms > 15_000:
                        raise BadRequestError("每段参考音频上限为 15 MB，音频总时长上限为 15 秒")
                if kind == "video" and provider == "aliyun_wan":
                    video_ms += obj.duration_ms or 0
                    if video_ms > 30_000:
                        raise BadRequestError("万相的参考视频和输出视频总时长不能超过 30 秒")
                yield obj

    async def to_request(self, db, *, model, actor, project, provider):
        images, urls = [], {"video": [], "audio": []}
        async for obj in self.references(db, actor, project, provider):
            if obj.kind == "image":
                if obj.size_bytes > MAX_COVER_BYTES:
                    raise BadRequestError("参考图超过 10 MB")
                buf = bytearray()
                async with aclosing(await service.open_range(db, obj)) as chunks:
                    async for chunk in chunks:
                        if len(buf) + len(chunk) > MAX_COVER_BYTES:
                            raise BadRequestError("参考图超过 10 MB")
                        buf.extend(chunk)
                images.append(ImageInput(bytes(buf), obj.mime))
            else:
                url = await signing.external_url(db, obj)
                if url is None:
                    raise BadRequestError("参考音视频需要公网媒体地址，请配置 PUBLIC_BASE_URL 或 S3 存储")
                urls[obj.kind].append(url)
        return VideoRequest(prompt=self.prompt, model=model, duration_seconds=self.duration_seconds,
                            aspect_ratio=self.aspect_ratio, resolution=self.resolution,
                            images=await asyncio.to_thread(normalize_images, images),
                            generate_audio=self.generate_audio,
                            video_urls=tuple(urls["video"]), audio_urls=tuple(urls["audio"]))
