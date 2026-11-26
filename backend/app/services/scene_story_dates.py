"""结构操作在持有作品写锁的事务内保留场景日期的显式／继承关系。"""

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import SceneStoryDate, StoryCalendar
from app.schemas.story_timeline import TimelineItem
from app.services.story_timeline import get_item


def date_source_label(item: TimelineItem) -> str:
    if item.date_label is None:
        return "未标时间"
    source = "场景指定" if item.source == "scene" else "沿用本章"
    return f"{source} · {item.date_label}"


def require_same_date_revision(first: TimelineItem, second: TimelineItem) -> None:
    if first.revision != second.revision:
        raise ConflictError("故事日期已变化，请重新预览", extra={"code": "story_timeline_changed"})


async def move_dates(db: AsyncSession, project_id: int, scene_id: int, target_id: int) -> tuple[str, str, int]:
    scene = await get_item(db, project_id, "scene", scene_id)
    before = date_source_label(scene)
    if scene.explicit_date is not None:
        return before, before, scene.revision
    target = await get_item(db, project_id, "chapter", target_id)
    require_same_date_revision(scene, target)
    return before, date_source_label(target), scene.revision


async def copy_explicit_date(db: AsyncSession, source_id: int, target_id: int) -> None:
    source = await db.get(SceneStoryDate, source_id)
    target = await db.get(SceneStoryDate, target_id)
    if source is None:
        if target is not None:
            await db.delete(target)
    elif target is None:
        db.add(SceneStoryDate(scene_id=target_id, year=source.year, month_id=source.month_id, day=source.day))
    else:
        target.year, target.month_id, target.day = source.year, source.month_id, source.day


async def advance_timeline_revision(db: AsyncSession, project_id: int) -> None:
    await db.execute(update(StoryCalendar).where(StoryCalendar.project_id == project_id)
                     .values(revision=StoryCalendar.revision + 1))
