"""关闭区块的明确选区单次出站；普通上下文仍沿用既有可见性与预算门禁。"""

import asyncio
import json
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts import build_rewrite_messages
from app.ai.prompts.registry import prompt_meta
from app.ai.providers import ProviderFactory
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.security import SecretCipher
from app.models import GenerationLog, SceneSelectionReceipt
from app.schemas.rich_document import SceneDocument, SceneDocumentOut
from app.schemas.scene_ai import SectionRewriteApply, SectionRewriteRequest
from app.services import (
    ai_config_service,
    authorization_service,
    context_builder,
    planning_context_service,
    prompt_service,
    scene_ai_context,
    scene_service,
    setting_ai_visibility,
    skill_service,
    style_service,
    version_service,
)
from app.services.ai_dispatch import VisibilityPolicyError, guard_provider, visibility_sessionmaker
from app.services.chapter_service import commit_versioned, get_owned_chapter
from app.services.generation_service import ensure_outside_batch
from app.services.project_service import get_owned_project
from app.services.rewrite_service import MAX_SELECTION_CHARS, MAX_SETTING_CONTEXT_CHARS, RewriteJob
from app.services.rich_document_ai import scene_reference_context
from app.services.rich_document_core import selected_section_text
from app.services.rich_document_store import capture_documents


def _used() -> ConflictError:
    return ConflictError("本次选区发送确认已使用，请重新确认", extra={"code": "section_confirmation_used"})


async def prepare_section_rewrite(
    db: AsyncSession, *, user_id: int, project_id: int, chapter_id: int, scene_id: int, section_key: UUID,
    req: SectionRewriteRequest, cipher: SecretCipher, factory: ProviderFactory, ai_timeout: float,
    allow_private: bool, context_budget: int,
) -> RewriteJob:
    authenticated = authorization_service.actor_snapshot(db, user_id)
    chapter = await get_owned_chapter(db, user_id, chapter_id)
    project = await get_owned_project(db, user_id, project_id)
    if chapter.project_id != project.id:
        raise NotFoundError("章节不存在")
    if await db.get(SceneSelectionReceipt, req.request_id) is not None:
        raise _used()
    scene = await scene_service.require_scene(db, chapter, scene_id)
    if chapter.status == "generating":
        raise ConflictError("该章节正在生成中，请稍候再试")
    await ensure_outside_batch(db, chapter)
    await scene_ai_context.check_revisions(db, chapter, req.expected_content_revision, req.expected_planning_revision)
    stamp = await scene_ai_context.read_stamp(db, project.id, chapter.id)
    document = (await capture_documents(db, [scene]))[0][scene.id]
    section = next((item for item in document.sections if item.key == section_key), None)
    if section is None:
        raise NotFoundError("区块不存在")
    if req.end - req.start > MAX_SELECTION_CHARS:
        raise BadRequestError(f"单次选区不能超过 {MAX_SELECTION_CHARS} 字")
    try:
        original = selected_section_text(section, req.start, req.end, confirmed=req.confirmed)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if scene_service.content_fingerprint(original) != req.selected_hash:
        raise ConflictError("区块选区已变化，请重新选择并确认", extra={"code": "stale_section_selection"})

    cfg = await ai_config_service.resolve_config(db, user_id, req.ai_config_id)
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    task = await context_builder.chapter_task(db, chapter)
    resolved = await style_service.resolve(db, user_id, project, chapter, req.style)
    override = await prompt_service.get_override(db, user_id, "chapter.rewrite")
    skill = await skill_service.snapshot(db, user_id, req.skill_id, req.skill_inputs)
    budget = min(MAX_SETTING_CONTEXT_CHARS, ai_config_service.context_char_budget(cfg, context_budget))
    scenes = await scene_service.list_active_scenes(db, chapter.id)
    preceding = "".join(item.separator_before + item.content for item in scenes if item.sort < scene.sort)
    scene_context = scene_ai_context.scope_block(
        scene_id=scene.id, title=scene.title, summary=scene.summary[:500],
        index=next(index for index, item in enumerate(scenes, 1) if item.id == scene.id), total=len(scenes),
        target_words=0, preceding_text=preceding[-500:], selection=True,
    )
    # 允许原有正式正文作为背景；关闭区块的名称、选区外正文与其他关闭区块绝不读取进上下文。
    heading = "\n【本场景正式正文】\n"
    formal_budget = max(0, min(500, budget // 8, budget - len(scene_context) - len(heading)))
    if formal_budget >= 2:
        low, high = 0, min(len(scene.content), formal_budget)
        while low < high:
            middle = (low + high + 1) // 2
            if len(json.dumps(scene.content[:middle], ensure_ascii=False)) <= formal_budget:
                low = middle
            else:
                high = middle - 1
        if 0 < low < len(scene.content) and scene.content[low - 1:low + 1] == "\r\n":
            low -= 1
        scene_context += heading + json.dumps(scene.content[:low], ensure_ascii=False)
    author_budget = max(0, min(2_000, budget // 4, budget - len(scene_context) - 2))
    author_context = await scene_reference_context(
        db, project.id, [(item.id, item.sort) for item in scenes if item.sort <= scene.sort], author_budget,
    )
    if author_context:
        scene_context += "\n\n" + author_context
    remaining = max(0, budget - len(scene_context) - 2)
    hint = "\n".join((original, author_context))
    planning = await planning_context_service.load_planning_context(
        db, project, "scene", scene.id, scope="selection", budget=remaining,
        text_hint="\n".join((hint, req.instruction)), legacy_text_hint=hint,
        stamp=stamp.visibility, preceding_text=preceding if scenes[0].id != scene.id else None,
    )
    settings = await context_builder.build_setting_context(
        db, project, chapter, text_hint=hint, budget=remaining - planning.reserved_chars, planning=planning,
    )
    messages = build_rewrite_messages(
        op=req.op, selected=original, before="", after="", style=resolved.block if resolved is not None else "",
        summary=task.summary, instruction=req.instruction, skill_guide=(skill or {}).get("body", ""),
        setting_context=settings, planning_context=planning.text, scene_context=scene_context, override=override,
    )
    await scene_ai_context.require_stamp(db, stamp)

    # 确认记录不存作品、区块身份或文本；只保留至账户删除，防旧 UUID 在项目删建后复用。
    await setting_ai_visibility.lock_author_visibility_write(db, user_id, project_id, manage=False)
    await scene_ai_context.require_stamp(db, stamp)
    db.add(SceneSelectionReceipt(request_key=req.request_id, user_id=user_id))
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _used() from exc

    # HTTPX 的每次实际请求（含重定向与 SDK 重试）都经过 lease；最多放行第一跳。
    # 故障发生在发送前后不确定时也不得自动重发，作者可以重新明确确认。
    sent, send_lock = False, asyncio.Lock()

    async def verify(session: AsyncSession) -> None:
        nonlocal sent
        async with send_lock:
            if sent:
                raise VisibilityPolicyError("本次选区确认不可重发，请重新确认", extra={
                    "code": "section_confirmation_used",
                })
            await scene_ai_context.require_stamp(session, stamp, sending=True)
            sent = True

    async def authorize(session: AsyncSession) -> None:
        await authorization_service.require_authenticated_for_send(session, authenticated)

    provider = guard_provider(provider, stamp.visibility, visibility_sessionmaker(db), verify=verify,
                              authorize=authorize)
    return RewriteJob(
        project_id=project.id, chapter_id=chapter.id, scene_id=scene.id, op=req.op, start=req.start, end=req.end,
        scene_start=req.start, scene_end=req.end, context_stamp=stamp, original=original, before="", provider=provider,
        section_key=str(section_key), request_id=req.request_id, model=cfg.model, messages=messages,
        ai_config_id=cfg.id, provider_name=cfg.provider,
        thinking=cfg.thinking_level, max_tokens=ai_config_service.max_output_tokens(cfg),  # type: ignore[arg-type]
        params={"op": req.op, "scene_id": scene.id, "section_key": str(section_key), "request_id": req.request_id,
                "scene_start": req.start, "scene_end": req.end, "original_hash": req.selected_hash,
                "context_snapshot": stamp.log_data(), "selection_chars": req.end - req.start,
                "prompt": prompt_meta("chapter.rewrite", override), "style_choice": req.style,
                "style": style_service.style_log(resolved), "skill": skill_service.skill_log(skill),
                "planning": planning.log_data("selection")},
    )


async def apply_section_rewrite(
    db: AsyncSession, *, user_id: int, project_id: int, chapter_id: int, scene_id: int, section_key: UUID,
    data: SectionRewriteApply,
) -> SceneDocumentOut:
    from app.services.rich_document_mapping import FormatLossRequired, UnsafeMapping, map_explicit_replacement
    from app.services.rich_document_service import write_mapped_document

    await setting_ai_visibility.lock_author_visibility_write(db, user_id, project_id, manage=False)
    chapter = await get_owned_chapter(db, user_id, chapter_id, lock=True)
    await db.refresh(chapter)
    if chapter.project_id != project_id:
        raise NotFoundError("章节不存在")
    if chapter.status == "generating":
        raise ConflictError("该章节正在生成中，请稍候再试")
    await ensure_outside_batch(db, chapter)
    await scene_ai_context.check_revisions(db, chapter, data.expected_content_revision, data.expected_planning_revision)
    scene = await scene_service.require_scene(db, chapter, scene_id)
    current = (await capture_documents(db, [scene]))[0][scene.id]
    section = next((item for item in current.sections if item.key == section_key), None)
    if section is None:
        raise NotFoundError("区块不存在")
    receipt = await db.get(SceneSelectionReceipt, data.request_id)
    log = await db.scalar(select(GenerationLog).where(
        GenerationLog.id == data.log_id, GenerationLog.project_id == project_id,
        GenerationLog.chapter_id == chapter_id, GenerationLog.status == "success",
    ))
    if receipt is None or receipt.user_id != user_id or log is None:
        raise ConflictError("选区预览已失效，请重新确认", extra={"code": "stale_section_selection"})
    params = log.params or {}
    if (params.get("scene_id") != scene_id or params.get("section_key") != str(section_key)
            or params.get("request_id") != data.request_id or params.get("scene_start") != data.start
            or params.get("scene_end") != data.end
            or params.get("original_hash") != scene_service.content_fingerprint(data.original)
            or data.start > data.end or section.content[data.start:data.end] != data.original):
        raise ConflictError("区块选区已变化，请重新选择并确认", extra={"code": "stale_section_selection"})
    await scene_ai_context.require_stamp(db, params.get("context_snapshot") or {})
    raw = current.model_dump(mode="json")
    markers = [item.model_dump(mode="json") | {"section_key": None} for item in current.markers
               if item.section_key == section_key]
    target = SceneDocument.model_validate({
        "content": section.content, "formatting": section.formatting.model_dump(mode="json"), "markers": markers,
    })
    try:
        changed = map_explicit_replacement(target, data.start, data.end, data.replacement,
                                           confirm_format_loss=data.confirm_format_loss)
    except FormatLossRequired as exc:
        raise ConflictError(str(exc), extra={"code": "rich_document_confirmation_required"}) from exc
    except UnsafeMapping as exc:
        raise ConflictError(str(exc), extra={"code": "rich_document_unmapped"}) from exc
    for item in raw["sections"]:
        if item["key"] == str(section_key):
            item["content"] = changed.content
            item["formatting"] = changed.formatting.model_dump(mode="json")
    changed_markers = {str(item.key): item.model_dump(mode="json") | {"section_key": str(section_key)}
                       for item in changed.markers}
    raw["markers"] = [changed_markers.get(item["key"], item) for item in raw["markers"]]
    try:
        updated = SceneDocument.model_validate(raw)
    except ValidationError as exc:
        raise scene_service.SceneParameterError("替换后的区块正文或格式超过限制，原稿未改变") from exc
    await version_service.ensure_baseline(db, chapter, force=True)
    await write_mapped_document(db, project_id, scene, current, updated, data.expected_content_revision)
    await scene_service.refresh_projection(db, chapter, expected_revision=data.expected_content_revision)
    await version_service.record(db, chapter, "rewrite", log_id=log.id)
    await commit_versioned(db)
    result = (await capture_documents(db, [scene]))[0][scene.id]
    return SceneDocumentOut(content_revision=chapter.content_revision,
                            planning_revision=await scene_service._planning_revision(db, project_id), document=result)
