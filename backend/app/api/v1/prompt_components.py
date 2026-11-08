"""提示词组件库（本人资源）。固定路径（/export、/import）先于 /{component_id} 注册。"""

from typing import Any

from fastapi import APIRouter, Query, Response, status

from app.core.deps import CurrentUser, DbDep
from app.schemas.prompt_component import (
    ComponentBrief,
    ComponentCreate,
    ComponentImportRequest,
    ComponentImportResult,
    ComponentOut,
    ComponentUpdate,
)
from app.services import prompt_component_service as svc

router = APIRouter(prefix="/prompt-components", tags=["prompt-components"])


@router.get("", response_model=list[ComponentBrief])
async def list_components(
    user: CurrentUser, db: DbDep, q: str = Query(default="", max_length=100)
) -> list[ComponentBrief]:
    return await svc.list_components(db, user.id, q)


@router.post("", response_model=ComponentOut, status_code=status.HTTP_201_CREATED)
async def create_component(body: ComponentCreate, user: CurrentUser, db: DbDep) -> ComponentOut:
    return await svc.create_component(db, user.id, body)


@router.get("/export")
async def export_components(user: CurrentUser, db: DbDep) -> dict[str, Any]:
    return await svc.export_components(db, user.id)


@router.post("/import", response_model=ComponentImportResult)
async def import_components(body: ComponentImportRequest, user: CurrentUser, db: DbDep) -> ComponentImportResult:
    return await svc.import_components(db, user.id, body.data)


@router.get("/{component_id}", response_model=ComponentOut)
async def get_component(component_id: int, user: CurrentUser, db: DbDep) -> ComponentOut:
    return await svc.to_out(db, user.id, await svc.get_component(db, user.id, component_id))


@router.put("/{component_id}", response_model=ComponentOut)
async def update_component(component_id: int, body: ComponentUpdate, user: CurrentUser, db: DbDep) -> ComponentOut:
    return await svc.update_component(db, user.id, component_id, body)


@router.delete("/{component_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_component(component_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_component(db, user.id, component_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
