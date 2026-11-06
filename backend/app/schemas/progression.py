"""按章节回溯设定的公共请求、响应与位置类型。"""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.schemas.setting_field_value import EntityRef, FieldType, PositiveDbInt, SettingEntryType, TypedFieldValue

SubjectKind = Literal["character", "world", "relationship", "career", "membership", "organization", "career_entry"]
Boundary = Literal["start", "end"]
AnchorKind = Literal["chapter", "planned"]
ProgressionOperation = Literal["append", "replace"]
ProgressionSource = Literal["manual", "analysis", "legacy_import"]
ProgressionProtection = Literal["automatic", "confirmed", "manual"]
BaselineStatus = Literal["confirmed", "legacy_unverified"]
NonnegativeDbInt = Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)]
DbInt = Annotated[int, Field(strict=True, ge=-2_147_483_648, le=2_147_483_647)]


class StoryPosition(BaseModel):
    """已解析的故事位置：同一章号具有相同时间，不比较章节 id。"""

    chapter_id: PositiveDbInt | None = None
    chapter_no: PositiveDbInt
    boundary: Boundary = "start"


class SettingContextOut(BaseModel):
    """人工查看的章节有效设定，并非生成请求的完整提示词。"""

    position: StoryPosition
    text: str
    legacy_unverified_count: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


class TimelineCreate(BaseModel):
    """稳定主体；每个类型只接收其实际拥有的外键。"""

    model_config = ConfigDict(extra="forbid")

    subject_kind: SubjectKind
    character_id: PositiveDbInt | None = None
    related_character_id: PositiveDbInt | None = None
    world_setting_id: PositiveDbInt | None = None
    career_id: PositiveDbInt | None = None
    organization_id: PositiveDbInt | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        present = {
            name
            for name in ("character_id", "related_character_id", "world_setting_id", "career_id", "organization_id")
            if getattr(self, name) is not None
        }
        required = {
            "character": {"character_id"},
            "world": {"world_setting_id"},
            "relationship": {"character_id", "related_character_id"},
            "career": {"character_id", "career_id"},
            "membership": {"character_id", "organization_id"},
            "organization": {"organization_id"},
            "career_entry": {"career_id"},
        }[self.subject_kind]
        if present != required:
            raise ValueError("主体类型与引用不匹配")
        if self.subject_kind == "relationship" and self.character_id == self.related_character_id:
            raise ValueError("人物关系的两端不能相同")
        return self


class TimelineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    subject_kind: SubjectKind
    character_id: int | None = None
    related_character_id: int | None = None
    world_setting_id: int | None = None
    career_id: int | None = None
    organization_id: int | None = None
    revision: int
    baseline_status: BaselineStatus
    legacy_compat_allowed: bool


class TimelineListOut(BaseModel):
    items: list[TimelineOut]
    total: int


class BaselineOut(BaseModel):
    timeline_id: int
    baseline: dict[str, JsonValue]
    baseline_status: BaselineStatus
    legacy_compat_allowed: bool
    timeline_revision: int


class BaselineUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: PositiveDbInt
    changes: dict[str, JsonValue] = Field(default_factory=dict)
    confirm: bool = False

    @model_validator(mode="after")
    def require_intent(self) -> Self:
        if not self.changes and not self.confirm:
            raise ValueError("请选择要修改的基础字段或确认已校准")
        return self


class ProgressionCreate(BaseModel):
    """创建事件以平铺位置表达，便于与 ORM 和前端表单一致。"""

    model_config = ConfigDict(extra="forbid")

    expected_revision: PositiveDbInt
    expected_fields_revision: NonnegativeDbInt | None = None
    field_key: str | None = Field(default=None, min_length=1, max_length=64)
    custom_field_id: PositiveDbInt | None = None
    operation: ProgressionOperation
    value: JsonValue
    anchor_kind: AnchorKind
    chapter_id: PositiveDbInt | None = None
    chapter_no: PositiveDbInt | None = None
    boundary: Boundary = "start"
    sort_order: NonnegativeDbInt = 0
    note: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def validate_reference_and_anchor(self) -> Self:
        if (self.field_key is None) == (self.custom_field_id is None):
            raise ValueError("内置字段与自定义字段必须且只能指定一个")
        if self.anchor_kind == "chapter" and (self.chapter_id is None or self.chapter_no is not None):
            raise ValueError("已有章节锚点必须指定章节 id，不能同时指定计划章节号")
        if self.anchor_kind == "planned" and (self.chapter_no is None or self.chapter_id is not None):
            raise ValueError("计划章节锚点必须指定章节号，不能同时指定章节 id")
        if self.operation == "append" and (not isinstance(self.value, str) or not self.value.strip()):
            raise ValueError("追加内容不能为空")
        return self


class ProgressionUpdate(BaseModel):
    """PATCH 省略表示不变；显式 null 由服务合并现有事件后校验。"""

    model_config = ConfigDict(extra="forbid")

    expected_revision: PositiveDbInt
    expected_fields_revision: NonnegativeDbInt | None = None
    field_key: str | None = Field(default=None, min_length=1, max_length=64)
    custom_field_id: PositiveDbInt | None = None
    operation: ProgressionOperation | None = None
    value: JsonValue = None
    anchor_kind: AnchorKind | None = None
    chapter_id: PositiveDbInt | None = None
    chapter_no: PositiveDbInt | None = None
    boundary: Boundary | None = None
    sort_order: NonnegativeDbInt | None = None
    note: str | None = Field(default=None, max_length=200)


class ProgressionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    timeline_id: int
    field_key: str | None = None
    custom_field_id: int | None = None
    operation: ProgressionOperation
    value: JsonValue
    field_type: FieldType | None = None
    definition_id: int | None = None
    field_label: str | None = None
    reference: EntityRef | None = None
    reference_name: str | None = None
    reference_type: SettingEntryType | None = None
    anchor_kind: AnchorKind
    chapter_id: int | None = None
    chapter_no: int | None = None
    anchor_label: str = ""
    boundary: Boundary
    sort_order: int
    source: ProgressionSource
    protection: ProgressionProtection
    source_change_id: int | None = None
    source_content_hash: str | None = None
    revision: int
    active: bool
    superseded_by_id: int | None = None
    reverted_at: datetime | None = None
    note: str = ""
    resolved_chapter_no: int | None = None
    orphaned: bool = False
    created_at: datetime
    updated_at: datetime


class ProgressionListOut(BaseModel):
    items: list[ProgressionOut]
    total: int
    timeline_revision: int
    fields_revision: int | None = None


class ProgressionMutationOut(BaseModel):
    item: ProgressionOut
    timeline_revision: int
    fields_revision: int | None = None


class RevisionOut(BaseModel):
    timeline_revision: int
    fields_revision: int | None = None


class EffectiveOut(BaseModel):
    timeline_id: int
    position: StoryPosition
    fields: dict[str, JsonValue]
    custom_fields: dict[int, str]
    field_values: dict[int, TypedFieldValue] = Field(default_factory=dict)
    applied_event_ids: list[int]
    orphaned_event_ids: list[int]
    baseline_status: BaselineStatus
    timeline_revision: int
    fields_revision: int | None = None
    field_labels: dict[int, str] = Field(default_factory=dict)
    reference_names: dict[int, str] = Field(default_factory=dict)
    reference_types: dict[int, SettingEntryType] = Field(default_factory=dict)


class TextFieldCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: PositiveDbInt
    label: str = Field(min_length=1, max_length=100)
    base_value: str = ""
    sort: DbInt = 0

    @model_validator(mode="after")
    def nonblank_label(self) -> Self:
        if not self.label.strip():
            raise ValueError("字段名称不能为空")
        return self


class TextFieldUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: PositiveDbInt
    label: str | None = Field(default=None, min_length=1, max_length=100)
    base_value: str | None = None
    sort: DbInt | None = None

    @model_validator(mode="after")
    def nonblank_label(self) -> Self:
        if self.label is not None and not self.label.strip():
            raise ValueError("字段名称不能为空")
        return self


class TextFieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    timeline_id: int
    label: str
    base_value: str
    sort: int


class TextFieldListOut(BaseModel):
    items: list[TextFieldOut]
    total: int
    timeline_revision: int


class TextFieldMutationOut(BaseModel):
    item: TextFieldOut
    timeline_revision: int
