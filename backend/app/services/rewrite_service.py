"""局部改写：预览（SSE，只流式返回结果、不改正文、不抢占 generating 锁）+ 采纳（乐观并发校验）。

偏移一律为 Python 字符串的码点偏移（前端负责与 UTF-16 偏移互转）。
"""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.prompts import RewriteOp, build_rewrite_messages
from app.ai.prompts.registry import prompt_meta
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.core.security import SecretCipher
from app.models import Chapter, GenerationLog
from app.schemas.chapter_version import RewriteApply, RewriteRequest
from app.schemas.scene import SceneOut
from app.services import (
    ai_config_service,
    context_builder,
    generation_log_service,
    planning_context_service,
    prompt_service,
    scene_ai_context,
    scene_service,
    skill_service,
    style_service,
    version_service,
)
from app.services.ai_dispatch import guard_provider, visibility_sessionmaker
from app.services.chapter_service import commit_versioned, count_words, get_owned_chapter
from app.services.generation_service import Event, elapsed_ms, ensure_outside_batch, stream_pieces
from app.services.project_service import get_owned_project

logger = logging.getLogger(__name__)

MAX_SELECTION_CHARS = 5000
BEFORE_CHARS = 1500
AFTER_CHARS = 500
CONTINUE_BEFORE_CHARS = 3000
MAX_SETTING_CONTEXT_CHARS = 4000
# 采纳时用于乐观校验的"选区前原文"长度（码点）；光标续写时 original 为空，只能靠它发现前文被改
APPLY_BEFORE_CHARS = 200
STALE_SELECTION = "正文已变化，请重新选择后再改写"


@dataclass
class RewriteJob:
    project_id: int
    chapter_id: int
    op: RewriteOp
    start: int
    end: int
    original: str
    before: str
    provider: LLMProvider
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
    chapter = await get_owned_chapter(db, user_id, chapter_id)
    project = await get_owned_project(db, user_id, chapter.project_id)
    if project_id is not None and chapter.project_id != project_id:
        raise NotFoundError("章节不存在")
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

    cfg = await ai_config_service.resolve_config(db, user_id, req.ai_config_id)
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    task = await context_builder.chapter_task(db, chapter)
    resolved = await style_service.resolve(db, user_id, project, chapter, req.style)
    override = await prompt_service.get_override(db, user_id, "chapter.rewrite")
    skill = await skill_service.snapshot(db, user_id, req.skill_id)
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
    selected_hint = "\n".join((before, original, after))
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
    provider = guard_provider(provider, stamp.visibility, visibility_sessionmaker(db),
                              verify=lambda session: scene_ai_context.require_stamp(session, stamp, sending=True))
    return RewriteJob(
        project_id=project.id,
        chapter_id=chapter.id,
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
        }

    try:
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
        async with sessionmaker() as db:
            await scene_ai_context.require_stamp(db, job.context_stamp)
        log_id = await generation_log_service.save_log(sessionmaker, **log_kwargs("success", "", text))
        finished = True
        yield (
            "done",
            {
                "text": text,
                "start": job.start,
                "end": job.end,
                "original": job.original,
                "before": job.before,
                "log_id": log_id,
                "scene_id": job.scene_id, "chapter_id": job.chapter_id,
                "content_revision": job.context_stamp.content_revision,
                "planning_revision": job.context_stamp.planning_revision,
            },
        )
    except AppError as exc:
        fail_status, fail_error = "error", exc.detail
        yield "error", {"detail": exc.detail, **(exc.extra or {})}
    except AIProviderError as exc:
        fail_status, fail_error = "error", str(exc)
        yield "error", {"detail": fail_error}
    except Exception:  # noqa: BLE001
        logger.exception("章节 %s 局部改写失败", job.chapter_id)
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
    chapter = await get_owned_chapter(db, user_id, chapter_id, lock=True)
    await db.refresh(chapter)
    if chapter.project_id != project_id:
        raise NotFoundError("章节不存在")
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
            GenerationLog.chapter_id == chapter.id,
        ))
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
    await version_service.ensure_baseline(db, chapter)
    await scene_service.replace_scene_content(
        db, chapter, scene, content[:start] + data.replacement + content[end:],
        expected_revision=data.expected_content_revision,
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
