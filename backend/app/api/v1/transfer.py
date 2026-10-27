"""项目备份（ainovel.project）：导出 / 大小估算 / 导入预览 / 导入。他人项目一律 404，导入的项目归属当前用户。"""

from typing import Annotated

import anyio
from fastapi import APIRouter, File, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.core.deps import CurrentUser, DbDep, SessionmakerDep, TaskEngineDep
from app.core.errors import BadRequestError
from app.schemas.transfer import BackupEstimate, BackupOptions, ImportPreview, ImportRequest, ImportResult
from app.services.project_transfer import exporter, importer

router = APIRouter(prefix="/projects", tags=["project-transfer"])


async def _read_backup(file: StarletteUploadFile) -> importer.Backup:
    # 多读 1 字节即可判断是否超限（请求体总量已由 UploadLimitMiddleware 在解析前拦截）
    data = await file.read(importer.MAX_BYTES + 1)
    if len(data) > importer.MAX_BYTES:
        raise BadRequestError(importer.TOO_LARGE)
    return await anyio.to_thread.run_sync(importer.parse_backup, data)


@router.post("/import/preview", response_model=ImportPreview)
async def preview_import(
    user: CurrentUser,
    db: DbDep,
    file: Annotated[UploadFile, File(description="ainovel.project 备份文件（JSON），≤ 100MB")],
) -> ImportPreview:
    backup = await _read_backup(file)
    return await importer.preview(db, user.id, backup)


@router.post("/import", response_model=ImportResult, status_code=status.HTTP_201_CREATED)
async def import_project(request: Request, user: CurrentUser, db: DbDep, engine: TaskEngineDep) -> ImportResult:
    """JSON {token, title?}（先预览）；或 multipart（file + 可选 title）直接上传导入。"""
    token: str | None = None
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        file = form.get("file")
        if not isinstance(file, StarletteUploadFile):
            raise BadRequestError("请选择备份文件")
        raw_title = form.get("title")
        title = raw_title.strip()[:200] if isinstance(raw_title, str) else None
        backup = await _read_backup(file)
    else:
        try:
            body = ImportRequest.model_validate(await request.json())
        except (ValueError, ValidationError) as exc:
            raise BadRequestError("请求参数格式不正确：需要 token") from exc
        token, title = body.token, body.title
        backup = importer.get_token(user.id, token)
    result = await importer.import_backup(db, engine, user.id, backup, title)
    if token is not None:
        importer.drop_token(token)
    return result


@router.get("/{project_id}/backup")
async def export_backup(
    project_id: int,
    user: CurrentUser,
    db: DbDep,
    sessionmaker: SessionmakerDep,
    versions: Annotated[bool, Query()] = True,
    snapshots: Annotated[bool, Query()] = False,
    logs: Annotated[bool, Query()] = False,
    inspiration: Annotated[bool, Query()] = False,
) -> StreamingResponse:
    options = BackupOptions(versions=versions, snapshots=snapshots, generation_logs=logs, inspiration=inspiration)
    plan = await exporter.prepare_export(db, user.id, project_id, options)
    # 流式输出期间不占用请求 session：生成器自己开 session
    await db.close()
    return StreamingResponse(
        exporter.stream_export(sessionmaker, plan),
        media_type="application/json",
        headers={"Content-Disposition": plan.content_disposition},
    )


@router.get("/{project_id}/backup/estimate", response_model=BackupEstimate)
async def estimate_backup(project_id: int, user: CurrentUser, db: DbDep) -> BackupEstimate:
    return await exporter.estimate(db, user.id, project_id)
