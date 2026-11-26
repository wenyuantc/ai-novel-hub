"""单书固定历法。显式日期随目标身份保存，继承和排序读取章景的当前归属。"""

from collections import defaultdict

from sqlalchemy import Boolean, Integer, String, case, func, literal, null, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Chapter, ChapterStoryDate, Scene, SceneStoryDate, StoryCalendar
from app.schemas.story_timeline import (
    CalendarImpact,
    CalendarOut,
    CalendarWrite,
    DateWrite,
    Kind,
    StoryDate,
    TimelineItem,
    TimelinePage,
)
from app.services import project_access
from app.services.audit_writer import AuditOperation


async def get_calendar(db: AsyncSession, pid: int) -> CalendarOut:
    row = await db.get(StoryCalendar, pid, populate_existing=True)
    if row is None:
        return CalendarOut(revision=0, era_name=None, months=[])
    return CalendarOut.model_validate(row, from_attributes=True)


def _items(pid: int, *, retired=False):
    """两种视图共用窄投影，分页、过滤和故事时间排序均在数据库完成。"""
    chapter = [Chapter.id.label("chapter_id"), Chapter.chapter_no, Chapter.title.label("chapter_title")]
    cd, sd = ChapterStoryDate, SceneStoryDate
    chapters = select(
        *chapter, null().cast(Integer).label("scene_id"), null().cast(Integer).label("scene_sort"),
        null().cast(String).label("scene_title"), literal(True, Boolean).label("active"),
        cd.year, cd.month_id, cd.day, cd.year.is_not(None).label("explicit"),
    ).outerjoin(cd, cd.chapter_id == Chapter.id).where(Chapter.project_id == pid)
    scenes = select(
        *chapter, Scene.id, Scene.sort, Scene.title, Scene.active,
        func.coalesce(sd.year, cd.year), func.coalesce(sd.month_id, cd.month_id), func.coalesce(sd.day, cd.day),
        sd.year.is_not(None),
    ).join(Scene, Scene.chapter_id == Chapter.id).outerjoin(cd, cd.chapter_id == Chapter.id).outerjoin(
        sd, sd.scene_id == Scene.id,
    ).where(Chapter.project_id == pid)
    if not retired:
        scenes = scenes.where(Scene.active)
    return union_all(chapters, scenes).subquery()


def _present(row, calendar: CalendarOut, names: dict[str, str]) -> TimelineItem:
    is_chapter = row.scene_id is None
    date = label = None
    source = "unknown"
    if row.year is not None:
        date = StoryDate(year=row.year, month_id=row.month_id, day=row.day)
        label = f"{calendar.era_name}{date.year}年{names[date.month_id]}{date.day}日"
        source = "chapter" if is_chapter else ("scene" if row.explicit else "inherited")
    return TimelineItem(
        kind="chapter" if is_chapter else "scene", chapter_id=row.chapter_id, chapter_no=row.chapter_no,
        chapter_title=row.chapter_title, scene_id=row.scene_id, scene_sort=row.scene_sort,
        scene_title=row.scene_title, active=row.active, effective_date=date,
        explicit_date=date if row.explicit else None,
        source=source, date_label=label, revision=calendar.revision,
    )


def _check_revision(current: CalendarOut, expected: int):
    if current.revision != expected:
        raise ConflictError("故事日期已变化，请刷新后再保存", extra={"code": "story_timeline_changed"})


async def _present_all(db, pid, query, calendar):
    names = {m.id: m.name for m in calendar.months}
    rows = (await db.execute(query)).all()
    # PostgreSQL 可在两条查询之间提交，避免用旧历法解释新的月份。
    _check_revision(await get_calendar(db, pid), calendar.revision)
    return [_present(row, calendar, names) for row in rows]


async def chapter_items(db: AsyncSession, pid: int, chapter_ids: list[int], calendar: CalendarOut):
    if not chapter_ids:
        return []
    rows = _items(pid)
    return await _present_all(db, pid, select(rows).where(rows.c.chapter_id.in_(chapter_ids)), calendar)


async def get_item(db: AsyncSession, pid: int, kind: Kind, target_id: int) -> TimelineItem:
    calendar = await get_calendar(db, pid)
    rows = _items(pid, retired=True)
    query = select(rows).where(rows.c.scene_id == target_id) if kind == "scene" else select(rows).where(
        rows.c.scene_id.is_(None), rows.c.chapter_id == target_id,
    )
    result = await _present_all(db, pid, query, calendar)
    if not result:
        raise NotFoundError("章节或场景不存在")
    return result[0]


async def list_items(db: AsyncSession, pid: int, order: str, offset: int, limit: int) -> TimelinePage:
    calendar = await get_calendar(db, pid)
    rows = _items(pid)
    c = rows.c
    sort = []
    if order == "story":
        sort.extend([c.year.is_(None), c.year])
        if calendar.months:
            sort.append(case({m.id: i for i, m in enumerate(calendar.months)}, value=c.month_id))
        sort.append(c.day)
    sort.extend([c.chapter_no, c.chapter_id, c.scene_id.is_not(None), c.scene_sort, c.scene_id])
    total = await db.scalar(select(func.count()).select_from(rows))
    query = select(rows).order_by(*sort).offset(offset).limit(limit)
    items = await _present_all(db, pid, query, calendar)
    return TimelinePage(total=total, items=items, revision=calendar.revision)


async def calendar_impact(db: AsyncSession, pid: int, body: CalendarWrite) -> CalendarImpact:
    calendar = await get_calendar(db, pid)
    _check_revision(calendar, body.expected_revision)
    rows = _items(pid, retired=True)
    items = await _present_all(db, pid, select(rows).where(rows.c.explicit), calendar)
    limits = {m.id: m.days for m in body.months}
    old = {m.id: i for i, m in enumerate(calendar.months)}
    new = {m.id: i for i, m in enumerate(body.months)}
    blocked, valid = [], []
    used = defaultdict(set)
    for item in items:
        date = item.explicit_date
        if date.day > limits.get(date.month_id, 0):
            blocked.append(item)
        else:
            valid.append(item)
            used[date.year].add(date.month_id)
    changed = {
        (year, month) for year, months in used.items() for month in months
        if any((old[month] < old[other]) != (new[month] < new[other]) for other in months)
    }
    reordered = [item for item in valid if (item.explicit_date.year, item.explicit_date.month_id) in changed]
    return CalendarImpact(blocked=blocked, reordered=reordered)


async def write_calendar(db: AsyncSession, user_id: int, pid: int, body: CalendarWrite) -> CalendarOut:
    async with AuditOperation(db, user_id, "project.update", "project", pid, project_id=pid) as audit:
        project = await project_access.require_project(db, user_id, pid, "content.edit", for_update=True)
        audit.target(project, label="编辑作品历法")
        result = await _apply_calendar(db, pid, body)
        await audit.commit()
        return result


async def _apply_calendar(db: AsyncSession, pid: int, body: CalendarWrite) -> CalendarOut:
    impact = await calendar_impact(db, pid, body)
    if impact.blocked or (impact.reordered and not body.confirm_reorder):
        raise ConflictError("请先处理越界日期或确认顺序变化", extra=impact.model_dump())
    months = [m.model_dump() for m in body.months]
    row = await db.get(StoryCalendar, pid)
    if row is None:
        row = StoryCalendar(project_id=pid, revision=1, era_name=body.era_name, months=months)
        db.add(row)
    elif (row.era_name, row.months) != (body.era_name, months):
        row.era_name, row.months = body.era_name, months
        row.revision += 1
    await db.flush()
    return await get_calendar(db, pid)


async def write_date(
    db: AsyncSession, user_id: int, pid: int, kind: Kind, target_id: int, body: DateWrite,
) -> TimelineItem:
    async with AuditOperation(db, user_id, "project.update", "project", pid, project_id=pid) as audit:
        project = await project_access.require_project(db, user_id, pid, "content.edit", for_update=True)
        audit.target(project, label=f"编辑{'章节' if kind == 'chapter' else '场景'}故事日期 · {target_id}")
        item = await _apply_date(db, pid, kind, target_id, body)
        await audit.commit()
        return item


async def _apply_date(db: AsyncSession, pid: int, kind: Kind, target_id: int, body: DateWrite) -> TimelineItem:
    calendar = await get_calendar(db, pid)
    _check_revision(calendar, body.expected_revision)
    item = await get_item(db, pid, kind, target_id)
    date = body.date
    if item.explicit_date != date:
        model, key = (ChapterStoryDate, "chapter_id") if kind == "chapter" else (SceneStoryDate, "scene_id")
        if date is None:
            row = await db.get(model, target_id)
            await db.delete(row)
        else:
            if date.day > next((m.days for m in calendar.months if m.id == date.month_id), 0):
                raise BadRequestError("该日期超出作品历法，请核对月份和天数")
            await db.merge(model(**{key: target_id}, **date.model_dump()))
        row = await db.get(StoryCalendar, pid)
        row.revision += 1
        await db.flush()
        item = await get_item(db, pid, kind, target_id)
    return item
