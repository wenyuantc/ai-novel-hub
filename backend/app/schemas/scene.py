"""场景 HTTP 合同。正文坐标一律为 Unicode 码点，客户端不可修改归属与派生字数。"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.rich_document import SceneDocument, validate_chapter_document_boundaries

SceneId = Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]
ContentRevision = Annotated[int, Field(strict=True, ge=0)]
SceneTitle = Annotated[str, Field(strict=True, max_length=200)]


class SceneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SceneWrite(SceneRequest):
    expected_content_revision: ContentRevision


class SceneCreate(SceneWrite):
    title: SceneTitle = ""
    content: str = ""
    summary: str = Field(default="", max_length=200_000)
    after_scene_id: SceneId | None = None


class SceneUpdate(SceneWrite):
    title: SceneTitle | None = None
    content: str | None = None
    summary: str | None = Field(default=None, max_length=200_000)

    @model_validator(mode="after")
    def validate_patch(self):
        fields = self.model_fields_set - {"expected_content_revision"}
        if not fields:
            raise ValueError("请提供需要修改的场景字段")
        if any(getattr(self, name) is None for name in fields):
            raise ValueError("场景字段不能为 null")
        return self


class SceneReorder(SceneWrite):
    scene_ids: list[SceneId] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.scene_ids)) != len(self.scene_ids):
            raise ValueError("场景 ID 不能重复")
        return self


class SceneSplit(SceneWrite):
    offset: Annotated[int, Field(strict=True, gt=0)]
    right_title: SceneTitle = ""


class SceneMergePreview(SceneReorder):
    @model_validator(mode="after")
    def exactly_two(self):
        if len(self.scene_ids) != 2:
            raise ValueError("合并需要两个按原顺序排列的相邻场景")
        return self


class SceneMerge(SceneMergePreview):
    expected_planning_revision: ContentRevision
    signature: str = Field(min_length=64, max_length=64, pattern="^[a-f0-9]+$")
    metadata_source: Literal["left", "right"]


class SceneBrief(BaseModel):
    id: int
    chapter_id: int
    chapter_no: int
    chapter_title: str
    title: str
    summary: str
    sort: int
    word_count: int
    content_revision: int
    planning_revision: int
    created_at: datetime
    updated_at: datetime
    planning: dict[str, Any]


class SceneOut(SceneBrief):
    content: str
    separator_before: str
    active: bool


class ScenePage(BaseModel):
    items: list[SceneBrief]
    total: int
    offset: int
    limit: int
    content_revision: int | None
    planning_revision: int


class SceneMutation(BaseModel):
    items: list[SceneOut]
    content_revision: int
    planning_revision: int


class SceneBlocker(BaseModel):
    kind: str
    id: int
    label: str
    scene_id: int | None = None


class SceneConflict(BaseModel):
    field: str
    left: str | None
    right: str | None


class SceneMergePreviewOut(BaseModel):
    scene_ids: list[int]
    content_revision: int
    planning_revision: int
    signature: str
    blockers: list[SceneBlocker]
    conflicts: list[SceneConflict]


class ScenePositionOut(BaseModel):
    scene_id: int | None = None
    start: int | None = None
    end: int | None = None
    content_revision: int
    fingerprint: str
    reason: Literal["stale", "cross_scene", "separator"] | None = None


class SnapshotScene(SceneRequest):
    id: SceneId
    title: SceneTitle
    content: str
    summary: str
    sort: Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]
    separator_before: str
    planning: dict[str, Any] | None


class SceneSnapshot(SceneRequest):
    schema_version: Literal[1]
    scenes: list[SnapshotScene] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def strict_snapshot_version(cls, value):
        if isinstance(value, dict) and type(value.get("schema_version")) is not int:
            raise ValueError("场景快照版本必须为整数")
        return value

    @model_validator(mode="after")
    def validate_identity_order(self):
        ids = [scene.id for scene in self.scenes]
        ranks = [scene.sort for scene in self.scenes]
        if len(set(ids)) != len(ids) or len(set(ranks)) != len(ranks) or ranks != sorted(ranks):
            raise ValueError("场景快照身份或顺序无效")
        return self


class SnapshotSceneV2(SnapshotScene):
    # 历史正文已有独立 content 字段；文档部分只存新增元数据，避免复制两份原文。
    document: dict[str, Any]

    @model_validator(mode="after")
    def validate_document(self):
        if "content" in self.document:
            raise ValueError("场景历史正文不能重复存储")
        SceneDocument.model_validate({"content": self.content, **self.document})
        return self


class SceneSnapshotV2(SceneSnapshot):
    schema_version: Literal[2]
    scenes: list[SnapshotSceneV2] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_chapter_boundaries(self):
        validate_chapter_document_boundaries([
            (item.separator_before, SceneDocument.model_validate({"content": item.content, **item.document}))
            for item in self.scenes
        ])
        return self


def parse_scene_snapshot(value: dict[str, Any]) -> SceneSnapshot | SceneSnapshotV2:
    if isinstance(value, dict) and value.get("schema_version") == 2:
        return SceneSnapshotV2.model_validate(value)
    return SceneSnapshot.model_validate(value)
