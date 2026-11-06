"""章节 AI 生成 / 润色 / 按建议重写（作为后台任务 chapter.generate / chapter.polish / chapter.revise 运行）。

分三段：
- validate_and_claim：创建任务的请求内同步执行鉴权、配置解析、SSRF 与参数校验，并原子地把章节置为 generating。
  任何业务错误（404/400/409）在此抛出，以普通 JSON 响应返回。
- build_chapter_job：任务开始运行时按最新数据组装 provider、上下文与 messages。
- run_chapter_job：流式阶段，使用独立会话落库；产出 (event, data) 事件，由任务引擎消费。
  成功时在同一事务内：ensure_baseline → 覆盖正文 → 写生成记录 → 写 generate/polish/revise 版本。
  未正常完成（模型报错、客户端断开等）时把章节状态回退为原状态，正文与版本不变，只留一条失败/取消记录。
- 整章生成可选自动续写：字数低于目标 CONTINUE_RATIO 时最多续写 MAX_CONTINUE_ROUNDS 轮，
  每轮开始前产出 continue 事件；续写轮报错时保留已生成正文照常落库，done 附带 warning。
- 按建议重写（revise）：原文全文 + 勾选的分析建议 + 补充要求 + 需保留的要素 + 与整章生成相同的上下文；
  analysis_id 须属于本章、suggestion_ids 须是该次分析里的建议；不自动续写；版本来源 / 生成记录类型均为 revise。
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.prompts import (
    build_chapter_messages,
    build_continuation_messages,
    build_polish_messages,
    build_revise_messages,
)
from app.ai.prompts.registry import prompt_meta
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.ai.templating import PromptOverride, TemplateRenderError
from app.core.errors import BadRequestError, ConflictError
from app.core.security import SecretCipher
from app.models import Chapter, ChapterAnalysis, Task
from app.models.entities import ACTIVE_TASK_STATUSES
from app.schemas.chapter import ChapterOut, GenerateChapterRequest, PolishChapterRequest, ReviseChapterRequest
from app.services import (
    ai_config_service,
    generation_log_service,
    planning_context_service,
    prompt_service,
    retrieval_service,
    skill_service,
    style_service,
    version_service,
)
from app.services.ai_dispatch import guard_provider, visibility_sessionmaker
from app.services.chapter_service import count_words, get_owned_chapter
from app.services.context_builder import build_chapter_context, chapter_task
from app.services.project_service import get_owned_project

logger = logging.getLogger(__name__)

Event = tuple[Literal["delta", "continue", "done", "error"], dict]
Mode = Literal["generate", "polish", "revise"]
ChapterRequest = GenerateChapterRequest | PolishChapterRequest | ReviseChapterRequest

GENERATING_CONFLICT = "该章节正在生成中，请稍候再试"
BATCH_KIND = "chapter.batch"
BATCH_RANGE_CONFLICT = "该章节在批量生成范围内，请等待批量完成或暂停后再操作"

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
    mode: Mode = "generate"
    project_id: int = 0
    ai_config_id: int | None = None
    provider_name: str = ""
    target_words: int = 0
    auto_continue: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    task_id: int | None = None
    # 自动续写指令的用户覆盖（组装 job 时取出）
    continue_override: PromptOverride | None = None


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


@dataclass
class ChapterClaim:
    """创建任务时的校验 + 抢占结果（可序列化进 tasks.input）。"""

    chapter_id: int
    project_id: int
    prev_status: str
    ai_config_id: int
    chapter_no: int
    chapter_title: str
    # 润色 / 重写时原文字数（用于估算进度）
    source_words: int = 0
    # 选用的 Skill 快照 {id, name, body}（存进 tasks.input.skill；重试沿用）
    skill: dict[str, Any] | None = None


async def validate_and_claim(
    db: AsyncSession,
    *,
    user_id: int,
    chapter_id: int,
    mode: Mode,
    req: ChapterRequest,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
    skill_snapshot: dict[str, Any] | None = None,
) -> ChapterClaim:
    """鉴权、配置解析、SSRF 校验与参数校验，并原子地把章节置为 generating。

    不 commit：由调用方与任务插入放在同一事务内提交（插入失败时抢占随回滚撤销）。
    """
    chapter = await get_owned_chapter(db, user_id, chapter_id, lock=True)
    project = await get_owned_project(db, user_id, chapter.project_id, lock=True)
    if chapter.status == "generating":
        raise ConflictError(GENERATING_CONFLICT)
    await ensure_outside_batch(db, chapter)

    cfg = await ai_config_service.resolve_config(db, user_id, req.ai_config_id)
    # 只做校验（解密 + 端点防护），运行时再按最新配置重新构建
    await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    await style_service.validate_choice(db, user_id, req.style)
    # 重试沿用原任务的快照（Skill 之后被修改或删除都不影响）
    skill = skill_snapshot or await skill_service.snapshot(db, user_id, req.skill_id)
    if mode == "polish" and not chapter.content.strip():
        raise BadRequestError("章节正文为空，无法润色")
    if mode == "revise":
        assert isinstance(req, ReviseChapterRequest)
        await resolve_revision(db, chapter, req)

    prev_status = await claim_chapter(db, chapter)
    return ChapterClaim(
        chapter_id=chapter.id,
        project_id=project.id,
        prev_status=prev_status,
        ai_config_id=cfg.id,
        chapter_no=chapter.chapter_no,
        chapter_title=chapter.title,
        source_words=chapter.word_count if mode in ("polish", "revise") else 0,
        skill=skill,
    )


async def claim_chapter(db: AsyncSession, chapter: Chapter) -> str:
    """原子地把章节置为 generating（防止同一章节被并发生成），返回抢占前的状态。

    不 commit（由调用方与后续写入同一事务提交）；已在生成中 → 回滚并 409。
    """
    prev_status = chapter.status
    result = await db.execute(
        update(Chapter).where(Chapter.id == chapter.id, Chapter.status != "generating").values(status="generating")
    )
    if result.rowcount != 1:
        await db.rollback()
        raise ConflictError(GENERATING_CONFLICT)
    return prev_status


async def ensure_outside_batch(db: AsyncSession, chapter: Chapter) -> None:
    """范围锁：同项目排队中 / 运行中的批量生成尚未处理到本章（chapter_ids[step_cursor:]）→ 409。

    读后判断、尽力而为；并发竞争由章节 generating 的原子抢占兜底。待审核（paused）的批量不锁范围。
    """
    rows = await db.execute(
        select(Task.input, Task.step_cursor).where(
            Task.project_id == chapter.project_id,
            Task.kind == BATCH_KIND,
            Task.status.in_(ACTIVE_TASK_STATUSES),
        )
    )
    for task_input, cursor in rows.all():
        ids = (task_input or {}).get("chapter_ids") or []
        if chapter.id in ids[cursor:]:
            raise ConflictError(BATCH_RANGE_CONFLICT)


async def resolve_revision(db: AsyncSession, chapter: Chapter, req: ReviseChapterRequest) -> list[str]:
    """校验重写请求并返回勾选建议的文本（按勾选顺序）。"""
    if not chapter.content.strip():
        raise BadRequestError("章节正文为空，无法重写")
    analysis = await db.scalar(
        select(ChapterAnalysis).where(ChapterAnalysis.id == req.analysis_id, ChapterAnalysis.chapter_id == chapter.id)
    )
    if analysis is None:
        raise BadRequestError("分析结果不存在或不属于该章节")
    by_id = {s.get("id"): s for s in analysis.suggestions or [] if isinstance(s, dict)}
    ids = list(dict.fromkeys(req.suggestion_ids))
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise BadRequestError(f"建议不存在：{'、'.join(str(i) for i in missing)}")
    if not ids and not req.instruction.strip():
        raise BadRequestError("请至少选择一条建议或填写修改要求")
    return [str(by_id[i].get("text") or "") for i in ids]


async def build_chapter_job(
    db: AsyncSession,
    *,
    user_id: int,
    chapter_id: int,
    mode: Mode,
    req: ChapterRequest,
    prev_status: str,
    ai_config_id: int | None,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
    context_budget: int,
    task_id: int | None = None,
    skill: dict[str, Any] | None = None,
) -> ChapterJob:
    """任务开始运行时按最新数据组装 provider、上下文与 messages；章节须仍处于 generating。"""
    chapter = await get_owned_chapter(db, user_id, chapter_id)
    project = await get_owned_project(db, user_id, chapter.project_id)
    if chapter.status != "generating":
        raise ConflictError("章节生成状态已被复位，任务未执行")
    cfg = await ai_config_service.resolve_config(db, user_id, ai_config_id)
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    # 风格：临时指定 > 章节覆盖 > 项目默认 > 不用风格；风格段由 style.block 模板渲染
    resolved = await style_service.resolve(db, user_id, project, chapter, req.style)
    style = resolved.block if resolved is not None else ""
    # Skill 快照（创建任务时写入 tasks.input.skill）作为补充工作流
    skill_guide = str((skill or {}).get("body") or "")
    template_key = {"generate": "chapter.generate", "revise": "chapter.revise"}.get(mode, "chapter.polish")
    override = await prompt_service.get_override(db, user_id, template_key)
    continue_override = (
        await prompt_service.get_override(db, user_id, "chapter.continue") if mode == "generate" else None
    )
    # 知识库 / 剧情记忆检索用的向量模型（未配置或不可用时为 None，只用关键词检索）
    embedder = (
        await retrieval_service.get_embedder(
            db, user_id, cipher=cipher, factory=factory, ai_timeout=ai_timeout, allow_private=allow_private
        )
        if mode in ("generate", "revise")
        else None
    )

    if mode == "generate":
        assert isinstance(req, GenerateChapterRequest)
        task = await chapter_task(db, chapter)
        budget = ai_config_service.context_char_budget(cfg, context_budget)
        planning = await planning_context_service.load_planning_context(
            db, project, "chapter", chapter.id, budget=budget, text_hint=req.instruction,
        )
        context = await build_chapter_context(
            db, project, chapter, budget - planning.reserved_chars, embedder=embedder, planning=planning
        )
        params: dict[str, Any] = {
            "target_words": req.target_words,
            "instruction": req.instruction,
            "auto_continue": req.auto_continue,
            "planning": planning.log_data("chapter"),
        }
        messages = build_chapter_messages(
            context=context,
            chapter_no=chapter.chapter_no,
            title=chapter.title or task.title,
            summary=task.summary,
            target_words=req.target_words,
            style=style,
            instruction=req.instruction,
            key_events=task.key_events,
            goal=task.goal,
            emotion=task.emotion,
            skill_guide=skill_guide,
            planning_context=planning.text,
            override=override,
        )
    elif mode == "revise":
        assert isinstance(req, ReviseChapterRequest)
        suggestions = await resolve_revision(db, chapter, req)
        task = await chapter_task(db, chapter)
        budget = ai_config_service.context_char_budget(cfg, context_budget)
        planning = await planning_context_service.load_planning_context(
            db, project, "chapter", chapter.id, budget=budget,
            text_hint="\n".join((chapter.content, req.instruction)),
        )
        context = await build_chapter_context(
            db, project, chapter, budget - planning.reserved_chars, embedder=embedder, planning=planning
        )
        target_words = req.target_words or max(chapter.word_count, 300)
        params = {
            "analysis_id": req.analysis_id,
            "suggestion_ids": req.suggestion_ids,
            "instruction": req.instruction,
            "preserve": req.preserve,
            "target_words": target_words,
            "planning": planning.log_data("chapter"),
        }
        messages = build_revise_messages(
            context=context,
            chapter_no=chapter.chapter_no,
            title=chapter.title or task.title,
            content=chapter.content,
            suggestions=suggestions,
            target_words=target_words,
            style=style,
            instruction=req.instruction,
            preserve=req.preserve,
            skill_guide=skill_guide,
            planning_context=planning.text,
            override=override,
        )
    else:
        if not chapter.content.strip():
            raise BadRequestError("章节正文为空，无法润色")
        messages = build_polish_messages(
            content=chapter.content,
            style=style,
            instruction=req.instruction,
            skill_guide=skill_guide,
            override=override,
        )
        params = {"instruction": req.instruction}
    # 生成记录：提示词来源与所用风格（渲染后的风格段）
    params["prompt"] = prompt_meta(template_key, override)
    if mode == "generate":
        params["continue_prompt"] = prompt_meta("chapter.continue", continue_override)
    params["style_choice"] = req.style
    params["style"] = style_service.style_log(resolved)
    params["skill"] = skill_service.skill_log(skill)
    if mode in ("generate", "revise"):
        assert planning.setting_scope is not None
        await planning.setting_scope.scope.verify(db)
        provider = guard_provider(provider, planning.setting_scope.scope.stamp, visibility_sessionmaker(db))

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
        task_id=task_id,
        continue_override=continue_override,
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


async def _save_chapter_result(
    job: ChapterJob,
    sessionmaker: async_sessionmaker[AsyncSession],
    text: str,
    log_values: dict[str, Any],
) -> ChapterOut | None:
    """只保护有界的原子保存；取消必须等连接确定提交或回滚后才能清理。

    不能在执行中的 INSERT ... RETURNING 上直接取消：SQLite 关闭仍有活游标的
    连接后可能继续持写锁，随后独立的回退 / 终态事务将无法落库。
    提交前收到取消则回滚；提交已开始则以保存结果为准，不声称撤销已提交正文。
    """
    cancelled = asyncio.Event()

    async def save() -> ChapterOut | None:
        async with sessionmaker() as db:
            chapter = await db.get(Chapter, job.chapter_id)
            if chapter is None:
                return None
            await version_service.ensure_baseline(db, chapter)
            chapter.content = text
            chapter.word_count = count_words(text)
            chapter.status = "done"
            log = await generation_log_service.write_log(db, **log_values)
            await version_service.record(db, chapter, job.mode, log_id=log.id)
            if cancelled.is_set():
                # 不取消任何进行中的数据库await，让会话退出完整回滚、关闭游标。
                raise asyncio.CancelledError
            await db.commit()
            await db.refresh(chapter)
            return ChapterOut.model_validate(chapter)

    saving = asyncio.create_task(save())
    try:
        return await asyncio.shield(saving)
    except asyncio.CancelledError:
        cancelled.set()
        out = await saving
        # 已进入提交的极晚取消：保存获胜，继续产出done供引擎记录唯一成功终态。
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        return out


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
            "task_id": job.task_id,
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
            try:
                messages = build_continuation_messages(
                    messages=job.messages,
                    generated=text,
                    current_words=words,
                    target_words=job.target_words,
                    override=job.continue_override,
                )
            except TemplateRenderError as exc:
                # 续写指令的用户覆盖渲染失败：与续写轮报错同样处理，已生成正文照常落库
                warning = f"自动续写失败：{exc.detail}"
                break
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

        out = await _save_chapter_result(
            job, sessionmaker, text, log_kwargs("success", warning or "", count_words(text))
        )
        if out is None:
            fail_status, fail_error = "error", "章节已被删除，生成结果未保存"
            yield "error", {"detail": fail_error}
            return
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
