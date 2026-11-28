"""角色造型的图片历史、默认参考和选定图。写操作与业务状态在同一作品锁下提交。"""

from collections import defaultdict

from sqlalchemy import func, select, update
from sqlalchemy.orm import aliased

from app.core.errors import ConflictError, NotFoundError
from app.models import Project
from app.models.media import MediaObject
from app.plugins.drama.asset_schemas import ImageOut
from app.plugins.drama.models import DramaAssetImage, DramaCharacter, DramaLook, DramaSeries
from app.plugins.drama.services.assets import authorized
from app.services import project_lifecycle

HISTORY_LIMIT = 10


def _look_images(look_id):
    return select(DramaAssetImage).where(DramaAssetImage.owner_type == "look", DramaAssetImage.owner_id == look_id)


def _look_project(look_id):
    return (
        select(DramaCharacter.series_id, DramaSeries.project_id, Project.user_id)
        .select_from(DramaLook)
        .join(DramaCharacter, DramaCharacter.id == DramaLook.character_id)
        .join(DramaSeries, DramaSeries.id == DramaCharacter.series_id)
        .join(Project, Project.id == DramaSeries.project_id)
        .where(DramaLook.id == look_id)
    )


async def _scope(db, look_id, *, lock):
    point = (await db.execute(_look_project(look_id))).one_or_none()
    if point is None:
        raise NotFoundError("角色造型不存在")
    if lock:
        await project_lifecycle.lock_active(db, point.project_id)
        if point != (await db.execute(_look_project(look_id))).one_or_none():
            raise NotFoundError("角色造型不存在")
    return point


async def _check_object(db, scope, media_id):
    valid = await db.scalar(
        select(MediaObject.id).where(
            MediaObject.id == media_id,
            MediaObject.owner_user_id == scope.user_id,
            MediaObject.project_id == scope.project_id,
            MediaObject.scope == "project",
            MediaObject.plugin_id == "drama",
            MediaObject.kind == "image",
            MediaObject.parent_id.is_(None),
            MediaObject.status == "ready",
            MediaObject.deleted_at.is_(None),
        )
    )
    if valid is None:
        raise NotFoundError("本作品没有这张短剧图片")


async def ensure_capacity(db, look_id):
    count = await db.scalar(
        select(func.count())
        .select_from(DramaAssetImage)
        .where(
            DramaAssetImage.owner_type == "look",
            DramaAssetImage.owner_id == look_id,
        )
    )
    if count >= HISTORY_LIMIT:
        raise ConflictError("每套造型最多保留 10 张图片，请先删除旧图", extra={"code": "image_history_full"})


async def reserve_generation(db, look, task_id=None, prompt="", reference_image_ids=()):
    scope = await _scope(db, look.id, lock=True)
    await ensure_capacity(db, look.id)
    row = DramaAssetImage(
        series_id=scope.series_id,
        owner_type="look",
        owner_id=look.id,
        source="generated",
        status="pending",
        task_id=task_id,
        prompt=prompt,
        reference_image_ids=list(reference_image_ids),
        error_message="",
        is_selected=False,
    )
    db.add(row)
    await db.flush()
    return row


async def add_generated(db, look, media_id, prompt, reference_image_ids, *, task_id=None):
    """任务已完成媒体发布；同一媒体重复收尾返回原行。调用方负责提交。"""
    scope = await _scope(db, look.id, lock=True)
    await _check_object(db, scope, media_id)
    existing = await db.scalar(select(DramaAssetImage).where(DramaAssetImage.media_object_id == media_id))
    if existing is not None:
        if (existing.series_id, existing.owner_type, existing.owner_id, existing.status, existing.task_id) != (
            scope.series_id,
            "look",
            look.id,
            "ready",
            task_id,
        ):
            raise ConflictError("图片已被其他历史记录使用")
        return existing
    base = _look_images(look.id).where(DramaAssetImage.series_id == scope.series_id)
    if task_id is None:
        row = await reserve_generation(db, look, prompt=prompt, reference_image_ids=reference_image_ids)
    else:
        row = await db.scalar(
            base.where(DramaAssetImage.task_id == task_id, DramaAssetImage.status == "pending").execution_options(
                populate_existing=True
            )
        )
        if row is None:
            raise ConflictError("图片生成记录已变化", extra={"code": "image_history_changed"})
    selected = await db.scalar(base.where(DramaAssetImage.is_selected))
    row.status = "ready"
    row.media_object_id = media_id
    row.prompt = prompt
    row.reference_image_ids = list(reference_image_ids)
    row.error_message = ""
    row.is_selected = selected is None
    await db.flush()
    return row


async def default_reference(db, look):
    if look.is_default:
        return None
    scope = await _scope(db, look.id, lock=False)
    image = await db.scalar(
        select(DramaAssetImage)
        .join(DramaLook, DramaLook.id == DramaAssetImage.owner_id)
        .where(
            DramaLook.character_id == look.character_id,
            DramaLook.is_default,
            DramaAssetImage.series_id == scope.series_id,
            DramaAssetImage.owner_type == "look",
            DramaAssetImage.is_selected,
            DramaAssetImage.status == "ready",
        )
    )
    if image is not None:
        await _check_object(db, scope, image.media_object_id)
    return image


async def history(db, actor, look_id):
    series, look = await authorized(db, actor, DramaLook, look_id, write=False)
    rows = await db.scalars(
        _look_images(look.id).where(DramaAssetImage.series_id == series.id).order_by(DramaAssetImage.id.desc())
    )
    return [ImageOut.model_validate(row) for row in rows]


async def _edit_row(db, actor, image_id):
    look_id = await db.scalar(
        select(DramaAssetImage.owner_id).where(
            DramaAssetImage.id == image_id,
            DramaAssetImage.owner_type == "look",
        )
    )
    if look_id is None:
        raise NotFoundError("造型图片不存在")
    series, look = await authorized(db, actor, DramaLook, look_id, write=True)
    row = await db.get(DramaAssetImage, image_id, populate_existing=True)
    if row is None or (row.series_id, row.owner_type, row.owner_id) != (series.id, "look", look.id):
        raise NotFoundError("造型图片不存在")
    return series, look, row


async def select_image(db, actor, image_id):
    series, look, row = await _edit_row(db, actor, image_id)
    if row.status != "ready":
        raise ConflictError("只能选定已保存的图片")
    await _check_object(db, await _scope(db, look.id, lock=False), row.media_object_id)
    if not row.is_selected:
        await db.execute(
            update(DramaAssetImage)
            .where(
                DramaAssetImage.series_id == series.id,
                DramaAssetImage.owner_type == "look",
                DramaAssetImage.owner_id == look.id,
                DramaAssetImage.is_selected,
            )
            .values(is_selected=False)
        )
        row.is_selected = True
        await db.flush()
        await db.refresh(row)
    result = ImageOut.model_validate(row)
    await db.commit()
    return result


async def delete_image(db, actor, image_id):
    from app.plugins.drama.services.image_lifecycle import discard_image_media

    series, look, row = await _edit_row(db, actor, image_id)
    if row.status == "pending":
        raise ConflictError("图片正在生成，请先取消任务")
    selected = row.is_selected
    await discard_image_media(db, series.project_id, [row.id])
    if selected:
        latest = await db.scalar(
            _look_images(look.id)
            .where(
                DramaAssetImage.series_id == series.id,
                DramaAssetImage.status == "ready",
            )
            .order_by(DramaAssetImage.id.desc())
            .limit(1)
        )
        if latest is not None:
            latest.is_selected = True
    await db.commit()


async def library_overviews(db, series_id, look_query):
    """同一查询聚合所有造型的状态，仅返回每套的选定图和最近一次生成记录。"""
    owner = DramaAssetImage.owner_id
    ranked = (
        select(
            DramaAssetImage,
            func.count().over(partition_by=owner).label("total"),
            func.count().filter(DramaAssetImage.status == "pending").over(partition_by=owner).label("pending"),
            func.row_number().over(partition_by=owner, order_by=DramaAssetImage.id.desc()).label("rank"),
        )
        .where(DramaAssetImage.series_id == series_id, DramaAssetImage.owner_type == "look", owner.in_(look_query))
        .subquery()
    )
    image = aliased(DramaAssetImage, ranked)
    rows = await db.execute(
        select(image, ranked.c.total, ranked.c.pending, ranked.c.rank).where(image.is_selected | (ranked.c.rank == 1))
    )
    result = defaultdict(
        lambda: {"selected_image": None, "image_count": 0, "pending_image_count": 0, "latest_image_error": None}
    )
    for row, total, pending, rank in rows:
        summary = result[row.owner_id]
        summary.update(image_count=total, pending_image_count=pending)
        if row.is_selected:
            summary["selected_image"] = ImageOut.model_validate(row)
        if rank == 1 and row.status == "failed":
            summary["latest_image_error"] = row.error_message
    return result
