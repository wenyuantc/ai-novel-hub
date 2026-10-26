"""提示词模板（系统默认 + 用户覆盖）的请求 / 响应结构。"""

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROMPTS_FORMAT = "ainovel.prompts"
PROMPTS_VERSION = 1
MAX_IMPORT_BYTES = 1024 * 1024
# 请求体粗上限；超过 20000 字由模板校验给出带位置的 400
_BODY_LIMIT = 100000


class TemplateIssueOut(BaseModel):
    part: Literal["system", "user"] | None
    line: int | None
    message: str


class TemplateVarOut(BaseModel):
    name: str
    label: str
    kind: str
    required: bool
    locked: bool
    sample: Any = None


class LockedBlockOut(BaseModel):
    name: str
    label: str
    value: str


class PromptTemplateBrief(BaseModel):
    key: str
    name: str
    category: str
    category_label: str
    description: str
    overridden: bool = False
    enabled: bool = False
    outdated: bool = False
    updated_at: datetime | None = None


class PromptOverrideOut(BaseModel):
    system: str
    user: str
    enabled: bool
    base_fingerprint: str
    # 保存覆盖时的系统默认全文（outdated 时用于三方对比：旧默认 / 新默认 / 我的版本）
    base_system: str
    base_user: str
    updated_at: datetime


class PromptTemplateDetail(PromptTemplateBrief):
    default_system: str
    default_user: str
    default_fingerprint: str
    variables: list[TemplateVarOut]
    locked: list[LockedBlockOut]
    override: PromptOverrideOut | None = None


class PromptSave(BaseModel):
    system: str = Field(default="", max_length=_BODY_LIMIT)
    user: str = Field(max_length=_BODY_LIMIT)
    note: str = Field(default="", max_length=200)


class PromptPatch(BaseModel):
    enabled: bool | None = None
    # 确认已阅新默认：内容不变，把 base 更新为当前系统默认
    ack_default: bool = False


class PromptVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    system: str
    user: str
    note: str
    created_at: datetime


PREVIEW_VARIABLES_LIMIT = 200_000


class PromptPreviewRequest(BaseModel):
    system: str = Field(default="", max_length=_BODY_LIMIT)
    user: str = Field(max_length=_BODY_LIMIT)
    # 变量值；缺省时用示例值（锁定变量始终用代码提供的内容）
    variables: dict[str, Any] = Field(default_factory=dict, max_length=100)

    @field_validator("variables")
    @classmethod
    def _limit_variables(cls, value: dict[str, Any]) -> dict[str, Any]:
        # 预览变量只用于渲染示例，限制总大小，避免超大请求放大渲染开销
        if len(json.dumps(value, ensure_ascii=False, default=str)) > PREVIEW_VARIABLES_LIMIT:
            raise ValueError(f"变量值总长度不能超过 {PREVIEW_VARIABLES_LIMIT} 字")
        return value


class PreviewMessage(BaseModel):
    role: str
    content: str


class PromptPreviewOut(BaseModel):
    messages: list[PreviewMessage]
    errors: list[TemplateIssueOut]
    locked: list[LockedBlockOut]


class PromptImportRequest(BaseModel):
    data: Any


class PromptImportItemResult(BaseModel):
    key: str
    # created / updated：已保存覆盖；unchanged：与现有覆盖相同；default：与系统默认相同，未创建覆盖；
    # skipped：未知 key 或格式不对；invalid：未通过模板校验
    status: Literal["created", "updated", "unchanged", "default", "skipped", "invalid"]
    message: str = ""
    errors: list[TemplateIssueOut] = Field(default_factory=list)


class PromptImportResult(BaseModel):
    items: list[PromptImportItemResult]
