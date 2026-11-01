from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.deps import DbDep, TaskEngineDep, require_permission
from app.models import User
from app.schemas.admin_task import (
    AdminTaskCancelOut,
    AdminTaskCancelRequest,
    AdminTaskDetail,
    AdminTaskFilters,
    AdminTaskList,
)
from app.services import admin_task_service as svc

PermissionTasksRead = Annotated[User, Depends(require_permission("tasks.read"))]
PermissionTasksCancel = Annotated[User, Depends(require_permission("tasks.cancel"))]

router = APIRouter(prefix="/admin/tasks", tags=["admin"])


@router.get("", response_model=AdminTaskList)
async def list_tasks(admin: PermissionTasksRead, db: DbDep, engine: TaskEngineDep,
                     filters: Annotated[AdminTaskFilters, Query()]) -> AdminTaskList:
    return await svc.list_tasks(db, engine, filters)


@router.get("/{task_key}", response_model=AdminTaskDetail)
async def get_task(task_key: UUID, admin: PermissionTasksRead, db: DbDep, engine: TaskEngineDep) -> AdminTaskDetail:
    return await svc.get_task(db, engine, str(task_key))


@router.post("/{task_key}/cancel", response_model=AdminTaskCancelOut, status_code=202)
async def cancel_task(task_key: UUID, body: AdminTaskCancelRequest, admin: PermissionTasksCancel, db: DbDep,
                      engine: TaskEngineDep) -> AdminTaskCancelOut:
    return await svc.cancel_task(db, engine, admin, str(task_key), body)
