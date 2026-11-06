"""档案人工可见性设置；条目身份和内容不随 AI 限制变化。"""

from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.schemas.progression import NonnegativeDbInt
from app.schemas.setting_fields import EntityRef, EntryKind, SettingEntry

AiMode = Literal["always", "detected", "manual_only", "never"]
ExclusionPhrase = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80, strict=True)]
BuiltinField = Annotated[str, StringConstraints(min_length=1, max_length=32, strict=True)]

MAX_AI_LINKS = 32
MAX_MENTION_EXCLUSIONS = 20
HIDEABLE_BUILTIN_FIELDS: dict[EntryKind, tuple[str, ...]] = {
    "character": (
        "role_type", "age", "gender", "profile", "appearance", "personality", "background",
        "current_state", "life_status", "relations_text",
    ),
    "world": ("content",),
    "organization": ("org_type", "purpose", "location", "power_level", "description"),
    "career": ("description", "requirements", "abilities", "rules", "stages"),
}


def normalize_exclusions(phrases: Sequence[str], *, case_sensitive: bool) -> list[str]:
    """已经校验长度／非空的排除短语，按实际匹配语义去重并保留首个拼写。"""
    result, seen = [], set()
    for phrase in phrases:
        key = phrase if case_sensitive else phrase.casefold()
        if key not in seen:
            seen.add(key)
            result.append(phrase)
    return result


class StoredAiSettings(BaseModel):
    """备份与服务复用的持久配置；NULL 只用于兼容旧资料或继承追踪缺省。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)
    ai_mode: AiMode | None
    mention_enabled: bool | None = Field(strict=True)
    mention_case_sensitive: bool = Field(strict=True)
    mention_exclusions: list[ExclusionPhrase] = Field(max_length=MAX_MENTION_EXCLUSIONS)
    ai_hidden_builtin_fields: list[BuiltinField] = Field(max_length=10)

    @field_validator("ai_hidden_builtin_fields")
    @classmethod
    def distinct_fields(cls, fields: list[str]) -> list[str]:
        if len(fields) != len(set(fields)):
            raise ValueError("内建字段不能重复")
        return fields


def legacy_ai_settings() -> StoredAiSettings:
    return StoredAiSettings(
        ai_mode=None, mention_enabled=None, mention_case_sensitive=True,
        mention_exclusions=[], ai_hidden_builtin_fields=[],
    )


class EntryAiSettingsOut(StoredAiSettings):
    # 公开已经解析过的追踪状态，不把兼容 NULL 推给前端推断。
    mention_enabled: bool = Field(strict=True)
    hideable_builtin_fields: list[str]
    links: list[SettingEntry]
    ai_visibility_revision: int


class EntryAiSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_ai_visibility_revision: NonnegativeDbInt
    ai_mode: AiMode | None = None
    mention_enabled: bool | None = Field(default=None, strict=True)
    mention_case_sensitive: bool | None = Field(default=None, strict=True)
    mention_exclusions: list[ExclusionPhrase] | None = Field(default=None, max_length=MAX_MENTION_EXCLUSIONS)
    ai_hidden_builtin_fields: list[BuiltinField] | None = Field(default=None, max_length=10)
    links: list[EntityRef] | None = Field(default=None, max_length=MAX_AI_LINKS)

    @model_validator(mode="after")
    def check_partial_update(self) -> Self:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("请省略未修改字段；不能使用 null 恢复兼容设置")
        if self.ai_hidden_builtin_fields is not None:
            StoredAiSettings.distinct_fields(self.ai_hidden_builtin_fields)
        if self.links is not None and len(self.links) != len({(link.kind, link.id) for link in self.links}):
            raise ValueError("关联目标不能重复")
        return self


class LegacyFieldAiVisibilityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_ai_visibility_revision: NonnegativeDbInt
    expected_timeline_revision: NonnegativeDbInt
    ai_hidden: bool = Field(strict=True)
