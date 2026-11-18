"""作品共用的连载排期、定稿和分平台手动发布记录。"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Response

from app.core.deps import CurrentUser, DbDep, EditorUser
from app.schemas import serialization as schema
from app.services import serialization_read, serialization_write

router = APIRouter(prefix="/projects/{project_id}/serialization", tags=["serialization"])
ResourceId = Annotated[int, Path(ge=1, le=2_147_483_647)]
OptionalId = Annotated[int | None, Query(ge=1, le=2_147_483_647)]
Offset = Annotated[int, Query(ge=0, le=2_147_483_647)]
Limit = Annotated[int, Query(ge=1, le=200)]


@router.get("/overview", response_model=schema.Overview)
async def overview(
    project_id: ResourceId, user: CurrentUser, db: DbDep,
    month: Annotated[str, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")],
):
    if month.startswith("0000-"):
        raise HTTPException(status_code=422, detail="月份需要有效的公历年份")
    start = date.fromisoformat(f"{month}-01")
    return await serialization_read.overview(db, user.id, project_id, start)


@router.get("/days/{day}", response_model=schema.DayDetail)
async def day_detail(
    project_id: ResourceId, day: schema.CalendarDate, user: CurrentUser, db: DbDep,
    offset: Offset = 0, limit: Limit = 50,
):
    return await serialization_read.day_detail(db, user.id, project_id, day, offset=offset, limit=limit)


@router.put("/goal", response_model=schema.GoalOut)
async def set_goal(project_id: ResourceId, body: schema.GoalUpdate, user: EditorUser, db: DbDep):
    return await serialization_write.set_goal(db, user.id, project_id, body)


@router.get("/chapters", response_model=schema.Page[schema.ChapterRow])
async def chapters(
    project_id: ResourceId, user: CurrentUser, db: DbDep, planned_date: schema.CalendarDate | None = None,
    stage: schema.Stage | None = None, stock_only: bool = False, offset: Offset = 0, limit: Limit = 50,
):
    return await serialization_read.list_chapters(
        db, user.id, project_id, planned_date=planned_date, stage=stage,
        stock_only=stock_only, offset=offset, limit=limit,
    )


@router.get("/chapters/{chapter_id}", response_model=schema.ChapterDetail)
async def chapter_detail(
    project_id: ResourceId, chapter_id: ResourceId, user: CurrentUser, db: DbDep, edition_id: OptionalId = None,
):
    return await serialization_read.chapter_detail(db, user.id, project_id, chapter_id, edition_id=edition_id)


@router.patch("/chapters/{chapter_id}", response_model=schema.EntryMutation)
async def update_entry(
    project_id: ResourceId, chapter_id: ResourceId, body: schema.EntryUpdate, user: EditorUser, db: DbDep,
):
    return await serialization_write.update_entry(db, user.id, project_id, chapter_id, body)


@router.post("/chapters/{chapter_id}/editions", response_model=schema.EditionBrief)
async def finalize(
    project_id: ResourceId, chapter_id: ResourceId, body: schema.FinalizeRequest, user: EditorUser, db: DbDep,
):
    return await serialization_write.finalize(db, user.id, project_id, chapter_id, body)


@router.post("/chapters/{chapter_id}/publications", response_model=schema.PublicationReceipt)
async def publish(
    project_id: ResourceId, chapter_id: ResourceId, body: schema.PublishRequest, user: EditorUser, db: DbDep,
):
    return await serialization_write.publish(db, user.id, project_id, chapter_id, body)


@router.get("/publications", response_model=schema.Page[schema.PublicationRow])
async def publications(
    project_id: ResourceId, user: CurrentUser, db: DbDep,
    channel: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    chapter_id: OptionalId = None, offset: Offset = 0, limit: Limit = 50,
):
    return await serialization_read.list_publications(
        db, user.id, project_id, channel=channel, chapter_id=chapter_id, offset=offset, limit=limit,
    )


@router.get("/publications/{publication_id}", response_model=schema.PublicationDetail)
async def publication_detail(project_id: ResourceId, publication_id: ResourceId, user: CurrentUser, db: DbDep):
    return await serialization_read.publication_detail(db, user.id, project_id, publication_id)


@router.get("/publications/{publication_id}/export")
async def export_publication(
    project_id: ResourceId, publication_id: ResourceId, user: CurrentUser, db: DbDep,
    format: schema.Format,
) -> Response:
    file = await serialization_read.export_publication(db, user.id, project_id, publication_id, format)
    headers = {"Content-Disposition": file.content_disposition, "Cache-Control": "no-store"}
    if file.warnings_header:
        headers["X-Export-Warnings"] = file.warnings_header
    return Response(content=file.body, media_type=file.media_type, headers=headers)
