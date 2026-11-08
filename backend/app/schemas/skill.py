"""Skill：用户私有 / 管理员全局的写作工作流与参考资料。"""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("名称不能为空")
        return value

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

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str | None) -> str | None:
        return None if value is None else check_slug(value)

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


class SkillRef(BaseModel):
    """生成记录 / 任务里引用的 Skill（不含正文）。"""

    id: int
    name: str
