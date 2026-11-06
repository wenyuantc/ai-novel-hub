"""作品字段目录与条目字段的公共合同；引用仅公开实体身份。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, field_validator, model_validator

from app.schemas.progression import (
    AnchorKind,
    Boundary,
    DbInt,
    NonnegativeDbInt,
    PositiveDbInt,
    ProgressionOperation,
    StoryPosition,
)
from app.schemas.setting_field_value import (
    EntityKind,
    TypedFieldValue,
)
from app.schemas.setting_field_value import (
    EntityRef as EntityRef,
)
from app.schemas.setting_field_value import (
    FieldType as FieldType,
)
from app.schemas.setting_field_value import (
    SettingEntryType as EntryType,
)

EntryKind = EntityKind
_PUBLIC_VALUE = TypeAdapter(TypedFieldValue)
ENTRY_TYPES = (
    "character",
    "world.place",
    "world.faction",
    "world.item",
    "world.rule",
    "world.history",
    "world.other",
    "organization",
    "career",
)


class FieldRevision(BaseModel):
    fields_revision: int


class DefinitionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_fields_revision: NonnegativeDbInt
    label: str = Field(min_length=1, max_length=100)
    field_type: FieldType
    applicable_types: list[EntryType] = Field(min_length=1, max_length=9)
    reference_types: list[EntryType] = Field(default_factory=list, max_length=9)
    sort: DbInt = 0
    archived: bool = Field(default=False, strict=True)
    ai_hidden: bool = Field(default=False, strict=True)

    @field_validator("label")
    @classmethod
    def clean_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("字段名称不能为空")
        return value.strip()

    @field_validator("applicable_types", "reference_types")
    @classmethod
    def unique_types(cls, value: list[EntryType]) -> list[EntryType]:
        if len(value) != len(set(value)):
            raise ValueError("适用类型不能重复")
        return value

    @model_validator(mode="after")
    def reference_scope(self) -> Self:
        if (self.field_type == "reference") != bool(self.reference_types):
            raise ValueError("引用字段必须设置允许的目标类型，其他字段不能设置引用范围")
        return self


class DefinitionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_fields_revision: NonnegativeDbInt
    label: str | None = Field(default=None, min_length=1, max_length=100)
    field_type: FieldType | None = None
    applicable_types: list[EntryType] | None = Field(default=None, min_length=1, max_length=9)
    reference_types: list[EntryType] | None = Field(default=None, max_length=9)
    sort: DbInt | None = None
    archived: bool | None = Field(default=None, strict=True)
    ai_hidden: bool | None = Field(default=None, strict=True)

    @field_validator("ai_hidden")
    @classmethod
    def visibility_not_null(cls, value: bool | None) -> bool:
        if value is None:
            raise ValueError("请省略未修改的 AI 可见性")
        return value


class OptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_fields_revision: NonnegativeDbInt
    label: str = Field(min_length=1, max_length=100)
    sort: DbInt = 0
    archived: bool = Field(default=False, strict=True)

    @field_validator("label")
    @classmethod
    def clean_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("选项名称不能为空")
        return value.strip()


class OptionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_fields_revision: NonnegativeDbInt
    label: str | None = Field(default=None, min_length=1, max_length=100)
    sort: DbInt | None = None
    archived: bool | None = Field(default=None, strict=True)


class OptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    definition_id: int
    label: str
    sort: int
    archived: bool


class DefinitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    label: str
    field_type: FieldType
    applicable_types: list[EntryType]
    reference_types: list[EntryType]
    sort: int
    archived: bool
    ai_hidden: bool = False
    options: list[OptionOut] = Field(default_factory=list)


class DefinitionPage(FieldRevision):
    items: list[DefinitionOut]
    total: int


class DefinitionMutation(FieldRevision):
    item: DefinitionOut


class OptionMutation(FieldRevision):
    item: OptionOut


class SettingEntry(BaseModel):
    reference: EntityRef
    type: EntryType
    name: str
    category: str | None = None


class SettingEntryPage(BaseModel):
    items: list[SettingEntry]
    total: int


class FieldSlot(BaseModel):
    field_id: int | None
    definition_id: int | None
    label: str
    field_type: FieldType
    source: Literal["shared", "legacy"]
    base_value: JsonValue
    value: JsonValue
    reference_types: list[EntryType] = Field(default_factory=list)
    options: list[OptionOut] = Field(default_factory=list)
    archived: bool = False
    ai_hidden: bool = False
    sort: int = 0
    # 名称投影仅含引用目标身份和当前名称，绝不递归目标正文。
    reference: SettingEntry | None = None
    base_reference: SettingEntry | None = None


class EntryFieldsOut(FieldRevision):
    entry: SettingEntry
    timeline_id: int | None
    timeline_revision: int
    ai_visibility_revision: int = 0
    position: StoryPosition | None
    fields: list[FieldSlot]
    legacy_fields: list[FieldSlot]


class FieldValueWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_fields_revision: NonnegativeDbInt
    expected_timeline_revision: NonnegativeDbInt
    field_type: FieldType
    value: JsonValue

    @model_validator(mode="after")
    def typed_value(self) -> Self:
        # 公共请求也由同一判别联合校验；定义仍由事务服务再次核对。
        _PUBLIC_VALUE.validate_python({"field_type": self.field_type, "value": self.value})
        return self


class FieldProgressionCreate(FieldValueWrite):
    operation: ProgressionOperation
    anchor_kind: AnchorKind
    chapter_id: PositiveDbInt | None = None
    chapter_no: PositiveDbInt | None = None
    boundary: Boundary = "start"
    sort_order: NonnegativeDbInt = 0
    note: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def anchor(self) -> Self:
        if self.anchor_kind == "chapter" and (self.chapter_id is None or self.chapter_no is not None):
            raise ValueError("已有章节必须指定章节 ID")
        if self.anchor_kind == "planned" and (self.chapter_no is None or self.chapter_id is not None):
            raise ValueError("计划章节必须指定章节号")
        return self


class LegacyBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_fields_revision: NonnegativeDbInt
    expected_timeline_revision: NonnegativeDbInt
    definition_id: PositiveDbInt
