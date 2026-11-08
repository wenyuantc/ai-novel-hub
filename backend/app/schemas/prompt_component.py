"""提示词组件的请求 / 响应结构。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.skill import check_slug

COMPONENTS_FORMAT = "ainovel.prompt-components"
COMPONENTS_VERSION = 1
MAX_IMPORT_BYTES = 1024 * 1024
CONTENT_LIMIT = 20000


class ComponentFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    content: str = Field(default="", max_length=CONTENT_LIMIT)

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        return check_slug(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("名称不能为空")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        return value.strip()


class ComponentCreate(ComponentFields):
    pass


class ComponentUpdate(ComponentFields):
    # 读取时的修订号；与当前不一致 → 409 stale_revision
    revision: int = Field(ge=1)


class ComponentRef(BaseModel):
    id: int
    slug: str
    name: str


class ComponentUsage(BaseModel):
    kind: Literal["skill", "override"]
    # skill：Skill id；override：模板 key
    id: int | None = None
    key: str | None = None
    name: str
    # 覆盖是否启用；Skill 为 null
    enabled: bool | None = None


class ComponentBrief(BaseModel):
    id: int
    slug: str
    name: str
    description: str
    revision: int
    content_chars: int
    usage_count: int
    created_at: datetime
    updated_at: datetime


class ComponentOut(ComponentBrief):
    content: str
    usages: list[ComponentUsage]


class ComponentImportRequest(BaseModel):
    data: Any


class ComponentImportItemResult(BaseModel):
    slug: str
    status: Literal["created", "updated", "unchanged", "invalid"]
    message: str = ""
    failures: list[dict[str, Any]] = Field(default_factory=list)


class ComponentImportResult(BaseModel):
    items: list[ComponentImportItemResult]
