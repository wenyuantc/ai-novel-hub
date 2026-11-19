from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.core.deps import DbDep, SettingsDep, require_permission
from app.models import User
from app.schemas.audit import (
    AuditCleanupOut,
    AuditCleanupPreview,
    AuditCleanupRequest,
    AuditDateRange,
    AuditLogDetail,
    AuditLogFilters,
    AuditLogList,
)
from app.schemas.collaboration_audit import AdminCollaborationAuditFilters, CollaborationAuditList
from app.services import audit_service as svc
from app.services import collaboration_audit_query as collab_audit_svc

PermissionAuditRead = Annotated[User, Depends(require_permission("audit.read"))]
PermissionAuditCleanup = Annotated[User, Depends(require_permission("audit.cleanup"))]

router = APIRouter(prefix="/admin/audit-logs", tags=["admin"])


@router.get("", response_model=AuditLogList)
async def list_logs(
    admin: PermissionAuditRead, db: DbDep, filters: Annotated[AuditLogFilters, Query()]
) -> AuditLogList:
    return await svc.list_logs(db, filters)


@router.get("/collaboration", response_model=CollaborationAuditList)
async def list_collaboration_logs(
    admin: PermissionAuditRead, db: DbDep, filters: Annotated[AdminCollaborationAuditFilters, Query()]
) -> CollaborationAuditList:
    return await collab_audit_svc.list_for_admin(db, filters)


@router.post("/cleanup-preview", response_model=AuditCleanupPreview)
async def preview_cleanup(
    body: AuditDateRange,
    admin: PermissionAuditCleanup,
    db: DbDep,
    settings: SettingsDep,
) -> AuditCleanupPreview:
    return await svc.preview_cleanup(db, admin, body, settings.jwt_secret)


@router.post("/cleanup", response_model=AuditCleanupOut)
async def cleanup(
    body: AuditCleanupRequest,
    admin: PermissionAuditCleanup,
    db: DbDep,
    settings: SettingsDep,
) -> AuditCleanupOut:
    return await svc.cleanup(db, admin, body, settings.jwt_secret)


@router.get("/{log_id}", response_model=AuditLogDetail)
async def get_log(log_id: Annotated[int, Path(gt=0)], admin: PermissionAuditRead, db: DbDep) -> AuditLogDetail:
    return await svc.get_log(db, log_id)
