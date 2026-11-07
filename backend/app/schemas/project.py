from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.style import StyleBrief

ProjectStatus = Literal["planning", "writing", "completed"]

# 立项结构化字段的限制（与 projects 列 / 灵感会话一致）
MAX_TAGS = 8
TAG_MAX_LEN = 10
SYNOPSIS_MAX = 2000
THEME_MAX = 200
SELLING_POINTS_MAX = 500
AUDIENCE_MAX = 100
TARGET_WORDS_MIN = 1000
TARGET_WORDS_MAX = 10_000_000


def normalize_tags(values: list[str]) -> list[str]:
    """类型标签：去首尾空白、丢弃空串、去重（保持顺序）；超过 8 个或单个超过 10 字 → ValueError。"""
    out: list[str] = []
    for v in values:
        v = str(v).strip()
        if v and v not in out:
            out.append(v)
    if len(out) > MAX_TAGS:
        raise ValueError(f"类型标签最多 {MAX_TAGS} 个")
    for v in out:
        if len(v) > TAG_MAX_LEN:
            raise ValueError(f"类型标签每个最多 {TAG_MAX_LEN} 字：{v}")
    return out


# ---------------- 向导进度 ----------------


class WizardParams(BaseModel):
    """向导参数建议（灵感模式转为项目时写入；向导页优先用它初始化参数）。范围与 WizardRequest 一致。"""

    volume_count: int = Field(ge=1, le=20)
    chapters_per_volume: int = Field(ge=1, le=100)
    character_count: int = Field(ge=3, le=30)


class WizardProgress(BaseModel):
    done: list[str] = Field(default_factory=list, max_length=20)
    skipped: list[str] = Field(default_factory=list, max_length=20)
    current: str | None = Field(default=None, max_length=32)
    params: WizardParams | None = None


class WizardProgressUpdate(BaseModel):
    """给出的字段整体替换，未给的保持不变；current 显式 null = 清空。"""

    done: list[str] | None = Field(default=None, max_length=20)
    skipped: list[str] | None = Field(default=None, max_length=20)
    current: str | None = Field(default=None, max_length=32)


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    genre: str = Field(default="", max_length=100)
    premise: str = Field(default="", max_length=5000)
    # 已废弃（兼容一个版本）：非空时为项目创建「原风格」并设为默认，响应 notice 给出提示
    style: str = Field(default="", max_length=5000)
    default_style_id: int | None = None
    synopsis: str = Field(default="", max_length=SYNOPSIS_MAX)
    theme: str = Field(default="", max_length=THEME_MAX)
    tags: list[str] = Field(default_factory=list, max_length=50)
    selling_points: str = Field(default="", max_length=SELLING_POINTS_MAX)
    audience: str = Field(default="", max_length=AUDIENCE_MAX)
    target_words: int | None = Field(default=None, ge=TARGET_WORDS_MIN, le=TARGET_WORDS_MAX)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return normalize_tags(v)


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    genre: str | None = Field(default=None, max_length=100)
    premise: str | None = Field(default=None, max_length=5000)
    # 已废弃（兼容一个版本）：创建或更新项目的「原风格」并设为默认
    style: str | None = Field(default=None, max_length=5000)
    status: ProjectStatus | None = None
    auto_analyze: bool | None = None
    # 显式 null = 取消项目默认风格
    default_style_id: int | None = None
    synopsis: str | None = Field(default=None, max_length=SYNOPSIS_MAX)
    theme: str | None = Field(default=None, max_length=THEME_MAX)
    tags: list[str] | None = Field(default=None, max_length=50)
    selling_points: str | None = Field(default=None, max_length=SELLING_POINTS_MAX)
    audience: str | None = Field(default=None, max_length=AUDIENCE_MAX)
    # 显式 null = 清空目标总字数
    target_words: int | None = Field(default=None, ge=TARGET_WORDS_MIN, le=TARGET_WORDS_MAX)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str] | None) -> list[str] | None:
        return normalize_tags(v) if v is not None else None


class ReadingOut(BaseModel):
    """阅读进度（读取时已兜底：进度指向的章节被删除或正文为空时退回前面最近的已写章节，ratio 置 0）。"""

    chapter_id: int
    chapter_no: int
    ratio: float


class ProjectOwner(BaseModel):
    id: int
    username: str


class ProjectSeriesBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    revision: int


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner: ProjectOwner | None = None
    series: ProjectSeriesBrief | None = None
    title: str
    genre: str
    premise: str
    status: ProjectStatus
    synopsis: str = ""
    theme: str = ""
    tags: list[str] = Field(default_factory=list)
    selling_points: str = ""
    audience: str = ""
    target_words: int | None = None
    auto_analyze: bool = True
    wizard_progress: WizardProgress = Field(default_factory=WizardProgress)
    created_at: datetime
    updated_at: datetime
    cover_updated_at: datetime | None = None
    chapter_count: int = 0
    total_words: int = 0
    default_style_id: int | None = None
    default_style: StyleBrief | None = None
    # 正文非空、updated_at 最大的章节（书架「继续写作」）
    last_edited_chapter_id: int | None = None
    # 阅读进度；没有进度或兜底后没有已写章节时为 null
    reading: ReadingOut | None = None
    # 兼容提示（例如请求里仍传了已废弃的 style 字段）
    notice: str | None = None
