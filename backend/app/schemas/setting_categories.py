"""当前标签、分类规则及只读结果；复用字段身份和故事位置。"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from app.schemas.progression import Boundary, DbInt, NonnegativeDbInt, PositiveDbInt, StoryPosition
from app.schemas.setting_fields import EntryType, SettingEntry


def normalize_setting_tags(value: object) -> list[str]:
    """标签是严格字符串数组；按第一次出现保序，大小写精确，不改输入。"""
    if not isinstance(value, list):
        raise ValueError("整理标签必须是字符串数组")
    result = []
    seen = set()
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError("整理标签必须是字符串")
        tag = raw.strip()
        if not tag:
            continue
        if len(tag) > 50:
            raise ValueError("每个整理标签不能超过 50 字")
        if tag not in seen:
            seen.add(tag)
            result.append(tag)
            if len(result) > 50:
                raise ValueError("每条设定最多保存 50 个整理标签")
    return result


class SettingTagsValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    setting_tags: list[StrictStr] = Field(max_length=50)

    @field_validator("setting_tags", mode="before")
    @classmethod
    def normalize(cls, value: object) -> list[str]:
        return normalize_setting_tags(value)


class TagRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["tag"]
    tag: StrictStr
    sort: DbInt = 0

    @field_validator("tag", mode="before")
    @classmethod
    def normalized_tag(cls, value: object) -> str:
        tags = normalize_setting_tags([value])
        if not tags:
            raise ValueError("标签条件不能为空")
        return tags[0]


class DropdownRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["dropdown"]
    definition_id: PositiveDbInt
    option_id: PositiveDbInt
    sort: DbInt = 0


CategoryRule = Annotated[TagRule | DropdownRule, Field(discriminator="kind")]


class CategorySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr = Field(min_length=1, max_length=100)
    applicable_types: list[EntryType] = Field(min_length=1, max_length=9)
    match_mode: Literal["all", "any"]
    sort: DbInt = 0
    rules: list[CategoryRule] = Field(min_length=1, max_length=20)

    @field_validator("name")
    @classmethod
    def nonblank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("分类名称不能为空")
        return value.strip()

    @field_validator("applicable_types")
    @classmethod
    def unique_types(cls, value: list[EntryType]) -> list[EntryType]:
        if len(value) != len(set(value)):
            raise ValueError("分类适用类型不能重复")
        return value


class TagRuleOut(TagRule):
    id: PositiveDbInt
    category_id: PositiveDbInt


class DropdownRuleOut(DropdownRule):
    id: PositiveDbInt
    category_id: PositiveDbInt


CategoryRuleOut = Annotated[TagRuleOut | DropdownRuleOut, Field(discriminator="kind")]


class CategoryOut(CategorySpec):
    id: PositiveDbInt
    project_id: PositiveDbInt
    rules: list[CategoryRuleOut] = Field(min_length=1, max_length=20)

    def to_spec(self) -> CategorySpec:
        return CategorySpec.model_validate(
            {
                **self.model_dump(exclude={"id", "project_id", "rules"}),
                "rules": [rule.model_dump(exclude={"id", "category_id"}) for rule in self.rules],
            }
        )


class CategoryEntryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    types: list[EntryType] | None = Field(default=None, max_length=9)
    q: StrictStr | None = Field(default=None, max_length=200)
    page: PositiveDbInt = 1
    page_size: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    chapter_id: PositiveDbInt | None = None
    chapter_no: PositiveDbInt | None = None
    boundary: Boundary = "start"

    @model_validator(mode="after")
    def one_position(self) -> Self:
        if self.chapter_id is not None and self.chapter_no is not None:
            raise ValueError("章节 ID 与计划章节号只能选择一个")
        return self


class CategoryMatchReason(BaseModel):
    rule_index: NonnegativeDbInt
    rule_id: PositiveDbInt | None = None
    kind: Literal["tag", "dropdown"]
    description: str
    tag: str | None = None
    definition_id: int | None = None
    definition_label: str | None = None
    option_id: int | None = None
    option_label: str | None = None


class CategoryEntryOut(SettingEntry):
    setting_tags: list[str]
    reasons: list[CategoryMatchReason]


class CategoryEntryPage(BaseModel):
    category: CategoryOut | CategorySpec
    categories_revision: NonnegativeDbInt
    fields_revision: NonnegativeDbInt
    position: StoryPosition | None
    items: list[CategoryEntryOut]
    total: NonnegativeDbInt
    page: PositiveDbInt
    page_size: int


class CategoryRevision(BaseModel):
    categories_revision: NonnegativeDbInt


class CategoryVersions(CategoryRevision):
    fields_revision: NonnegativeDbInt


class CategoryCreate(CategorySpec):
    expected_categories_revision: NonnegativeDbInt
    expected_fields_revision: NonnegativeDbInt


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_categories_revision: NonnegativeDbInt
    expected_fields_revision: NonnegativeDbInt
    name: StrictStr | None = Field(default=None, min_length=1, max_length=100)
    applicable_types: list[EntryType] | None = Field(default=None, min_length=1, max_length=9)
    match_mode: Literal["all", "any"] | None = None
    sort: DbInt | None = None
    rules: list[CategoryRule] | None = Field(default=None, min_length=1, max_length=20)

    @model_validator(mode="before")
    @classmethod
    def no_explicit_null(cls, value):
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("分类修改不能设为 null；省略字段表示保留原值")
        return value


class CategoryMutation(CategoryVersions):
    item: CategoryOut


class CategoryPage(CategoryVersions):
    items: list[CategoryOut]
    total: NonnegativeDbInt
    page: PositiveDbInt
    page_size: int


class CategoryPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft: CategorySpec
    filters: CategoryEntryQuery = Field(default_factory=CategoryEntryQuery)
    editing_category_id: PositiveDbInt | None = None


class SettingTagsUpdate(SettingTagsValue):
    expected_categories_revision: NonnegativeDbInt


class SettingTagsOut(CategoryRevision):
    entry: SettingEntry
    setting_tags: list[str]


class SettingTagSuggestions(CategoryRevision):
    items: list[str]
    has_more: bool
