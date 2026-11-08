"""章级富文档提案：来源快照、任务身份和一次执行轮次必须在同一事务中绑定。"""

import hashlib
import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models import Chapter, SceneReviewPayloadChunk, SceneReviewProposal, Task
from app.schemas.chapter import ChapterOut
from app.schemas.rich_document import SceneDocument
from app.schemas.rich_document_review import ReviewAccept, ReviewIdentity
from app.services import (
    scene_ai_context,
    scene_service,
    setting_ai_visibility,
    task_statistics_service,
    version_service,
)
from app.services.chapter_service import commit_versioned, get_owned_chapter
from app.services.rich_document_proposal_mapping import (
    ProposalMappingError,
    SceneProposalMapping,
    build_scene_proposal,
    resolve_scene_proposal,
)

if TYPE_CHECKING:
    from app.services.generation_service import ChapterJob
    from app.services.task_engine.engine import TaskEngine

# 单行按字符分块，UTF-8 编码至多约 1 MiB；完整候选没有应用层总量截断。
PROPOSAL_CHUNK_CHARS = 256 * 1024
PROPOSAL_CHUNK_BATCH = 32


async def _write_payload(db: AsyncSession, proposal: SceneReviewProposal, payload: dict[str, Any]) -> None:
    """在同一事务流式写入完整提案；任务／提案完成前任何失败均整体回滚。"""
    await db.flush()  # 提案 ID 是外键；尚未提交，不会出现无分块的持久待审任务。
    count, pending, parts, size = 0, [], [], 0
    digest = hashlib.sha256()

    async def emit() -> None:
        nonlocal count, pending, parts, size
        if not parts:
            return
        content = "".join(parts)
        digest.update(content.encode("utf-8"))
        pending.append({"proposal_id": proposal.id, "ordinal": count, "content": content})
        count += 1
        parts, size = [], 0
        if len(pending) >= PROPOSAL_CHUNK_BATCH:
            await db.execute(insert(SceneReviewPayloadChunk), pending)
            pending = []

    try:
        for token in json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).iterencode(payload):
            cursor = 0
            while cursor < len(token):
                take = min(PROPOSAL_CHUNK_CHARS - size, len(token) - cursor)
                parts.append(token[cursor:cursor + take])
                size += take
                cursor += take
                if size == PROPOSAL_CHUNK_CHARS:
                    await emit()
        await emit()
        if pending:
            await db.execute(insert(SceneReviewPayloadChunk), pending)
    except (TypeError, UnicodeEncodeError) as exc:
        raise ConflictError("候选稿包含无法保存的内容，原章未修改", extra={"code": "rich_review_invalid"}) from exc
    proposal.payload = {"storage_version": 2, "chunk_count": count, "sha256": digest.hexdigest()}


async def _read_payload(db: AsyncSession, proposal: SceneReviewProposal) -> dict[str, Any]:
    header = proposal.payload
    if header.get("storage_version") != 2:
        return header  # 兼容已存在的早期单行提案。
    count = header.get("chunk_count")
    if type(count) is not int or count < 1 or not isinstance(header.get("sha256"), str):
        raise ConflictError("提案分块格式无效，原章未修改", extra={"code": "rich_review_invalid"})
    digest, chunks = hashlib.sha256(), []
    rows = await db.stream(select(SceneReviewPayloadChunk.ordinal, SceneReviewPayloadChunk.content).where(
        SceneReviewPayloadChunk.proposal_id == proposal.id,
    ).order_by(SceneReviewPayloadChunk.ordinal).execution_options(yield_per=4))
    try:
        async for ordinal, content in rows:
            if ordinal != len(chunks):
                raise ConflictError("提案分块不完整，原章未修改", extra={"code": "rich_review_invalid"})
            digest.update(content.encode("utf-8"))
            chunks.append(content)
    finally:
        await rows.close()
    if len(chunks) != count or digest.hexdigest() != header["sha256"]:
        raise ConflictError("提案分块不完整，原章未修改", extra={"code": "rich_review_invalid"})
    try:
        payload = json.loads("".join(chunks))
        if not isinstance(payload, dict):
            raise ValueError("候选格式错误")
    except (ValueError, TypeError) as exc:
        raise ConflictError("提案分块格式无效，原章未修改", extra={"code": "rich_review_invalid"}) from exc
    return payload


def review_identity(proposal: SceneReviewProposal) -> dict[str, Any]:
    return {"reason": "rich_document_review", "token": proposal.token, "task_key": proposal.task_key,
            "run_key": proposal.run_key, "chapter_id": proposal.chapter_id}


async def pending_for_task(db: AsyncSession, task: Task, *, lock: bool = False) -> SceneReviewProposal | None:
    if task.statistics_key is None:
        return None
    statement = select(SceneReviewProposal).where(
        SceneReviewProposal.task_id == task.id, SceneReviewProposal.task_key == task.statistics_key,
        SceneReviewProposal.status == "pending",
    ).order_by(SceneReviewProposal.id.desc()).limit(1)
    return await db.scalar(statement.with_for_update() if lock else statement)


async def recover_pending_identity(db: AsyncSession, task: Task) -> dict[str, Any] | None:
    proposal = await pending_for_task(db, task)
    if proposal is None:
        return None
    review = (task.checkpoint or {}).get("rich_review") or {}
    if (review.get("phase") != "awaiting_review" or review.get("token") != proposal.token
            or review.get("run_key") != proposal.run_key or review.get("chapter_id") != proposal.chapter_id):
        return None
    return review_identity(proposal)


async def require_review_identity(db: AsyncSession, task: Task, expected: ReviewIdentity | None) -> None:
    proposal = await pending_for_task(db, task)
    if proposal is None:
        if expected is not None:
            raise ConflictError("原提案轮次已变化，请刷新后重试", extra={"code": "rich_review_task_changed"})
        return
    if (expected is None or expected.task_key != proposal.task_key or expected.run_key != proposal.run_key
            or expected.token != proposal.token):
        raise ConflictError("请针对当前提案明确确认操作", extra={"code": "rich_review_task_changed"})


def require_finished_review_identity(task: Task, expected: ReviewIdentity | None) -> None:
    review = (task.checkpoint or {}).get("rich_review") if isinstance(task.checkpoint, dict) else None
    if not isinstance(review, dict):
        if expected is not None:
            raise ConflictError("原提案轮次已变化，请刷新后重试", extra={"code": "rich_review_task_changed"})
        return
    if (expected is None or task.statistics_key != expected.task_key or review.get("run_key") != expected.run_key
            or review.get("token") != expected.token):
        raise ConflictError("请针对当前提案明确确认操作", extra={"code": "rich_review_task_changed"})


async def discard_pending(db: AsyncSession, task: Task) -> bool:
    """调用方已通过 task UUID/status CAS 取得任务写权，和任务终态同事务提交。"""
    proposal = await pending_for_task(db, task, lock=True)
    if proposal is None:
        return False
    proposal.status = "discarded"
    checkpoint = deepcopy(task.checkpoint or {})
    review = checkpoint.get("rich_review") or {}
    if review.get("token") == proposal.token and review.get("run_key") == proposal.run_key:
        review["phase"] = "discarded"
        checkpoint["rich_review"] = review
        if task.kind == "chapter.batch" and checkpoint.get("phase") == "awaiting_review":
            checkpoint["phase"], checkpoint["current"] = "generate", None
        task.checkpoint = checkpoint
    return True


async def persist_pending(
    db: AsyncSession, chapter: Chapter, job: "ChapterJob", plans: dict[int, SceneProposalMapping],
    log_values: dict[str, Any],
) -> dict[str, Any]:
    """提案、断点与释放生成抢占同时落库；调用方持有可见性和项目锁。"""
    if job.task_id is None or not job.task_key or job.context_stamp is None:
        raise ConflictError("此操作需要持久任务才能安全核对富文档", extra={"code": "rich_review_task_required"})
    task = await db.scalar(select(Task).where(
        Task.id == job.task_id, Task.statistics_key == job.task_key, Task.status == "running",
        Task.admin_cancel_operation_id.is_(None),
    ).with_for_update())
    if task is None:
        raise ConflictError("任务状态已变化，候选稿未保存", extra={"code": "rich_review_task_changed"})
    checkpoint = deepcopy(task.checkpoint or {})
    if checkpoint.get("rich_review", {}).get("phase") == "awaiting_review":
        raise ConflictError("该任务已有待核对提案", extra={"code": "rich_review_pending"})
    token = str(uuid4())
    payload = {
        "mapping_version": 1, "stamp": job.context_stamp.log_data(), "mode": job.mode, "prev_status": job.prev_status,
        "target": job.target.model_dump(mode="json"), "log_values": log_values,
        "scenes": [{"scene_id": scene["id"], "separator_before": scene["separator_before"],
                    "original": plans[scene["id"]].original.model_dump(mode="json"),
                    "candidate": plans[scene["id"]].candidate,
                    "template": plans[scene["id"]].template,
                    "unresolved_sections": plans[scene["id"]].unresolved_sections,
                    "format_loss": plans[scene["id"]].format_loss}
                   for scene in job.scenes],
    }
    proposal = SceneReviewProposal(task_id=task.id, chapter_id=chapter.id, task_key=job.task_key,
                                   run_key=job.run_key, token=token, content_revision=chapter.content_revision,
                                   status="pending", payload={})
    db.add(proposal)
    await _write_payload(db, proposal, payload)
    checkpoint["rich_review"] = {"phase": "awaiting_review", "token": token, "run_key": job.run_key,
                                 "chapter_id": chapter.id}
    if task.kind == "chapter.batch":
        checkpoint["phase"], checkpoint["current"] = "awaiting_review", None
    task.checkpoint = checkpoint
    chapter.status = job.prev_status
    await db.flush()
    return review_identity(proposal)


async def _owned_proposal(
    db: AsyncSession, user_id: int, task_id: int, task_key: str, run_key: str, token: str, *, lock: bool = False,
) -> tuple[Task, SceneReviewProposal]:
    from app.services.task_service import get_owned_task

    task = await get_owned_task(db, user_id, task_id)
    if task.statistics_key != task_key:
        raise NotFoundError("提案不存在")
    statement = select(SceneReviewProposal).where(
        SceneReviewProposal.task_id == task_id, SceneReviewProposal.task_key == task_key,
        SceneReviewProposal.run_key == run_key, SceneReviewProposal.token == token,
    )
    if lock:
        task = await db.scalar(select(Task).where(
            Task.id == task_id, Task.user_id == user_id, Task.statistics_key == task_key,
        ).with_for_update().execution_options(populate_existing=True))
        statement = statement.with_for_update().execution_options(populate_existing=True)
    proposal = await db.scalar(statement)
    if task is None or proposal is None:
        raise NotFoundError("提案不存在")
    return task, proposal


def _stored_plans(payload: dict[str, Any]) -> list[tuple[int, SceneProposalMapping]]:
    if payload.get("mapping_version") != 1 or not isinstance(payload.get("scenes"), list):
        raise ConflictError("提案格式已失效，原稿未修改", extra={"code": "rich_review_invalid"})
    plans = []
    seen: set[int] = set()
    try:
        for item in payload["scenes"]:
            scene_id = item["scene_id"]
            if type(scene_id) is not int or scene_id in seen:
                raise ValueError("场景身份无效")
            seen.add(scene_id)
            source = SceneDocument.model_validate(item["original"])
            plan = build_scene_proposal(source, item["candidate"])
            if (plan.template != item["template"] or plan.unresolved_sections != item["unresolved_sections"]
                    or plan.format_loss is not item["format_loss"]):
                raise ValueError("映射版本不一致")
            plans.append((scene_id, plan))
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise ConflictError("提案结构或映射已失效，请保留原稿并核对", extra={
            "code": "rich_review_invalid",
        }) from exc
    return plans


async def get_review(
    db: AsyncSession, user_id: int, task_id: int, task_key: str, run_key: str, token: str,
) -> dict[str, Any]:
    task, proposal = await _owned_proposal(db, user_id, task_id, task_key, run_key, token)
    plans = _stored_plans(await _read_payload(db, proposal))
    return {**review_identity(proposal), "status": proposal.status, "task_status": task.status,
            "content_revision": proposal.content_revision,
            "scenes": [{"scene_id": scene_id, "original": plan.original.model_dump(mode="json"),
                        "candidate": plan.candidate, "proposed_formatting": plan.template["formatting"],
                        "proposed_markers": plan.template["markers"],
                        "proposed_sections": [{"key": item["key"], "anchor": item["anchor"], "order": item["order"]}
                                              for item in plan.template["sections"]],
                        "unresolved_sections": plan.unresolved_sections, "format_loss": plan.format_loss}
                       for scene_id, plan in plans]}


async def accept_review(
    db: AsyncSession, engine: "TaskEngine", user_id: int, task_id: int, token: str, data: ReviewAccept,
) -> dict[str, Any]:
    from app.services import analysis_service, batch_service, generation_log_service, task_service
    from app.services.rich_document_service import write_mapped_document
    from app.services.rich_document_store import capture_documents

    task, proposal = await _owned_proposal(db, user_id, task_id, data.task_key, data.run_key, token)
    project_id, chapter_id = task.project_id, proposal.chapter_id
    if project_id is None:
        raise NotFoundError("提案不存在")
    project = await setting_ai_visibility.lock_author_visibility_write(db, user_id, project_id, manage=False)
    chapter = await get_owned_chapter(db, user_id, chapter_id, lock=True)
    await db.refresh(chapter)
    if chapter.project_id != project_id:
        raise NotFoundError("提案不存在")
    task, proposal = await _owned_proposal(db, user_id, task_id, data.task_key, data.run_key, token, lock=True)
    positions: dict[tuple[int, str], int] = {}
    for item in data.sections:
        key = item.scene_id, item.section_key
        if key in positions:
            raise ConflictError("区块位置不能重复指定", extra={"code": "rich_review_invalid_positions"})
        positions[key] = item.anchor
    decision = {"sections": [[scene_id, key, positions[(scene_id, key)]] for scene_id, key in sorted(positions)],
                "confirm_format_loss": data.confirm_format_loss}
    if proposal.status == "applied":
        if proposal.payload.get("accepted_decision") != decision:
            raise ConflictError("提案已按其他选择采纳", extra={"code": "rich_review_already_applied"})
        return proposal.payload["applied_result"]
    proposal_data = await _read_payload(db, proposal)
    checkpoint = deepcopy(task.checkpoint or {})
    review = checkpoint.get("rich_review") or {}
    if (proposal.status != "pending" or task.status != "paused" or task.admin_cancel_operation_id is not None
            or review.get("phase") != "awaiting_review" or review.get("token") != token
            or review.get("run_key") != data.run_key or review.get("chapter_id") != chapter_id):
        raise ConflictError("提案或任务状态已变化，请重新核对", extra={"code": "rich_review_task_changed"})
    if chapter.status != proposal_data.get("prev_status"):
        raise ConflictError("章节状态已变化，请保留提案并重新核对", extra={"code": "scene_context_changed"})
    await scene_ai_context.require_stamp(db, proposal_data.get("stamp") or {})
    if task.kind == "chapter.batch":
        await batch_service.revalidate(db, engine.ctx, task)
    else:
        from app.services.generation_service import ensure_outside_batch

        await ensure_outside_batch(db, chapter)
    plans = _stored_plans(proposal_data)
    scenes = await scene_service.list_active_scenes(db, chapter.id)
    if [scene.id for scene in scenes] != [scene_id for scene_id, _ in plans]:
        raise ConflictError("场景结构已变化，请保留提案并重新核对", extra={"code": "scene_context_changed"})
    source, _ = await capture_documents(db, scenes)
    expected_positions = {(scene_id, key) for scene_id, plan in plans for key in plan.unresolved_sections}
    if set(positions) != expected_positions:
        raise ConflictError("请为每个待核对区块明确选择位置", extra={"code": "rich_review_invalid_positions"})
    mapped: dict[int, SceneDocument] = {}
    for scene_id, plan in plans:
        if source[scene_id] != plan.original:
            raise ConflictError("原始文档已变化，请保留提案并重新核对", extra={"code": "scene_context_changed"})
        try:
            mapped[scene_id] = resolve_scene_proposal(
                plan, {key: anchor for (owner, key), anchor in positions.items() if owner == scene_id},
                confirm_format_loss=data.confirm_format_loss,
            )
        except ProposalMappingError as exc:
            raise ConflictError(str(exc), extra={"code": "rich_review_invalid_positions"}) from exc
    await version_service.ensure_baseline(db, chapter, force=True)
    rich_ids = await scene_service._rich_scene_ids(db, [scene.id for scene in scenes])
    for scene in scenes:
        document = mapped[scene.id]
        if document == source[scene.id]:
            continue
        if scene.id in rich_ids:
            await write_mapped_document(db, project_id, scene, source[scene.id], document, proposal.content_revision)
        else:
            scene.content = document.content
    await scene_service.refresh_projection(db, chapter, expected_revision=proposal.content_revision)
    chapter.status = "done"
    values = proposal_data.get("log_values") or {}
    if (values.get("project_id") != project_id or values.get("chapter_id") != chapter_id
            or values.get("task_id") != task_id or values.get("kind") != proposal_data.get("mode")):
        raise ConflictError("提案生成记录身份无效", extra={"code": "rich_review_invalid"})
    log = await generation_log_service.write_log(db, **values)
    await version_service.record(db, chapter, proposal_data["mode"], log_id=log.id)
    await db.flush()
    await db.refresh(chapter, ["updated_at"])
    review["phase"] = "applied"
    checkpoint["rich_review"] = review
    if task.kind == "chapter.batch":
        items = checkpoint.get("items") or []
        if task.step_cursor >= len(items) or items[task.step_cursor].get("chapter_id") != chapter.id:
            raise ConflictError("批量断点已变化，请保留提案", extra={"code": "rich_review_task_changed"})
        item = items[task.step_cursor]
        item.update(state="done", words=chapter.word_count, error="", warning=values.get("error") or "")
        checkpoint["phase"], checkpoint["current"] = "analyze", None
        from app.services.rich_document_ai import analysis_fingerprints

        fingerprint = (await analysis_fingerprints(db, [chapter]))[chapter.id]
        checkpoint["rich_analysis"] = {"run_key": data.run_key, "task_key": data.task_key,
                                       "chapter_id": chapter.id, "content_hash": fingerprint}
        task.status, task.result, task.error = "queued", None, ""
        task.stage, task.finished_at = task_service.STAGE_LABELS["queued"], None
        task.queued_at = task_service._next_queued_at(task)
        await task_statistics_service.record_state_change(db, task, "queued")
    else:
        payload: dict[str, Any] = {"chapter": ChapterOut.model_validate(chapter).model_dump(mode="json")}
        target = proposal_data.get("target") or {}
        if target.get("kind") == "scene":
            scene = await scene_service.require_scene(db, chapter, target.get("scene_id"))
            payload["scene"] = (await scene_service.scene_out(db, chapter, scene)).model_dump(mode="json")
        else:
            payload["scene_ids"] = [scene.id for scene in scenes]
            if engine.ctx.settings.auto_analyze_enabled and project.auto_analyze:
                await analysis_service.enqueue_auto_in_transaction(db, project, chapter.id)
        stamp = task_statistics_service.now_utc()
        task.status, task.result, task.error, task.progress = "succeeded", payload, "", 100
        task.stage, task.finished_at = task_service.STAGE_LABELS["succeeded"], stamp
        await task_statistics_service.record_state_change(db, task, "succeeded", finished_at=stamp)
    task.checkpoint = checkpoint
    outcome = {"status": "applied", "task_status": task.status, "chapter_id": chapter.id,
               "content_revision": chapter.content_revision}
    proposal.status = "applied"
    proposal.payload = {**proposal.payload, "accepted_decision": decision, "applied_result": outcome}
    try:
        await commit_versioned(db)
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("任务资源已被占用，请保留提案并稍后再试", extra={
            "code": "rich_review_task_changed",
        }) from exc
    engine.notify()
    return outcome
