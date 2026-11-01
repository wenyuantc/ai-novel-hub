from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.deps import DbDep, TaskEngineDep, require_permission
from app.models import User
from app.schemas.recycle_bin import RecycleProjectList, RecyclePurgeRequest, RecycleRestoreRequest
from app.services import recycle_bin_service as svc

PermissionRecycleBinRead = Annotated[User, Depends(require_permission("recycle_bin.read"))]
PermissionRecycleBinRestore = Annotated[User, Depends(require_permission("recycle_bin.restore"))]
PermissionRecycleBinPurge = Annotated[User, Depends(require_permission("recycle_bin.purge"))]

router = APIRouter(prefix="/admin/recycle-bin/projects", tags=["admin"])


@router.get("", response_model=RecycleProjectList)
async def list_projects(
    admin: PermissionRecycleBinRead,
    db: DbDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    owner_user_id: Annotated[int | None, Query(gt=0)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RecycleProjectList:
    return await svc.list_projects(db, q=q, owner_user_id=owner_user_id, page=page, page_size=page_size)


@router.post("/{project_id}/restore", status_code=status.HTTP_204_NO_CONTENT)
async def restore(
    project_id: int, body: RecycleRestoreRequest, admin: PermissionRecycleBinRestore, db: DbDep
) -> Response:
    await svc.restore(db, admin.id, project_id, body.deleted_at)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})


@router.post("/{project_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
async def purge(
    project_id: int, body: RecyclePurgeRequest, admin: PermissionRecycleBinPurge, db: DbDep, engine: TaskEngineDep
) -> Response:
    await svc.purge(db, engine, admin.id, project_id, body.deleted_at, title=body.title)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})
