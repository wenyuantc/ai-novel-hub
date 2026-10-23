"""世界观 / 角色 / 大纲 的 CRUD 路由（结构相同，用工厂生成）。"""

from typing import Any

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app.core.deps import CurrentUser, DbDep
from app.schemas.story import (
    CharacterCreate,
    CharacterOut,
    CharacterUpdate,
    OutlineCreate,
    OutlineOut,
    OutlineUpdate,
    WorldCategory,
    WorldSettingCreate,
    WorldSettingOut,
    WorldSettingUpdate,
)
from app.services import story_service as svc


def _make_router(
    segment: str,
    kind: svc.StoryKind,
    create_schema: type[BaseModel],
    update_schema: type[BaseModel],
    out_schema: type[BaseModel],
    tag: str,
) -> APIRouter:
    router = APIRouter(prefix=f"/projects/{{project_id}}/{segment}", tags=[tag])

    if kind is svc.WORLD:

        @router.get("", response_model=list[out_schema])  # type: ignore[valid-type]
        async def list_world(
            project_id: int, user: CurrentUser, db: DbDep, category: WorldCategory | None = None
        ) -> Any:
            return await svc.list_owned(db, kind, user.id, project_id, category=category)

    else:

        @router.get("", response_model=list[out_schema])  # type: ignore[valid-type]
        async def list_items(project_id: int, user: CurrentUser, db: DbDep) -> Any:
            return await svc.list_owned(db, kind, user.id, project_id)

    @router.post("", response_model=out_schema, status_code=status.HTTP_201_CREATED)
    async def create_item(project_id: int, body: create_schema, user: CurrentUser, db: DbDep) -> Any:  # type: ignore[valid-type]
        return await svc.create_owned(db, kind, user.id, project_id, body)

    @router.get("/{item_id}", response_model=out_schema)
    async def get_item(project_id: int, item_id: int, user: CurrentUser, db: DbDep) -> Any:
        return await svc.get_owned(db, kind, user.id, project_id, item_id)

    @router.patch("/{item_id}", response_model=out_schema)
    async def update_item(
        project_id: int,
        item_id: int,
        body: update_schema,  # type: ignore[valid-type]
        user: CurrentUser,
        db: DbDep,
    ) -> Any:
        return await svc.update_owned(db, kind, user.id, project_id, item_id, body)

    @router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_item(project_id: int, item_id: int, user: CurrentUser, db: DbDep) -> Response:
        await svc.delete_owned(db, kind, user.id, project_id, item_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


world_router = _make_router("world", svc.WORLD, WorldSettingCreate, WorldSettingUpdate, WorldSettingOut, "world")
characters_router = _make_router(
    "characters", svc.CHARACTERS, CharacterCreate, CharacterUpdate, CharacterOut, "characters"
)
outlines_router = _make_router("outlines", svc.OUTLINES, OutlineCreate, OutlineUpdate, OutlineOut, "outlines")
