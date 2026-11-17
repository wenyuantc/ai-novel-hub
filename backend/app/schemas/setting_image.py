from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class ThumbnailMeta(BaseModel):
    version: str
    mime_type: str
    width: int
    height: int


class SeriesThumbnailResult(BaseModel):
    thumbnail: ThumbnailMeta | None
    revision: int


class ThumbnailAiPrompt(BaseModel):
    prompt: str
    prompt_token: str


class ThumbnailAiGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    prompt_token: Annotated[str, StringConstraints(min_length=1, max_length=8192)]
    config_id: Annotated[int, Field(strict=True, gt=0)] | None = None
    expected_version: Annotated[str, StringConstraints(max_length=64)]
    expected_revision: Annotated[int, Field(strict=True, ge=0)] | None = None


class ThumbnailAiGenerateOut(BaseModel):
    image_base64: str
    mime_type: Literal["image/webp"] = "image/webp"
    model_source: Literal["actor", "owner"]
    preview_token: str


class ThumbnailAiApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_base64: Annotated[str, StringConstraints(min_length=1, max_length=699052)]
    preview_token: Annotated[str, StringConstraints(min_length=1, max_length=8192)]
