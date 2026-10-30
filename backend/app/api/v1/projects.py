from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response, status

from app.core.deps import CurrentUser, DbDep
from app.schemas.project import ProjectCreate, ProjectOut, ProjectUpdate, WizardProgressUpdate
from app.services import export_service
from app.services import project_service as svc

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
async def list_projects(user: CurrentUser, db: DbDep) -> list[ProjectOut]:
    return await svc.list_projects(db, user.id)


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, user: CurrentUser, db: DbDep) -> ProjectOut:
    project, notice = await svc.create_project(db, user.id, body)
    return await svc.project_out(db, project, notice)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: int, user: CurrentUser, db: DbDep) -> ProjectOut:
    project = await svc.get_manageable_project(db, user.id, project_id)
    return await svc.project_out(db, project, actor_user_id=user.id)


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(project_id: int, body: ProjectUpdate, user: CurrentUser, db: DbDep) -> ProjectOut:
    project, notice = await svc.update_project(db, user.id, project_id, body)
    return await svc.project_out(db, project, notice, actor_user_id=user.id)


@router.patch("/{project_id}/wizard-progress", response_model=ProjectOut)
async def update_wizard_progress(
    project_id: int, body: WizardProgressUpdate, user: CurrentUser, db: DbDep
) -> ProjectOut:
    project = await svc.update_wizard_progress(db, user.id, project_id, body)
    return await svc.project_out(db, project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_project(db, user.id, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{project_id}/export")
async def export_project(
    project_id: int,
    user: CurrentUser,
    db: DbDep,
    format: Annotated[Literal["txt", "md", "epub"], Query()] = "txt",  # noqa: A002
    chapter_ids: Annotated[list[str] | None, Query()] = None,
    author: Annotated[str | None, Query(max_length=export_service.AUTHOR_LIMIT)] = None,
    include_synopsis: Annotated[bool, Query()] = True,
) -> Response:
    """EPUB：author 默认取用户名；include_synopsis 控制是否生成简介页。跳过的空章节经 X-Export-Warnings 返回。"""
    ids = export_service.parse_chapter_ids(chapter_ids)
    file = await export_service.export_project(
        db,
        user.id,
        project_id,
        format,
        ids,
        author=user.username if author is None else author,
        include_synopsis=include_synopsis,
    )
    headers = {"Content-Disposition": file.content_disposition}
    if file.warnings_header:
        headers["X-Export-Warnings"] = file.warnings_header
    return Response(content=file.body, media_type=file.media_type, headers=headers)
