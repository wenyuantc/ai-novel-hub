"""角色 / 组织卡片（ainovel.cards v6，兼容导入 v1–v5），不含源数据库 ID。

v2 = v1 + 角色的职业挂接（characters[].careers）+ 所涉职业的完整定义（顶层 careers）。
v3 = v2 + 角色自定义文本字段的最新快照（characters[].custom_text_fields）。
v4 = v3 + 类型化字段配置及当前快照，以包内 ref 和显式外部映射解析引用。
v5 = v4 + 包内实体当前整理标签，不携带作品分类规则。
v6 = v5 + 条目 AI 设置、随包字段的 AI 可见性及显式定向关联。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from app.schemas.progression import DbInt
from app.schemas.setting_ai_visibility import HIDEABLE_BUILTIN_FIELDS, MAX_AI_LINKS, StoredAiSettings
from app.schemas.setting_categories import normalize_setting_tags
from app.schemas.setting_field_value import EntityKind, EntityRef, FieldType, PositiveDbInt, SettingEntryType
from app.schemas.setting_fields import DefinitionCreate, DefinitionOut, OptionCreate

CARD_FORMAT = "ainovel.cards"
CARD_VERSION = 6
# 可导入的版本（旧版本不解释后版本的字段与局部引用）
SUPPORTED_CARD_VERSIONS = (1, 2, 3, 4, 5, 6)
# 导入数据大小上限（序列化后的 UTF-8 字节数）
MAX_CARD_BYTES = 2 * 1024 * 1024



class CardFieldSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition_ref: str = Field(min_length=1, max_length=100)
    value: JsonValue


class CardFieldOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    sort: DbInt = 0
    archived: bool = Field(default=False, strict=True)


    @field_validator("label", mode="before")
    @classmethod
    def clean_label(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_option(self):
        OptionCreate.model_validate({**self.model_dump(exclude={"ref"}), "expected_fields_revision": 0})
        return self


class CardFieldDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=100)
    field_type: FieldType
    applicable_types: list[SettingEntryType]
    reference_types: list[SettingEntryType] = Field(default_factory=list)
    sort: DbInt = 0
    archived: bool = Field(default=False, strict=True)
    ai_hidden: bool = Field(default=False, strict=True)
    options: list[CardFieldOption] = Field(default_factory=list, max_length=500)

    @field_validator("label", mode="before")
    @classmethod
    def clean_label(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_definition(self):
        DefinitionCreate.model_validate({**self.model_dump(exclude={"ref", "options"}), "expected_fields_revision": 0})
        if self.options and self.field_type != "dropdown":
            raise ValueError("只有下拉字段可带选项")
        if len({o.label for o in self.options}) != len(self.options):
            raise ValueError("字段选项名称不能重复")
        return self


class CardExternalReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1, max_length=100)
    kind: EntityKind
    type: SettingEntryType
    name: str = Field(min_length=1, max_length=200)


class CardTaggedEntry(BaseModel):
    setting_tags: list[str] = Field(default_factory=list)
    ai_settings: StoredAiSettings | None = None
    ai_link_refs: list[str] = Field(default_factory=list, max_length=MAX_AI_LINKS)

    @field_validator("setting_tags", mode="before")
    @classmethod
    def canonical_tags(cls, value):
        normalized = normalize_setting_tags(value)
        if normalized != value:
            raise ValueError("卡片整理标签必须已去空白、去空并去重")
        return normalized


class CardCareerAssignment(BaseModel):
    career_name: str = Field(min_length=1, max_length=100)
    role: str = "main"
    # 为空 = 第 1 阶
    stage_name: str = Field(default="", max_length=50)
    note: str = Field(default="", max_length=200)


class CardStage(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    description: str = ""


class CardCareer(CardTaggedEntry):
    ref: str | None = Field(default=None, min_length=1, max_length=100)
    fields: list[CardFieldSnapshot] = Field(default_factory=list, max_length=500)
    name: str = Field(min_length=1, max_length=100)
    kind: str = "main"
    description: str = ""
    requirements: str = ""
    abilities: str = ""
    rules: str = ""
    stages: list[CardStage] = Field(min_length=1, max_length=100)

    @field_validator("name", mode="before")
    @classmethod
    def clean_entry_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class CardTextField(BaseModel):
    """名称与文本快照；内部字段 ID 和故事位置不随卡片携带。"""

    name: str = Field(min_length=1, max_length=100)
    value: str = ""
    ai_hidden: bool = Field(default=False, strict=True)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class CardCharacter(CardTaggedEntry):
    ref: str | None = Field(default=None, min_length=1, max_length=100)
    fields: list[CardFieldSnapshot] = Field(default_factory=list, max_length=500)
    name: str = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    role_type: str = Field(default="", max_length=50)
    age: str = Field(default="", max_length=50)
    gender: str = Field(default="", max_length=20)
    appearance: str = ""
    personality: str = ""
    background: str = ""
    profile: str = ""
    current_state: str = ""
    life_status: str = "alive"
    legacy_always_in_context: bool | None = Field(default=None, strict=True)
    careers: list[CardCareerAssignment] = Field(default_factory=list, max_length=10)
    custom_text_fields: list[CardTextField] = Field(default_factory=list)

    @field_validator("custom_text_fields")
    @classmethod
    def unique_text_field_names(cls, value: list[CardTextField]) -> list[CardTextField]:
        names = [item.name for item in value]
        if len(set(names)) != len(names):
            raise ValueError("同一角色的自定义字段名称不能重复")
        return value

    @field_validator("name", mode="before")
    @classmethod
    def clean_entry_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class CardMember(BaseModel):
    character_name: str = Field(min_length=1, max_length=100)
    position: str = Field(default="", max_length=50)
    status: str = "active"
    note: str = ""


class CardOrganization(CardTaggedEntry):
    ref: str | None = Field(default=None, min_length=1, max_length=100)
    fields: list[CardFieldSnapshot] = Field(default_factory=list, max_length=500)
    name: str = Field(min_length=1, max_length=100)
    org_type: str = Field(default="", max_length=50)
    purpose: str = ""
    location: str = Field(default="", max_length=200)
    power_level: int | None = Field(default=None, ge=0, le=100)
    description: str = ""
    parent_name: str | None = Field(default=None, max_length=100)
    members: list[CardMember] = Field(default_factory=list, max_length=500)

    @field_validator("name", mode="before")
    @classmethod
    def clean_entry_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class CardRelationship(BaseModel):
    a_name: str = Field(min_length=1, max_length=100)
    b_name: str = Field(min_length=1, max_length=100)
    role_a: str = Field(min_length=1, max_length=20)
    role_b: str = Field(min_length=1, max_length=20)
    category: str = "other"
    intimacy: int = 0
    status: str = "normal"
    description: str = ""


class CardFile(BaseModel):
    format: str
    version: int
    exported_at: str | None = None
    characters: list[CardCharacter] = Field(default_factory=list, max_length=1000)
    organizations: list[CardOrganization] = Field(default_factory=list, max_length=500)
    relationships: list[CardRelationship] = Field(default_factory=list, max_length=5000)
    careers: list[CardCareer] = Field(default_factory=list, max_length=200)
    field_definitions: list[CardFieldDefinition] = Field(default_factory=list, max_length=500)
    external_references: list[CardExternalReference] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def discard_future_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict) or type(data.get("version")) is not int:
            return data
        if data["version"] < 6:
            def rows(container, key):
                items = container.get(key)
                return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

            legacy_ai_keys = {"ai_settings", "ai_link_refs", "legacy_always_in_context", *StoredAiSettings.model_fields}
            entries = [item for name in ("characters", "organizations", "careers") for item in rows(data, name)]
            fields = [*rows(data, "field_definitions"),
                      *(field for item in entries for field in rows(item, "custom_text_fields"))]
            if (any(legacy_ai_keys.intersection(item) for item in entries)
                    or any("ai_hidden" in field for field in fields)):
                raise ValueError("旧版卡片不支持 AI 可见性配置，请使用原始版本文件")
        if data["version"] < 5:
            for name in ("characters", "organizations", "careers"):
                rows = data.get(name)
                for row in rows if isinstance(rows, list) else []:
                    if isinstance(row, dict) and "setting_tags" in row:
                        value = row["setting_tags"]
                        if not isinstance(value, list) or value:
                            raise ValueError("旧版卡片不支持整理标签资料，请使用原始版本文件")
        if data["version"] >= 4:
            return data
        result = {key: value for key, value in data.items() if key not in ("field_definitions", "external_references")}
        for key in ("characters", "organizations", "careers"):
            if isinstance(result.get(key), list):
                result[key] = [{k: v for k, v in item.items() if k not in ("ref", "fields")
                                and (data["version"] >= 3 or k != "custom_text_fields")}
                               if isinstance(item, dict) else item for item in result[key]]
        return result

    @model_validator(mode="after")
    def validate_local_graph(self):
        if self.version < 4:
            return self
        all_refs: set[str] = set()

        def add(ref):
            if not ref or ref in all_refs:
                raise ValueError("v4 包内 ref 必须非空且唯一")
            all_refs.add(ref)

        entries = [*self.characters, *self.organizations, *self.careers]
        # 角色允许同名，字段引用和 AI 关联均按局部 ref 区分角色身份。
        for group in (self.organizations, self.careers):
            if len({item.name for item in group}) != len(group):
                raise ValueError("v4 组织及职业名称不能重复")
        if len({item.label for item in self.field_definitions}) != len(self.field_definitions):
            raise ValueError("v4 字段定义名称不能重复")
        for item in entries:
            add(item.ref)
        for item in self.external_references:
            add(item.ref)
            if item.kind != item.type.split(".")[0]:
                raise ValueError("外部引用类型不一致")
        entry_refs = set(all_refs)
        if self.version >= 6:
            self._validate_ai_settings(entry_refs)
        definitions = {item.ref: item for item in self.field_definitions}
        for definition in self.field_definitions:
            add(definition.ref)
            for option in definition.options:
                add(option.ref)
        for entry in entries:
            used = set()
            for snapshot in entry.fields:
                definition = definitions.get(snapshot.definition_ref)
                if definition is None or snapshot.definition_ref in used:
                    raise ValueError("字段定义缺失或同条目字段重复")
                used.add(snapshot.definition_ref)
                value = snapshot.value
                if definition.field_type in ("text", "line"):
                    from app.services.setting_field_values import FieldSpec, validate_field_value
                    validate_field_value(FieldSpec(definition.field_type), value)
                elif value is not None:
                    key = "option_ref" if definition.field_type == "dropdown" else "entry_ref"
                    refs = {o.ref for o in definition.options} if key == "option_ref" else entry_refs
                    if (
                        not isinstance(value, dict)
                        or set(value) != {key}
                        or not isinstance(value[key], str)
                        or value[key] not in refs
                    ):
                        raise ValueError("字段值包含不存在的包内引用")
        return self

    def _validate_ai_settings(self, entry_refs: set[str]) -> None:
        for kind, entries in (("character", self.characters), ("organization", self.organizations),
                              ("career", self.careers)):
            for entry in entries:
                if entry.ai_settings is None or "ai_link_refs" not in entry.model_fields_set:
                    raise ValueError("v6 卡片条目缺少 AI 设置或关联")
                if set(entry.ai_settings.ai_hidden_builtin_fields).difference(HIDEABLE_BUILTIN_FIELDS[kind]):
                    raise ValueError("v6 卡片条目包含不可隐藏的内建字段")
                refs = set(entry.ai_link_refs)
                if len(refs) != len(entry.ai_link_refs) or entry.ref in refs or refs.difference(entry_refs):
                    raise ValueError("AI 关联不能重复、指向自身或不存在的卡片 ref")
        for entry in self.characters:
            if entry.legacy_always_in_context is None:
                raise ValueError("v6 角色卡片缺少兼容常驻配置")
            if any("ai_hidden" not in field.model_fields_set for field in entry.custom_text_fields):
                raise ValueError("v6 卡片文本字段缺少 AI 可见性配置")
        if any("ai_hidden" not in field.model_fields_set for field in self.field_definitions):
            raise ValueError("v6 卡片字段定义缺少 AI 可见性配置")
        if any(not any(stage.name.strip() for stage in entry.stages) for entry in self.careers):
            raise ValueError("v6 卡片职业必须有有效阶段")


class CardExportRequest(BaseModel):
    character_ids: list[int] = Field(default_factory=list, max_length=1000)
    organization_ids: list[int] = Field(default_factory=list, max_length=500)


Resolution = Literal["skip", "rename"]


class CardResolutions(BaseModel):
    characters: dict[str, Resolution] = Field(default_factory=dict)
    organizations: dict[str, Resolution] = Field(default_factory=dict)
    careers: dict[str, Resolution] = Field(default_factory=dict)


class CardDefinitionMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["reuse", "rename"]
    target_definition_id: PositiveDbInt | None = None
    label: str | None = Field(default=None, min_length=1, max_length=100)
    option_mappings: dict[str, PositiveDbInt] = Field(default_factory=dict)


class CardMappings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    references: dict[str, EntityRef] = Field(default_factory=dict)
    definitions: dict[str, CardDefinitionMapping] = Field(default_factory=dict)


class CardPreviewRequest(BaseModel):
    data: Any
    resolutions: CardResolutions = Field(default_factory=CardResolutions)
    mappings: CardMappings = Field(default_factory=CardMappings)


class CardImportRequest(CardPreviewRequest):
    expected_fields_revision: int | None = Field(default=None, strict=True, ge=0, le=2_147_483_647)
    expected_categories_revision: int | None = Field(default=None, strict=True, ge=0, le=2_147_483_647)
    expected_ai_visibility_revision: int | None = Field(default=None, strict=True, ge=0, le=2_147_483_647)


class PreviewFieldDefinition(BaseModel):
    ref: str
    label: str
    field_type: FieldType
    ai_hidden: bool = False
    status: Literal["new", "conflict", "reuse", "rename", "incompatible"]
    rename_to: str | None = None
    target_definition_id: int | None = None
    options: list[CardFieldOption] = Field(default_factory=list)
    compatible_targets: list[DefinitionOut] = Field(default_factory=list)
    reason: str | None = None


class PreviewReference(BaseModel):
    ref: str
    kind: EntityKind
    name: str
    status: Literal["internal", "mapped", "unresolved"]
    target: EntityRef | None = None
    reason: str | None = None
    source_fields: list[str] = Field(default_factory=list)
    source_links: list[str] = Field(default_factory=list)



class PreviewItem(BaseModel):
    name: str
    status: Literal["new", "conflict"]
    # 冲突时选择「重命名导入」将使用的名字
    rename_to: str | None = None


class PreviewRelationship(BaseModel):
    a_name: str
    b_name: str
    role_a: str
    role_b: str
    status: Literal["ok", "unresolved"]


class PreviewCareer(BaseModel):
    name: str
    # reuse 仅为旧版本补阶段；v4 同名默认跳过内容，可显式选择 rename。
    status: Literal["new", "reuse", "conflict", "rename"]
    rename_to: str | None = None
    # reuse 时将追加到目标阶梯末尾的阶段名
    stages_added: list[str] = Field(default_factory=list)


class CardPreview(BaseModel):
    fields_revision: int = 0
    categories_revision: int = 0
    ai_visibility_revision: int = 0
    can_import: bool = True
    field_definitions: list[PreviewFieldDefinition] = Field(default_factory=list)
    references: list[PreviewReference] = Field(default_factory=list)
    characters: list[PreviewItem]
    organizations: list[PreviewItem]
    relationships: list[PreviewRelationship]
    careers: list[PreviewCareer] = Field(default_factory=list)
    warnings: list[str]


class ImportedItem(BaseModel):
    name: str
    imported_name: str | None
    id: int | None
    action: Literal["created", "renamed", "skipped"]


class ImportedCareer(BaseModel):
    name: str
    id: int
    action: Literal["created", "reused"]
    stages_added: int = 0


class CardImportResult(BaseModel):
    characters: list[ImportedItem]
    organizations: list[ImportedItem]
    careers: list[ImportedCareer] = Field(default_factory=list)
    relationships_created: int
    members_created: int
    careers_assigned: int = 0
    warnings: list[str]
