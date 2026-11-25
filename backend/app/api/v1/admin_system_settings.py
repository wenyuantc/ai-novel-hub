from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.core.deps import DbDep, SystemSettingsDep, TaskEngineDep, require_permission
from app.models import User
from app.schemas.system_settings import SystemSettingsOut, SystemSettingsUpdate, TaskDrainStatus
from app.services import task_drain_status
from app.services.authorization_service import AuthenticatedActor

PermissionSettingsRead = Annotated[User, Depends(require_permission("settings.read"))]
PermissionSettingsUpdate = Annotated[User, Depends(require_permission("settings.update"))]

router = APIRouter(prefix="/admin/system-settings", tags=["admin"])


@router.get("", response_model=SystemSettingsOut)
async def read_settings(
    user: PermissionSettingsRead, manager: SystemSettingsDep, response: Response
) -> SystemSettingsOut:
    response.headers["Cache-Control"] = "no-store"
    return await manager.load()


@router.get("/drain-status", response_model=TaskDrainStatus)
async def read_drain_status(
    user: PermissionSettingsRead, db: DbDep, engine: TaskEngineDep, response: Response,
) -> TaskDrainStatus:
    response.headers["Cache-Control"] = "no-store"
    return await task_drain_status.read_status(db, lease_seconds=engine.lease_seconds)


@router.patch("", response_model=SystemSettingsOut)
async def update_settings(
    body: SystemSettingsUpdate, user: PermissionSettingsUpdate, manager: SystemSettingsDep, response: Response,
) -> SystemSettingsOut:
    response.headers["Cache-Control"] = "no-store"
    return await manager.update(AuthenticatedActor(user.id, user.token_version), body)
