"""跨章移动的预览和确认合同。"""

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.progression import Boundary
from app.schemas.scene import SceneBlocker, SceneId, SceneRequest


class SceneMovePreview(SceneRequest):
    target_chapter_id: SceneId
    insert_before_scene_id: SceneId | None = None


class SceneMove(SceneMovePreview):
    signature: str = Field(min_length=64, max_length=64, pattern="^[a-f0-9]+$")


class SceneMoveChapter(BaseModel):
    chapter_id: int
    chapter_no: int
    title: str
    content_revision: int
    scene_count: int


class SceneMovePlanningChange(BaseModel):
    field: Literal["pov", "tags", "character", "world", "organization", "career", "relationship"]
    before: list[str]
    after: list[str]


class SceneMoveTimelineEvent(BaseModel):
    id: int
    label: str
    boundary: Boundary


class SceneMoveSeam(BaseModel):
    before: str
    after: str


class SceneMovePreviewOut(BaseModel):
    scene_id: int
    source: SceneMoveChapter
    target: SceneMoveChapter
    source_position: int
    target_position: int
    insert_before_scene_id: int | None
    planning_revision: int
    signature: str
    story_date_before: str
    story_date_after: str
    source_will_be_empty: bool
    planning_changes: list[SceneMovePlanningChange]
    timeline_events: list[SceneMoveTimelineEvent]
    source_seam: SceneMoveSeam
    target_seam: SceneMoveSeam
    moved_start: str
    moved_end: str
    blockers: list[SceneBlocker]


class SceneMoveOut(BaseModel):
    scene_id: int
    source_chapter_id: int
    target_chapter_id: int
    source_content_revision: int
    target_content_revision: int
    planning_revision: int
