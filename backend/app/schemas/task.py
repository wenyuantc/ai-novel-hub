from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.skill import SkillRef

TaskStatus = Literal["queued", "running", "paused", "succeeded", "failed", "cancelled", "interrupted"]
TaskKind = Literal[
    "agent.turn",
    "chapter.generate",
    "chapter.polish",
    "wizard.outline",
    "wizard.characters",
    "wizard.world",
    "character.generate",
    "organization.generate",
    "relations.parse",
    "relations.generate",
    "career.generate",
    "wizard.careers",
    "chapter.analyze",
    "chapter.revise",
    "outline.continue",
    "outline.expand",
    "chapter.batch",
    "kb.index",
    "book.import",
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
    # 生成类任务选用的 Skill（取自 input.skill 快照，只返回 id 与名称）
    skill: SkillRef | None = None
    # 多步任务的结构化断点（chapter.batch：阶段、当前章、每章结果，前端据此渲染章节清单；
    # book.import：断点摘要，见 book_import.reverse.summarize，不含体积较大的中间结果）
    checkpoint: dict[str, Any] | None = None
    # 断点：下一步要处理的章节在 input.chapter_ids 中的下标（仅多步任务有意义），前端据此显示「从第 N 章续跑」
    step_cursor: int = 0
    # 仅排队中的任务有值：前面还在排队的任务数（全局先进先出，0 = 下一个执行）
    queue_position: int | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TaskOutput(BaseModel):
    text: str


class TaskClearResult(BaseModel):
    deleted: int
