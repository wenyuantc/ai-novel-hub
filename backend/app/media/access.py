"""读取媒体前统一检查对象、派生关系和作品访问能力。"""

from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.media import MediaObject
from app.plugins import state as plugin_state
from app.plugins.registry import current
from app.services.project_access import require_project

Variant = Literal["thumb", "poster"]


def missing() -> NotFoundError:
    return NotFoundError("媒体文件不存在")


async def ready_object(db: AsyncSession, object_id: str) -> MediaObject:
    obj = await db.get(MediaObject, object_id, populate_existing=True)
    if (obj is None or obj.status != "ready" or obj.owner_user_id is None
            or (obj.scope == "project" and obj.project_id is None)):
        raise missing()
    if obj.plugin_id and (
        current.get().get(obj.plugin_id) is None or not await plugin_state.is_enabled(db, obj.plugin_id)
    ):
        raise missing()
    return obj


async def resolve(
    db: AsyncSession, object_id: str, *, actor_id: int | None, variant: Variant | None = None,
) -> MediaObject:
    """actor_id 为空仅用于已经通过外链签名校验的请求。"""
    obj = await ready_object(db, object_id)
    # 直接访问缩略图 UUID 时也受原文件删除与可见性约束。
    root = await ready_object(db, obj.parent_id) if obj.parent_id else obj
    if actor_id is not None:
        if root.scope == "project":
            try:
                await require_project(db, actor_id, root.project_id, "content.read")
            except NotFoundError:
                raise missing() from None
        elif root.scope != "user" or actor_id != root.owner_user_id:
            raise missing()
    if variant is not None:
        child_id = await db.scalar(select(MediaObject.id).where(
            MediaObject.parent_id == obj.id, MediaObject.variant == variant,
        ))
        if not child_id:
            raise missing()
        obj = await ready_object(db, child_id)
    return obj
