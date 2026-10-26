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
from app.core.errors import BadRequestError, ConflictError
from app.core.security import SecretCipher
from app.models import Chapter, GenerationLog
from app.schemas.chapter_version import RewriteApply, RewriteRequest
from app.services import ai_config_service, generation_log_service, prompt_service, style_service, version_service
from app.services.chapter_service import commit_versioned, count_words, get_owned_chapter, get_project_chapter
from app.services.context_builder import chapter_task
from app.services.generation_service import Event, elapsed_ms, stream_pieces
from app.services.project_service import get_owned_project

logger = logging.getLogger(__name__)

MAX_SELECTION_CHARS = 5000
BEFORE_CHARS = 1500
AFTER_CHARS = 500
CONTINUE_BEFORE_CHARS = 3000
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
) -> RewriteJob:
    chapter = await get_owned_chapter(db, user_id, chapter_id)
    project = await get_owned_project(db, user_id, chapter.project_id)
    if chapter.status == "generating":
        raise ConflictError("该章节正在生成中，请稍候再试")
    content = chapter.content
    start, end = req.start, req.end
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
    task = await chapter_task(db, chapter)
    resolved = await style_service.resolve(db, user_id, project, chapter, req.style)
    override = await prompt_service.get_override(db, user_id, "chapter.rewrite")
    original = content[start:end]
    if req.op == "continue":
        before, after = content[max(0, start - CONTINUE_BEFORE_CHARS) : start], ""
    else:
        before, after = content[max(0, start - BEFORE_CHARS) : start], content[end : end + AFTER_CHARS]
    messages = build_rewrite_messages(
        op=req.op,
        selected=original,
        before=before,
        after=after,
        style=resolved.block if resolved is not None else "",
        summary=task.summary,
        instruction=req.instruction,
        override=override,
    )
    return RewriteJob(
        project_id=project.id,
        chapter_id=chapter.id,
        op=req.op,
        start=start,
        end=end,
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
            "start": start,
            "end": end,
            "selection_chars": end - start,
            "instruction": req.instruction,
            "prompt": prompt_meta("chapter.rewrite", override),
            "style_choice": req.style,
            "style": style_service.style_log(resolved),
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
            },
        )
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


async def apply_rewrite(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, data: RewriteApply
) -> Chapter:
    """采纳改写：选区原文（及可选的选区前原文）与预览时一致才替换（乐观并发），并写 rewrite 版本。"""
    chapter = await get_project_chapter(db, user_id, project_id, chapter_id)
    if chapter.status == "generating":
        raise ConflictError("该章节正在生成中，请稍候再试")
    content = chapter.content
    start, end = data.start, data.end
    if start > end or end > len(content) or content[start:end] != data.original:
        raise ConflictError(STALE_SELECTION)
    # 光标续写时 original 为空串必然匹配，需核对光标前原文，否则前文被改后会插到错误位置
    if data.before is not None and (
        start < len(data.before) or content[start - len(data.before) : start] != data.before
    ):
        raise ConflictError(STALE_SELECTION)
    log_id = None
    if data.log_id is not None:
        # 不属于本章的记录忽略，不报错
        log_id = await db.scalar(
            select(GenerationLog.id).where(GenerationLog.id == data.log_id, GenerationLog.chapter_id == chapter.id)
        )
    await version_service.ensure_baseline(db, chapter)
    chapter.content = content[: data.start] + data.replacement + content[data.end :]
    chapter.word_count = count_words(chapter.content)
    await version_service.record(db, chapter, "rewrite", log_id=log_id)
    await commit_versioned(db)
    await db.refresh(chapter)
    return chapter
