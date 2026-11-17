"""小说模板 v1：仅包含显式白名单与包内身份，不携带来源数据库身份。"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.schemas.card import CardFieldDefinition, CardFieldSnapshot
from app.schemas.progression import DbInt
from app.schemas.project import normalize_tags
from app.schemas.setting_ai_visibility import (
    HIDEABLE_BUILTIN_FIELDS,
    MAX_AI_LINKS,
    StoredAiSettings,
    normalize_exclusions,
)
from app.schemas.setting_categories import normalize_setting_tags
from app.schemas.setting_field_value import EntityKind, FieldType, PositiveDbInt, SettingEntryType
from app.schemas.story import clean_aliases

Ref = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=100)]
MAX_ENTRIES = 2000
MAX_OUTLINES = 2000
MAX_PLANS = 5000
MAX_JSON_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 50 * 1024 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TemplateSelection(StrictModel):
    include_config: bool = Field(default=True, strict=True)
    character_ids: list[PositiveDbInt] = Field(default_factory=list, max_length=10000)
    world_ids: list[PositiveDbInt] = Field(default_factory=list, max_length=10000)
    organization_ids: list[PositiveDbInt] = Field(default_factory=list, max_length=10000)
    career_ids: list[PositiveDbInt] = Field(default_factory=list, max_length=10000)
    outline_ids: list[PositiveDbInt] = Field(default_factory=list, max_length=10000)

    @model_validator(mode="after")
    def check_selection(self) -> Self:
        groups = (self.character_ids, self.world_ids, self.organization_ids, self.career_ids, self.outline_ids)
        if any(len(items) != len(set(items)) for items in groups):
            raise ValueError("选择的条目不能重复")
        return self


class TemplateDependency(StrictModel):
    kind: EntityKind
    id: PositiveDbInt
    name: str = Field(max_length=200)
    reason: str = Field(max_length=500)


class TemplateIncludedField(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    field_type: FieldType
    options: list[str] = Field(default_factory=list, max_length=500)


class TemplateIncludedRelationshipType(StrictModel):
    origin: Literal["global", "project"]
    category: Literal["family", "social", "hostile", "work", "other"]
    role_a: str = Field(min_length=1, max_length=20)
    role_b: str = Field(min_length=1, max_length=20)


class TemplateIncludedCareer(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    stages: list[str] = Field(default_factory=list, max_length=100)


class TemplateIncludedDefinitions(StrictModel):
    """仅展示已带入内容的名称；总量由快照 10 MiB 上限约束。"""

    fields: list[TemplateIncludedField] = Field(default_factory=list, max_length=500)
    categories: list[str] = Field(default_factory=list)
    relationship_types: list[TemplateIncludedRelationshipType] = Field(default_factory=list)
    careers: list[TemplateIncludedCareer] = Field(default_factory=list, max_length=MAX_ENTRIES)


class TemplateStyle(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=200)
    pov: str = Field(max_length=500)
    rhythm: str = Field(max_length=500)
    dialogue: str = Field(max_length=500)
    description_density: str = Field(max_length=500)
    tone: str = Field(max_length=500)
    diction: str = Field(max_length=500)
    taboos: str = Field(max_length=1000)
    sample: str = Field(max_length=2000)
    extra: str = Field(max_length=5000)
    sort: DbInt

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("风格名称不能为空")
        return value


class TemplateConfig(StrictModel):
    genre: str = Field(max_length=100)
    premise: str = Field(max_length=5000)
    synopsis: str = Field(max_length=2000)
    theme: str = Field(max_length=200)
    tags: list[str]
    selling_points: str = Field(max_length=500)
    audience: str = Field(max_length=100)
    target_words: int | None = Field(ge=1000, le=10_000_000, strict=True)
    auto_analyze: bool = Field(strict=True)
    style: TemplateStyle | None

    @field_validator("tags")
    @classmethod
    def check_tags(cls, value: list[str]) -> list[str]:
        if normalize_tags(value) != value:
            raise ValueError("作品标签必须为规范形式")
        return value


class TemplateLegacyField(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    value: str
    ai_hidden: bool = Field(strict=True)

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("旧字段名称不能为空")
        return value


class TemplateEntry(StrictModel):
    ref: Ref
    fields: list[CardFieldSnapshot] = Field(default_factory=list, max_length=500)
    custom_text_fields: list[TemplateLegacyField] = Field(default_factory=list, max_length=500)
    setting_tags: list[str] = Field(default_factory=list)
    ai_settings: StoredAiSettings
    ai_link_refs: list[Ref] = Field(default_factory=list, max_length=MAX_AI_LINKS)
    image_ref: Ref | None = None
    sort: DbInt

    @field_validator("setting_tags")
    @classmethod
    def canonical_tags(cls, value: list[str]) -> list[str]:
        if normalize_setting_tags(value) != value:
            raise ValueError("整理标签必须为规范形式")
        return value


class TemplateCharacter(TemplateEntry):
    name: str = Field(min_length=1, max_length=100)
    aliases: list[str]
    role_type: str = Field(max_length=50)
    age: str = Field(max_length=50)
    gender: str = Field(max_length=20)
    appearance: str
    personality: str
    background: str
    profile: str
    relations_text: str
    current_state: str
    life_status: Literal["alive", "dead", "missing", "other"]
    always_in_context: bool = Field(strict=True)

    @field_validator("aliases")
    @classmethod
    def canonical_aliases(cls, value: list[str]) -> list[str]:
        if clean_aliases(value) != value:
            raise ValueError("别名必须规范、去重且不为空")
        return value


class TemplateWorld(TemplateEntry):
    title: str = Field(min_length=1, max_length=200)
    aliases: list[str]
    category: Literal["place", "faction", "item", "rule", "history", "other"]
    content: str

    @field_validator("aliases")
    @classmethod
    def canonical_aliases(cls, value: list[str]) -> list[str]:
        if clean_aliases(value) != value:
            raise ValueError("别名必须规范、去重且不为空")
        return value


class TemplateOrganization(TemplateEntry):
    name: str = Field(min_length=1, max_length=100)
    org_type: str = Field(max_length=50)
    purpose: str
    location: str = Field(max_length=200)
    power_level: int | None = Field(ge=0, le=100, strict=True)
    description: str
    parent_ref: Ref | None


class TemplateStage(StrictModel):
    ref: Ref
    name: str = Field(min_length=1, max_length=50)
    description: str
    position: PositiveDbInt


class TemplateCareer(TemplateEntry):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["main", "sub"]
    description: str
    requirements: str
    abilities: str
    rules: str
    stages: list[TemplateStage] = Field(min_length=1, max_length=100)


class TemplateRelationshipType(StrictModel):
    ref: Ref
    origin: Literal["global", "project"]
    category: Literal["family", "social", "hostile", "work", "other"]
    role_a: str = Field(min_length=1, max_length=20)
    role_b: str = Field(min_length=1, max_length=20)
    sort: DbInt

    @field_validator("role_a", "role_b")
    @classmethod
    def canonical_role(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("关系称呼必须已去除首尾空白")
        return value


class TemplateRelationship(StrictModel):
    type_ref: Ref
    a_ref: Ref
    b_ref: Ref
    intimacy: int = Field(strict=True, ge=-100, le=100)
    status: Literal["normal", "broken", "past", "complicated"]
    description: str


class TemplateMembership(StrictModel):
    organization_ref: Ref
    character_ref: Ref
    position: str = Field(max_length=50)
    status: Literal["active", "left", "expelled", "dead"]
    note: str


class TemplateAssignment(StrictModel):
    character_ref: Ref
    career_ref: Ref
    stage_ref: Ref
    role: Literal["main", "sub"]
    note: str = Field(max_length=200)


class TemplateCategoryRule(StrictModel):
    kind: Literal["tag", "dropdown"]
    sort: DbInt
    tag: str | None
    definition_ref: Ref | None
    option_ref: Ref | None


class TemplateCategory(StrictModel):
    ref: Ref
    name: str = Field(min_length=1, max_length=100)
    applicable_types: list[SettingEntryType] = Field(min_length=1, max_length=9)
    match_mode: Literal["all", "any"]
    sort: DbInt
    rules: list[TemplateCategoryRule] = Field(min_length=1, max_length=20)

    @field_validator("name")
    @classmethod
    def canonical_name(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("分类名称必须已去除首尾空白")
        return value

    @field_validator("applicable_types")
    @classmethod
    def distinct_types(cls, value: list[SettingEntryType]) -> list[SettingEntryType]:
        if len(set(value)) != len(value):
            raise ValueError("分类适用类型不能重复")
        return value


class TemplatePlan(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str
    key_events: list[Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]] = Field(
        max_length=20
    )
    character_refs: list[Ref] = Field(max_length=100)
    emotion: str = Field(max_length=100)
    goal: str
    est_words: int = Field(strict=True, ge=300, le=20000)
    seq: PositiveDbInt

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("细纲标题不能为空")
        return value

    @field_validator("key_events")
    @classmethod
    def canonical_events(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(event != event.strip() for event in value):
            raise ValueError("细纲关键事件必须去重且去除首尾空白")
        return value


class TemplateOutline(StrictModel):
    ref: Ref
    volume_no: PositiveDbInt
    volume_title: str = Field(max_length=200)
    title: str = Field(min_length=1, max_length=200)
    summary: str
    sort: DbInt
    plans: list[TemplatePlan] = Field(default_factory=list, max_length=MAX_PLANS)


class NovelTemplateSnapshot(StrictModel):
    format: Literal["ainovel.novel-template"]
    version: Literal[1]
    config: TemplateConfig | None
    field_definitions: list[CardFieldDefinition] = Field(default_factory=list, max_length=500)
    categories: list[TemplateCategory] = Field(default_factory=list)
    characters: list[TemplateCharacter] = Field(default_factory=list)
    world_settings: list[TemplateWorld] = Field(default_factory=list)
    organizations: list[TemplateOrganization] = Field(default_factory=list)
    careers: list[TemplateCareer] = Field(default_factory=list)
    relationship_types: list[TemplateRelationshipType] = Field(default_factory=list)
    relationships: list[TemplateRelationship] = Field(default_factory=list)
    memberships: list[TemplateMembership] = Field(default_factory=list)
    assignments: list[TemplateAssignment] = Field(default_factory=list)
    outlines: list[TemplateOutline] = Field(default_factory=list, max_length=MAX_OUTLINES)

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError("模板版本必须是整数")
        return value

    @model_validator(mode="after")
    def check_graph(self) -> Self:
        groups = (
            ("character", self.characters),
            ("world", self.world_settings),
            ("organization", self.organizations),
            ("career", self.careers),
        )
        if sum(len(entries) for _, entries in groups) > MAX_ENTRIES:
            raise ValueError("设定条目超过上限")
        if sum(len(outline.plans) for outline in self.outlines) > MAX_PLANS:
            raise ValueError("细纲超过上限")
        entries = {entry.ref: (kind, entry) for kind, rows in groups for entry in rows}
        if len(entries) != sum(len(rows) for _, rows in groups):
            raise ValueError("设定局部引用重复")
        for ref, (kind, entry) in entries.items():
            if not ref.startswith(f"{kind}.") or (entry.image_ref is not None and entry.image_ref != ref):
                raise ValueError("设定或图片引用无效")
            if len(set(entry.ai_link_refs)) != len(entry.ai_link_refs) or ref in entry.ai_link_refs:
                raise ValueError("AI 关联引用重复或指向自身")
            if any(link not in entries for link in entry.ai_link_refs):
                raise ValueError("AI 关联目标不存在")
            settings = entry.ai_settings
            if set(settings.ai_hidden_builtin_fields).difference(HIDEABLE_BUILTIN_FIELDS[kind]):
                raise ValueError("AI 隐藏字段不适用于该条目")
            if normalize_exclusions(settings.mention_exclusions, case_sensitive=settings.mention_case_sensitive) != (
                settings.mention_exclusions
            ):
                raise ValueError("AI 排除词必须已去重")
            if len({field.name for field in entry.custom_text_fields}) != len(entry.custom_text_fields):
                raise ValueError("自定义字段名称重复")
        definitions = {item.ref: item for item in self.field_definitions}
        if len(definitions) != len(self.field_definitions) or len(
            {item.label for item in self.field_definitions}
        ) != len(self.field_definitions):
            raise ValueError("字段定义引用重复")
        options = {option.ref: definition.ref for definition in self.field_definitions for option in definition.options}
        if len(options) != sum(len(definition.options) for definition in self.field_definitions):
            raise ValueError("字段选项引用重复")
        for kind, entry in entries.values():
            if len({field.definition_ref for field in entry.fields}) != len(entry.fields):
                raise ValueError("条目字段引用重复")
            for field in entry.fields:
                definition = definitions.get(field.definition_ref)
                value = field.value
                if definition is None:
                    raise ValueError("字段定义不存在")
                entry_type = f"world.{entry.category}" if kind == "world" else kind
                if entry_type not in definition.applicable_types:
                    raise ValueError("字段定义不适用于该条目")
                if definition.field_type in ("text", "line"):
                    if not isinstance(value, str):
                        raise ValueError("字段文本值无效")
                    if definition.field_type == "line" and (
                        len(value) > 500 or any(c in value for c in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")
                    ):
                        raise ValueError("单行字段值无效")
                elif value is not None:
                    key = "option_ref" if definition.field_type == "dropdown" else "entry_ref"
                    if not isinstance(value, dict) or set(value) != {key} or not isinstance(value[key], str):
                        raise ValueError("字段引用形状无效")
                    if key == "option_ref" and options.get(value[key]) != definition.ref:
                        raise ValueError("字段选项不属于定义")
                    if key == "entry_ref":
                        target = entries.get(value[key])
                        if target is None:
                            raise ValueError("字段引用目标不存在")
                        target_type = f"world.{target[1].category}" if target[0] == "world" else target[0]
                        if target_type not in definition.reference_types:
                            raise ValueError("字段引用目标类型不兼容")
        if len({item.ref for item in self.categories}) != len(self.categories) or len(
            {item.name for item in self.categories}
        ) != len(self.categories):
            raise ValueError("分类名称或引用重复")
        for category in self.categories:
            for rule in category.rules:
                if rule.kind == "tag":
                    if (
                        not rule.tag
                        or normalize_setting_tags([rule.tag]) != [rule.tag]
                        or rule.definition_ref is not None
                        or rule.option_ref is not None
                    ):
                        raise ValueError("分类标签规则无效")
                else:
                    definition = definitions.get(rule.definition_ref)
                    if (
                        rule.tag is not None
                        or definition is None
                        or definition.field_type != "dropdown"
                        or options.get(rule.option_ref) != rule.definition_ref
                        or not set(category.applicable_types).intersection(definition.applicable_types)
                    ):
                        raise ValueError("分类下拉规则无效")
        orgs = {org.ref: org for org in self.organizations}
        if len({org.name for org in self.organizations}) != len(self.organizations):
            raise ValueError("组织名称重复")
        for org in self.organizations:
            seen = {org.ref}
            parent = org.parent_ref
            while parent is not None:
                if parent in seen or parent not in orgs:
                    raise ValueError("组织父级不存在或形成循环")
                seen.add(parent)
                if len(seen) > 50:
                    raise ValueError("组织层级过深")
                parent = orgs[parent].parent_ref
        chars = {entry.ref for entry in self.characters}
        type_refs = {entry.ref for entry in self.relationship_types}
        if len(type_refs) != len(self.relationship_types):
            raise ValueError("关系类型引用重复")
        pairs = set()
        for rel in self.relationships:
            pair = frozenset((rel.a_ref, rel.b_ref))
            if rel.type_ref not in type_refs or not pair <= chars or len(pair) != 2 or pair in pairs:
                raise ValueError("关系引用无效或重复")
            pairs.add(pair)
        members = set()
        for member in self.memberships:
            pair = (member.organization_ref, member.character_ref)
            if pair in members or member.organization_ref not in orgs or member.character_ref not in chars:
                raise ValueError("组织成员引用无效或重复")
            members.add(pair)
        careers = {entry.ref: entry for entry in self.careers}
        if len({career.name for career in self.careers}) != len(self.careers):
            raise ValueError("职业名称重复")
        for career in self.careers:
            if [stage.position for stage in career.stages] != list(range(1, len(career.stages) + 1)) or len(
                {stage.name for stage in career.stages}
            ) != len(career.stages):
                raise ValueError("职业阶段名称或顺序无效")
        stages = {stage.ref: career.ref for career in self.careers for stage in career.stages}
        if len(stages) != sum(len(career.stages) for career in self.careers):
            raise ValueError("职业阶段引用重复")
        assignments = set()
        assignment_counts: dict[tuple[str, str], int] = {}
        for assignment in self.assignments:
            pair = (assignment.character_ref, assignment.career_ref)
            career = careers.get(assignment.career_ref)
            if (
                pair in assignments
                or assignment.character_ref not in chars
                or career is None
                or stages.get(assignment.stage_ref) != assignment.career_ref
                or assignment.role != career.kind
            ):
                raise ValueError("职业挂接引用无效或重复")
            assignments.add(pair)
            count_key = (assignment.character_ref, assignment.role)
            assignment_counts[count_key] = assignment_counts.get(count_key, 0) + 1
            if assignment_counts[count_key] > (1 if assignment.role == "main" else 3):
                raise ValueError("角色职业数量超过上限")
        outline_refs = set()
        for outline in self.outlines:
            if outline.ref in outline_refs:
                raise ValueError("大纲引用重复")
            outline_refs.add(outline.ref)
            for plan in outline.plans:
                if len(set(plan.character_refs)) != len(plan.character_refs) or not set(plan.character_refs) <= chars:
                    raise ValueError("细纲角色引用无效")
        all_refs = (
            [*entries]
            + [*definitions]
            + [*options]
            + [item.ref for item in self.categories]
            + [*type_refs]
            + [*stages]
            + [*outline_refs]
        )
        if len(all_refs) != len(set(all_refs)):
            raise ValueError("不同类型的局部引用不能重复")
        return self
