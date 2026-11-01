from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.deps import DbDep, require_permission
from app.models import User
from app.schemas.admin import (
    AdminProjectList,
    AdminUserCreate,
    AdminUserDetail,
    AdminUserList,
    AdminUserOut,
    AdminUserUpdate,
    ResetPasswordRequest,
)
from app.schemas.project import ProjectStatus
from app.services import admin_project_service
from app.services import admin_service as svc

PermissionProjectsRead = Annotated[User, Depends(require_permission("projects.read"))]
PermissionUsersRead = Annotated[User, Depends(require_permission("users.read"))]
PermissionUsersCreate = Annotated[User, Depends(require_permission("users.create"))]
PermissionUsersStatus = Annotated[User, Depends(require_permission("users.status"))]
PermissionUsersResetPassword = Annotated[User, Depends(require_permission("users.reset_password"))]
PermissionUsersDelete = Annotated[User, Depends(require_permission("users.delete"))]

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/projects", response_model=AdminProjectList)
async def list_projects(
    admin: PermissionProjectsRead,
    db: DbDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    owner_user_id: Annotated[int | None, Query(gt=0)] = None,
    status: ProjectStatus | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AdminProjectList:
    return await admin_project_service.list_projects(
        db, q=q, owner_user_id=owner_user_id, status=status, page=page, page_size=page_size
    )


@router.get("/users", response_model=AdminUserList)
async def list_users(
    admin: PermissionUsersRead,
    db: DbDep,
    q: Annotated[str | None, Query(max_length=64)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AdminUserList:
    return await svc.list_users(db, q=q, page=page, page_size=page_size)


@router.post("/users", response_model=AdminUserOut, status_code=status.HTTP_201_CREATED)
async def create_user(body: AdminUserCreate, admin: PermissionUsersCreate, db: DbDep) -> AdminUserOut:
    return await svc.create_user(db, admin, body)


@router.get("/users/{user_id}", response_model=AdminUserDetail)
async def get_user_detail(user_id: int, admin: PermissionUsersRead, db: DbDep) -> AdminUserDetail:
    return await svc.get_user_detail(db, user_id)


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(user_id: int, body: AdminUserUpdate, admin: PermissionUsersStatus, db: DbDep) -> AdminUserOut:
    return await svc.update_user(db, admin, user_id, body)


@router.post("/users/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    user_id: int, body: ResetPasswordRequest, admin: PermissionUsersResetPassword, db: DbDep
) -> Response:
    await svc.reset_password(db, admin, user_id, body)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: int, admin: PermissionUsersDelete, db: DbDep) -> Response:
    await svc.delete_user(db, admin, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})
