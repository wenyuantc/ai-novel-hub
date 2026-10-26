"""灵感会话：state（JSON 列）的结构与接口请求 / 响应。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.project import TARGET_WORDS_MAX, TARGET_WORDS_MIN

StepKey = Literal["title", "synopsis", "theme", "tags", "selling_points", "audience"]
# AI 生成选项的固定步骤（顺序即 stale 传播顺序）；想法 idea 是第 0 步，规模 / 确认不调用 AI
STEPS: tuple[StepKey, ...] = ("title", "synopsis", "theme", "tags", "selling_points", "audience")
STEP_LABELS: dict[str, str] = {
    "title": "书名",
    "synopsis": "简介",
    "theme": "主题",
    "tags": "类型标签",
    "selling_points": "核心卖点",
    "audience": "目标读者",
}
CurrentStep = Literal["idea", "title", "synopsis", "theme", "tags", "selling_points", "audience", "scale", "confirm"]
StepStatus = Literal["pending", "decided", "skipped"]
StepSource = Literal["ai", "edited", "manual", "project"]
SessionStatus = Literal["active", "converted"]

# 与项目一句话设定（premise）上限一致：从项目发起时原样带入，应用回项目也不截断
IDEA_MAX = 5000
NOTE_MAX = 200
MAX_NOTES = 20
MAX_ROUNDS = 10
FEEDBACK_MAX = 500
MAX_SESSIONS = 50


class Round(BaseModel):
    options: list[str] = Field(default_factory=list)
    feedback: str = ""
    created_at: str = ""


class StepState(BaseModel):
    # tags 步骤为 list[str]，其余为 str；未定 / 跳过为 None
    value: str | list[str] | None = None
    status: StepStatus = "pending"
    # 前面的步骤被修改后为 true（保留原值，提示「可能需要更新」）
    stale: bool = False
    source: StepSource | None = None
    rounds: list[Round] = Field(default_factory=list, max_length=MAX_ROUNDS)


class Scale(BaseModel):
    target_words: int | None = Field(default=None, ge=TARGET_WORDS_MIN, le=TARGET_WORDS_MAX)
    # 范围与 WizardRequest 一致
    volume_count: int = Field(ge=1, le=20)
    chapters_per_volume: int = Field(ge=1, le=100)
    character_count: int = Field(ge=3, le=30)


class SessionState(BaseModel):
    idea: str = Field(default="", max_length=IDEA_MAX)
    notes: list[str] = Field(default_factory=list, max_length=MAX_NOTES)
    # 从已有项目发起时项目的题材（主题材），作为已定内容参与生成
    genre: str = Field(default="", max_length=100)
    steps: dict[StepKey, StepState] = Field(default_factory=dict)
    scale: Scale | None = None
    current: CurrentStep = "idea"

    @field_validator("notes")
    @classmethod
    def _notes(cls, v: list[str]) -> list[str]:
        if any(len(n) > NOTE_MAX for n in v):
            raise ValueError(f"补充想法每条最多 {NOTE_MAX} 字")
        return v

    @model_validator(mode="after")
    def _all_steps(self) -> "SessionState":
        # 每个步骤都有条目，且按固定顺序排列
        self.steps = {k: self.steps.get(k) or StepState() for k in STEPS}
        return self


# ---------------- 请求 ----------------


class SessionCreate(BaseModel):
    idea: str = Field(min_length=1, max_length=IDEA_MAX)


class SessionUpdate(BaseModel):
    """给出的字段才修改；修改 idea 时所有已定步骤标为 stale；scale 显式 null = 清空。"""

    idea: str | None = Field(default=None, min_length=1, max_length=IDEA_MAX)
    current: CurrentStep | None = None
    scale: Scale | None = None


class StepUpdate(BaseModel):
    """decide：定下 value（tags 为列表）；skip：跳过；keep：保持不变（清除 stale）；clear：回到待定。"""

    action: Literal["decide", "skip", "keep", "clear"]
    value: str | list[str] | None = None
    source: Literal["ai", "edited", "manual"] = "manual"


class NoteCreate(BaseModel):
    text: str = Field(min_length=1, max_length=NOTE_MAX)


class GenerateRequest(BaseModel):
    feedback: str = Field(default="", max_length=FEEDBACK_MAX)
    ai_config_id: int | None = None


class ConvertRequest(BaseModel):
    title_override: str | None = Field(default=None, max_length=200)
    # 不给时用会话里保存的规模；都没有则不写向导参数
    scale: Scale | None = None


ApplyField = Literal["title", "synopsis", "theme", "tags", "selling_points", "audience", "premise", "target_words"]
APPLY_FIELDS: tuple[ApplyField, ...] = (
    "title",
    "synopsis",
    "theme",
    "tags",
    "selling_points",
    "audience",
    "premise",
    "target_words",
)
APPLY_LABELS: dict[str, str] = {**STEP_LABELS, "premise": "一句话设定", "target_words": "目标总字数"}


class ApplyRequest(BaseModel):
    project_id: int
    fields: list[ApplyField] = Field(default_factory=list, max_length=len(APPLY_FIELDS))


# ---------------- 响应 ----------------


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    status: SessionStatus
    project_id: int | None = None
    converted_project_id: int | None = None
    state: SessionState
    created_at: datetime
    updated_at: datetime


class SessionBrief(BaseModel):
    id: int
    title: str
    status: SessionStatus
    project_id: int | None = None
    converted_project_id: int | None = None
    # 已定或已跳过的步骤数 / 步骤总数
    done_steps: int = 0
    total_steps: int = len(STEPS)
    stale_steps: int = 0
    current: CurrentStep = "idea"
    created_at: datetime
    updated_at: datetime


class ApplyPreviewItem(BaseModel):
    field: ApplyField
    label: str
    current: str | list[str] | int | None = None
    proposed: str | list[str] | int | None = None
    # 项目中该字段为空时默认勾选
    default_checked: bool = False
