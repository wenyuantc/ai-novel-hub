"""管理员首页：全站统计只读端点，每个入口独立校验管理员。"""

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BeforeValidator

from app.core.deps import AdminUser, DbDep
from app.schemas.admin_dashboard import (
    AdminDashboardHistory,
    AdminDashboardOverview,
    AdminDashboardTasks,
    DashboardDays,
)
from app.services import admin_dashboard_service as svc

router = APIRouter(prefix="/admin/dashboard", tags=["admin"])


@router.get("/overview", response_model=AdminDashboardOverview)
async def overview(admin: AdminUser, db: DbDep) -> AdminDashboardOverview:
    return await svc.overview(db)


@router.get("/tasks", response_model=AdminDashboardTasks)
async def tasks(admin: AdminUser, db: DbDep) -> AdminDashboardTasks:
    return await svc.tasks(db)


@router.get("/history", response_model=AdminDashboardHistory)
async def history(
    admin: AdminUser, db: DbDep, days: Annotated[DashboardDays, BeforeValidator(int), Query()] = 30
) -> AdminDashboardHistory:
    return await svc.history(db, days)
