from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

TaskStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
TaskKind = Literal[
    "chapter.generate",
    "chapter.polish",
    "wizard.outline",
    "wizard.characters",
    "wizard.world",
    "character.generate",
    "organization.generate",
    "relations.parse",
    "career.generate",
    "wizard.careers",
    "chapter.analyze",
    "chapter.revise",
    "outline.continue",
    "outline.expand",
]


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: TaskKind
    status: TaskStatus
    title: str
    project_id: int | None
    chapter_id: int | None
    progress: int
    stage: str
    error: str
    retry_of_id: int | None
    # 任务针对的角色（取自 input.request.character_id，目前只有 relations.parse 有值），前端据此按角色接上活动任务
    character_id: int | None = None
    # 按建议重写引用的分析（取自 input.request.analysis_id，仅 chapter.revise 有值），前端据此对比重写前后评分
    analysis_id: int | None = None
    # 展开的大纲（取自 input.request.outline_id，仅 outline.expand 有值），前端据此按大纲接上活动任务
    outline_id: int | None = None
    # 仅排队中的任务有值：前面还在排队的任务数（全局先进先出，0 = 下一个执行）
    queue_position: int | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TaskOutput(BaseModel):
    text: str


class TaskClearResult(BaseModel):
    deleted: int
