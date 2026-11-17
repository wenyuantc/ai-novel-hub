"""本人小说模板与模板建书。"""

from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, Response, status

from app.core.deps import CurrentUser, DbDep, SessionmakerDep
from app.schemas.novel_template import (
    TemplateDetail,
    TemplateGroup,
    TemplateList,
    TemplatePreview,
    TemplateProjectCreate,
    TemplateSave,
    TemplateSnapshotReplace,
    TemplateSummary,
    TemplateUpdate,
)
from app.schemas.novel_template_snapshot import TemplateSelection
from app.schemas.project import ProjectOut
from app.services import authorization_service as authz
from app.services import novel_template_service as svc

router = APIRouter(tags=["novel-templates"])
Id = Annotated[int, Path(gt=0, le=2_147_483_647)]
Page = Annotated[int, Query(ge=1, le=2_147_483_647)]
PageSize = Annotated[int, Query(ge=1, le=50)]
Revision = Annotated[int, Query(ge=1, le=2_147_483_647)]


@router.get("/novel-templates", response_model=TemplateList)
async def list_templates(user: CurrentUser, db: DbDep, q: str = Query(default="", max_length=100),
                         page: Page = 1, page_size: PageSize = 20):
    return await svc.list_templates(db, user, q=q.strip(), page=page, page_size=page_size)


@router.get("/novel-templates/{template_id}", response_model=TemplateDetail)
async def get_template(template_id: Id, user: CurrentUser, db: DbDep, group: TemplateGroup | None = None,
                       page: Page = 1, page_size: PageSize = 50):
    return await svc.template_detail(db, user, template_id, group=group, page=page, page_size=page_size)


@router.get("/novel-templates/{template_id}/images/{ref}")
async def get_template_image(template_id: Id, ref: str, revision: Revision, user: CurrentUser, db: DbDep,
                             if_none_match: Annotated[str | None, Header()] = None) -> Response:
    data, mime, digest = await svc.template_image(db, user, template_id, ref, revision)
    etag = f'"{revision}-{digest}"'
    headers = {"Cache-Control": "private, no-cache", "ETag": etag, "X-Content-Type-Options": "nosniff"}
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type=mime, headers=headers)


@router.post("/projects/{project_id}/novel-template-preview", response_model=TemplatePreview)
async def preview_template(project_id: Id, body: TemplateSelection, user: CurrentUser, db: DbDep,
                           factory: SessionmakerDep):
    actor = authz.actor_snapshot(db, user)
    if not isinstance(actor, authz.AuthenticatedActor):
        raise TypeError("模板预览必须携带原登录身份")
    return await svc.preview_source(factory, actor, project_id, body)


@router.post("/novel-templates", response_model=TemplateSummary, status_code=status.HTTP_201_CREATED)
async def create_template(body: TemplateSave, user: CurrentUser, db: DbDep):
    return await svc.save_template(db, user, body)


@router.patch("/novel-templates/{template_id}", response_model=TemplateSummary)
async def update_template(template_id: Id, body: TemplateUpdate, user: CurrentUser, db: DbDep):
    return await svc.update_template(db, user, template_id, body)


@router.put("/novel-templates/{template_id}/snapshot", response_model=TemplateSummary)
async def replace_template(template_id: Id, body: TemplateSnapshotReplace, user: CurrentUser, db: DbDep):
    return await svc.replace_snapshot(db, user, template_id, body)


@router.delete("/novel-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template(template_id: Id, expected_revision: Revision, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_template(db, user, template_id, expected_revision)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/novel-templates/{template_id}/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(template_id: Id, body: TemplateProjectCreate, user: CurrentUser, db: DbDep):
    return await svc.create_project_from_template(db, user, template_id, body)
