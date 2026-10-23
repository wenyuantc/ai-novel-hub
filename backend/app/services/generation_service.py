"""章节 AI 生成 / 润色。

分两阶段：
- prepare_*：在请求会话内完成鉴权、配置解析、上下文组装，并原子地把章节置为 generating。
  任何业务错误（404/400/409）在此抛出，以普通 JSON 响应返回。
- run_chapter_job：流式阶段，使用独立会话落库；产出 (event, data) 事件。
  未正常完成（模型报错、客户端断开等）时把章节状态回退为原状态，正文保持不变。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Literal

import anyio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.prompts import build_chapter_messages, build_polish_messages
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.core.errors import BadRequestError, ConflictError
from app.core.security import SecretCipher
from app.models import Chapter
from app.schemas.chapter import ChapterOut, GenerateChapterRequest, PolishChapterRequest
from app.services import ai_config_service
from app.services.chapter_service import count_words, get_owned_chapter
from app.services.context_builder import build_chapter_context, find_chapter_outline
from app.services.project_service import get_owned_project

logger = logging.getLogger(__name__)

Event = tuple[Literal["delta", "done", "error"], dict]


@dataclass
class ChapterJob:
    chapter_id: int
    prev_status: str
    provider: LLMProvider
    model: str
    messages: list[ChatMessage]
    thinking: ThinkingLevel = "off"
    max_tokens: int | None = None


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


async def run_chapter_job(job: ChapterJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    finished = False
    pieces: list[str] = []
    try:
        stream_iter = job.provider.generate_stream(
            job.messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking
        )
        async with aclosing(stream_iter) as stream:
            async for piece in stream:
                pieces.append(piece)
                yield "delta", {"text": piece}
        text = "".join(pieces).strip()
        if not text:
            raise AIProviderError("模型未返回任何内容")

        async with sessionmaker() as db:
            chapter = await db.get(Chapter, job.chapter_id)
            if chapter is None:
                yield "error", {"detail": "章节已被删除，生成结果未保存"}
                return
            chapter.content = text
            chapter.word_count = count_words(text)
            chapter.status = "done"
            await db.commit()
            await db.refresh(chapter)
            out = ChapterOut.model_validate(chapter)
        finished = True
        yield "done", {"chapter": out.model_dump(mode="json")}
    except AIProviderError as exc:
        yield "error", {"detail": str(exc)}
    except Exception:  # noqa: BLE001
        logger.exception("章节 %s 生成失败", job.chapter_id)
        yield "error", {"detail": "生成失败：服务器内部错误"}
    finally:
        if not finished:
            # 客户端断开时处于取消作用域内，需屏蔽取消才能完成回退
            with anyio.CancelScope(shield=True):
                await _restore_status(sessionmaker, job.chapter_id, job.prev_status)
