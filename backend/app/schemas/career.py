"""职业、阶段、角色挂接与挂接历史的请求 / 响应模型。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CareerKind = Literal["main", "sub"]

MAX_STAGES = 100


class StageIn(BaseModel):
    """整体提交阶段时的一项：带 id = 更新已有阶段，不带 id = 新建；提交顺序即新的 position。"""

    id: int | None = None
    name: str = Field(min_length=1, max_length=50)
    description: str = ""


class StageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    position: int
    name: str
    description: str


class CareerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: CareerKind = "main"
    description: str = ""
    requirements: str = ""
    abilities: str = ""
    rules: str = ""
    # 至少 1 阶由服务层校验（400「职业至少需要 1 个阶段」）
    stages: list[StageIn] = Field(max_length=MAX_STAGES)
    sort: int | None = None


class CareerUpdate(BaseModel):
    """省略 = 不变；stages 给出时整体同步（缺失的阶段被删除，被角色引用的返回 409）。"""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    kind: CareerKind | None = None
    description: str | None = None
    requirements: str | None = None
    abilities: str | None = None
    rules: str | None = None
    stages: list[StageIn] | None = Field(default=None, max_length=MAX_STAGES)
    sort: int | None = None


class CareerOut(BaseModel):
    id: int
    project_id: int
    name: str
    kind: str
    description: str
    requirements: str
    abilities: str
    rules: str
    sort: int
    stages: list[StageOut]
    holder_count: int = 0
    created_at: datetime
    updated_at: datetime


class CareerBatchCreate(BaseModel):
    """AI 生成职业预览确认后一次保存（单事务）。"""

    careers: list[CareerCreate] = Field(min_length=1, max_length=20)


class CareerHolderOut(BaseModel):
    id: int
    character_id: int
    character_name: str
    role: str
    stage_id: int
    stage_name: str
    stage_position: int
    stage_total: int
    note: str


class AssignmentCreate(BaseModel):
    career_id: int
    # 未给 = 第 1 阶
    stage_id: int | None = None
    note: str = Field(default="", max_length=200)
    # 可选；给出时必须与职业类别一致（主修 → main，副修 → sub）
    role: CareerKind | None = None
    chapter_no: int | None = Field(default=None, ge=1)
    chapter_id: int | None = Field(default=None, ge=1)
    anchor_kind: Literal["chapter", "planned", "scene"] | None = None
    scene_id: int | None = Field(default=None, ge=1, le=2_147_483_647, strict=True)
    boundary: Literal["start", "end"] = "start"
    edit_mode: Literal["baseline", "progression"] | None = None
    # 0 表示创建前必须没有该角色／职业时间线。
    expected_revision: int | None = Field(default=None, ge=0)
    history_note: str = Field(default="", max_length=200)


class AssignmentUpdate(BaseModel):
    """省略 = 不变；章节锚点记录剧情变化，基础修正须明确指定。"""

    stage_id: int | None = None
    note: str | None = Field(default=None, max_length=200)
    chapter_no: int | None = Field(default=None, ge=1)
    chapter_id: int | None = Field(default=None, ge=1)
    anchor_kind: Literal["chapter", "planned", "scene"] | None = None
    scene_id: int | None = Field(default=None, ge=1, le=2_147_483_647, strict=True)
    boundary: Literal["start", "end"] = "start"
    edit_mode: Literal["baseline", "progression"] | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    history_note: str = Field(default="", max_length=200)


class AssignmentOut(BaseModel):
    id: int
    character_id: int
    career_id: int
    career_name: str
    career_kind: str
    role: str
    stage_id: int
    stage_name: str
    stage_position: int
    stage_total: int
    note: str
    updated_at: datetime


class CareerHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    character_id: int
    career_id: int | None
    career_name: str
    source: str
    chapter_no: int | None
    changes: dict[str, Any]
    note: str
    created_at: datetime


class CareerAssignDraft(BaseModel):
    """随新角色一起保存的职业挂接（characters/with-relations 的 careers）。"""

    career_id: int
    stage_id: int | None = None
    note: str = Field(default="", max_length=200)


class CareerGenerateRequest(BaseModel):
    ai_config_id: int | None = None
    count: int = Field(default=3, ge=1, le=5)
    # 指定后全部生成该类别；未指定由模型决定
    kind: CareerKind | None = None
    instruction: str = Field(default="", max_length=2000)
