"""章节分析与回写的请求 / 响应模型。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

# none = 未分析；analyzing = 有排队 / 运行中的分析任务；analyzed = 最新分析与正文一致；stale = 正文已变化
AnalysisStatus = Literal["none", "analyzing", "analyzed", "stale"]
ChangeKind = Literal[
    "character_state",
    "life_status",
    "relationship_update",
    "relationship_create",
    "career_stage",
    "org_membership",
    "foreshadow_resolve",
    "foreshadow_plant_planned",
    "foreshadow_new",
]
ChangeStatus = Literal["applied", "pending", "reverted", "rejected", "skipped", "failed"]


class AnalyzeRequest(BaseModel):
    ai_config_id: int | None = None


class AnalyzePendingResult(BaseModel):
    queued: int
    task_ids: list[int]


class ChapterAnalysisState(BaseModel):
    chapter_id: int
    status: AnalysisStatus
    latest_analysis_id: int | None = None
    scores: dict[str, Any] | None = None
    analyzed_at: datetime | None = None
    # 排队 / 运行中的分析任务（最新一个）
    active_task_id: int | None = None


class AnalysisBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    chapter_id: int
    chapter_no: int
    task_id: int | None
    model: str
    scores: dict[str, Any]
    metrics: dict[str, Any]
    # 正文已变化（content_hash 与当前正文不同）
    stale: bool = False
    created_at: datetime


class AnalysisChangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    analysis_id: int
    chapter_no: int
    kind: ChangeKind
    target: dict[str, Any]
    field_key: str
    before: Any = None
    after: Any = None
    status: ChangeStatus
    reason: str
    applied_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime


class AnalysisOut(AnalysisBrief):
    summary: str
    plot_points: list[Any]
    hooks: list[Any]
    conflict: dict[str, Any]
    emotion: dict[str, Any]
    changes_raw: dict[str, Any]
    foreshadow_candidates: list[Any]
    suggestions: list[Any]
    warnings: list[Any]
    changes: list[AnalysisChangeOut] = []
