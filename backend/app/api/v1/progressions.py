"""设定基础值、章节变化及按章求值的人工内容管理路由。"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.core.deps import CurrentUser, DbDep
from app.schemas.progression import (
    BaselineOut,
    BaselineUpdate,
    Boundary,
    EffectiveOut,
    ProgressionCreate,
    ProgressionListOut,
    ProgressionMutationOut,
    ProgressionUpdate,
    RevisionOut,
    SubjectKind,
    TextFieldCreate,
    TextFieldListOut,
    TextFieldMutationOut,
    TextFieldUpdate,
    TimelineCreate,
    TimelineListOut,
    TimelineOut,
)
from app.services import setting_progression_service as svc

router = APIRouter(prefix="/projects/{project_id}/setting-timelines", tags=["setting-progressions"])
MAX_DB_INT = 2_147_483_647
PathId = Annotated[int, Path(ge=1, le=MAX_DB_INT)]


@router.get("", response_model=TimelineListOut)
async def list_timelines(
    project_id: PathId,
    user: CurrentUser,
    db: DbDep,
    subject_kind: SubjectKind | None = None,
    character_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
    related_character_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
    world_setting_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
    career_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
    organization_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
) -> TimelineListOut:
    return await svc.list_timelines(
        db,
        user.id,
        project_id,
        subject_kind=subject_kind,
        character_id=character_id,
        related_character_id=related_character_id,
        world_setting_id=world_setting_id,
        career_id=career_id,
        organization_id=organization_id,
    )


@router.post("", response_model=TimelineOut, status_code=status.HTTP_201_CREATED)
async def create_timeline(project_id: PathId, body: TimelineCreate, user: CurrentUser, db: DbDep) -> TimelineOut:
    return await svc.create_timeline(db, user.id, project_id, body)


@router.get("/{timeline_id}/baseline", response_model=BaselineOut)
async def read_baseline(project_id: PathId, timeline_id: PathId, user: CurrentUser, db: DbDep) -> BaselineOut:
    return await svc.read_baseline(db, user.id, project_id, timeline_id)


@router.patch("/{timeline_id}/baseline", response_model=BaselineOut)
async def update_baseline(
    project_id: PathId, timeline_id: PathId, body: BaselineUpdate, user: CurrentUser, db: DbDep
) -> BaselineOut:
    return await svc.update_baseline(db, user.id, project_id, timeline_id, body)


@router.get("/{timeline_id}/progressions", response_model=ProgressionListOut)
async def list_progressions(
    project_id: PathId,
    timeline_id: PathId,
    user: CurrentUser,
    db: DbDep,
    offset: int = Query(default=0, ge=0, le=MAX_DB_INT),
    limit: int = Query(default=50, ge=1, le=200),
    field: str | None = Query(default=None, max_length=100),
    include_inactive: bool = False,
) -> ProgressionListOut:
    if field and field.startswith("custom:"):
        try:
            custom_id = int(field.removeprefix("custom:"))
        except ValueError:
            pass  # 格式错误保留服务层现有的 400 错误。
        else:
            if not 1 <= custom_id <= MAX_DB_INT:
                raise HTTPException(status_code=422, detail="自定义字段编号超出有效范围")
    return await svc.list_progressions(
        db, user.id, project_id, timeline_id, offset=offset, limit=limit, field=field, include_inactive=include_inactive
    )


@router.post("/{timeline_id}/progressions", response_model=ProgressionMutationOut, status_code=status.HTTP_201_CREATED)
async def create_progression(
    project_id: PathId, timeline_id: PathId, body: ProgressionCreate, user: CurrentUser, db: DbDep
) -> ProgressionMutationOut:
    return await svc.create_progression(db, user.id, project_id, timeline_id, body)


@router.get("/{timeline_id}/progressions/{event_id}", response_model=ProgressionMutationOut)
async def read_progression(
    project_id: PathId, timeline_id: PathId, event_id: PathId, user: CurrentUser, db: DbDep
) -> ProgressionMutationOut:
    return await svc.read_progression(db, user.id, project_id, timeline_id, event_id)


@router.patch("/{timeline_id}/progressions/{event_id}", response_model=ProgressionMutationOut)
async def update_progression(
    project_id: PathId, timeline_id: PathId, event_id: PathId, body: ProgressionUpdate, user: CurrentUser, db: DbDep
) -> ProgressionMutationOut:
    return await svc.update_progression(db, user.id, project_id, timeline_id, event_id, body)


@router.delete("/{timeline_id}/progressions/{event_id}", response_model=RevisionOut)
async def delete_progression(
    project_id: PathId,
    timeline_id: PathId,
    event_id: PathId,
    user: CurrentUser,
    db: DbDep,
    expected_revision: int = Query(ge=1, le=MAX_DB_INT),
    expected_fields_revision: int | None = Query(default=None, ge=0, le=MAX_DB_INT),
) -> RevisionOut:
    return await svc.delete_progression(
        db,
        user.id,
        project_id,
        timeline_id,
        event_id,
        expected_revision,
        expected_fields_revision=expected_fields_revision,
    )


@router.get("/{timeline_id}/effective", response_model=EffectiveOut)
async def read_effective(
    project_id: PathId,
    timeline_id: PathId,
    user: CurrentUser,
    db: DbDep,
    chapter_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
    chapter_no: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
    scene_id: int | None = Query(default=None, ge=1, le=MAX_DB_INT),
    boundary: Boundary = "start",
) -> EffectiveOut:
    return await svc.read_effective(
        db, user.id, project_id, timeline_id, chapter_id=chapter_id, chapter_no=chapter_no,
        scene_id=scene_id, boundary=boundary
    )


@router.get("/{timeline_id}/text-fields", response_model=TextFieldListOut)
async def list_text_fields(project_id: PathId, timeline_id: PathId, user: CurrentUser, db: DbDep) -> TextFieldListOut:
    return await svc.list_text_fields(db, user.id, project_id, timeline_id)


@router.post("/{timeline_id}/text-fields", response_model=TextFieldMutationOut, status_code=status.HTTP_201_CREATED)
async def create_text_field(
    project_id: PathId, timeline_id: PathId, body: TextFieldCreate, user: CurrentUser, db: DbDep
) -> TextFieldMutationOut:
    return await svc.create_text_field(db, user.id, project_id, timeline_id, body)


@router.patch("/{timeline_id}/text-fields/{field_id}", response_model=TextFieldMutationOut)
async def update_text_field(
    project_id: PathId, timeline_id: PathId, field_id: PathId, body: TextFieldUpdate, user: CurrentUser, db: DbDep
) -> TextFieldMutationOut:
    return await svc.update_text_field(db, user.id, project_id, timeline_id, field_id, body)


@router.delete("/{timeline_id}/text-fields/{field_id}", response_model=RevisionOut)
async def delete_text_field(
    project_id: PathId,
    timeline_id: PathId,
    field_id: PathId,
    user: CurrentUser,
    db: DbDep,
    expected_revision: int = Query(ge=1, le=MAX_DB_INT),
) -> RevisionOut:
    return await svc.delete_text_field(db, user.id, project_id, timeline_id, field_id, expected_revision)
