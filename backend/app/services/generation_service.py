"""章节 AI 生成 / 润色。

分两阶段：
- prepare_*：在请求会话内完成鉴权、配置解析、上下文组装，并原子地把章节置为 generating。
  任何业务错误（404/400/409）在此抛出，以普通 JSON 响应返回。
- run_chapter_job：流式阶段，使用独立会话落库；产出 (event, data) 事件。
  成功时在同一事务内：ensure_baseline → 覆盖正文 → 写生成记录 → 写 generate/polish 版本。
  未正常完成（模型报错、客户端断开等）时把章节状态回退为原状态，正文与版本不变，只留一条失败/取消记录。
- 整章生成可选自动续写：字数低于目标 CONTINUE_RATIO 时最多续写 MAX_CONTINUE_ROUNDS 轮，
  每轮开始前产出 continue 事件；续写轮报错时保留已生成正文照常落库，done 附带 warning。
"""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.prompts import build_chapter_messages, build_continuation_messages, build_polish_messages
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.core.errors import BadRequestError, ConflictError
from app.core.security import SecretCipher
from app.models import Chapter
from app.schemas.chapter import ChapterOut, GenerateChapterRequest, PolishChapterRequest
from app.services import ai_config_service, generation_log_service, version_service
from app.services.chapter_service import count_words, get_owned_chapter
from app.services.context_builder import build_chapter_context, find_chapter_outline
from app.services.project_service import get_owned_project

logger = logging.getLogger(__name__)

Event = tuple[Literal["delta", "continue", "done", "error"], dict]

# 字数低于目标的该比例时自动续写
CONTINUE_RATIO = 0.85
MAX_CONTINUE_ROUNDS = 2


@dataclass
class ChapterJob:
    chapter_id: int
    prev_status: str
    provider: LLMProvider
    model: str
    messages: list[ChatMessage]
    thinking: ThinkingLevel = "off"
    max_tokens: int | None = None
    mode: Literal["generate", "polish"] = "generate"
    project_id: int = 0
    ai_config_id: int | None = None
    provider_name: str = ""
    target_words: int = 0
    auto_continue: bool = False
    params: dict[str, Any] = field(default_factory=dict)


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def stream_pieces(
    provider: LLMProvider,
    messages: list[ChatMessage],
    model: str,
    *,
    max_tokens: int | None,
    thinking: ThinkingLevel,
    pieces: list[str],
    prefix: str = "",
) -> AsyncIterator[Event]:
    """流式调用模型：每个分块追加到 pieces 并产出 delta；prefix 在首个分块前作为 delta 发出。"""
    stream_iter = provider.generate_stream(messages, model, max_tokens=max_tokens, thinking=thinking)
    async with aclosing(stream_iter) as stream:
        async for piece in stream:
            if prefix:
                yield "delta", {"text": prefix}
                prefix = ""
            pieces.append(piece)
            yield "delta", {"text": piece}


async def prepare_chapter_job(
    db: AsyncSession,
    *,
    user_id: int,
    chapter_id: int,
    mode: Literal["generate", "polish"],
    req: GenerateChapterRequest | PolishChapterRequest,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
    context_budget: int,
) -> ChapterJob:
    chapter = await get_owned_chapter(db, user_id, chapter_id)
    project = await get_owned_project(db, user_id, chapter.project_id)
    if chapter.status == "generating":
        raise ConflictError("该章节正在生成中，请稍候再试")

    cfg = await ai_config_service.resolve_config(db, user_id, req.ai_config_id)
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)

    if mode == "generate":
        assert isinstance(req, GenerateChapterRequest)
        outline = await find_chapter_outline(db, chapter)
        budget = ai_config_service.context_char_budget(cfg, context_budget)
        context = await build_chapter_context(db, project, chapter, budget)
        params: dict[str, Any] = {
            "target_words": req.target_words,
            "instruction": req.instruction,
            "auto_continue": req.auto_continue,
        }
        messages = build_chapter_messages(
            context=context,
            chapter_no=chapter.chapter_no,
            title=chapter.title or (outline.title if outline else ""),
            summary=outline.summary if outline else "",
            target_words=req.target_words,
            style=project.style,
            instruction=req.instruction,
        )
    else:
        if not chapter.content.strip():
            raise BadRequestError("章节正文为空，无法润色")
        messages = build_polish_messages(content=chapter.content, style=project.style, instruction=req.instruction)
        params = {"instruction": req.instruction}

    prev_status = chapter.status
    # 原子抢占：防止同一章节被并发生成
    result = await db.execute(
        update(Chapter).where(Chapter.id == chapter.id, Chapter.status != "generating").values(status="generating")
    )
    await db.commit()
    if result.rowcount != 1:
        raise ConflictError("该章节正在生成中，请稍候再试")
    return ChapterJob(
        chapter_id=chapter.id,
        prev_status=prev_status,
        provider=provider,
        model=cfg.model,
        messages=messages,
        thinking=cfg.thinking_level,  # type: ignore[arg-type]
        max_tokens=ai_config_service.max_output_tokens(cfg),
        mode=mode,
        project_id=project.id,
        ai_config_id=cfg.id,
        provider_name=cfg.provider,
        target_words=req.target_words if isinstance(req, GenerateChapterRequest) else 0,
        auto_continue=mode == "generate" and isinstance(req, GenerateChapterRequest) and req.auto_continue,
        params=params,
    )


async def _restore_status(sessionmaker: async_sessionmaker[AsyncSession], chapter_id: int, status: str) -> None:
    try:
        async with sessionmaker() as db:
            await db.execute(
                update(Chapter).where(Chapter.id == chapter_id, Chapter.status == "generating").values(status=status)
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("回退章节 %s 状态失败", chapter_id)


def _below_target(text: str, target_words: int) -> bool:
    return count_words(text) < CONTINUE_RATIO * target_words


async def run_chapter_job(job: ChapterJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    started = time.monotonic()
    finished = False
    # 未正常完成时写入生成记录的状态；没有被异常分支改写即视为客户端断开
    fail_status, fail_error = "cancelled", ""
    pieces: list[str] = []
    text = ""
    rounds = 0
    warning: str | None = None
    log_messages: list[ChatMessage] = list(job.messages)

    def log_kwargs(status: str, error: str, output_chars: int) -> dict[str, Any]:
        params = dict(job.params)
        if job.mode == "generate":
            params["continuation_rounds"] = rounds
        return {
            "project_id": job.project_id,
            "chapter_id": job.chapter_id,
            "kind": job.mode,
            "ai_config_id": job.ai_config_id,
            "provider": job.provider_name,
            "model": job.model,
            "params": params,
            "messages": log_messages,
            "status": status,
            "error": error,
            "output_chars": output_chars,
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

        while job.auto_continue and rounds < MAX_CONTINUE_ROUNDS and _below_target(text, job.target_words):
            rounds += 1
            words = count_words(text)
            yield "continue", {"round": rounds, "words": words, "target": job.target_words}
            messages = build_continuation_messages(
                messages=job.messages, generated=text, current_words=words, target_words=job.target_words
            )
            # 生成记录不重复存已生成正文，只留占位与续写指令
            log_messages += [
                {"role": "assistant", "content": f"（第 {rounds} 轮续写前已生成的正文，共 {words} 字，记录中省略）"},
                messages[-1],
            ]
            round_pieces: list[str] = []
            try:
                async with aclosing(
                    stream_pieces(
                        job.provider,
                        messages,
                        job.model,
                        max_tokens=job.max_tokens,
                        thinking=job.thinking,
                        pieces=round_pieces,
                        prefix="\n\n",
                    )
                ) as events:
                    async for event in events:
                        yield event
            except AIProviderError as exc:
                # 续写失败不影响已生成的正文；本轮的半截内容丢弃
                warning = f"自动续写失败：{exc}"
                break
            except Exception:  # noqa: BLE001
                logger.exception("章节 %s 自动续写失败", job.chapter_id)
                warning = "自动续写失败：服务器内部错误"
                break
            piece = "".join(round_pieces).strip()
            if not piece:
                break
            text = f"{text}\n\n{piece}"

        async with sessionmaker() as db:
            chapter = await db.get(Chapter, job.chapter_id)
            if chapter is None:
                fail_status, fail_error = "error", "章节已被删除，生成结果未保存"
                yield "error", {"detail": fail_error}
                return
            await version_service.ensure_baseline(db, chapter)
            chapter.content = text
            chapter.word_count = count_words(text)
            chapter.status = "done"
            log = await generation_log_service.write_log(db, **log_kwargs("success", warning or "", chapter.word_count))
            await version_service.record(db, chapter, job.mode, log_id=log.id)
            await db.commit()
            await db.refresh(chapter)
            out = ChapterOut.model_validate(chapter)
        finished = True
        payload: dict[str, Any] = {"chapter": out.model_dump(mode="json")}
        if warning:
            payload["warning"] = warning
        yield "done", payload
    except AIProviderError as exc:
        fail_status, fail_error = "error", str(exc)
        yield "error", {"detail": fail_error}
    except Exception:  # noqa: BLE001
        logger.exception("章节 %s 生成失败", job.chapter_id)
        fail_status, fail_error = "error", "生成失败：服务器内部错误"
        yield "error", {"detail": fail_error}
    finally:
        if not finished:
            # 客户端断开时处于取消作用域内，需屏蔽取消才能完成回退与记录
            with anyio.CancelScope(shield=True):
                await _restore_status(sessionmaker, job.chapter_id, job.prev_status)
                produced = text or "".join(pieces)
                await generation_log_service.save_log(
                    sessionmaker, **log_kwargs(fail_status, fail_error, count_words(produced))
                )
