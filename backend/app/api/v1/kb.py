"""知识库：管理员可维护已有内容；上传、索引任务及模型检索保持严格项目归属。"""

from typing import Annotated

from fastapi import APIRouter, File, Query, Response, UploadFile, status

from app.core.deps import CurrentUser, DbDep, EmbedEnvDep, TaskEngineDep
from app.core.errors import BadRequestError
from app.schemas.kb import (
    IndexStatus,
    KbChunkPage,
    KbDocumentOut,
    KbDocumentUpdate,
    KbSearchRequest,
    KbSearchResult,
    KbSettingsOut,
    KbSettingsUpdate,
    KbUploadResult,
)
from app.schemas.task import TaskOut
from app.services import kb_parser
from app.services import kb_service as svc

router = APIRouter(prefix="/projects/{project_id}/kb", tags=["knowledge-base"])


@router.get("/settings", response_model=KbSettingsOut)
async def get_settings(project_id: int, user: CurrentUser, db: DbDep) -> KbSettingsOut:
    return await svc.get_settings(db, user.id, project_id)


@router.patch("/settings", response_model=KbSettingsOut)
async def update_settings(project_id: int, body: KbSettingsUpdate, user: CurrentUser, db: DbDep) -> KbSettingsOut:
    return await svc.update_settings(db, user.id, project_id, body)


@router.get("/documents", response_model=list[KbDocumentOut])
async def list_documents(project_id: int, user: CurrentUser, db: DbDep) -> list[KbDocumentOut]:
    return await svc.list_documents(db, user.id, project_id)


@router.post("/documents", response_model=KbUploadResult, status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    project_id: int,
    user: CurrentUser,
    db: DbDep,
    engine: TaskEngineDep,
    file: Annotated[UploadFile, File(description="txt / md / json / docx / epub / pdf，≤ 20MB")],
) -> KbUploadResult:
    # 多读 1 字节即可判断是否超限，不把超大文件整个读进内存
    data = await file.read(kb_parser.MAX_FILE_BYTES + 1)
    if len(data) > kb_parser.MAX_FILE_BYTES:
        raise BadRequestError(kb_parser.TOO_LARGE)
    return await svc.upload_document(db, engine, user.id, project_id, file.filename or "", data)


@router.patch("/documents/{document_id}", response_model=KbDocumentOut)
async def update_document(
    project_id: int, document_id: int, body: KbDocumentUpdate, user: CurrentUser, db: DbDep
) -> KbDocumentOut:
    return await svc.set_inject(db, user.id, project_id, document_id, body.inject)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(project_id: int, document_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_document(db, user.id, project_id, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/documents/{document_id}/reindex", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def reindex_document(
    project_id: int, document_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep
) -> TaskOut:
    return await svc.reindex_document(db, engine, user.id, project_id, document_id)


@router.get("/documents/{document_id}/chunks", response_model=KbChunkPage)
async def list_chunks(
    project_id: int,
    document_id: int,
    user: CurrentUser,
    db: DbDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> KbChunkPage:
    return await svc.list_chunks(db, user.id, project_id, document_id, page=page, page_size=page_size)


@router.post("/search", response_model=KbSearchResult)
async def search(
    project_id: int, body: KbSearchRequest, user: CurrentUser, db: DbDep, env: EmbedEnvDep
) -> KbSearchResult:
    return await svc.search_debug(db, user.id, project_id, body, env=env)


@router.get("/index-status", response_model=IndexStatus)
async def index_status(project_id: int, user: CurrentUser, db: DbDep) -> IndexStatus:
    return await svc.index_status(db, user.id, project_id)


@router.post("/rebuild", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def rebuild(project_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await svc.start_rebuild(db, engine, user.id, project_id)
