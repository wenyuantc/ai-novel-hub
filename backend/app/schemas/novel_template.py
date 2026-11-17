"""个人小说模板 HTTP 请求与安全摘要。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.novel_template_snapshot import TemplateDependency, TemplateIncludedDefinitions, TemplateSelection
from app.schemas.project import AUDIENCE_MAX, SELLING_POINTS_MAX, SYNOPSIS_MAX, THEME_MAX, normalize_tags

DbId = int
PositiveId = Field(gt=0, le=2_147_483_647, strict=True)


class TemplateSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    source_project_id: int = PositiveId
    selection: TemplateSelection
    source_stamp: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("模板名称不能为空")
        return value

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str) -> str:
        return value.strip()


class TemplateSnapshotReplace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_project_id: int = PositiveId
    selection: TemplateSelection
    source_stamp: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = PositiveId


class TemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = PositiveId
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def clean_changes(self):
        fields = self.model_fields_set & {"name", "description"}
        if not fields or any(getattr(self, field) is None for field in fields):
            raise ValueError("请提供模板名称或说明")
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("模板名称不能为空")
        if self.description is not None:
            self.description = self.description.strip()
        return self


class TemplateProjectOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    genre: str = Field(default="", max_length=100)
    premise: str = Field(default="", max_length=5000)
    synopsis: str = Field(default="", max_length=SYNOPSIS_MAX)
    theme: str = Field(default="", max_length=THEME_MAX)
    tags: list[str] = Field(default_factory=list, max_length=50)
    selling_points: str = Field(default="", max_length=SELLING_POINTS_MAX)
    audience: str = Field(default="", max_length=AUDIENCE_MAX)
    target_words: int | None = Field(default=None, ge=1000, le=10_000_000, strict=True)

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: list[str]) -> list[str]:
        return normalize_tags(value)

    @field_validator("genre", "premise", "synopsis", "theme", "selling_points", "audience")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return value.strip()


class TemplateProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = PositiveId
    intent_key: str
    title: str = Field(min_length=1, max_length=200)
    pen_name_id: int | None = Field(default=None, gt=0, le=2_147_483_647, strict=True)
    overrides: TemplateProjectOverrides = Field(default_factory=TemplateProjectOverrides)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("书名不能为空")
        return value

    @field_validator("intent_key")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except (TypeError, ValueError, AttributeError):
            raise ValueError("建书意图必须是规范 UUID") from None
        if str(parsed) != value:
            raise ValueError("建书意图必须是规范 UUID")
        return value


class TemplatePreview(BaseModel):
    source_stamp: str
    missing_dependencies: list[TemplateDependency]
    included_definitions: TemplateIncludedDefinitions
    included_relations: int
    omitted_relations: int
    counts: dict[str, int]
    estimated_bytes: int
    json_bytes: int
    image_bytes: int
    warnings: list[str]


class TemplateSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    revision: int
    source_title: str
    counts: dict[str, int]
    created_at: datetime
    updated_at: datetime


class TemplateList(BaseModel):
    items: list[TemplateSummary]
    total: int
    page: int
    page_size: int


class TemplateContentItem(BaseModel):
    ref: str
    name: str
    has_image: bool


class TemplateConfigSummary(BaseModel):
    genre: str
    premise: str
    default_style_name: str | None
    field_count: int
    category_count: int


class TemplateDetail(TemplateSummary):
    include_config: bool
    config_summary: TemplateConfigSummary | None
    contents: dict[str, list[TemplateContentItem]]


TemplateGroup = Literal["characters", "world_settings", "organizations", "careers", "outlines"]
