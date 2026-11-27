from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.deps import DbDep, TaskEngineDep, require_permission
from app.models import User
from app.plugins import state
from app.schemas.feature_plugins import PluginListOut, PluginStateOut, PluginUpdate
from app.services.authorization_service import AuthenticatedActor

router = APIRouter(prefix="/admin/plugins", tags=["admin"])


@router.get("")
async def list_plugins(user: Annotated[User, Depends(require_permission("plugins.read"))], db: DbDep) -> PluginListOut:
    return PluginListOut(items=await state.list_states(db))


@router.patch("/{plugin_id}")
async def update_plugin(plugin_id: str, body: PluginUpdate, db: DbDep, engine: TaskEngineDep,
                        user: Annotated[User, Depends(require_permission("plugins.update"))]) -> PluginStateOut:
    return await state.set_enabled(db, engine, AuthenticatedActor(user.id, user.token_version), plugin_id, body)
