"""AI 调用记录：写入（每个项目保留最近 MAX_LOGS_PER_PROJECT 条）、列表、详情。"""

import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import NotFoundError
from app.models import AIConfig, Chapter, GenerationLog, Task
from app.services import project_lifecycle
from app.services.chapter_service import get_project_chapter

logger = logging.getLogger(__name__)

MAX_LOGS_PER_PROJECT = 200


async def write_log(
    db: AsyncSession,
    *,
    project_id: int,
    chapter_id: int | None,
    kind: str,
    ai_config_id: int | None,
    provider: str,
    model: str,
    params: dict[str, Any],
    messages: list[Any],
    status: str,
    error: str = "",
    output_chars: int = 0,
    duration_ms: int = 0,
    task_id: int | None = None,
) -> GenerationLog:
    """写入一条记录并裁剪（不 commit）。章节 / 配置 / 任务在生成期间被删除时对应外键置空。"""
    if chapter_id is not None and await db.get(Chapter, chapter_id) is None:
        chapter_id = None
    if ai_config_id is not None and await db.get(AIConfig, ai_config_id) is None:
        ai_config_id = None
    if task_id is not None and await db.get(Task, task_id) is None:
        task_id = None
    log = GenerationLog(
        project_id=project_id,
        chapter_id=chapter_id,
        kind=kind,
        ai_config_id=ai_config_id,
        provider=provider[:32],
        model=model[:200],
        params=params,
        messages=list(messages),
        status=status,
        error=error,
        output_chars=output_chars,
        duration_ms=duration_ms,
        task_id=task_id,
    )
    db.add(log)
    await db.flush()
    stale = list(
        await db.scalars(
            select(GenerationLog.id)
            .where(GenerationLog.project_id == project_id)
            .order_by(GenerationLog.id.desc())
            .offset(MAX_LOGS_PER_PROJECT)
        )
    )
    if stale:
        await db.execute(delete(GenerationLog).where(GenerationLog.id.in_(stale)))
    return log


async def save_log(sessionmaker: async_sessionmaker[AsyncSession], **kwargs: Any) -> int | None:
    """在独立会话中写一条记录并提交（失败 / 取消路径使用）；出错只记日志，不向上抛。"""
    try:
        async with sessionmaker() as db:
            project = await project_lifecycle.lock_record(db, kwargs["project_id"])
            if project.deleted_at is not None:
                return None
            log = await write_log(db, **kwargs)
            await db.commit()
            return log.id
    except Exception:  # noqa: BLE001
        logger.exception("写入生成记录失败")
        return None


async def list_logs(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> list[GenerationLog]:
    await get_project_chapter(db, user_id, project_id, chapter_id)
    rows = await db.scalars(
        select(GenerationLog)
        .where(GenerationLog.project_id == project_id, GenerationLog.chapter_id == chapter_id)
        .order_by(GenerationLog.id.desc())
    )
    return list(rows)


async def get_log(db: AsyncSession, user_id: int, project_id: int, chapter_id: int, log_id: int) -> GenerationLog:
    await get_project_chapter(db, user_id, project_id, chapter_id)
    log = await db.scalar(
        select(GenerationLog).where(
            GenerationLog.id == log_id,
            GenerationLog.project_id == project_id,
            GenerationLog.chapter_id == chapter_id,
        )
    )
    if log is None:
        raise NotFoundError("生成记录不存在")
    return log
