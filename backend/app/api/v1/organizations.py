"""组织与成员路由；AI 生成组织走后台任务（202）。"""

from typing import Any, Literal

from fastapi import APIRouter, Query, Response, status

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.schemas.entity_ai import OrganizationGenerateRequest
from app.schemas.organization import (
    MemberCreate,
    MemberOut,
    MemberUpdate,
    OrganizationCreate,
    OrganizationOut,
    OrganizationUpdate,
    OrganizationWithMembers,
    OrganizationWithMembersOut,
)
from app.schemas.task import TaskOut
from app.services import organization_service as svc
from app.services import task_service

router = APIRouter(prefix="/projects/{project_id}", tags=["organizations"])


@router.get("/organizations", response_model=list[OrganizationOut])
async def list_orgs(project_id: int, user: CurrentUser, db: DbDep) -> list[OrganizationOut]:
    return await svc.list_orgs(db, user.id, project_id)


@router.post("/organizations", response_model=OrganizationOut, status_code=status.HTTP_201_CREATED)
async def create_org(project_id: int, body: OrganizationCreate, user: CurrentUser, db: DbDep) -> OrganizationOut:
    return await svc.create_org(db, user.id, project_id, body)


@router.post(
    "/organizations/with-members", response_model=OrganizationWithMembersOut, status_code=status.HTTP_201_CREATED
)
async def create_with_members(
    project_id: int, body: OrganizationWithMembers, user: CurrentUser, db: DbDep
) -> dict[str, Any]:
    return await svc.create_with_members(db, user.id, project_id, body)


@router.post("/organizations/generate", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def generate_org(
    project_id: int,
    user: CurrentUser,
    db: DbDep,
    engine: TaskEngineDep,
    body: OrganizationGenerateRequest | None = None,
) -> TaskOut:
    params = {"project_id": project_id, **(body or OrganizationGenerateRequest()).model_dump(mode="json")}
    return await task_service.create_task(db, engine, user.id, "organization.generate", params)


@router.get("/organizations/{org_id}", response_model=OrganizationOut)
async def get_org(project_id: int, org_id: int, user: CurrentUser, db: DbDep) -> OrganizationOut:
    return await svc.get_org(db, user.id, project_id, org_id)


@router.patch("/organizations/{org_id}", response_model=OrganizationOut)
async def update_org(
    project_id: int, org_id: int, body: OrganizationUpdate, user: CurrentUser, db: DbDep
) -> OrganizationOut:
    return await svc.update_org(db, user.id, project_id, org_id, body)


@router.delete("/organizations/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_org(project_id: int, org_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_org(db, user.id, project_id, org_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/organizations/{org_id}/members", response_model=list[MemberOut])
async def list_members(project_id: int, org_id: int, user: CurrentUser, db: DbDep) -> list[MemberOut]:
    return await svc.list_members(db, user.id, project_id, org_id)


@router.post("/organizations/{org_id}/members", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def add_member(project_id: int, org_id: int, body: MemberCreate, user: CurrentUser, db: DbDep) -> MemberOut:
    return await svc.add_member(db, user.id, project_id, org_id, body)


@router.patch("/organizations/{org_id}/members/{member_id}", response_model=MemberOut)
async def update_member(
    project_id: int, org_id: int, member_id: int, body: MemberUpdate, user: CurrentUser, db: DbDep
) -> MemberOut:
    return await svc.update_member(db, user.id, project_id, org_id, member_id, body)


@router.delete("/organizations/{org_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    project_id: int,
    org_id: int,
    member_id: int,
    user: CurrentUser,
    db: DbDep,
    edit_mode: Literal["progression", "delete"] | None = None,
    expected_revision: int | None = Query(default=None, ge=1, le=2_147_483_647),
    anchor_kind: Literal["chapter", "planned"] | None = None,
    chapter_id: int | None = Query(default=None, ge=1, le=2_147_483_647),
    chapter_no: int | None = Query(default=None, ge=1, le=2_147_483_647),
    boundary: Literal["start", "end"] = "start",
) -> Response:
    await svc.remove_member(
        db,
        user.id,
        project_id,
        org_id,
        member_id,
        edit_mode=edit_mode,
        expected_revision=expected_revision,
        anchor_kind=anchor_kind,
        chapter_id=chapter_id,
        chapter_no=chapter_no,
        boundary=boundary,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/organizations/{org_id}/membership-records/{character_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_membership_record(
    project_id: int,
    org_id: int,
    character_id: int,
    user: CurrentUser,
    db: DbDep,
    expected_revision: int = Query(ge=1, le=2_147_483_647),
) -> Response:
    """删除完整成员资料及其所有剧情记录，包括当前已退出的成员。"""
    await svc.delete_membership_record(db, user.id, project_id, org_id, character_id, expected_revision)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/characters/{character_id}/memberships", response_model=list[MemberOut])
async def character_memberships(project_id: int, character_id: int, user: CurrentUser, db: DbDep) -> list[MemberOut]:
    return await svc.character_memberships(db, user.id, project_id, character_id)
