from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.core.deps import AdminUser, DbDep, TaskEngineDep
from app.schemas.recycle_bin import RecycleProjectList, RecyclePurgeRequest, RecycleRestoreRequest
from app.services import recycle_bin_service as svc

router = APIRouter(prefix="/admin/recycle-bin/projects", tags=["admin"])


@router.get("", response_model=RecycleProjectList)
async def list_projects(
    admin: AdminUser,
    db: DbDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    owner_user_id: Annotated[int | None, Query(gt=0)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RecycleProjectList:
    return await svc.list_projects(db, q=q, owner_user_id=owner_user_id, page=page, page_size=page_size)


@router.post("/{project_id}/restore", status_code=status.HTTP_204_NO_CONTENT)
async def restore(project_id: int, body: RecycleRestoreRequest, admin: AdminUser, db: DbDep) -> Response:
    await svc.restore(db, admin.id, project_id, body.deleted_at)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{project_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
async def purge(
    project_id: int, body: RecyclePurgeRequest, admin: AdminUser, db: DbDep, engine: TaskEngineDep
) -> Response:
    await svc.purge(db, engine, admin.id, project_id, body.deleted_at, title=body.title)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
