"""角色 / 组织卡片（ainovel.cards v3，兼容导入 v1、v2）。卡片按名称关联，不含任何内部 ID。

v2 = v1 + 角色的职业挂接（characters[].careers）+ 所涉职业的完整定义（顶层 careers）。
v3 = v2 + 角色自定义文本字段的最新快照（characters[].custom_text_fields）。
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

CARD_FORMAT = "ainovel.cards"
CARD_VERSION = 3
# 可导入的版本（v1 没有职业，v1/v2 没有自定义文本字段）
SUPPORTED_CARD_VERSIONS = (1, 2, 3)
# 导入数据大小上限（序列化后的 UTF-8 字节数）
MAX_CARD_BYTES = 2 * 1024 * 1024


class CardCareerAssignment(BaseModel):
    career_name: str = Field(min_length=1, max_length=100)
    role: str = "main"
    # 为空 = 第 1 阶
    stage_name: str = Field(default="", max_length=50)
    note: str = Field(default="", max_length=200)


class CardStage(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    description: str = ""


class CardCareer(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = "main"
    description: str = ""
    requirements: str = ""
    abilities: str = ""
    rules: str = ""
    stages: list[CardStage] = Field(min_length=1, max_length=100)


class CardTextField(BaseModel):
    """名称与文本快照；内部字段 ID 和故事位置不随卡片携带。"""

    name: str = Field(min_length=1, max_length=100)
    value: str = ""

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


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
    careers: list[CardCareerAssignment] = Field(default_factory=list, max_length=10)
    custom_text_fields: list[CardTextField] = Field(default_factory=list)

    @field_validator("custom_text_fields")
    @classmethod
    def unique_text_field_names(cls, value: list[CardTextField]) -> list[CardTextField]:
        names = [item.name for item in value]
        if len(set(names)) != len(names):
            raise ValueError("同一角色的自定义字段名称不能重复")
        return value


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
    careers: list[CardCareer] = Field(default_factory=list, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def discard_unknown_v1_v2_fields(cls, data: Any) -> Any:
        """旧版没有此字段，保持原先忽略未知键的行为。"""
        if not isinstance(data, dict) or isinstance(data.get("version"), bool) or data.get("version") not in (1, 2):
            return data
        characters = data.get("characters")
        if not isinstance(characters, list):
            return data
        return {
            **data,
            "characters": [
                {key: val for key, val in item.items() if key != "custom_text_fields"}
                if isinstance(item, dict)
                else item
                for item in characters
            ],
        }


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


class PreviewCareer(BaseModel):
    name: str
    # new = 目标项目没有，将新建；reuse = 复用目标项目的同名职业
    status: Literal["new", "reuse"]
    # reuse 时将追加到目标阶梯末尾的阶段名
    stages_added: list[str] = Field(default_factory=list)


class CardPreview(BaseModel):
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
