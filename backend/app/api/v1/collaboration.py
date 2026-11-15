"""项目/系列成员与本人邀请入口。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query, Response, status

from app.core.deps import CurrentUser, DbDep, SessionmakerDep, TaskEngineDep
from app.schemas.collaboration import (
    InvitationOut,
    InvitationResponse,
    MemberOut,
    ProjectInvite,
    ProjectMemberUpdate,
    Scope,
    SeriesInvite,
)
from app.services import collaboration_service as svc

router = APIRouter(tags=["collaboration"])
Id = Annotated[int, Path(gt=0, le=2_147_483_647)]
Revision = Annotated[int, Query(gt=0, le=2_147_483_647)]


@router.get("/projects/{project_id}/collaborators", response_model=list[MemberOut])
async def project_members(project_id: Id, user: CurrentUser, db: DbDep) -> list[MemberOut]:
    return await svc.list_members(db, user.id, "project", project_id)


@router.post("/projects/{project_id}/collaborators", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def invite_project(
    project_id: Id, body: ProjectInvite, user: CurrentUser, db: DbDep, factory: SessionmakerDep, engine: TaskEngineDep,
) -> MemberOut:
    return await svc.invite(db, factory, user, "project", project_id, body.username, body.role, body.allow_owner_ai,
                            notify=engine.notify_collaboration_change)


@router.patch("/projects/{project_id}/collaborators/{user_id}", response_model=MemberOut)
async def modify_project_member(
    project_id: Id, user_id: Id, body: ProjectMemberUpdate, user: CurrentUser, db: DbDep, factory: SessionmakerDep,
    engine: TaskEngineDep,
) -> MemberOut:
    result = await svc.change_member(db, factory, user, "project", project_id, user_id, body.expected_revision,
                                     body=body, notify=engine.notify_collaboration_change)
    assert result is not None
    return result


@router.delete("/projects/{project_id}/collaborators/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_project_member(
    project_id: Id, user_id: Id, expected_revision: Revision, user: CurrentUser, db: DbDep, factory: SessionmakerDep,
    engine: TaskEngineDep,
) -> Response:
    await svc.change_member(db, factory, user, "project", project_id, user_id, expected_revision,
                            notify=engine.notify_collaboration_change)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/series/{sid}/collaborators", response_model=list[MemberOut])
async def series_members(sid: Id, user: CurrentUser, db: DbDep) -> list[MemberOut]:
    return await svc.list_members(db, user.id, "series", sid)


@router.post("/series/{sid}/collaborators", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
async def invite_series(
    sid: Id, body: SeriesInvite, user: CurrentUser, db: DbDep, factory: SessionmakerDep,
) -> MemberOut:
    return await svc.invite(db, factory, user, "series", sid, body.username)


@router.delete("/series/{sid}/collaborators/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_series_member(
    sid: Id, user_id: Id, expected_revision: Revision, user: CurrentUser, db: DbDep, factory: SessionmakerDep,
) -> Response:
    await svc.change_member(db, factory, user, "series", sid, user_id, expected_revision)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/collaboration/invitations", response_model=list[InvitationOut])
async def invitations(user: CurrentUser, db: DbDep) -> list[InvitationOut]:
    return await svc.list_invitations(db, user.id)


@router.post("/collaboration/invitations/{scope}/{invitation_id}/accept", response_model=MemberOut)
async def accept_invitation(
    scope: Scope, invitation_id: UUID, body: InvitationResponse, user: CurrentUser, db: DbDep, factory: SessionmakerDep,
    engine: TaskEngineDep,
) -> MemberOut:
    return await svc.respond(db, factory, user, scope, str(invitation_id), body.expected_revision, accept=True,
                             notify=engine.notify_collaboration_change)


@router.post("/collaboration/invitations/{scope}/{invitation_id}/decline", response_model=MemberOut)
async def decline_invitation(
    scope: Scope, invitation_id: UUID, body: InvitationResponse, user: CurrentUser, db: DbDep, factory: SessionmakerDep,
    engine: TaskEngineDep,
) -> MemberOut:
    return await svc.respond(db, factory, user, scope, str(invitation_id), body.expected_revision, accept=False,
                             notify=engine.notify_collaboration_change)
