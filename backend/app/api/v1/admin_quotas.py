from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.core.deps import DbDep, SettingsDep, TaskEngineDep, require_permission
from app.models import User
from app.schemas.rbac import UserQuotaOut, UserQuotaUpdate
from app.services import quota_service as svc

router = APIRouter(prefix="/admin/users", tags=["admin"])
QuotaReader = Annotated[User, Depends(require_permission("quotas.read"))]
QuotaWriter = Annotated[User, Depends(require_permission("quotas.update"))]


@router.get("/{user_id}/quotas", response_model=UserQuotaOut)
async def read(
    user_id: int, actor: QuotaReader, db: DbDep, settings: SettingsDep, engine: TaskEngineDep, response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    return await svc.get_quotas(db, user_id, settings, engine)


@router.patch("/{user_id}/quotas", response_model=UserQuotaOut)
async def update(
    user_id: int, body: UserQuotaUpdate, actor: QuotaWriter, db: DbDep,
    settings: SettingsDep, engine: TaskEngineDep, response: Response,
):
    response.headers["Cache-Control"] = "no-store"
    return await svc.update_quotas(db, actor, user_id, body, settings, engine)
