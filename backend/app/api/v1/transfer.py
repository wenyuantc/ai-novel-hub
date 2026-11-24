"""项目备份（ainovel.project）：导出 / 大小估算 / 导入预览 / 导入。他人项目一律 404，导入的项目归属当前用户。"""

from tempfile import SpooledTemporaryFile
from typing import Annotated

import anyio
from fastapi import APIRouter, File, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.core.deps import CurrentUser, DbDep, SessionmakerDep, SettingsDep, TaskEngineDep
from app.core.errors import BadRequestError
from app.schemas.transfer import BackupEstimate, BackupOptions, ImportPreview, ImportRequest, ImportResult
from app.services.project_transfer import exporter, importer, preview_store

router = APIRouter(prefix="/projects", tags=["project-transfer"])


class _ClosingStreamingResponse(StreamingResponse):
    """响应成功、断连或取消均立即释放私有暂存文件。"""

    def __init__(self, *args, backing, **kwargs):
        super().__init__(*args, **kwargs)
        self._backing = backing

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._backing.close()


async def _validated_backup_stream(backing, length, sessionmaker, plan):
    """先重验每个发送段的授权；最后两字节仅在完整快照复验后送出。"""
    remaining = length
    try:
        while remaining > 2:
            async with sessionmaker() as db:
                await exporter.validate_export(db, plan, complete=False)
            part = await anyio.to_thread.run_sync(backing.read, min(1024 * 1024, remaining - 2))
            if not part:
                raise RuntimeError("备份暂存文件意外结束")
            remaining -= len(part)
            yield part
        async with sessionmaker() as db:
            await exporter.validate_export(db, plan)
        yield await anyio.to_thread.run_sync(backing.read, remaining)
    finally:
        backing.close()


async def _read_backup(file: StarletteUploadFile) -> bytes:
    # 多读 1 字节即可判断是否超限（请求体总量已由 UploadLimitMiddleware 在解析前拦截）
    data = await file.read(importer.MAX_BYTES + 1)
    if len(data) > importer.MAX_BYTES:
        raise BadRequestError(importer.TOO_LARGE)
    return data


@router.post("/import/preview", response_model=ImportPreview)
async def preview_import(
    user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="ainovel.project 备份文件（JSON），≤ 100MB")],
) -> ImportPreview:
    raw = await _read_backup(file)
    backup = await anyio.to_thread.run_sync(importer.parse_backup, raw)
    return await importer.preview(db, user.id, backup, raw, settings.encryption_key)


@router.post("/import", response_model=ImportResult, status_code=status.HTTP_201_CREATED)
async def import_project(
    request: Request, user: CurrentUser, db: DbDep, engine: TaskEngineDep, settings: SettingsDep,
) -> ImportResult:
    """JSON {token, title?}（先预览）；或 multipart（file + 可选 title）直接上传导入。"""
    user_id = user.id
    preview = None
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        file = form.get("file")
        if not isinstance(file, StarletteUploadFile):
            raise BadRequestError("请选择备份文件")
        raw_title = form.get("title")
        title = raw_title.strip()[:200] if isinstance(raw_title, str) else None
        raw = await _read_backup(file)
    else:
        try:
            body = ImportRequest.model_validate(await request.json())
        except (ValueError, ValidationError) as exc:
            raise BadRequestError("请求参数格式不正确：需要 token") from exc
        title = body.title
        preview, raw = await preview_store.load(db, user_id, body.token, settings.encryption_key)
        # 不持读取事务解析近上限的备份。提交前重新验证并消费同一条暂存记录。
        await db.rollback()
    backup = await anyio.to_thread.run_sync(importer.parse_backup, raw)
    del raw
    return await importer.import_backup(db, engine, user_id, backup, title, preview=preview)


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
    # 完整生成并核对体积、版本及原身份后才发送状态行。小于 1 MiB 留在内存，大文件落入私有临时文件。
    await db.close()
    backing = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    try:
        async for chunk in exporter.stream_export(sessionmaker, plan):
            # 避免单条超大文本在 SpooledTemporaryFile 溢写前再次完整复制进内存缓冲。
            for offset in range(0, len(chunk), 1024 * 1024):
                await anyio.to_thread.run_sync(backing.write, memoryview(chunk)[offset:offset + 1024 * 1024])
        length = backing.tell()
        await anyio.to_thread.run_sync(backing.seek, 0)
        async with sessionmaker() as check_db:
            await exporter.validate_export(check_db, plan)
    except BaseException:
        backing.close()
        raise
    return _ClosingStreamingResponse(
        _validated_backup_stream(backing, length, sessionmaker, plan), backing=backing,
        media_type="application/json",
        headers={"Content-Disposition": plan.content_disposition},
    )


@router.get("/{project_id}/backup/estimate", response_model=BackupEstimate)
async def estimate_backup(project_id: int, user: CurrentUser, db: DbDep) -> BackupEstimate:
    return await exporter.estimate(db, user.id, project_id)
