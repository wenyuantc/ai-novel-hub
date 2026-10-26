"""伏笔：CRUD、快捷操作（埋设 / 回收 / 废弃）、历史、统计与章节提醒。全部先校验项目归属，他人资源一律 404。"""

from fastapi import APIRouter, Response, status

from app.core.deps import CurrentUser, DbDep
from app.schemas.foreshadow import (
    ForeshadowCreate,
    ForeshadowHistoryOut,
    ForeshadowOut,
    ForeshadowReminders,
    ForeshadowSort,
    ForeshadowStats,
    ForeshadowStatus,
    ForeshadowUpdate,
    PlantRequest,
    ResolveRequest,
)
from app.services import foreshadow_service as svc

router = APIRouter(prefix="/projects/{project_id}", tags=["foreshadows"])


@router.get("/foreshadows", response_model=list[ForeshadowOut])
async def list_foreshadows(
    project_id: int,
    user: CurrentUser,
    db: DbDep,
    status: ForeshadowStatus | None = None,
    overdue: bool = False,
    long_term: bool = False,
    character_id: int | None = None,
    sort: ForeshadowSort = "planned_resolve",
) -> list[ForeshadowOut]:
    return await svc.list_foreshadows(
        db,
        user.id,
        project_id,
        status=status,
        overdue=overdue,
        long_term=long_term,
        character_id=character_id,
        sort=sort,
    )


@router.post("/foreshadows", response_model=ForeshadowOut, status_code=status.HTTP_201_CREATED)
async def create_foreshadow(project_id: int, body: ForeshadowCreate, user: CurrentUser, db: DbDep) -> ForeshadowOut:
    return await svc.create_foreshadow(db, user.id, project_id, body)


# 固定路径先于 /foreshadows/{foreshadow_id} 注册
@router.get("/foreshadows/stats", response_model=ForeshadowStats)
async def foreshadow_stats(project_id: int, user: CurrentUser, db: DbDep) -> ForeshadowStats:
    return await svc.stats(db, user.id, project_id)


@router.get("/foreshadows/{foreshadow_id}", response_model=ForeshadowOut)
async def get_foreshadow(project_id: int, foreshadow_id: int, user: CurrentUser, db: DbDep) -> ForeshadowOut:
    return await svc.get_foreshadow(db, user.id, project_id, foreshadow_id)


@router.patch("/foreshadows/{foreshadow_id}", response_model=ForeshadowOut)
async def update_foreshadow(
    project_id: int, foreshadow_id: int, body: ForeshadowUpdate, user: CurrentUser, db: DbDep
) -> ForeshadowOut:
    return await svc.update_foreshadow(db, user.id, project_id, foreshadow_id, body)


@router.delete("/foreshadows/{foreshadow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_foreshadow(project_id: int, foreshadow_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_foreshadow(db, user.id, project_id, foreshadow_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/foreshadows/{foreshadow_id}/plant", response_model=ForeshadowOut)
async def plant_foreshadow(
    project_id: int, foreshadow_id: int, user: CurrentUser, db: DbDep, body: PlantRequest | None = None
) -> ForeshadowOut:
    return await svc.plant(db, user.id, project_id, foreshadow_id, body or PlantRequest())


@router.post("/foreshadows/{foreshadow_id}/resolve", response_model=ForeshadowOut)
async def resolve_foreshadow(
    project_id: int, foreshadow_id: int, user: CurrentUser, db: DbDep, body: ResolveRequest | None = None
) -> ForeshadowOut:
    return await svc.resolve(db, user.id, project_id, foreshadow_id, body or ResolveRequest())


@router.post("/foreshadows/{foreshadow_id}/abandon", response_model=ForeshadowOut)
async def abandon_foreshadow(project_id: int, foreshadow_id: int, user: CurrentUser, db: DbDep) -> ForeshadowOut:
    return await svc.abandon(db, user.id, project_id, foreshadow_id)


@router.get("/foreshadows/{foreshadow_id}/history", response_model=list[ForeshadowHistoryOut])
async def foreshadow_history(
    project_id: int, foreshadow_id: int, user: CurrentUser, db: DbDep
) -> list[ForeshadowHistoryOut]:
    return await svc.list_history(db, user.id, project_id, foreshadow_id)


@router.get("/chapters/{chapter_id}/foreshadow-reminders", response_model=ForeshadowReminders)
async def chapter_foreshadow_reminders(
    project_id: int, chapter_id: int, user: CurrentUser, db: DbDep
) -> ForeshadowReminders:
    return await svc.chapter_reminders(db, user.id, project_id, chapter_id)
