"""场景 AI 的严格入口；目标只由路径设置，任务内部另存可重放的判别目标。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.schemas.chapter import GenerateChapterRequest, PolishChapterRequest, ReviseChapterRequest
from app.schemas.chapter_version import RewriteApply, RewriteRequest

DatabaseId = Annotated[int, Field(gt=0, le=2_147_483_647)]


class ChapterTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["chapter"] = "chapter"


class SceneTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["scene"]
    scene_id: DatabaseId


AITarget = Annotated[ChapterTarget | SceneTarget, Field(discriminator="kind")]
_TARGET = TypeAdapter(AITarget)


def parse_target(value: object = None) -> ChapterTarget | SceneTarget:
    return ChapterTarget() if value is None else _TARGET.validate_python(value)


class SceneRevisions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_content_revision: int = Field(ge=0)
    expected_planning_revision: int = Field(ge=0)


class SceneModelOptions(SceneRevisions):
    ai_config_id: DatabaseId | None = None
    skill_id: DatabaseId | None = None
    style: Literal["inherit", "none"] | DatabaseId = "inherit"


class SceneGenerateRequest(SceneModelOptions, GenerateChapterRequest):
    model_config = ConfigDict(extra="forbid", strict=True)


class ScenePolishRequest(SceneModelOptions, PolishChapterRequest):
    model_config = ConfigDict(extra="forbid", strict=True)


class SceneReviseRequest(SceneModelOptions, ReviseChapterRequest):
    model_config = ConfigDict(extra="forbid", strict=True)

    analysis_id: DatabaseId
    suggestion_ids: list[DatabaseId] = Field(default_factory=list, max_length=20)


class SceneRewriteRequest(SceneModelOptions, RewriteRequest):
    model_config = ConfigDict(extra="forbid", strict=True)


class SceneRewriteApply(SceneRevisions, RewriteApply):
    model_config = ConfigDict(extra="forbid", strict=True)

    log_id: DatabaseId | None = None
