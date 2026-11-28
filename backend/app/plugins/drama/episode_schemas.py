"""剧集、来源快照与分镜的 HTTP 契约。"""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.core.errors import BadRequestError

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Revision = Annotated[int, Field(strict=True, ge=0)]
PositiveId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
Ratio = Literal["16:9", "9:16", "1:1"]
Resolution = Literal["480p", "720p", "1080p"]
Status = Literal["draft", "active", "completed"]
Duration = Annotated[int, Field(strict=True, ge=2, le=30)]
ShortText = Annotated[str, Field(max_length=200)]
LongText = Annotated[str, Field(max_length=60_000)]
Script = Annotated[str, Field(max_length=200_000)]
Ids = Annotated[list[PositiveId], Field(max_length=200)]


def _check_source(value: str):
    if len(value) > 60_000:
        raise BadRequestError("原始内容不能超过 6 万字")
    return value


SourceText = Annotated[str, AfterValidator(_check_source)]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class Patch(Body):
    revision: Revision

    @model_validator(mode="after")
    def validate_changes(self):
        changes = self.model_fields_set - {"revision"}
        if not changes:
            raise ValueError("至少填写一个修改项")
        if any(getattr(self, key) is None for key in changes - {"scene_id"}):
            raise ValueError("字段不能为空值")
        return self


class SeriesCreate(Body):
    title: Title
    style: ShortText = "3d"
    aspect_ratio: Ratio = "16:9"
    description: LongText = ""


class SeriesPatch(Patch):
    title: Title | None = None
    style: ShortText | None = None
    aspect_ratio: Ratio | None = None
    description: LongText | None = None


class SeriesOut(SeriesCreate):
    id: int
    project_id: int
    sort: int
    revision: int


class EpisodeCreate(Body):
    title: Title
    chapter_ids: list[PositiveId] = Field(default_factory=list)
    source_text: SourceText | None = None
    resolution: Resolution = "720p"


class EpisodePatch(Patch):
    title: Title | None = None
    script: Script | None = None
    script_skipped: bool | None = None
    source_text: SourceText | None = None
    resolution: Resolution | None = None
    status: Status | None = None


class EpisodeSummary(Body):
    id: int
    series_id: int
    number: int
    title: str
    status: Status
    resolution: Resolution
    revision: int


class SourceOut(Body):
    chapter_id: int | None
    chapter_title: str
    sort: int


class EpisodeOut(EpisodeSummary):
    script: str
    script_skipped: bool
    source_text: str
    chapter_ids: list[int]
    sources: list[SourceOut]


class ShotCreate(Body):
    title: ShortText = ""
    description: LongText = ""
    shot_size: ShortText = ""
    atmosphere: LongText = ""
    duration_seconds: Duration = 5
    scene_id: PositiveId | None = None
    video_prompt: LongText = ""
    look_ids: Ids = Field(default_factory=list)
    prop_ids: Ids = Field(default_factory=list)


class ShotPatch(Patch):
    title: ShortText | None = None
    description: LongText | None = None
    shot_size: ShortText | None = None
    atmosphere: LongText | None = None
    duration_seconds: Duration | None = None
    scene_id: PositiveId | None = None
    video_prompt: LongText | None = None
    look_ids: Ids | None = None
    prop_ids: Ids | None = None


class ShotOut(ShotCreate):
    id: int
    episode_id: int
    position: int
    revision: int


class ShotOrder(Body):
    revision: Revision
    shot_ids: Ids


ConfigId = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]


class EpisodeModels(Body):
    text: ConfigId | None = None
    image: ConfigId | None = None
    video: ConfigId | None = None
