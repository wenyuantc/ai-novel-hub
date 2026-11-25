"""局部改写：预览（SSE，只流式返回结果、不改正文、不抢占 generating 锁）+ 采纳（乐观并发校验）。

偏移一律为 Python 字符串的码点偏移（前端负责与 UTF-16 偏移互转）。
"""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Any, get_args

import anyio
from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.prompts import RewriteOp, build_rewrite_messages
from app.ai.prompts.registry import prompt_meta
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.core.security import SecretCipher
from app.models import AIConfig, Chapter, GenerationLog, Project, ProjectCollaborator, User
from app.schemas.chapter_version import RewriteApply, RewriteRequest
from app.schemas.scene import SceneOut
from app.services import (
    ai_config_service,
    authorization_service,
    collaboration_ai_guard,
    context_builder,
    generation_log_service,
    planning_context_service,
    project_access,
    project_ai_config,
    project_lifecycle,
    prompt_service,
    scene_ai_context,
    scene_service,
    skill_service,
    style_service,
    version_service,
)
from app.services.ai_dispatch import guard_provider, visibility_sessionmaker
from app.services.ai_usage_recording import meter_provider
from app.services.chapter_service import commit_versioned, count_words, get_manual_chapter, get_owned_chapter
from app.services.collaboration_task_access import task_grant_identity
from app.services.generation_service import Event, elapsed_ms, ensure_outside_batch, stream_pieces
from app.services.project_service import get_owned_project
from app.services.writing_stats import count_added_words, record_words

logger = logging.getLogger(__name__)

MAX_SELECTION_CHARS = 5000
BEFORE_CHARS = 1500
AFTER_CHARS = 500
CONTINUE_BEFORE_CHARS = 3000
MAX_SETTING_CONTEXT_CHARS = 4000
# 采纳时用于乐观校验的"选区前原文"长度（码点）；光标续写时 original 为空，只能靠它发现前文被改
APPLY_BEFORE_CHARS = 200
STALE_SELECTION = "正文已变化，请重新选择后再改写"
REWRITE_KINDS = frozenset(get_args(RewriteOp))
# 单帧交付时短持读锁；慢客户端不能无限期阻塞撤权或作品写入。
OUTPUT_LEASE_SECONDS = 5.0


@dataclass
class RewriteJob:
    project_id: int
    chapter_id: int
    user_id: int
    op: RewriteOp
    start: int
    end: int
    original: str
    before: str
    provider: LLMProvider | None
    model: str
    messages: list[ChatMessage]
    ai_config_id: int | None = None
    provider_name: str = ""
    thinking: ThinkingLevel = "off"
    max_tokens: int | None = None
    params: dict[str, Any] = field(default_factory=dict)
    scene_id: int = 0
    scene_start: int = 0
    scene_end: int = 0
    context_stamp: scene_ai_context.SceneContextStamp | None = None
    section_key: str | None = None
    request_id: str | None = None
    actor: authorization_service.AuthenticatedActor | None = None
    binding: project_ai_config.ResolvedProjectModel | None = None
    log_binding: project_ai_config.ResolvedProjectModel | None = None
    send_guard: collaboration_ai_guard.SendGuard | None = None
    factory: ProviderFactory | None = None
    ai_timeout: float = 0
    cipher: SecretCipher | None = None
    allow_private: bool = False


def _has_log_identity_columns() -> bool:
    return hasattr(GenerationLog, "model_source") and hasattr(GenerationLog, "model_config_owner_id")


async def _require_log_identity_storage(db: AsyncSession) -> None:
    """缺少持久来源证据时不得先向协作者发送正文。"""
    if not _has_log_identity_columns():
        raise collaboration_ai_guard.CollaborationSendBlocked()
    try:
        with db.no_autoflush:
            await db.execute(select(GenerationLog.model_source, GenerationLog.model_config_owner_id)
                             .where(GenerationLog.id == -1))
    except SQLAlchemyError:
        raise collaboration_ai_guard.CollaborationSendBlocked() from None


async def _ai_project_chapter(
    db: AsyncSession, user_id: int, chapter_id: int, project_id: int | None, *,
    for_update: bool = False, visibility_write: bool = False,
) -> tuple[Project, Chapter]:
    """先定位父作品，再以真实 actor 验证 AI 能力并重查目标章。"""
    with db.no_autoflush:
        actual_project_id = await db.scalar(select(Chapter.project_id).where(Chapter.id == chapter_id))
        if actual_project_id is None or (project_id is not None and project_id != actual_project_id):
            raise NotFoundError("章节不存在")
        project = await project_access.require_project(
            db, user_id, actual_project_id, "ai.use", for_update=for_update, visibility_write=visibility_write,
        )
        chapter = await get_manual_chapter(
            db, user_id, actual_project_id, chapter_id, "content.edit" if for_update else "content.read",
            for_update=for_update,
        )
    return project, chapter


async def _stream_scope(db: AsyncSession, job: RewriteJob) -> None:
    """最后一条 SQL 在同一快照验证原令牌、邀请、模型、作品和章节版本。"""
    binding, actor, stamp = job.binding, job.actor, job.context_stamp
    if binding is None or actor is None or stamp is None or binding.grant_identity is None:
        raise collaboration_ai_guard.CollaborationSendBlocked()
    await scene_ai_context.require_stamp(db, stamp)
    invitation, revision = binding.grant_identity
    statement = (
        select(AIConfig)
        .select_from(Project)
        .join(User, User.id == actor.id)
        .join(ProjectCollaborator, and_(
            ProjectCollaborator.project_id == Project.id, ProjectCollaborator.user_id == actor.id,
        ))
        .join(Chapter, and_(Chapter.id == job.chapter_id, Chapter.project_id == Project.id))
        .join(AIConfig, and_(AIConfig.id == binding.config_id, AIConfig.user_id == binding.config_owner_id))
        .where(
            Project.id == job.project_id, Project.user_id == binding.project_owner_id,
            *project_lifecycle.active_conditions(), Project.last_deleted_at == stamp.visibility.last_deleted_at,
            Project.ai_visibility_revision == stamp.visibility.revision,
            Project.ai_history_epoch == stamp.visibility.epoch, Project.planning_revision == stamp.planning_revision,
            Chapter.content_revision == stamp.content_revision,
            User.is_active.is_(True), User.token_version == actor.token_version,
            ProjectCollaborator.status == "accepted", ProjectCollaborator.role == "editor",
            ProjectCollaborator.invitation_id == invitation, ProjectCollaborator.revision == revision,
            AIConfig.model_type == binding.model_type,
        )
    )
    if binding.source == "owner":
        statement = statement.where(ProjectCollaborator.allow_owner_ai.is_(True))
    with db.no_autoflush:
        config = await db.scalar(statement)
    if config is None or project_ai_config._fingerprint(config) != binding.config_fingerprint:
        raise collaboration_ai_guard.CollaborationSendBlocked()


@asynccontextmanager
async def _output_lease(job: RewriteJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """单帧交付完成前持有原 actor 与项目读屏障，不持锁等待整个模型流。"""
    if job.actor is None or job.binding is None or job.cipher is None:
        raise collaboration_ai_guard.CollaborationSendBlocked()
    db = sessionmaker()
    try:
        with db.no_autoflush:
            await authorization_service.require_authenticated_for_send(db, job.actor)
            project_id = await db.scalar(select(Project.id).where(
                Project.id == job.project_id, *project_lifecycle.active_conditions(),
            ).with_for_update(read=True))
            if project_id is None:
                raise collaboration_ai_guard.CollaborationSendBlocked()
            await project_ai_config.revalidate_project_model(
                db, job.binding, cipher=job.cipher, allow_private=job.allow_private, lock_config=True,
            )
            await _stream_scope(db, job)
        yield
    finally:
        # ASGI send 的取消不得中断事务释放；此 scope 不跨业务生成器 yield。
        failed = False
        with anyio.CancelScope(shield=True):
            try:
                await db.close()
            except Exception:  # noqa: BLE001
                failed = True
                try:
                    await db.invalidate()
                except Exception:  # noqa: BLE001
                    pass
        if failed:
            # close 失败时尽力使连接失效；不能向日志或客户端暴露 SQL/参数。
            raise collaboration_ai_guard.CollaborationSendBlocked() from None


def output_frame_lease(job: RewriteJob, sessionmaker: async_sessionmaker[AsyncSession]):
    """仅协作者帧需要由实际 ASGI 发送协程独立持有授权屏障。"""
    return partial(_output_lease, job, sessionmaker) if job.binding is not None else None


def _trusted_log_grant(log: GenerationLog | None, user_id: int, project: Project) -> tuple[str, int] | None:
    """只接受有服务端持久来源证明的本人成功记录；旧 params 不能证明邀请轮次。"""
    if (log is None or log.status != "success" or log.actor_user_id != user_id or log.kind not in REWRITE_KINDS
            or not isinstance(log.params, dict) or log.params.get("op") != log.kind
            or not isinstance(log.params.get("context_snapshot"), dict)):
        return None
    source = getattr(log, "model_source", None)
    owner = getattr(log, "model_config_owner_id", None)
    if source not in {"actor", "owner"} or owner != (user_id if source == "actor" else project.user_id):
        return None
    return task_grant_identity(log.params)


async def prepare_rewrite_job(
    db: AsyncSession,
    *,
    user_id: int,
    chapter_id: int,
    req: RewriteRequest,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
    context_budget: int,
    project_id: int | None = None,
    scene_id: int | None = None,
) -> RewriteJob:
    from app.services.rich_document_ai import scene_reference_context

    owner_row = (await db.execute(select(Project.user_id, Project.id).join(
        Chapter, Chapter.project_id == Project.id,
    ).where(Chapter.id == chapter_id))).one_or_none()
    if owner_row is not None and owner_row.user_id == user_id:
        chapter = await get_owned_chapter(db, user_id, chapter_id)
        project = await get_owned_project(db, user_id, chapter.project_id)
        if project_id is not None and project_id != project.id:
            raise NotFoundError("章节不存在")
    else:
        project, chapter = await _ai_project_chapter(db, user_id, chapter_id, project_id, for_update=True)
    target_scene = await scene_service.require_scene(db, chapter, scene_id) if scene_id is not None else None
    if chapter.status == "generating":
        raise ConflictError("该章节正在生成中，请稍候再试")
    await ensure_outside_batch(db, chapter)
    await scene_ai_context.check_revisions(db, chapter, req.expected_content_revision, req.expected_planning_revision)
    stamp = await scene_ai_context.read_stamp(db, project.id, chapter.id)
    await scene_service.capture_snapshot(db, chapter)
    if scene_id is None:
        if req.start > req.end or req.end > len(chapter.content):
            raise BadRequestError("选区超出正文范围，请重新选择")
        scene, start, end = await scene_service.resolve_chapter_range(db, chapter, req.start, req.end)
    else:
        assert target_scene is not None
        scene = target_scene
        start, end = req.start, req.end
    content = scene.content
    if start > end or end > len(content):
        raise BadRequestError("选区超出正文范围，请重新选择")
    if req.op == "continue":
        if start != end:
            raise BadRequestError("续写时请把光标放在续写位置，不要选中文字")
        if not content[:start].strip():
            raise BadRequestError("光标前没有正文，无法续写")
    else:
        if not content[start:end].strip():
            raise BadRequestError("请先选中要处理的文字")
        if end - start > MAX_SELECTION_CHARS:
            raise BadRequestError(f"单次选区不能超过 {MAX_SELECTION_CHARS} 字")

    binding = None
    if project.user_id == user_id:
        cfg = await ai_config_service.resolve_config(db, user_id, req.ai_config_id)
        provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
        log_binding = None
        if _has_log_identity_columns():
            log_binding = await project_ai_config.resolve_project_model(
                db, user_id, project.id, "text", cipher=cipher, config_id=cfg.id,
                allow_private=allow_private, for_update=True,
            )
            if project_ai_config._fingerprint(cfg) != log_binding.config_fingerprint:
                raise ConflictError("模型配置已变化，请重新预览")
    else:
        await _require_log_identity_storage(db)
        binding = await project_ai_config.resolve_project_model(
            db, user_id, project.id, "text", cipher=cipher, config_id=req.ai_config_id,
            allow_private=allow_private, for_update=True,
        )
        cfg = await db.scalar(select(AIConfig).where(
            AIConfig.id == binding.config_id, AIConfig.user_id == binding.config_owner_id,
        ))
        if cfg is None:
            raise collaboration_ai_guard.CollaborationSendBlocked()
        provider = None
        log_binding = binding
    task = await context_builder.chapter_task(db, chapter)
    resolved = await style_service.resolve(db, user_id, project, chapter, req.style)
    override = await prompt_service.get_override(db, user_id, "chapter.rewrite")
    skill = await skill_service.snapshot(db, user_id, req.skill_id, req.skill_inputs)
    original = content[start:end]
    if req.op == "continue":
        before, after = content[max(0, start - CONTINUE_BEFORE_CHARS) : start], ""
    else:
        before, after = content[max(0, start - BEFORE_CHARS) : start], content[end : end + AFTER_CHARS]
    budget = min(MAX_SETTING_CONTEXT_CHARS, ai_config_service.context_char_budget(cfg, context_budget))
    scenes = await scene_service.list_active_scenes(db, chapter.id)
    preceding = "".join(row.separator_before + row.content for row in scenes if row.sort < scene.sort)
    scene_context = scene_ai_context.scope_block(
        scene_id=scene.id, title=scene.title, summary=scene.summary[:500],
        index=next(index for index, row in enumerate(scenes, 1) if row.id == scene.id), total=len(scenes),
        target_words=0, preceding_text=preceding[-500:], selection=True,
    )
    author_budget = max(0, min(2_000, budget // 4, budget - len(scene_context) - 2))
    author_context = await scene_reference_context(
        db, project.id, [(item.id, item.sort) for item in scenes if item.sort <= scene.sort], author_budget,
    )
    if author_context:
        scene_context += "\n\n" + author_context
    selected_hint = "\n".join((before, original, after, author_context))
    remaining = budget - len(scene_context) - 2
    planning = await planning_context_service.load_planning_context(
        db, project, "scene", scene.id, scope="selection", budget=remaining,
        text_hint="\n".join((selected_hint, req.instruction)), legacy_text_hint=selected_hint,
        stamp=stamp.visibility, preceding_text=preceding if scenes[0].id != scene.id else None,
    )
    setting_context = await context_builder.build_setting_context(
        db, project, chapter, text_hint=selected_hint,
        budget=remaining - planning.reserved_chars, planning=planning,
    )
    if scene_id is None:
        # 旧章级入口的显示标题保留；实际资格与时点仍严格来自映射后的 Scene。
        setting_context = setting_context.replace("【本场景有效设定】", "【本章有效设定】", 1)
    messages = build_rewrite_messages(
        op=req.op,
        selected=original,
        before=before,
        after=after,
        style=resolved.block if resolved is not None else "",
        summary=task.summary,
        instruction=req.instruction,
        skill_guide=(skill or {}).get("body", ""),
        setting_context=setting_context,
        planning_context=planning.text,
        scene_context=scene_context,
        override=override,
    )
    assert planning.setting_scope is not None
    await scene_ai_context.require_stamp(db, stamp)
    send_guard = None
    actor = None
    if binding is None:
        assert provider is not None
        maker = visibility_sessionmaker(db)
        provider = guard_provider(provider, stamp.visibility, maker,
                                  verify=lambda session: scene_ai_context.require_stamp(session, stamp, sending=True))
        provider = meter_provider(
            provider, cfg, maker, actor_id=user_id, kind="chapter.rewrite", project_id=project.id,
            project_owner_id=project.user_id, chapter_id=chapter.id, chapter_created_at=chapter.created_at,
        )
    else:
        send_guard = collaboration_ai_guard.for_http(
            db, user_id, binding, stamp.visibility, cipher=cipher, allow_private=allow_private, scene=stamp,
        )
        actor = db.info["authenticated_actor"]
    return RewriteJob(
        project_id=project.id,
        chapter_id=chapter.id,
        user_id=user_id,
        op=req.op,
        start=req.start,
        end=req.end,
        scene_id=scene.id, scene_start=start, scene_end=end, context_stamp=stamp,
        original=original,
        before=content[max(0, start - APPLY_BEFORE_CHARS) : start],
        provider=provider,
        model=cfg.model,
        messages=messages,
        ai_config_id=cfg.id,
        provider_name=cfg.provider,
        thinking=cfg.thinking_level,  # type: ignore[arg-type]
        max_tokens=ai_config_service.max_output_tokens(cfg),
        params={
            "op": req.op,
            "start": req.start,
            "end": req.end,
            "scene_id": scene.id, "scene_start": start, "scene_end": end,
            "original_hash": scene_service.content_fingerprint(original),
            "before_hash": scene_service.content_fingerprint(content[max(0, start - APPLY_BEFORE_CHARS):start]),
            "context_snapshot": stamp.log_data(),
            "selection_chars": end - start,
            "instruction": req.instruction,
            "prompt": prompt_meta("chapter.rewrite", override),
            "style_choice": req.style,
            "style": style_service.style_log(resolved),
            "skill": skill_service.skill_log(skill),
            "planning": planning.log_data("selection"),
        },
        actor=actor, binding=binding, log_binding=log_binding, send_guard=send_guard, factory=factory,
        ai_timeout=ai_timeout, cipher=cipher, allow_private=allow_private,
    )


async def run_rewrite_job(job: RewriteJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    """只流式输出改写结果并写生成记录；正文不变，由作者确认后调用 apply_rewrite。"""
    started = time.monotonic()
    finished = False
    fail_status, fail_error = "cancelled", ""
    pieces: list[str] = []

    def log_kwargs(status: str, error: str, output: str) -> dict[str, Any]:
        return {
            "project_id": job.project_id,
            "actor_user_id": job.user_id,
            "chapter_id": job.chapter_id,
            "kind": job.op,
            "ai_config_id": job.ai_config_id,
            "provider": job.provider_name,
            "model": job.model,
            "params": job.params,
            "messages": job.messages,
            "status": status,
            "error": error,
            "output_chars": count_words(output),
            "duration_ms": elapsed_ms(started),
            **({"model_binding": job.log_binding} if job.log_binding is not None else {}),
        }

    try:
        if job.send_guard is not None:
            assert job.factory is not None
            bound = await job.send_guard.prepare_provider(
                sessionmaker, job.factory, request_timeout=job.ai_timeout,
                usage_kind="chapter.section.rewrite" if job.section_key is not None else "chapter.rewrite",
                usage_chapter_id=job.chapter_id,
            )
            job.provider, job.model = bound.provider, bound.model
        assert job.provider is not None
        async with aclosing(
            stream_pieces(
                job.provider, job.messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking, pieces=pieces
            )
        ) as events:
            async for event in events:
                yield event
        text = "".join(pieces).strip()
        if not text:
            raise AIProviderError("模型未返回任何内容")
        assert job.context_stamp is not None
        if job.binding is None:
            async with sessionmaker() as db:
                await scene_ai_context.require_stamp(db, job.context_stamp)
            log_id = await generation_log_service.save_log(sessionmaker, **log_kwargs("success", "", text))
        else:
            assert job.actor is not None and job.cipher is not None
            async with sessionmaker() as db:
                db.info["authenticated_actor"] = job.actor
                await project_access.require_project(
                    db, job.user_id, job.project_id, "ai.use", for_update=True,
                    grant_identity=job.binding.grant_identity,
                )
                await project_ai_config.revalidate_project_model(
                    db, job.binding, cipher=job.cipher, allow_private=job.allow_private, lock_config=True,
                )
                await scene_ai_context.require_stamp(db, job.context_stamp)
                log = await generation_log_service.write_log(db, **log_kwargs("success", "", text))
                await db.commit()
                log_id = log.id
        finished = True
        payload = {
                "text": text,
                "start": job.start,
                "end": job.end,
                "original": job.original,
                "before": job.before,
                "log_id": log_id,
                "scene_id": job.scene_id, "chapter_id": job.chapter_id,
                "content_revision": job.context_stamp.content_revision,
                "planning_revision": job.context_stamp.planning_revision,
            }
        if job.section_key is not None:
            payload.update(section_key=job.section_key, request_id=job.request_id)
        yield "done", payload
    except AppError as exc:
        fail_status, fail_error = "error", exc.detail
        yield "error", {"detail": exc.detail, **(exc.extra or {})}
    except AIProviderError as exc:
        fail_status, fail_error = "error", str(exc) if job.binding is None else "模型调用失败，请稍后重试"
        yield "error", {"detail": fail_error}
    except Exception:  # noqa: BLE001
        if job.binding is None:
            logger.exception("章节 %s 局部改写失败", job.chapter_id)
        else:
            # 异常可能包含 SQL 参数、正文或作者模型信息，协作路径不记录原始堆栈。
            logger.error("章节 %s 局部改写失败", job.chapter_id)
        fail_status, fail_error = "error", "改写失败：服务器内部错误"
        yield "error", {"detail": fail_error}
    finally:
        if not finished:
            with anyio.CancelScope(shield=True):
                await generation_log_service.save_log(
                    sessionmaker, **log_kwargs(fail_status, fail_error, "".join(pieces))
                )


async def _apply(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, data: RewriteApply,
    scene_id: int | None,
) -> tuple[Chapter, object]:
    # 即使本次选区本身没有区块，替换也可能移动已启用区块的位置；先取得发送写门禁。
    project, chapter = await _ai_project_chapter(
        db, user_id, chapter_id, project_id, for_update=True, visibility_write=True,
    )
    target_scene = await scene_service.require_scene(db, chapter, scene_id) if scene_id is not None else None
    if chapter.status == "generating":
        raise ConflictError("该章节正在生成中，请稍候再试")
    await ensure_outside_batch(db, chapter)
    await scene_ai_context.check_revisions(db, chapter, data.expected_content_revision, data.expected_planning_revision)
    if scene_id is None:
        if data.start > data.end or data.end > len(chapter.content):
            raise ConflictError(STALE_SELECTION)
        scene, start, end = await scene_service.resolve_chapter_range(db, chapter, data.start, data.end)
    else:
        assert target_scene is not None
        scene = target_scene
        start, end = data.start, data.end
    content = scene.content
    if start > end or end > len(content) or content[start:end] != data.original:
        raise ConflictError(STALE_SELECTION)
    if data.before is not None and (start < len(data.before) or content[start-len(data.before):start] != data.before):
        raise ConflictError(STALE_SELECTION)
    log = None
    if data.log_id is not None:
        log = await db.scalar(select(GenerationLog).where(
            GenerationLog.id == data.log_id, GenerationLog.project_id == project_id,
            GenerationLog.chapter_id == chapter.id, GenerationLog.actor_user_id == user_id,
        ))
    if project.user_id != user_id:
        grant = _trusted_log_grant(log, user_id, project)
        required = {"scene_id", "scene_start", "scene_end", "original_hash", "before_hash", "context_snapshot"}
        if (grant is None or log is None or not isinstance(log.params, dict) or not required <= log.params.keys()
                or not isinstance(log.params["context_snapshot"], dict)):
            raise ConflictError("改写预览记录已失效，请重新预览", extra={"code": "scene_context_changed"})
        await project_access.require_project(db, user_id, project_id, "ai.use", for_update=True, grant_identity=grant)
    # 旧章级入口过去允许无版本/日志的手工替换；新增的格式损失确认不得借此绕过快照校验。
    if data.confirm_format_loss and (
        data.expected_content_revision is None or data.expected_planning_revision is None
        or log is None or log.status != "success" or "scene_id" not in (log.params or {})
    ):
        raise ConflictError("确认格式变化前，请重新预览并核对当前版本", extra={
            "code": "rich_document_confirmation_context_required",
        })
    # 新入口必须绑定真实预览记录才能证明时间线/字段/可见性仍是同一快照。
    # 旧单场景客户端无日志的手工采纳保留原乐观选区语义。
    if scene_id is not None and (log is None or log.status != "success"):
        raise ConflictError("改写预览记录已失效，请重新预览", extra={"code": "scene_context_changed"})
    if log is not None and "scene_id" in log.params:
        params = log.params
        if (params.get("scene_id") != scene.id or params.get("scene_start") != start
                or params.get("scene_end") != end
                or params.get("original_hash") != scene_service.content_fingerprint(data.original)
                or (data.before is not None and params.get("before_hash")
                    != scene_service.content_fingerprint(data.before))):
            raise ConflictError(STALE_SELECTION)
        await scene_ai_context.require_stamp(db, params.get("context_snapshot") or {})
    elif scene_id is not None:
        raise ConflictError("改写预览记录缺少场景快照，请重新预览", extra={"code": "scene_context_changed"})
    mapped = None
    current = None
    if await scene_service._rich_scene_ids(db, [scene.id]):
        from app.services.rich_document_mapping import FormatLossRequired, UnsafeMapping, map_explicit_replacement
        from app.services.rich_document_store import capture_documents

        current = (await capture_documents(db, [scene]))[0][scene.id]
        try:
            mapped = map_explicit_replacement(current, start, end, data.replacement,
                                              confirm_format_loss=data.confirm_format_loss)
        except FormatLossRequired as exc:
            raise ConflictError(str(exc), extra={"code": "rich_document_confirmation_required"}) from exc
        except UnsafeMapping as exc:
            raise ConflictError(str(exc), extra={"code": "rich_document_unmapped"}) from exc
    await version_service.ensure_baseline(db, chapter)
    from app.schemas.rich_document import ProseEdit
    from app.services.annotation_anchors import map_edits

    edits = [ProseEdit(start=start, end=end, insert=data.replacement)]
    if mapped is None:
        await scene_service.replace_scene_content(
            db, chapter, scene, content[:start] + data.replacement + content[end:],
            expected_revision=data.expected_content_revision, prose_edits=edits,
        )
    else:
        from app.services.rich_document_service import write_mapped_document

        assert current is not None
        expected = (data.expected_content_revision if data.expected_content_revision is not None
                    else chapter.content_revision)
        await map_edits(db, scene, mapped.content, edits)
        await write_mapped_document(db, project_id, scene, current, mapped, expected)
        await scene_service.refresh_projection(db, chapter, expected_revision=expected)
    # 新预览已在上面核验位置和场景快照。无绑定的兼容请求照常保存，无法归入 AI 写作。
    if (log is not None and log.status == "success" and log.kind in REWRITE_KINDS
            and "scene_id" in log.params and "section_key" not in log.params):
        await record_words(
            db, user_id=user_id, project_id=project_id, source="ai",
            words=count_added_words(data.original, data.replacement),
        )
    await version_service.record(db, chapter, "rewrite", log_id=log.id if log else None)
    return chapter, scene


async def apply_rewrite(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, data: RewriteApply,
) -> Chapter:
    chapter, _ = await _apply(db, user_id, project_id, chapter_id, data, None)
    await commit_versioned(db)
    await db.refresh(chapter)
    return chapter


async def apply_scene_rewrite(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, scene_id: int, data: RewriteApply,
) -> SceneOut:
    chapter, scene = await _apply(db, user_id, project_id, chapter_id, data, scene_id)
    out = await scene_service.scene_out(db, chapter, scene)
    await commit_versioned(db)
    return out
