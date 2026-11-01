from fastapi import APIRouter, Response

from app.core.deps import AdminUser, SystemSettingsDep
from app.schemas.system_settings import SystemSettingsOut, SystemSettingsUpdate

router = APIRouter(prefix="/admin/system-settings", tags=["admin"])


@router.get("", response_model=SystemSettingsOut)
async def read_settings(user: AdminUser, manager: SystemSettingsDep, response: Response) -> SystemSettingsOut:
    response.headers["Cache-Control"] = "no-store"
    return await manager.load()


@router.patch("", response_model=SystemSettingsOut)
async def update_settings(
    body: SystemSettingsUpdate, user: AdminUser, manager: SystemSettingsDep, response: Response,
) -> SystemSettingsOut:
    response.headers["Cache-Control"] = "no-store"
    return await manager.update(user.id, body)
