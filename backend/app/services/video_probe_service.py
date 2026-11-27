"""模型配置页的视频探针与临时参考图。"""

from sqlalchemy import select

from app.ai.images import MAX_COVER_BYTES
from app.core.errors import ConflictError
from app.media import service as media
from app.models import Task
from app.models.ai_external_jobs import AiExternalJob
from app.models.media import MediaObject
from app.services import ai_config_service, task_service
from app.services.task_engine.handlers.video_probe import KIND


async def require_cancellable(db, task: Task) -> None:
    """两个受理入口已持有发起人的写屏障，与视频成片及扣费事务串行。"""
    if task.kind == KIND and task.status in ("running", "queued"):
        job_ids = task.input.get("external_job_ids", [])
        completed = await db.scalar(select(AiExternalJob.id).where(
            AiExternalJob.id.in_(job_ids), AiExternalJob.actor_id == task.user_id,
            AiExternalJob.state == "succeeded", AiExternalJob.usage_call_id.is_not(None),
        ).limit(1))
        if completed is not None:
            raise ConflictError("视频已完成，正在更新任务结果")


async def latest(db, engine, actor_id: int, config_id: int):
    cfg = await ai_config_service.resolve_config(db, actor_id, config_id, "video")
    row = await db.scalar(select(Task).where(
        Task.user_id == actor_id, Task.kind == KIND, Task.resource_key == f"ai.video_probe:{cfg.instance_key}",
    ).order_by(Task.id.desc()).limit(1))
    if row is None:
        return None
    out = await task_service.task_out(db, engine, row)
    if out.video_object_id is not None:
        readable = await db.scalar(select(MediaObject.id).where(
            MediaObject.id == out.video_object_id, MediaObject.status == "ready",
            MediaObject.owner_user_id == actor_id, MediaObject.deleted_at.is_(None),
        ))
        out = out.model_copy(update={"video_object_id": readable})
    return out


async def store_reference(db, sessionmaker, actor_id, config_id, file):
    await ai_config_service.resolve_config(db, actor_id, config_id, "video")
    await db.close()

    async def chunks():
        while chunk := await file.read(128 * 1024):
            yield chunk

    async with sessionmaker() as storage:
        obj = await media.store(storage, owner_id=actor_id, scope="user", kind="image", mime=file.content_type,
                                source=chunks(), max_bytes=MAX_COVER_BYTES, origin="video_probe_reference")
        return obj.id
