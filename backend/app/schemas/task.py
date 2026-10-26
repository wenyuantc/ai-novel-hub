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
    # 仅排队中的任务有值：前面还在排队的任务数（全局先进先出，0 = 下一个执行）
    queue_position: int | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TaskOutput(BaseModel):
    text: str


class TaskClearResult(BaseModel):
    deleted: int
