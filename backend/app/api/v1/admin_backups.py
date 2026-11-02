"""分项授权整库备份API。恢复仅提供指引，不提供覆盖数据库入口。"""

from typing import TYPE_CHECKING, Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.core.deps import DbDep, require_permission
from app.models import User
from app.schemas.backup import (
    BackupDestination,
    BackupManualRequest,
    BackupOverview,
    BackupRunFilters,
    BackupRunList,
    BackupRunOut,
    BackupSettingsOut,
    BackupSettingsUpdate,
)
from app.services import authorization_service as authz
from app.services.backups.service import BackupManager

if TYPE_CHECKING:
    from app.services.backups.runtime import DownloadResource

Read = Annotated[User, Depends(require_permission("backups.read"))]
Configure = Annotated[User, Depends(require_permission("backups.configure"))]
Run = Annotated[User, Depends(require_permission("backups.run"))]
Download = Annotated[User, Depends(require_permission("backups.download"))]
router = APIRouter(prefix="/admin/backups", tags=["admin"])


def get_manager(request: Request) -> BackupManager:
    return request.app.state.backups


Manager = Annotated[BackupManager, Depends(get_manager)]


class _BackupStreamingResponse(StreamingResponse):
    """资源随整个响应关闭，包含首个正文块迭代前的断开窗口。"""

    def __init__(self, resource: "DownloadResource", headers: dict[str, str]):
        super().__init__(resource.iter_chunks(), headers=headers, media_type="application/octet-stream")
        self._resource = resource

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # generator尚未开始时其finally不会运行；BackgroundTask也不保证在异常后执行。
            await self._resource.close()


async def detached_actor(db, user) -> authz.AuthenticatedActor:
    # 原始JWT版本由依赖存进db.info，不能用后来刷新的User版本替换。
    actor = authz.actor_snapshot(db, user)
    await db.close()
    return actor


@router.get("", response_model=BackupOverview)
async def overview(user: Read, db: DbDep, manager: Manager, response: Response) -> BackupOverview:
    response.headers["Cache-Control"] = "no-store"
    return await manager.overview(await detached_actor(db, user))


@router.get("/settings", response_model=BackupSettingsOut)
async def settings(user: Configure, db: DbDep, manager: Manager, response: Response) -> BackupSettingsOut:
    response.headers["Cache-Control"] = "no-store"
    return await manager.read_settings(await detached_actor(db, user))


@router.patch("/settings", response_model=BackupSettingsOut)
async def update_settings(
    body: BackupSettingsUpdate,
    user: Configure,
    db: DbDep,
    manager: Manager,
    response: Response,
) -> BackupSettingsOut:
    response.headers["Cache-Control"] = "no-store"
    return await manager.update(await detached_actor(db, user), body)


@router.get("/runs", response_model=BackupRunList)
async def runs(
    user: Read,
    db: DbDep,
    manager: Manager,
    response: Response,
    filters: Annotated[BackupRunFilters, Query()],
) -> BackupRunList:
    response.headers["Cache-Control"] = "no-store"
    return await manager.list_runs(await detached_actor(db, user), filters)


@router.get("/runs/{run_id}", response_model=BackupRunOut)
async def run_detail(run_id: int, user: Read, db: DbDep, manager: Manager, response: Response) -> BackupRunOut:
    response.headers["Cache-Control"] = "no-store"
    return await manager.get_run(await detached_actor(db, user), run_id)


@router.post("/runs", response_model=BackupRunOut, status_code=202)
async def run_manual(
    body: BackupManualRequest,
    user: Run,
    db: DbDep,
    manager: Manager,
    response: Response,
) -> BackupRunOut:
    response.headers["Cache-Control"] = "no-store"
    return await manager.accept_manual(await detached_actor(db, user), body)


@router.get("/runs/{run_id}/download")
@router.head("/runs/{run_id}/download")
async def download(
    run_id: int,
    destination: BackupDestination,
    request: Request,
    user: Download,
    db: DbDep,
    manager: Manager,
):
    actor = await detached_actor(db, user)
    await manager.authorize_download(actor, run_id, destination)
    resource = await request.app.state.backup_runtime.prepare_download(run_id, destination)
    try:
        # 打开文件/网络流可能耗时；正文发送前复核原会话，并原子记录GET授权。
        await manager.authorize_download(actor, run_id, destination, audit=request.method == "GET")
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(resource.size_bytes),
            "Content-Disposition": "attachment; filename=backup.bin; filename*=UTF-8''" + quote(resource.filename),
        }
        if request.method == "HEAD":
            await resource.close()
            return Response(headers=headers, media_type="application/octet-stream")

        return _BackupStreamingResponse(resource, headers)
    except BaseException:
        await resource.close()
        raise
