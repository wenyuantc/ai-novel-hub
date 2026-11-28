"""角色造型的图片历史与选定图。文件和缩略图使用作品媒体读取接口。"""

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict

from app.core.deps import CurrentUser, DbDep, EditorUser
from app.plugins.drama.api.episodes import ResourceId
from app.plugins.drama.asset_schemas import ImageOut
from app.plugins.drama.services import images

router = APIRouter(prefix="/plugins/drama", tags=["短剧造型图"])


class SelectImage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    is_selected: Literal[True]


@router.get("/assets/look/{look_id}/images", response_model=list[ImageOut])
async def look_images(look_id: ResourceId, user: CurrentUser, db: DbDep):
    return await images.history(db, user.id, look_id)


@router.patch("/asset-images/{image_id}", response_model=ImageOut)
async def select_image(image_id: ResourceId, body: SelectImage, user: EditorUser, db: DbDep):
    return await images.select_image(db, user.id, image_id)


@router.delete("/asset-images/{image_id}", status_code=204)
async def delete_image(image_id: ResourceId, user: EditorUser, db: DbDep):
    await images.delete_image(db, user.id, image_id)
    return Response(status_code=204)
