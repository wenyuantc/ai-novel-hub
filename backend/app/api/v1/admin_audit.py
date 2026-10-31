from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.core.deps import AdminUser, DbDep, SettingsDep
from app.schemas.audit import (
    AuditCleanupOut,
    AuditCleanupPreview,
    AuditCleanupRequest,
    AuditDateRange,
    AuditLogDetail,
    AuditLogFilters,
    AuditLogList,
)
from app.services import audit_service as svc

router = APIRouter(prefix="/admin/audit-logs", tags=["admin"])


@router.get("", response_model=AuditLogList)
async def list_logs(admin: AdminUser, db: DbDep, filters: Annotated[AuditLogFilters, Query()]) -> AuditLogList:
    return await svc.list_logs(db, filters)


@router.post("/cleanup-preview", response_model=AuditCleanupPreview)
async def preview_cleanup(
    body: AuditDateRange,
    admin: AdminUser,
    db: DbDep,
    settings: SettingsDep,
) -> AuditCleanupPreview:
    return await svc.preview_cleanup(db, admin, body, settings.jwt_secret)


@router.post("/cleanup", response_model=AuditCleanupOut)
async def cleanup(
    body: AuditCleanupRequest,
    admin: AdminUser,
    db: DbDep,
    settings: SettingsDep,
) -> AuditCleanupOut:
    return await svc.cleanup(db, admin, body, settings.jwt_secret)


@router.get("/{log_id}", response_model=AuditLogDetail)
async def get_log(log_id: Annotated[int, Path(gt=0)], admin: AdminUser, db: DbDep) -> AuditLogDetail:
    return await svc.get_log(db, log_id)
