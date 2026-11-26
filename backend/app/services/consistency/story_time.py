"""把事实引句归到唯一场景，保存当时的日期依据，并在读取／合并时重新核对。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import batched
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chapter, Scene
from app.schemas.consistency import StoryTimeEvidence
from app.services import story_timeline

if TYPE_CHECKING:
    from app.services.scene_service import SceneRange

UNKNOWN = StoryTimeEvidence(source="unknown")


@dataclass
class DateContext:
    revision: int
    chapters: dict[int, tuple[str, list[SceneRange]]]
    scenes: dict[int, StoryTimeEvidence]
    chapter_numbers: dict[int, int] = field(default_factory=dict)

    def locate(self, chapter_id: int | None, evidence: dict[str, Any]) -> StoryTimeEvidence:
        projection = self.chapters.get(chapter_id) if chapter_id is not None else None
        if projection is None:
            return UNKNOWN
        content, spans = projection
        quote = evidence.get("quote")
        start, end = evidence.get("start"), evidence.get("end")
        if not quote or type(start) is not int or type(end) is not int or content.find(quote) != start:
            return UNKNOWN
        if start < 0 or end != start + len(quote) or content.find(quote, start + 1) >= 0:
            return UNKNOWN
        for span in spans:
            if span.start <= start < end <= span.end:
                return self.scenes.get(span.scene_id, UNKNOWN)
        return UNKNOWN


async def load_context(db: AsyncSession, project_id: int, chapter_ids: set[int]) -> DateContext:
    from app.services.scene_service import project_scenes

    if not chapter_ids:
        return DateContext(0, {}, {})
    calendar = await story_timeline.get_calendar(db, project_id)
    month_order = {month.id: i for i, month in enumerate(calendar.months, 1)}
    result = DateContext(calendar.revision, {}, {})
    for chunk in batched(sorted(chapter_ids), 200):
        rows = await db.execute(select(Chapter.id, Chapter.chapter_no, Chapter.content).where(
            Chapter.project_id == project_id, Chapter.id.in_(chunk),
        ))
        contents = {}
        for chapter_id, number, content in rows:
            result.chapter_numbers[chapter_id] = number
            result.chapters[chapter_id] = (content, [])
            contents[chapter_id] = content
        grouped: dict[int, list[Scene]] = defaultdict(list)
        for scene in await db.scalars(select(Scene).where(Scene.chapter_id.in_(contents), Scene.active.is_(True))):
            grouped[scene.chapter_id].append(scene)
        for chapter_id, scenes in grouped.items():
            projection = project_scenes(scenes)
            if projection[0] == contents[chapter_id]:
                result.chapters[chapter_id] = projection
        if not calendar.months:
            for scenes in grouped.values():
                for scene in scenes:
                    result.scenes[scene.id] = StoryTimeEvidence(scene_id=scene.id, source="unmarked")
            continue
        for item in await story_timeline.chapter_items(db, project_id, list(chunk), calendar):
            if item.scene_id is None:
                continue
            date = item.effective_date
            if date is None:
                snapshot = StoryTimeEvidence(scene_id=item.scene_id, source="unmarked")
            else:
                snapshot = StoryTimeEvidence(
                    scene_id=item.scene_id, source="scene" if item.source == "scene" else "chapter",
                    label=item.date_label, year=date.year, month_id=date.month_id,
                    month_order=month_order[date.month_id], day=date.day,
                )
            result.scenes[item.scene_id] = snapshot
    return result


def date_stale(context: DateContext, evidence: dict[str, Any]) -> bool:
    previous = evidence.get("story_time")
    return previous is not None and context.locate(
        evidence.get("chapter_id"), evidence,
    ) != StoryTimeEvidence.model_validate(previous)
