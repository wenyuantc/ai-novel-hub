"""写作风格库的请求 / 响应结构。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

STYLES_FORMAT = "ainovel.styles"
STYLES_VERSION = 1
MAX_STYLE_IMPORT_BYTES = 1024 * 1024
MAX_IMPORT_STYLES = 200

# 生成时的风格选择：inherit = 按「章节覆盖 > 项目默认」；none = 不用风格；整数 = 指定风格 id
StyleChoice = Literal["inherit", "none"] | int


class StyleFields(BaseModel):
    """全部字段选填（名称除外，由子类约束）；长度与 writing_styles 列一致。"""

    description: str = Field(default="", max_length=200)
    pov: str = Field(default="", max_length=500)
    rhythm: str = Field(default="", max_length=500)
    dialogue: str = Field(default="", max_length=500)
    description_density: str = Field(default="", max_length=500)
    tone: str = Field(default="", max_length=500)
    diction: str = Field(default="", max_length=500)
    taboos: str = Field(default="", max_length=1000)
    sample: str = Field(default="", max_length=2000)
    extra: str = Field(default="", max_length=5000)


class StyleCreate(StyleFields):
    name: str = Field(min_length=1, max_length=100)


class StyleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=200)
    pov: str | None = Field(default=None, max_length=500)
    rhythm: str | None = Field(default=None, max_length=500)
    dialogue: str | None = Field(default=None, max_length=500)
    description_density: str | None = Field(default=None, max_length=500)
    tone: str | None = Field(default=None, max_length=500)
    diction: str | None = Field(default=None, max_length=500)
    taboos: str | None = Field(default=None, max_length=1000)
    sample: str | None = Field(default=None, max_length=2000)
    extra: str | None = Field(default=None, max_length=5000)
    sort: int | None = None


class StyleReferences(BaseModel):
    projects: int = 0
    chapters: int = 0


class StyleOut(StyleFields):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    preset_key: str | None = None
    is_preset: bool = False
    sort: int = 0
    references: StyleReferences = Field(default_factory=StyleReferences)
    created_at: datetime
    updated_at: datetime


class StyleBrief(BaseModel):
    id: int
    name: str
    description: str
    is_preset: bool


class StyleExtractRequest(BaseModel):
    sample_text: str = Field(min_length=200, max_length=10000)
    ai_config_id: int | None = None


class StyleDraft(StyleFields):
    """未保存的风格字段（试写时使用）；名称可空。"""

    name: str = Field(default="", max_length=100)


class StyleTrialRequest(BaseModel):
    # 二选一：已保存的风格 id，或未保存的风格字段
    style_id: int | None = None
    style: StyleDraft | None = None
    scene: str = Field(min_length=1, max_length=500)
    words: int = Field(default=500, ge=200, le=1000)
    ai_config_id: int | None = None


class StyleImportRequest(BaseModel):
    data: Any


class StyleImportItem(BaseModel):
    id: int
    name: str
    # 同名改名时的原名
    renamed_from: str | None = None


class StyleImportResult(BaseModel):
    items: list[StyleImportItem]
    warnings: list[str] = Field(default_factory=list)
