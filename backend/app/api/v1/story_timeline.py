"""章景日期独立保存，作品编辑权限与写入屏障同正文工作台。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query

from app.core.deps import CipherDep, CurrentUser, DbDep, FactoryDep, SettingsDep
from app.schemas.story_timeline import (
    CalendarGenerateRequest,
    CalendarImpact,
    CalendarOut,
    CalendarWrite,
    DateWrite,
    Kind,
    TimelineItem,
    TimelinePage,
)
from app.services import project_access, story_calendar_ai
from app.services import story_timeline as timeline

router = APIRouter(prefix="/projects/{project_id}/story-timeline", tags=["story-timeline"])
Id = Annotated[int, Path(ge=1, le=2_147_483_647)]


@router.get("")
async def list_timeline(project_id: Id, db: DbDep, user: CurrentUser, order: Literal["story", "chapter"] = "story",
                        offset: Annotated[int, Query(ge=0)] = 0,
                        limit: Annotated[int, Query(ge=1, le=200)] = 50) -> TimelinePage:
    await project_access.require_project(db, user.id, project_id, "content.read")
    return await timeline.list_items(db, project_id, order, offset, limit)


@router.get("/calendar")
async def calendar(project_id: Id, db: DbDep, user: CurrentUser) -> CalendarOut:
    await project_access.require_project(db, user.id, project_id, "content.read")
    return await timeline.get_calendar(db, project_id)


@router.post("/calendar/generate")
async def generate_calendar(
    project_id: Id, body: CalendarGenerateRequest, db: DbDep, user: CurrentUser,
    settings: SettingsDep, cipher: CipherDep, factory: FactoryDep,
) -> CalendarOut:
    return await story_calendar_ai.generate(
        db, user.id, project_id, body, cipher, factory, ai_timeout=settings.ai_request_timeout,
        allow_private=settings.allow_private_ai_endpoints, context_budget=settings.context_char_budget,
    )


@router.post("/calendar/preview")
async def preview(project_id: Id, body: CalendarWrite, db: DbDep, user: CurrentUser) -> CalendarImpact:
    await project_access.require_project(db, user.id, project_id, "content.edit")
    return await timeline.calendar_impact(db, project_id, body)


@router.put("/calendar")
async def save_calendar(project_id: Id, body: CalendarWrite, db: DbDep, user: CurrentUser) -> CalendarOut:
    return await timeline.write_calendar(db, user.id, project_id, body)


@router.get("/{kind}/{target_id}/date")
async def date(project_id: Id, kind: Kind, target_id: Id, db: DbDep, user: CurrentUser) -> TimelineItem:
    await project_access.require_project(db, user.id, project_id, "content.read")
    return await timeline.get_item(db, project_id, kind, target_id)


@router.put("/{kind}/{target_id}/date")
async def save_date(project_id: Id, kind: Kind, target_id: Id, body: DateWrite, db: DbDep,
                    user: CurrentUser) -> TimelineItem:
    return await timeline.write_date(db, user.id, project_id, kind, target_id, body)
