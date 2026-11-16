from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response, status

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.schemas.export import DocumentExportRequest
from app.schemas.project import ProjectCreate, ProjectOut, ProjectUpdate, WizardProgressUpdate
from app.services import export_service, project_access
from app.services import project_service as svc

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
async def list_projects(user: CurrentUser, db: DbDep) -> list[ProjectOut]:
    return await svc.list_projects(db, user.id)


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, user: CurrentUser, db: DbDep) -> ProjectOut:
    project, notice = await svc.create_project(db, user.id, body)
    return await svc.project_out(db, project, notice, actor_user_id=user.id)


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: int, user: CurrentUser, db: DbDep) -> ProjectOut:
    project = await project_access.require_project(db, user.id, project_id, "content.read")
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
    return await svc.project_out(db, project, actor_user_id=user.id)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> Response:
    await svc.delete_project(db, user.id, project_id, engine=engine)
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
    """EPUB 默认署作品作者；可通过 author 覆盖，空章节提示放在 X-Export-Warnings。"""
    ids = export_service.parse_chapter_ids(chapter_ids)
    file = await export_service.export_manual_project(
        db,
        user.id,
        project_id,
        format,
        ids,
        author=author,
        include_synopsis=include_synopsis,
    )
    headers = {"Content-Disposition": file.content_disposition}
    if file.warnings_header:
        headers["X-Export-Warnings"] = file.warnings_header
    return Response(content=file.body, media_type=file.media_type, headers=headers)


@router.post("/{project_id}/export-document")
async def export_document(project_id: int, body: DocumentExportRequest, user: CurrentUser, db: DbDep) -> Response:
    """将已保存正文、摘要及所选资料组合为可阅读文档。"""
    from app.services.document_export.service import export_document as build_document

    file = await build_document(db, user.id, project_id, body)
    headers = {"Content-Disposition": file.content_disposition, "Cache-Control": "no-store"}
    if file.warnings_header:
        headers["X-Export-Warnings"] = file.warnings_header
    return Response(content=file.body, media_type=file.media_type, headers=headers)
