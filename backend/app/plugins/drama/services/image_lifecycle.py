"""在持有作品写锁的业务事务里移除资产图，媒体维护进程负责实际删文件。"""

from collections.abc import Iterable
from itertools import batched

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.media.cleanup import mark_deleted
from app.models import Project
from app.models.media import MediaObject
from app.plugins.drama.models import DramaAssetImage, DramaCharacter, DramaLook, DramaProp, DramaScene, DramaSeries


async def discard_image_media(db: AsyncSession, project_id: int, image_ids: Iterable[int]) -> None:
    for ids in batched(image_ids, 128):
        images = (
            select(DramaAssetImage.id)
            .join(DramaSeries)
            .where(
                DramaAssetImage.id.in_(ids),
                DramaSeries.project_id == project_id,
            )
        )
        roots = list(
            await db.scalars(
                select(DramaAssetImage.media_object_id).where(
                    DramaAssetImage.id.in_(images),
                    DramaAssetImage.media_object_id.is_not(None),
                )
            )
        )
        if roots:
            # 校验原件及其实际派生对象的归属，防止损坏的引用误删另一作品的文件。
            author = select(Project.user_id).where(Project.id == project_id).scalar_subquery()
            foreign = or_(
                MediaObject.owner_user_id.is_distinct_from(author),
                MediaObject.project_id.is_distinct_from(project_id),
                MediaObject.plugin_id.is_distinct_from("drama"),
                MediaObject.scope != "project",
                MediaObject.kind != "image",
            )
            invalid = select(MediaObject.id).where(
                or_(
                    MediaObject.id.in_(roots) & (foreign | MediaObject.parent_id.is_not(None)),
                    MediaObject.parent_id.in_(roots) & foreign,
                )
            )
            if await db.scalar(invalid.limit(1)) is not None:
                raise ConflictError("资产图的媒体归属异常，无法删除", extra={"code": "drama_image_reference_invalid"})
            await mark_deleted(db, roots)
        await db.execute(delete(DramaAssetImage).where(DramaAssetImage.id.in_(images)))


async def discard_owner_images(db: AsyncSession, series: DramaSeries, owner) -> None:
    images = select(DramaAssetImage.id).where(DramaAssetImage.series_id == series.id)
    if isinstance(owner, DramaCharacter):
        looks = select(DramaLook.id).where(DramaLook.character_id == owner.id)
        images = images.where(DramaAssetImage.owner_type == "look", DramaAssetImage.owner_id.in_(looks))
    elif not isinstance(owner, DramaSeries):
        kind = {DramaLook: "look", DramaScene: "scene", DramaProp: "prop"}[type(owner)]
        images = images.where(DramaAssetImage.owner_type == kind, DramaAssetImage.owner_id == owner.id)
    await discard_image_media(db, series.project_id, await db.scalars(images))
