"""拆书导入：上传解析、未完成的解析列表、详情 / 单章全文、重切、校正、预估、开始导入、删除，以及跳过暂停任务的当前步骤。

只能访问自己的记录（他人与不存在一律 404）。章节序号与范围从 1 起、两端包含。
"""

from typing import Annotated

from fastapi import APIRouter, File, Response, UploadFile, status

from app.core.deps import CurrentUser, DbDep, TaskEngineDep
from app.core.errors import BadRequestError
from app.schemas.book_import import (
    BookChapterText,
    BookImportBrief,
    BookImportOut,
    BookOpRequest,
    EstimateOut,
    EstimateRequest,
    ResplitRequest,
    StartRequest,
    StartResult,
)
from app.schemas.task import TaskOut
from app.services import kb_parser
from app.services.book_import import service as svc

router = APIRouter(prefix="/book-imports", tags=["book-import"])


@router.post("", response_model=BookImportOut, status_code=status.HTTP_201_CREATED)
async def upload(
    user: CurrentUser,
    db: DbDep,
    file: Annotated[UploadFile, File(description="txt / md / docx / epub，≤ 20MB")],
) -> BookImportOut:
    # 多读 1 字节即可判断是否超限（超大请求体已由 UploadLimitMiddleware 在解析前拦截）
    data = await file.read(kb_parser.MAX_FILE_BYTES + 1)
    if len(data) > kb_parser.MAX_FILE_BYTES:
        raise BadRequestError(kb_parser.TOO_LARGE)
    return await svc.upload(db, user.id, file.filename or "", data)


@router.get("", response_model=list[BookImportBrief])
async def list_imports(user: CurrentUser, db: DbDep) -> list[BookImportBrief]:
    return await svc.list_imports(db, user.id)


@router.post("/tasks/{task_id}/skip", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def skip_task(task_id: int, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> TaskOut:
    return await svc.skip_task(db, engine, user.id, task_id)


@router.get("/{import_id}", response_model=BookImportOut)
async def get_import(import_id: int, user: CurrentUser, db: DbDep) -> BookImportOut:
    return await svc.get_import(db, user.id, import_id)


@router.get("/{import_id}/chapters/{index}", response_model=BookChapterText)
async def chapter_text(import_id: int, index: int, user: CurrentUser, db: DbDep) -> BookChapterText:
    return await svc.chapter_text(db, user.id, import_id, index)


@router.post("/{import_id}/resplit", response_model=BookImportOut)
async def resplit(import_id: int, body: ResplitRequest, user: CurrentUser, db: DbDep) -> BookImportOut:
    return await svc.resplit(db, user.id, import_id, body)


@router.post("/{import_id}/ops", response_model=BookImportOut)
async def apply_op(import_id: int, body: BookOpRequest, user: CurrentUser, db: DbDep) -> BookImportOut:
    return await svc.apply_op(db, user.id, import_id, body)


@router.post("/{import_id}/estimate", response_model=EstimateOut)
async def estimate(import_id: int, body: EstimateRequest, user: CurrentUser, db: DbDep) -> EstimateOut:
    return await svc.estimate(db, user.id, import_id, body)


@router.post("/{import_id}/start", response_model=StartResult, status_code=status.HTTP_202_ACCEPTED)
async def start(import_id: int, body: StartRequest, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> StartResult:
    return await svc.start(db, engine, user.id, import_id, body)


@router.delete("/{import_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_import(import_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_import(db, user.id, import_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
