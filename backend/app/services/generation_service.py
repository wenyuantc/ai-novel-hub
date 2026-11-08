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
from uuid import uuid4

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
from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.core.security import SecretCipher
from app.models import Chapter, ChapterAnalysis, Task
from app.models.entities import ACTIVE_TASK_STATUSES
from app.schemas.chapter import ChapterOut, GenerateChapterRequest, PolishChapterRequest, ReviseChapterRequest
from app.schemas.scene_ai import ChapterTarget, SceneTarget
from app.services import (
    ai_config_service,
    generation_log_service,
    planning_context_service,
    prompt_service,
    retrieval_service,
    scene_ai_context,
    scene_service,
    setting_ai_visibility,
    skill_service,
    style_service,
    version_service,
)
from app.services.ai_dispatch import guard_provider, visibility_sessionmaker
from app.services.chapter_service import count_words, get_owned_chapter
from app.services.context_builder import build_chapter_context, build_setting_context, chapter_task
from app.services.project_service import get_owned_project

logger = logging.getLogger(__name__)

Event = tuple[Literal["delta", "continue", "done", "error", "stage", "paused"], dict]
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
    task_key: str | None = None
    run_key: str = field(default_factory=lambda: str(uuid4()))
    # 自动续写指令的用户覆盖（组装 job 时取出）
    continue_override: PromptOverride | None = None
    user_id: int = 0
    target: ChapterTarget | SceneTarget = field(default_factory=ChapterTarget)
    scenes: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    context_stamp: scene_ai_context.SceneContextStamp | None = None
    request: ChapterRequest | None = None
    raw_provider: LLMProvider | None = None
    context_budget: int = 4000
    override: PromptOverride | None = None
    style: str = ""
    skill_guide: str = ""
    embedder: Any = None
    suggestions: list[str] = field(default_factory=list)


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
    target: ChapterTarget | SceneTarget | None = None,
    project_id: int | None = None,
) -> ChapterClaim:
    """鉴权、配置解析、SSRF 校验与参数校验，并原子地把章节置为 generating。

    不 commit：由调用方与任务插入放在同一事务内提交（插入失败时抢占随回滚撤销）。
    """
    chapter = await get_owned_chapter(db, user_id, chapter_id, lock=True)
    project = await get_owned_project(db, user_id, chapter.project_id, lock=True)
    await db.refresh(chapter)
    if project_id is not None and project.id != project_id:
        raise NotFoundError("章节不存在")
    target = target or ChapterTarget()
    scenes = await scene_service.list_active_scenes(db, chapter.id)
    selected = ([await scene_service.require_scene(db, chapter, target.scene_id)]
                if isinstance(target, SceneTarget) else scenes)
    await scene_ai_context.check_revisions(
        db, chapter, req.expected_content_revision, req.expected_planning_revision,
    )
    await scene_service.capture_snapshot(db, chapter)
    if chapter.status == "generating":
        raise ConflictError(GENERATING_CONFLICT)
    await ensure_outside_batch(db, chapter)

    cfg = await ai_config_service.resolve_config(db, user_id, req.ai_config_id)
    # 只做校验（解密 + 端点防护），运行时再按最新配置重新构建
    await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    await style_service.validate_choice(db, user_id, req.style)
    # 重试沿用原任务的快照（Skill 之后被修改或删除都不影响）
    skill = skill_snapshot or await skill_service.snapshot(db, user_id, req.skill_id)
    if mode == "generate" and isinstance(req, GenerateChapterRequest) and req.target_words < len(selected):
        raise BadRequestError("整章目标字数不足以为每个场景分配正字数，请增大目标字数")
    if mode == "polish" and not any(scene.content.strip() for scene in selected):
        raise BadRequestError("章节正文为空，无法润色")
    if mode == "revise":
        assert isinstance(req, ReviseChapterRequest)
        await resolve_revision(db, chapter, req)
        if not any(scene.content.strip() for scene in selected):
            raise BadRequestError("场景正文为空，无法重写")

    prev_status = await claim_chapter(db, chapter)
    return ChapterClaim(
        chapter_id=chapter.id,
        project_id=project.id,
        prev_status=prev_status,
        ai_config_id=cfg.id,
        chapter_no=chapter.chapter_no,
        chapter_title=chapter.title,
        source_words=sum(scene.word_count for scene in selected) if mode in ("polish", "revise") else 0,
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
    task_key: str | None = None,
    skill: dict[str, Any] | None = None,
    target: ChapterTarget | SceneTarget | None = None,
) -> ChapterJob:
    """按稳定 Scene 身份组装第一步；后续步骤只能使用暂存的最新前景。"""
    chapter = await get_owned_chapter(db, user_id, chapter_id)
    project = await get_owned_project(db, user_id, chapter.project_id)
    if chapter.status != "generating":
        raise ConflictError("章节生成状态已被复位，任务未执行")
    await scene_ai_context.check_revisions(db, chapter, req.expected_content_revision, req.expected_planning_revision)
    stamp = await scene_ai_context.read_stamp(db, project.id, chapter.id)
    snapshot = await scene_service.capture_snapshot(db, chapter)
    scenes = snapshot["scenes"]
    target = target or ChapterTarget()
    if isinstance(target, SceneTarget):
        await scene_service.require_scene(db, chapter, target.scene_id)
    selected = [dict(scene) for scene in scenes
                if not isinstance(target, SceneTarget) or scene["id"] == target.scene_id]
    steps = [scene for scene in selected if mode == "generate" or scene["content"].strip()]
    if not steps:
        raise BadRequestError("章节正文为空，无法润色" if mode == "polish" else "章节正文为空，无法重写")
    if mode == "generate" or (mode == "revise" and req.target_words is not None):
        total_words = req.target_words
        if total_words < len(steps):
            raise BadRequestError("整章目标字数不足以为每个场景分配正字数，请增大目标字数")
        quotient, remainder = divmod(total_words, len(steps))
        for index, step in enumerate(steps):
            step["target_words"] = quotient + (index < remainder)
    else:
        legacy_revise_floor = (mode == "revise" and isinstance(target, ChapterTarget) and len(scenes) == 1)
        for step in steps:
            step["target_words"] = max(count_words(step["content"]), 300 if legacy_revise_floor else 1)
    cfg = await ai_config_service.resolve_config(db, user_id, ai_config_id)
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    resolved = await style_service.resolve(db, user_id, project, chapter, req.style)
    template_key = f"chapter.{mode}"
    override = await prompt_service.get_override(db, user_id, template_key)
    continue_override = (await prompt_service.get_override(db, user_id, "chapter.continue")
                         if mode == "generate" else None)
    embedder = (await retrieval_service.get_embedder(
        db, user_id, cipher=cipher, factory=factory, ai_timeout=ai_timeout, allow_private=allow_private,
    ) if mode in ("generate", "revise") else None)
    suggestions = await resolve_revision(db, chapter, req) if isinstance(req, ReviseChapterRequest) else []
    params = {
        "instruction": req.instruction, "target": target.model_dump(),
        "context_snapshot": stamp.log_data(), "prompt": prompt_meta(template_key, override),
        "style_choice": req.style, "style": style_service.style_log(resolved), "skill": skill_service.skill_log(skill),
        "scenes": [],
        "target_words": sum(step["target_words"] for step in steps) if mode in ("generate", "revise") else None,
    }
    if isinstance(req, GenerateChapterRequest):
        params.update(auto_continue=req.auto_continue,
                      continue_prompt=prompt_meta("chapter.continue", continue_override))
    if isinstance(req, ReviseChapterRequest):
        params.update(analysis_id=req.analysis_id, suggestion_ids=req.suggestion_ids, preserve=req.preserve)
    job = ChapterJob(
        chapter_id=chapter.id, prev_status=prev_status, provider=provider, raw_provider=provider, model=cfg.model,
        messages=[], thinking=cfg.thinking_level, max_tokens=ai_config_service.max_output_tokens(cfg), mode=mode,
        project_id=project.id, ai_config_id=cfg.id, provider_name=cfg.provider,
        target_words=steps[0]["target_words"],
        auto_continue=isinstance(req, GenerateChapterRequest) and req.auto_continue,
        params=params, task_id=task_id, task_key=task_key, continue_override=continue_override, user_id=user_id,
        target=target, scenes=scenes, steps=steps, context_stamp=stamp, request=req,
        context_budget=ai_config_service.context_char_budget(cfg, context_budget), override=override,
        style=resolved.block if resolved is not None else "", skill_guide=str((skill or {}).get("body") or ""),
        embedder=embedder, suggestions=suggestions,
    )
    previous = "".join(scene["separator_before"] + scene["content"] for scene in scenes
                       if scene["sort"] < steps[0]["sort"])
    await _prepare_scene_step(db, job, steps[0], previous)
    return job


async def _prepare_scene_step(db: AsyncSession, job: ChapterJob, step: dict[str, Any], preceding: str) -> None:
    """只读重建当前步骤，绝不暂写 Scene 或混入数据库中的旧前场景。"""
    from app.services.rich_document_ai import scene_reference_context

    assert job.context_stamp is not None and job.request is not None and job.raw_provider is not None
    await scene_ai_context.require_stamp(db, job.context_stamp)
    chapter = await get_owned_chapter(db, job.user_id, job.chapter_id)
    project = await get_owned_project(db, job.user_id, job.project_id)
    req = job.request
    index = next(index for index, scene in enumerate(job.scenes, 1) if scene["id"] == step["id"])
    # 范围块也消耗同一上下文预算；大摘要和衔接明确截断而不是无限追加。
    budget = job.context_budget
    clip = max(0, min(1500, budget // 8))
    def clipped(text: str) -> str:
        return text if len(text) <= clip else text[:clip] + "……[已截断]"
    preceding_tail = preceding[-clip:] if clip else ""
    if len(preceding) > clip:
        preceding_tail = "[前文已截断]……" + preceding_tail
    scene_context = scene_ai_context.scope_block(
        scene_id=step["id"], title=step["title"], summary=clipped(step["summary"]), index=index,
        total=len(job.scenes), target_words=step["target_words"], preceding_text=preceding_tail,
    )
    author_budget = max(0, min(3_000, budget // 4, budget - len(scene_context) - 2))
    author_context = await scene_reference_context(
        db, project.id, [(scene["id"], scene["sort"]) for scene in job.scenes if scene["sort"] <= step["sort"]],
        author_budget,
    )
    if author_context:
        scene_context += "\n\n" + author_context
    remaining = budget - len(scene_context) - 2
    planning = await planning_context_service.load_planning_context(
        db, project, "scene", step["id"], budget=remaining,
        text_hint="\n".join((step["content"], req.instruction, author_context)), stamp=job.context_stamp.visibility,
        preceding_text=preceding if index > 1 else None,
    )
    task = await chapter_task(db, chapter)
    available = remaining - planning.reserved_chars
    common = dict(style=job.style, instruction=req.instruction, skill_guide=job.skill_guide,
                  planning_context=planning.text, scene_context=scene_context, override=job.override)
    if job.mode == "polish":
        setting_context = await build_setting_context(db, project, chapter, budget=available, planning=planning)
        messages = build_polish_messages(content=step["content"], setting_context=setting_context, **common)
    else:
        context = await build_chapter_context(db, project, chapter, available, embedder=job.embedder, planning=planning)
        if job.mode == "generate":
            messages = build_chapter_messages(
                context=context, chapter_no=chapter.chapter_no, title=chapter.title or task.title,
                summary=task.summary, target_words=step["target_words"], key_events=task.key_events,
                goal=task.goal, emotion=task.emotion, **common,
            )
        else:
            assert isinstance(req, ReviseChapterRequest)
            messages = build_revise_messages(
                context=context, chapter_no=chapter.chapter_no, title=chapter.title or task.title,
                content=step["content"], suggestions=job.suggestions, preserve=req.preserve,
                target_words=step["target_words"], **common,
            )
    assert planning.setting_scope is not None
    await planning.setting_scope.scope.verify(db)
    await scene_ai_context.require_stamp(db, job.context_stamp)
    job.messages = messages
    job.provider = guard_provider(
        job.raw_provider, job.context_stamp.visibility, visibility_sessionmaker(db),
        verify=lambda session: scene_ai_context.require_stamp(session, job.context_stamp, sending=True),
    )
    job.target_words = step["target_words"]
    data = {"scene_id": step["id"], "target_words": step["target_words"], "planning": planning.log_data("chapter")}
    data.update(scope_truncated=len(preceding) > clip or len(step["summary"]) > clip, scope_chars=len(scene_context))
    job.params["scenes"].append(data)
    if len(job.params["scenes"]) == 1:
        job.params["planning"] = data["planning"]


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
    contents: dict[int, str],
    log_values: dict[str, Any],
) -> dict[str, Any] | None:
    """当前章所有提案在短事务一次 CAS 保存；极晚取消以实际 commit 为准。"""
    cancelled = asyncio.Event()

    async def save() -> dict[str, Any] | None:
        async with sessionmaker() as db:
            await setting_ai_visibility.lock_author_visibility_write(db, job.user_id, job.project_id, manage=False)
            try:
                chapter = await get_owned_chapter(db, job.user_id, job.chapter_id, lock=True)
            except NotFoundError:
                return None
            await db.refresh(chapter)
            if chapter.status != "generating":
                raise ConflictError("章节生成状态已被复位，生成结果未保存")
            assert job.context_stamp is not None
            await scene_ai_context.require_stamp(db, job.context_stamp)
            scenes = await scene_service.list_active_scenes(db, chapter.id)
            if set(contents) != {scene.id for scene in scenes}:
                raise ConflictError("场景结构已变化，请重新发起操作", extra={"code": "scene_context_changed"})
            rich_ids = await scene_service._rich_scene_ids(db, [scene.id for scene in scenes])
            plans = None
            if rich_ids:
                from app.services.rich_document_proposal_mapping import build_scene_proposal
                from app.services.rich_document_store import capture_documents

                documents, _ = await capture_documents(db, scenes)
                plans = {scene.id: build_scene_proposal(documents[scene.id], contents[scene.id]) for scene in scenes}
                if any(plan.requires_review for plan in plans.values()):
                    from app.services.rich_document_review import persist_pending

                    review = await persist_pending(db, chapter, job, plans, log_values)
                    if cancelled.is_set():
                        raise asyncio.CancelledError
                    await db.commit()
                    return {"rich_review": review}
            await version_service.ensure_baseline(db, chapter)
            if plans is not None:
                from app.services.rich_document_proposal_mapping import resolve_scene_proposal
                from app.services.rich_document_service import write_mapped_document

                for current in scenes:
                    plan = plans[current.id]
                    if current.content == contents[current.id]:
                        continue
                    if current.id in rich_ids:
                        mapped = resolve_scene_proposal(plan, {}, confirm_format_loss=False)
                        await write_mapped_document(db, job.project_id, current, plan.original, mapped,
                                                    job.context_stamp.content_revision)
                    else:
                        current.content = contents[current.id]
                await scene_service.refresh_projection(
                    db, chapter, expected_revision=job.context_stamp.content_revision,
                )
            elif isinstance(job.target, SceneTarget):
                scene = await scene_service.require_scene(db, chapter, job.target.scene_id)
                await scene_service.replace_scene_content(
                    db, chapter, scene, contents[scene.id], expected_revision=job.context_stamp.content_revision,
                )
            else:
                await scene_service.replace_chapter_contents(
                    db, chapter, contents, expected_revision=job.context_stamp.content_revision,
                )
            chapter.status = "done"
            log = await generation_log_service.write_log(db, **log_values)
            await version_service.record(db, chapter, job.mode, log_id=log.id)
            await db.flush()
            await db.refresh(chapter)
            payload: dict[str, Any] = {"chapter": ChapterOut.model_validate(chapter).model_dump(mode="json")}
            if isinstance(job.target, SceneTarget):
                scene = await scene_service.require_scene(db, chapter, job.target.scene_id)
                payload["scene"] = (await scene_service.scene_out(db, chapter, scene)).model_dump(mode="json")
            else:
                payload["scene_ids"] = [scene["id"] for scene in job.scenes]
            if cancelled.is_set():
                raise asyncio.CancelledError
            await db.commit()
            return payload

    saving = asyncio.create_task(save())
    try:
        return await asyncio.shield(saving)
    except asyncio.CancelledError:
        cancelled.set()
        out = await saving
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        return out


async def run_chapter_job(job: ChapterJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    started = time.monotonic()
    finished = False
    fail_status, fail_error = "cancelled", ""
    produced: list[str] = []
    rounds = 0
    warnings: list[str] = []
    log_messages: list[ChatMessage] = []
    contents = {scene["id"]: scene["content"] for scene in job.scenes}
    by_step = {step["id"]: step for step in job.steps}

    def log_kwargs(status: str, error: str, output_chars: int) -> dict[str, Any]:
        params = dict(job.params)
        if job.mode == "generate":
            params["continuation_rounds"] = rounds
        return {
            "project_id": job.project_id, "chapter_id": job.chapter_id, "kind": job.mode,
            "ai_config_id": job.ai_config_id, "provider": job.provider_name, "model": job.model,
            "params": params, "messages": log_messages or job.messages, "status": status, "error": error,
            "output_chars": output_chars, "duration_ms": elapsed_ms(started), "task_id": job.task_id,
        }

    try:
        previous = ""
        for index, scene in enumerate(job.scenes, 1):
            step = by_step.get(scene["id"])
            if isinstance(job.target, SceneTarget) and step is None:
                if scene["sort"] < job.steps[0]["sort"]:
                    previous += scene["separator_before"] + scene["content"]
                continue
            if not isinstance(job.target, SceneTarget) and scene["separator_before"]:
                produced.append(scene["separator_before"])
                yield "delta", {"text": scene["separator_before"]}
            if step is None:
                if scene["content"]:
                    produced.append(scene["content"])
                    yield "delta", {"text": scene["content"]}
                previous += scene["separator_before"] + scene["content"]
                continue
            if len(job.scenes) > 1 or isinstance(job.target, SceneTarget):
                label = {"generate": "生成", "polish": "润色", "revise": "重写"}[job.mode]
                yield "stage", {"stage": f"场景 {index}/{len(job.scenes)} · {label} · 目标 {step['target_words']} 字"}
            if step is not job.steps[0]:
                async with sessionmaker() as db:
                    await _prepare_scene_step(db, job, step, previous)
            # messages 已绑定同一个快照，provider 的实际发送门禁也再次检查。
            log_messages.extend(job.messages)
            pieces: list[str] = []
            async with aclosing(stream_pieces(
                job.provider, job.messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking, pieces=pieces,
            )) as events:
                async for event, data in events:
                    produced.append(data["text"])
                    yield event, data
            text = "".join(pieces).strip()
            if not text:
                raise AIProviderError("模型未返回任何内容")
            step_rounds = 0
            while job.auto_continue and step_rounds < MAX_CONTINUE_ROUNDS and _below_target(text, step["target_words"]):
                step_rounds += 1
                rounds += 1
                words = count_words(text)
                yield "continue", {"round": step_rounds, "words": words, "target": step["target_words"]}
                try:
                    messages = build_continuation_messages(
                        messages=job.messages, generated=text, current_words=words,
                        target_words=step["target_words"], override=job.continue_override,
                    )
                except TemplateRenderError as exc:
                    warnings.append(f"自动续写失败：{exc.detail}")
                    break
                log_messages += [{"role": "assistant", "content": f"（续写前已有 {words} 字，正文省略）"}, messages[-1]]
                continuation: list[str] = []
                try:
                    async with aclosing(stream_pieces(
                        job.provider, messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking,
                        pieces=continuation, prefix="\n\n",
                    )) as events:
                        async for event, data in events:
                            produced.append(data["text"])
                            yield event, data
                except AppError:
                    # 过期上下文/策略错误不是可容忍的额外续写失败，不能采纳旧首轮结果。
                    raise
                except AIProviderError as exc:
                    warnings.append(f"自动续写失败：{exc}")
                    break
                except Exception:
                    logger.exception("章节 %s 自动续写失败", job.chapter_id)
                    warnings.append("自动续写失败：服务器内部错误")
                    break
                piece = "".join(continuation).strip()
                if not piece:
                    break
                text = f"{text}\n\n{piece}"
            contents[step["id"]] = text
            previous += scene["separator_before"] + text
        final_text = (contents[job.target.scene_id] if isinstance(job.target, SceneTarget) else
                      "".join(scene["separator_before"] + contents[scene["id"]] for scene in job.scenes))
        warning = "；".join(warnings)
        out = await _save_chapter_result(job, sessionmaker, contents,
                                         log_kwargs("success", warning, count_words(final_text)))
        if out is None:
            raise NotFoundError("章节已被删除，生成结果未保存")
        finished = True
        if "rich_review" in out:
            yield "paused", out["rich_review"]
            return
        if warning:
            out["warning"] = warning
        yield "done", out
    except AppError as exc:
        fail_status, fail_error = "error", exc.detail
        yield "error", {"detail": exc.detail, **(exc.extra or {}), "retryable": False}
    except AIProviderError as exc:
        fail_status, fail_error = "error", str(exc)
        yield "error", {"detail": fail_error}
    except Exception:
        logger.exception("章节 %s 生成失败", job.chapter_id)
        fail_status, fail_error = "error", "生成失败：服务器内部错误"
        yield "error", {"detail": fail_error}
    finally:
        if not finished:
            with anyio.CancelScope(shield=True):
                await _restore_status(sessionmaker, job.chapter_id, job.prev_status)
                await generation_log_service.save_log(
                    sessionmaker, **log_kwargs(fail_status, fail_error, count_words("".join(produced))),
                )
