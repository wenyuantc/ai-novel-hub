"""AI 调用记录：按项目和触发者分别保留最近记录，并提供列表与详情。"""

import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import NotFoundError
from app.models import AIConfig, Chapter, GenerationLog, Project, Task
from app.schemas.chapter_version import GenerationLogOut
from app.services import personal_projection, project_ai_config, project_lifecycle
from app.services.task_engine.execution_identity import TaskExecutionIdentity, require_live_worker

logger = logging.getLogger(__name__)

# 每个触发者在同一项目中单独计算上限，不能裁剪其他人的私人记录。
MAX_LOGS_PER_PROJECT = 200


class GenerationLogWriteError(RuntimeError):
    """不向上层传播可能包含模型请求参数的数据库异常。"""

    def __init__(self, error_type: str, *, serialization_conflict: bool = False) -> None:
        self.error_type = error_type
        self.serialization_conflict = serialization_conflict
        super().__init__(f"生成记录写入失败（{error_type}）")


async def write_log(
    db: AsyncSession,
    *,
    project_id: int,
    actor_user_id: int,
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
    model_binding: project_ai_config.ResolvedProjectModel | None = None,
) -> GenerationLog:
    """写入一条记录并裁剪（不 commit）。章节 / 配置 / 任务在生成期间被删除时对应外键置空。"""
    if actor_user_id is None:
        raise ValueError("生成记录必须提供真实触发者")
    try:
        # 只接受服务端解析的绑定，绝不把来自请求/旧 params 的同名键当来源证明。
        clean_params = dict(params)
        for name in ("_model_source", "_model_config_owner_id", "_collaboration_grant", "_model_binding"):
            clean_params.pop(name, None)
        source, config_owner_id, bound_instance_key = None, None, None
        if model_binding is not None:
            if type(model_binding) is not project_ai_config.ResolvedProjectModel:
                raise ValueError("模型来源绑定无效")
            with db.no_autoflush:
                project = await db.get(Project, project_id)
            if project is None or model_binding.config_id != ai_config_id:
                raise ValueError("模型来源绑定无效")
            binding = project_ai_config.deserialize_project_model(
                project_ai_config.serialize_project_model(model_binding), actor_id=actor_user_id,
                project_id=project_id, project_owner_id=project.user_id,
            )
            if binding is None:
                raise ValueError("模型来源绑定无效")
            if not hasattr(GenerationLog, "model_source") or not hasattr(GenerationLog, "model_config_owner_id"):
                raise RuntimeError("生成记录来源存储尚未升级")
            source, config_owner_id = binding.source, binding.config_owner_id
            bound_instance_key = binding.config_instance_key
            if binding.grant_identity is not None:
                clean_params["_collaboration_grant"] = {
                    "invitation_id": binding.grant_identity[0], "revision": binding.grant_identity[1],
                }
        if chapter_id is not None and await db.get(Chapter, chapter_id) is None:
            chapter_id = None
        if ai_config_id is not None:
            if bound_instance_key is None:
                config_exists = await db.get(AIConfig, ai_config_id) is not None
            else:
                # 锁住绑定的原实例直到本事务提交，避免检查后旧配置被删除、同 ID 新实例
                # 恰好承接日志外键。无匹配实例时保留原始来源与用量，外键安全置空。
                config_exists = await db.scalar(select(AIConfig.id).where(
                    AIConfig.id == ai_config_id, AIConfig.instance_key == bound_instance_key,
                    AIConfig.user_id == config_owner_id,
                ).with_for_update(read=True)) is not None
            if not config_exists:
                ai_config_id = None
        if task_id is not None and await db.get(Task, task_id) is None:
            task_id = None
        source_fields = {"model_source": source, "model_config_owner_id": config_owner_id} \
            if hasattr(GenerationLog, "model_source") else {}
        # 模型配置实例键只来自服务端绑定；log_instance_key 由模型默认值在服务端独立生成。
        source_fields["model_config_instance_key"] = bound_instance_key
        log = GenerationLog(
            project_id=project_id,
            actor_user_id=actor_user_id,
            chapter_id=chapter_id,
            kind=kind,
            ai_config_id=ai_config_id,
            provider=provider[:32],
            model=model[:200],
            params=clean_params,
            messages=list(messages),
            status=status,
            error=error,
            output_chars=output_chars,
            duration_ms=duration_ms,
            task_id=task_id,
            **source_fields,
        )
        db.add(log)
        await db.flush()
        stale = list(
            await db.scalars(
                select(GenerationLog.id)
                .where(GenerationLog.project_id == project_id, GenerationLog.actor_user_id == actor_user_id)
                .order_by(GenerationLog.id.desc())
                .offset(MAX_LOGS_PER_PROJECT)
            )
        )
        if stale:
            # 删除前重验归属；若用户刚被删除，已置空的记录不再属于该触发者。
            await db.execute(delete(GenerationLog).where(
                GenerationLog.id.in_(stale), GenerationLog.project_id == project_id,
                GenerationLog.actor_user_id == actor_user_id,
            ))
        return log
    except SQLAlchemyError as exc:
        # 上层任务/HTTP 错误处理可能打印 traceback；SQLAlchemy 异常会带原始 SQL 参数。
        # 用不包含请求数据的类型名保留诊断信息，同时让调用方正常回滚并失败关闭。
        original = getattr(exc, "orig", None)
        sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        safe_error = GenerationLogWriteError(type(exc).__name__, serialization_conflict=sqlstate == "40001")
        del original, sqlstate
    # 离开 except 后再抛出，避免异常对象通过 __context__ 保留原始 SQL 参数。
    raise safe_error


async def save_log(
    sessionmaker: async_sessionmaker[AsyncSession], *, execution: TaskExecutionIdentity | None = None, **kwargs: Any,
) -> int | None:
    """在独立会话中写一条记录并提交（失败 / 取消路径使用）；出错只记日志，不向上抛。"""
    try:
        async with sessionmaker() as db:
            project = await project_lifecycle.lock_record(db, kwargs["project_id"])
            if project.deleted_at is not None:
                return None
            if execution is not None:
                # 本轮占用可能刚刚正常释放，原任务与进程身份仍必须匹配。
                if ((kwargs.get("task_id"), kwargs["project_id"], kwargs["actor_user_id"])
                        != (execution.task_id, execution.project_id, execution.actor_id)):
                    return None
                source = await db.scalar(select(Task.id).where(
                    Task.id == execution.task_id, Task.statistics_key == execution.task_key,
                    Task.user_id == execution.actor_id, Task.project_id == execution.project_id,
                    Task.kind == execution.kind, Task.queued_at == execution.queued_at,
                    Task.execution_key == execution.execution_key, Task.worker_key == execution.worker_key,
                    Task.status == "running",
                ).with_for_update())
                if source is None:
                    return None
                await require_live_worker(db, execution.worker_key)
            log = await write_log(db, **kwargs)
            await db.commit()
            return log.id
    except Exception as exc:  # noqa: BLE001
        error_type = exc.error_type if isinstance(exc, GenerationLogWriteError) else type(exc).__name__
        logger.error("写入生成记录失败 error_type=%s", error_type)
        return None


async def public_logs(
    db: AsyncSession, user_id: int, project_id: int, rows: list[GenerationLog],
) -> list[GenerationLogOut]:
    """生成独立输出；不能将脱敏字段写回原日志或更改有效用量。"""
    if any(row.project_id != project_id or row.actor_user_id != user_id for row in rows):
        raise NotFoundError("生成记录不存在")
    payload = [GenerationLogOut.model_validate(row).model_dump(mode="json") for row in rows]
    provenance = [(getattr(row, "model_source", None), getattr(row, "model_config_owner_id", None)) for row in rows]
    projected = await personal_projection.project_log_payload(db, user_id, project_id, payload, provenance)
    return [GenerationLogOut.model_validate(item) for item in projected]


async def _require_personal_chapter(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> None:
    with db.no_autoflush:
        await personal_projection.require_personal_project(db, user_id, project_id)
        chapter = await db.scalar(select(Chapter.id).where(Chapter.id == chapter_id, Chapter.project_id == project_id))
        if chapter is None:
            raise NotFoundError("章节不存在")


async def list_logs(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> list[GenerationLogOut]:
    with db.no_autoflush:
        await _require_personal_chapter(db, user_id, project_id, chapter_id)
        rows = list(await db.scalars(
            select(GenerationLog)
            .where(GenerationLog.project_id == project_id, GenerationLog.chapter_id == chapter_id,
                   GenerationLog.actor_user_id == user_id)
            .order_by(GenerationLog.id.desc())
        ))
        return await public_logs(db, user_id, project_id, rows)


async def get_log(db: AsyncSession, user_id: int, project_id: int, chapter_id: int, log_id: int) -> GenerationLogOut:
    with db.no_autoflush:
        await _require_personal_chapter(db, user_id, project_id, chapter_id)
        log = await db.scalar(
            select(GenerationLog).where(
                GenerationLog.id == log_id,
                GenerationLog.project_id == project_id,
                GenerationLog.chapter_id == chapter_id,
                GenerationLog.actor_user_id == user_id,
            )
        )
        if log is None:
            raise NotFoundError("生成记录不存在")
        return (await public_logs(db, user_id, project_id, [log]))[0]
