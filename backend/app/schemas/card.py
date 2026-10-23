"""角色 / 组织卡片（ainovel.cards v1）。卡片按名称关联，不含任何内部 ID。"""

from typing import Any, Literal

from pydantic import BaseModel, Field

CARD_FORMAT = "ainovel.cards"
CARD_VERSION = 1
# 导入数据大小上限（序列化后的 UTF-8 字节数）
MAX_CARD_BYTES = 2 * 1024 * 1024


class CardCharacter(BaseModel):
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


class CardMember(BaseModel):
    character_name: str = Field(min_length=1, max_length=100)
    position: str = Field(default="", max_length=50)
    status: str = "active"
    note: str = ""


class CardOrganization(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    org_type: str = Field(default="", max_length=50)
    purpose: str = ""
    location: str = Field(default="", max_length=200)
    power_level: int | None = Field(default=None, ge=0, le=100)
    description: str = ""
    parent_name: str | None = Field(default=None, max_length=100)
    members: list[CardMember] = Field(default_factory=list, max_length=500)


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


class CardExportRequest(BaseModel):
    character_ids: list[int] = Field(default_factory=list, max_length=1000)
    organization_ids: list[int] = Field(default_factory=list, max_length=500)


class CardPreviewRequest(BaseModel):
    # 前端读取文件后原样提交；结构在服务端校验（格式错误返回 400 中文提示，而非 422）
    data: Any


Resolution = Literal["skip", "rename"]


class CardResolutions(BaseModel):
    characters: dict[str, Resolution] = Field(default_factory=dict)
    organizations: dict[str, Resolution] = Field(default_factory=dict)


class CardImportRequest(BaseModel):
    data: Any
    # 未列出的同名冲突项默认跳过
    resolutions: CardResolutions = Field(default_factory=CardResolutions)


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


class CardPreview(BaseModel):
    characters: list[PreviewItem]
    organizations: list[PreviewItem]
    relationships: list[PreviewRelationship]
    warnings: list[str]


class ImportedItem(BaseModel):
    name: str
    imported_name: str | None
    id: int | None
    action: Literal["created", "renamed", "skipped"]


class CardImportResult(BaseModel):
    characters: list[ImportedItem]
    organizations: list[ImportedItem]
    relationships_created: int
    members_created: int
    warnings: list[str]
