"""Skill：用户私有 / 管理员全局的写作工作流与参考资料。"""

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.ai import skill_inputs as si

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
MAX_TRIGGERS = 10
MAX_REFERENCES = 20
BODY_LIMIT = 50000
REFERENCE_LIMIT = 100000
SkillScope = Literal["mine", "global"]
_BAD_TITLE = re.compile(r"[/\\\x00-\x1f\x7f]")


def normalize_triggers(values: list[str]) -> list[str]:
    result: list[str] = []
    for raw in values:
        value = str(raw).strip().removeprefix("/").strip()
        if not value:
            continue
        if len(value) > 30:
            raise ValueError("触发词每项不超过 30 字")
        if any(ch.isspace() for ch in value):
            raise ValueError("触发词不能包含空白字符")
        if value.casefold() not in {t.casefold() for t in result}:
            result.append(value)
    if len(result) > MAX_TRIGGERS:
        raise ValueError(f"触发词最多 {MAX_TRIGGERS} 个")
    return result


def check_slug(value: str) -> str:
    value = value.strip()
    if not SLUG_RE.match(value):
        raise ValueError("标识只能使用小写字母、数字和连字符，以字母或数字开头，不超过 48 个字符")
    return value


class SkillInputDef(BaseModel):
    """Skill 的一个自定义输入项；正文用 `{{ name }}` 引用其值。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=32)
    label: str = Field(min_length=1, max_length=si.LABEL_LIMIT)
    help: str = Field(default="", max_length=si.HELP_LIMIT)
    kind: Literal["text", "textarea", "select"] = "text"
    required: bool = False
    default: str = Field(default="", max_length=si.VALUE_LIMITS["textarea"])
    options: list[str] = Field(default_factory=list, max_length=si.MAX_OPTIONS)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not si.NAME_RE.match(value):
            raise ValueError("输入项标识只能使用字母、数字、下划线或汉字，不能以数字开头，不超过 32 个字符")
        return value

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("输入项显示名不能为空")
        return value

    @field_validator("help")
    @classmethod
    def _help(cls, value: str) -> str:
        return value.strip()

    @field_validator("options")
    @classmethod
    def _options(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for raw in value:
            option = raw.strip()
            if not option or len(option) > si.OPTION_LIMIT:
                raise ValueError(f"下拉选项不能为空，每项不超过 {si.OPTION_LIMIT} 字")
            if option in result:
                raise ValueError(f"下拉选项重复：{option}")
            result.append(option)
        return result

    @model_validator(mode="after")
    def _kind_rules(self) -> "SkillInputDef":
        limit = si.VALUE_LIMITS[self.kind]
        if self.kind == "select":
            if not si.MIN_OPTIONS <= len(self.options) <= si.MAX_OPTIONS:
                raise ValueError(f"下拉输入项「{self.label}」须有 {si.MIN_OPTIONS}–{si.MAX_OPTIONS} 个选项")
            if self.default and self.default not in self.options:
                raise ValueError(f"下拉输入项「{self.label}」的默认值须是选项之一")
        elif self.options:
            raise ValueError(f"只有下拉输入项可以设置选项（{self.label}）")
        if len(self.default) > limit:
            raise ValueError(f"输入项「{self.label}」的默认值不超过 {limit} 字")
        return self


def check_input_defs(value: list[SkillInputDef]) -> list[SkillInputDef]:
    if len(value) > si.MAX_INPUTS:
        raise ValueError(f"输入项最多 {si.MAX_INPUTS} 个")
    names = [d.name for d in value]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        raise ValueError(f"输入项标识重复：{'、'.join(duplicated)}")
    return value


def _check_values(value: dict[str, str]) -> dict[str, str]:
    if len(value) > si.MAX_INPUTS:
        raise ValueError(f"Skill 输入最多 {si.MAX_INPUTS} 项")
    if sum(len(k) + len(v) for k, v in value.items()) > si.VALUES_TOTAL_LIMIT:
        raise ValueError(f"Skill 输入总长度不超过 {si.VALUES_TOTAL_LIMIT} 字")
    return value


# 请求中的 Skill 输入值（标识 → 值）；按 Skill 当前定义的详细校验在服务端快照时进行
SkillInputValues = Annotated[dict[str, str], AfterValidator(_check_values)]


def require_skill_for_inputs(model: Any) -> Any:
    """请求 model_validator：填写了 skill_inputs 却没有选 Skill → 422。"""
    if getattr(model, "skill_inputs", None) and getattr(model, "skill_id", None) is None:
        raise ValueError("填写了 Skill 输入却没有选择 Skill")
    return model


class SkillReferenceIn(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(default="", max_length=REFERENCE_LIMIT)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        # 标题即导出时的文件名：不允许路径分隔符与控制字符，也不能以 . 开头
        if not value or _BAD_TITLE.search(value) or value.startswith("."):
            raise ValueError("参考资料标题不能为空，不能包含 / \\ 或控制字符，也不能以 . 开头")
        return value


class SkillFields(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    category: str = Field(default="", max_length=30)
    triggers: list[str] = Field(default_factory=list, max_length=50)
    body: str = Field(default="", max_length=BODY_LIMIT)
    inputs: list[SkillInputDef] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("名称不能为空")
        return value

    @field_validator("inputs")
    @classmethod
    def _inputs(cls, value: list[SkillInputDef]) -> list[SkillInputDef]:
        return check_input_defs(value)

    @field_validator("triggers")
    @classmethod
    def _triggers(cls, value: list[str]) -> list[str]:
        return normalize_triggers(value)


class SkillCreate(SkillFields):
    model_config = ConfigDict(extra="forbid")
    scope: SkillScope = "mine"
    slug: str

    references: list[SkillReferenceIn] = Field(default_factory=list, max_length=MAX_REFERENCES)

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        return check_slug(value)


class SkillUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=30)
    triggers: list[str] | None = Field(default=None, max_length=50)
    body: str | None = Field(default=None, max_length=BODY_LIMIT)
    inputs: list[SkillInputDef] | None = None

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str | None) -> str | None:
        return None if value is None else check_slug(value)

    @field_validator("inputs")
    @classmethod
    def _inputs(cls, value: list[SkillInputDef] | None) -> list[SkillInputDef] | None:
        return None if value is None else check_input_defs(value)

    @field_validator("triggers")
    @classmethod
    def _triggers(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else normalize_triggers(value)


class SkillReferencesUpdate(BaseModel):
    references: list[SkillReferenceIn] = Field(max_length=MAX_REFERENCES)


class SkillReferenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    title: str
    sort: int
    content: str


class SkillBrief(BaseModel):
    """列表项：不含正文与参考资料内容。"""

    id: int
    scope: SkillScope
    slug: str
    name: str
    description: str
    category: str
    triggers: list[str]
    builtin_key: str | None
    source_skill_id: int | None
    # 当前用户能否修改 / 删除（全局 Skill 仅管理员）
    editable: bool
    body_chars: int
    reference_titles: list[str]
    # 自定义输入项定义（列表也带，前端选中 Skill 后无需再取详情即可渲染表单）
    inputs: list[SkillInputDef] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class SkillComponentRef(BaseModel):
    id: int
    slug: str
    name: str


class SkillOut(SkillBrief):
    body: str
    references: list[SkillReferenceOut]
    # 正文引用的所有者本人的提示词组件
    components: list[SkillComponentRef] = Field(default_factory=list)
    # 占位提示：正文里未声明的 {{ x }}、声明了却未引用的输入项（不阻止保存）
    input_warnings: list[str] = Field(default_factory=list)


class SkillPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: SkillInputValues = Field(default_factory=dict)


class SkillPreviewOut(BaseModel):
    """填入输入值、展开组件后的正文（即实际注入的补充工作流）。不落库。"""

    body: str
    missing_required: list[str]
    errors: list[str]


class SkillRef(BaseModel):
    """生成记录 / 任务里引用的 Skill（不含正文）。"""

    id: int
    name: str
