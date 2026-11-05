"""规划 API 契约：来源、设定种类与集合操作均显式区分。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

PlanningTone = Literal["default", "red", "orange", "gold", "green", "cyan", "blue", "purple"]
PLANNING_TONES = ("default", "red", "orange", "gold", "green", "cyan", "blue", "purple")
SourceKind = Literal["chapter", "plan", "detached"]
ReferenceKind = Literal["character", "world", "organization", "career", "relationship"]
PositiveId = Annotated[int, Field(gt=0, le=2_147_483_647, strict=True)]
Revision = Annotated[int, Field(ge=0, le=2_147_483_646, strict=True)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
NodeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
IdList = Annotated[list[PositiveId], Field(max_length=500)]


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanningSource(StrictInput):
    kind: SourceKind
    id: PositiveId


class EntityReference(StrictInput):
    kind: Literal["character", "world", "organization", "career"]
    id: PositiveId


class RelationshipReference(StrictInput):
    kind: Literal["relationship"]
    timeline_id: PositiveId


PlanningReference = Annotated[EntityReference | RelationshipReference, Field(discriminator="kind")]


class PlanningEntry(BaseModel):
    reference: PlanningReference
    name: str
    category: str | None = None
    tracking_supported: bool = False
    relationship_id: int | None = None
    character_ids: list[int] = Field(default_factory=list)
    is_present: bool | None = None


class PlanningRevision(BaseModel):
    revision: int


class PlanningEntryPage(PlanningRevision):
    items: list[PlanningEntry]
    total: int
    page: int
    page_size: int


class PlanningTagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    tone: PlanningTone
    usage_count: int = 0


class PlanningTagCreate(StrictInput):
    expected_revision: Revision
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    tone: PlanningTone = "default"


class PlanningTagUpdate(StrictInput):
    expected_revision: Revision
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)] | None = None
    tone: PlanningTone | None = None

    @field_validator("name", "tone", mode="before")
    @classmethod
    def nonnull(cls, value):
        if value is None:
            raise ValueError("标签字段不能为 null")
        return value


class PlanningTagPage(PlanningRevision):
    items: list[PlanningTagOut]
    total: int
    page: int
    page_size: int


class PlanningTagMutation(PlanningRevision):
    item: PlanningTagOut


class SubplotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str
    tone: PlanningTone
    sort: int
    node_count: int = 0
    crossing_count: int = 0


class SubplotCreate(StrictInput):
    expected_revision: Revision
    name: Name
    description: Annotated[str, StringConstraints(max_length=5000)] = ""
    tone: PlanningTone = "default"
    sort: Annotated[int, Field(ge=0, le=1_000_000, strict=True)] = 0


class SubplotUpdate(StrictInput):
    expected_revision: Revision
    name: Name | None = None
    description: Annotated[str, StringConstraints(max_length=5000)] | None = None
    tone: PlanningTone | None = None
    sort: Annotated[int, Field(ge=0, le=1_000_000, strict=True)] | None = None

    @field_validator("name", "description", "tone", "sort", mode="before")
    @classmethod
    def nonnull(cls, value):
        if value is None:
            raise ValueError("支线字段不能为 null")
        return value


class SubplotPage(PlanningRevision):
    items: list[SubplotOut]
    total: int
    page: int
    page_size: int


class SubplotMutation(PlanningRevision):
    item: SubplotOut


class SubplotNodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    subplot_id: int
    unit_id: int
    content: str


class SubplotNodePut(StrictInput):
    expected_revision: Revision
    content: NodeText


class SubplotNodeMutation(PlanningRevision):
    item: SubplotNodeOut


class SubplotCrossingOut(BaseModel):
    id: int
    unit_id: int
    source: PlanningSource
    title: str
    description: str
    sort: int
    subplot_ids: list[int]


class SubplotCrossingCreate(StrictInput):
    expected_revision: Revision
    source: PlanningSource
    title: Name
    description: NodeText
    subplot_ids: Annotated[list[PositiveId], Field(min_length=2, max_length=20)]
    sort: Annotated[int, Field(ge=0, le=1_000_000, strict=True)] = 0

    @field_validator("subplot_ids")
    @classmethod
    def distinct(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("参与支线不能重复")
        return value


class SubplotCrossingUpdate(StrictInput):
    expected_revision: Revision
    source: PlanningSource | None = None
    title: Name | None = None
    description: NodeText | None = None
    subplot_ids: Annotated[list[PositiveId], Field(min_length=2, max_length=20)] | None = None
    sort: Annotated[int, Field(ge=0, le=1_000_000, strict=True)] | None = None

    @field_validator("source", "title", "description", "subplot_ids", "sort", mode="before")
    @classmethod
    def nonnull(cls, value):
        if value is None:
            raise ValueError("交汇字段不能为 null")
        return value

    @field_validator("subplot_ids")
    @classmethod
    def distinct(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError("参与支线不能重复")
        return value


class SubplotCrossingPage(PlanningRevision):
    items: list[SubplotCrossingOut]
    total: int
    page: int
    page_size: int


class SubplotCrossingMutation(PlanningRevision):
    item: SubplotCrossingOut


class PlanningRow(BaseModel):
    key: str
    unit_id: int | None
    source: PlanningSource
    chapter_id: int | None
    plan_id: int | None
    title: str
    chapter_no: int | None
    outline_id: int | None
    volume_no: int | None
    volume_title: str
    seq: int | None
    has_content: bool
    pov_character_id: int | None
    pov_character_name: str | None
    tags: list[PlanningTagOut]
    references: list[PlanningReference]


class SettingColumn(StrictInput):
    kind: Literal["setting"]
    reference: PlanningReference


class SubplotColumn(StrictInput):
    kind: Literal["subplot"]
    subplot_id: PositiveId


MatrixColumnRef = Annotated[SettingColumn | SubplotColumn, Field(discriminator="kind")]


class MatrixColumn(BaseModel):
    key: str
    selection: MatrixColumnRef
    name: str
    tone: PlanningTone = "default"
    tracking_supported: bool = False


class MatrixSettingCell(BaseModel):
    kind: Literal["setting"] = "setting"
    row_key: str
    column_key: str
    manual: bool
    pov: bool
    tracking_supported: bool
    has_content: bool
    mention_count: int = 0
    ambiguous_count: int = 0


class MatrixSubplotCell(BaseModel):
    kind: Literal["subplot"] = "subplot"
    row_key: str
    column_key: str
    node: SubplotNodeOut | None = None
    crossings: list[SubplotCrossingOut] = Field(default_factory=list)
    has_progress: bool


MatrixCell = Annotated[MatrixSettingCell | MatrixSubplotCell, Field(discriminator="kind")]


class PlanningMatrix(PlanningRevision):
    rows: list[PlanningRow]
    columns: list[MatrixColumn]
    cells: list[MatrixCell]
    total: int
    column_total: int
    page: int
    page_size: int
    column_page: int
    warnings: list[str] = Field(default_factory=list)


class CollectionOperation(StrictInput):
    op: Literal["add", "remove", "replace", "clear"]
    ids: IdList = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_operation(self):
        if len(self.ids) != len(set(self.ids)):
            raise ValueError("条目不能重复")
        if self.op == "clear" and self.ids:
            raise ValueError("清空操作不能携带条目")
        return self


class EntityOperation(CollectionOperation):
    kind: Literal["character", "world", "organization", "career"]


class RelationshipOperation(StrictInput):
    kind: Literal["relationship"]
    op: Literal["add", "remove", "replace", "clear"]
    timeline_ids: IdList = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_operation(self):
        if len(self.timeline_ids) != len(set(self.timeline_ids)):
            raise ValueError("关系不能重复")
        if self.op == "clear" and self.timeline_ids:
            raise ValueError("清空操作不能携带关系")
        return self


ReferenceOperation = Annotated[EntityOperation | RelationshipOperation, Field(discriminator="kind")]


class PovOperation(StrictInput):
    op: Literal["set", "clear"]
    character_id: PositiveId | None = None

    @model_validator(mode="after")
    def valid_operation(self):
        if (self.op == "set") != (self.character_id is not None):
            raise ValueError("设置 POV 必须选择角色，清空不能携带角色")
        return self


class PlanningBatch(StrictInput):
    expected_revision: Revision
    targets: Annotated[list[PlanningSource], Field(min_length=1, max_length=100)]
    pov: PovOperation | None = None
    tags: CollectionOperation | None = None
    references: Annotated[list[ReferenceOperation], Field(max_length=5)] = Field(default_factory=list)

    @model_validator(mode="after")
    def distinct(self):
        if len({(source.kind, source.id) for source in self.targets}) != len(self.targets):
            raise ValueError("目标不能重复")
        if len({item.kind for item in self.references}) != len(self.references):
            raise ValueError("每类设定只能指定一种操作")
        if self.pov is None and self.tags is None and not self.references:
            raise ValueError("至少提供一个修改字段")
        return self


class PlanningMutation(PlanningRevision):
    rows: list[PlanningRow]


class PlanningBinding(StrictInput):
    expected_revision: Revision
    target: PlanningSource

    @field_validator("target")
    @classmethod
    def live_target(cls, value):
        if value.kind == "detached":
            raise ValueError("目标必须是章节或细纲")
        return value


class PlanningNodeResolution(StrictInput):
    subplot_id: PositiveId
    keep_node_id: PositiveId
    content: NodeText


class PlanningMerge(StrictInput):
    expected_revision: Revision
    survivor_unit_id: PositiveId
    other_unit_id: PositiveId
    pov_character_id: PositiveId | None
    node_resolutions: Annotated[list[PlanningNodeResolution], Field(max_length=500)] = Field(default_factory=list)

    @model_validator(mode="after")
    def distinct(self):
        if self.survivor_unit_id == self.other_unit_id:
            raise ValueError("不能合并同一个规划")
        if len({item.subplot_id for item in self.node_resolutions}) != len(self.node_resolutions):
            raise ValueError("节点冲突解决不能重复")
        return self


class WeaveGap(BaseModel):
    after_key: str
    before_key: str | None
    after_label: str
    before_label: str | None
    gap_units: int
    is_tail: bool
    exceeds_threshold: bool


class WeaveTrack(BaseModel):
    subplot: SubplotOut
    progress_keys: list[str]
    nodes: list[SubplotNodeOut]
    crossing_ids: list[int]
    gaps: list[WeaveGap]
    unplanned: bool


class PlanningWeave(PlanningRevision):
    axis: Literal["chapter", "plan"]
    rows: list[PlanningRow]
    tracks: list[WeaveTrack]
    crossings: list[SubplotCrossingOut]
    same_position: dict[str, list[int]]
    total: int
    page: int
    page_size: int
    subplot_total: int
    subplot_page: int
    gap_threshold: int


class PlanningContextPreview(PlanningRevision):
    unit_id: int | None
    source: PlanningSource
    chapter_id: int | None
    plan_id: int | None
    text: str
    warnings: list[str] = Field(default_factory=list)
    node_ids: list[int] = Field(default_factory=list)
    crossing_ids: list[int] = Field(default_factory=list)
    subplot_ids: list[int] = Field(default_factory=list)
    references: list[PlanningReference] = Field(default_factory=list)
    truncated: bool = False


class PlanningDetached(PlanningRevision):
    items: list[PlanningRow]
    total: int
    page: int
    page_size: int
    updated_at: datetime | None = None
