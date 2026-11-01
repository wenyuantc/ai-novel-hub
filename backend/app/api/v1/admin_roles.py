from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.core.deps import AdminUser, DbDep
from app.schemas.admin import AdminUserOut
from app.schemas.rbac import PermissionList, RoleCreate, RoleList, RoleOut, RoleUpdate, UserRolesUpdate
from app.services import role_service as svc

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/permissions", response_model=PermissionList)
async def permissions(admin: AdminUser, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return svc.permission_catalog()


@router.get("/roles", response_model=RoleList)
async def roles(
    admin: AdminUser, db: DbDep, response: Response,
    q: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    response.headers["Cache-Control"] = "no-store"
    return await svc.list_roles(db, q=q, page=page, page_size=page_size)


@router.get("/roles/{role_id}", response_model=RoleOut)
async def role(role_id: int, admin: AdminUser, db: DbDep, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await svc.get_role(db, role_id)


@router.post("/roles", response_model=RoleOut, status_code=201)
async def create(body: RoleCreate, admin: AdminUser, db: DbDep, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await svc.create_role(db, admin, body)


@router.patch("/roles/{role_id}", response_model=RoleOut)
async def update(role_id: int, body: RoleUpdate, admin: AdminUser, db: DbDep, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await svc.update_role(db, admin, role_id, body)


@router.delete("/roles/{role_id}", status_code=204)
async def delete(
    role_id: int, admin: AdminUser, db: DbDep,
    revision: Annotated[int, Query(ge=0)], authorization_revision: Annotated[int, Query(ge=0)],
):
    await svc.delete_role(db, admin, role_id, revision=revision, authorization_revision=authorization_revision)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.put("/users/{user_id}/roles", response_model=AdminUserOut)
async def assign(user_id: int, body: UserRolesUpdate, admin: AdminUser, db: DbDep, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await svc.replace_user_roles(db, admin, user_id, body)
