"""正文批量修订：作品写屏障后预检全部章节，正文、版本及批次共用一次提交。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models import Chapter, RevisionBatch, RevisionBatchScene
from app.schemas.book_search import ApplyRequest, BatchSummary, SelectedHit
from app.schemas.rich_document import ProseEdit, SceneDocument
from app.services import book_search, project_access, rich_document_service, scene_service, version_service
from app.services.annotation_anchors import map_edits
from app.services.audit_writer import AuditOperation
from app.services.chapter_service import lock_manual_project_visibility
from app.services.rich_document_store import capture_documents


async def history(db: AsyncSession, user_id: int, project_id: int, offset: int, limit: int) -> dict:
    await project_access.require_project(db, user_id, project_id, "content.read")
    scope = RevisionBatch.project_id == project_id
    total = await db.scalar(select(func.count(RevisionBatch.id)).where(scope))
    rows = await db.scalars(select(RevisionBatch).where(scope).order_by(RevisionBatch.id.desc())
                            .offset(offset).limit(limit))
    return {"items": [BatchSummary.model_validate(row, from_attributes=True) for row in rows], "total": total}


def _request_digest(body: ApplyRequest) -> str:
    payload = body.model_dump(mode="json", exclude={"request_id"})
    payload["hits"].sort(key=lambda hit: (hit["chapter_id"], hit["start"], hit["end"]))
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _chapter_conflict(chapter_id: int | None, number: int, title: str, reason: str) -> dict:
    return {"chapter_id": chapter_id, "chapter_no": number, "chapter_title": title, "reason": reason}


def _raise_conflicts(conflicts: list[dict], detail: str) -> None:
    if conflicts:
        conflicts.sort(key=lambda item: item["chapter_no"])
        raise ConflictError(detail, extra={
            "code": "batch_revision_conflict", "conflicts": conflicts,
        })


async def _lock_chapter(db: AsyncSession, project_id: int, chapter_id: int) -> Chapter:
    """在已有的作品写屏障内，升序锁章，生成抢占也必须与该章行锁串行。"""
    chapter = await db.scalar(select(Chapter).where(Chapter.project_id == project_id, Chapter.id == chapter_id)
                              .with_for_update().execution_options(populate_existing=True))
    if chapter is None:
        raise NotFoundError("章节已删除或不属于当前作品")
    await scene_service.ensure_manual_write_available(db, chapter)
    return chapter


async def _apply_plans(
    db: AsyncSession, project_id: int, body: ApplyRequest,
) -> list[tuple[Chapter, list[book_search.SceneChange]]]:
    grouped: dict[int, list[SelectedHit]] = defaultdict(list)
    for hit in body.hits:
        grouped[hit.chapter_id].append(hit)
    plans, conflicts = [], []
    for chapter_id, hits in sorted(grouped.items()):
        try:
            chapter = await _lock_chapter(db, project_id, chapter_id)
            changes = await book_search.plan_chapter(db, project_id, chapter, body, hits)
            plans.append((chapter, changes))
        except (ConflictError, NotFoundError) as exc:
            first = hits[0]
            conflicts.append(_chapter_conflict(chapter_id, first.chapter_no, first.chapter_title, exc.detail))
    _raise_conflicts(conflicts, "章节状态已变化，整批修订未执行，请核对冲突章节")
    return plans


async def _write_chapter(
    db: AsyncSession, chapter: Chapter, changes: list[book_search.SceneChange],
    query: str, replacement: str, *, undoing: bool = False,
) -> None:
    revision = chapter.content_revision
    await version_service.ensure_baseline(db, chapter, force=True)
    for change in changes:
        # 批次起点相对原稿。撤销时先换算为修订后的起点，再从右至左恢复。
        delta = len(replacement) - len(query)
        edits = []
        for i, start in reversed(list(enumerate(change.start_offsets))):
            if undoing:
                start += i * delta
                edits.append(ProseEdit(start=start, end=start + len(replacement), insert=query))
            else:
                edits.append(ProseEdit(start=start, end=start + len(query), insert=replacement))
        await map_edits(db, change.scene, change.after.content, edits)
        await rich_document_service.write_mapped_document(
            db, chapter.project_id, change.scene, change.before, change.after, revision,
        )
    await scene_service.refresh_projection(db, chapter, expected_revision=revision)
    await version_service.record_manual(db, chapter)


def _record_audit(audit: AuditOperation, chapter_ids: list[int]) -> None:
    audit.affected(chapter_ids)
    audit.summary.chapter_count = len(chapter_ids)
    audit.summary.changed_fields = ["content"]


async def apply(db: AsyncSession, user_id: int, project_id: int, body: ApplyRequest) -> BatchSummary:
    digest = _request_digest(body)
    async with AuditOperation(db, user_id, "chapter.update", "project", project_id, project_id=project_id) as audit:
        project = await lock_manual_project_visibility(db, user_id, project_id)
        audit.target(project)
        existing = await db.scalar(select(RevisionBatch).where(
            RevisionBatch.project_id == project_id, RevisionBatch.actor_id == user_id,
            RevisionBatch.request_id == str(body.request_id),
        ))
        if existing is not None:
            if existing.payload_hash != digest:
                raise ConflictError("这次请求已用于其他修订，请重新确认", extra={
                    "code": "batch_revision_request_conflict",
                })
            return BatchSummary.model_validate(existing, from_attributes=True)

        plans = await _apply_plans(db, project_id, body)
        batch = RevisionBatch(
            project_id=project_id, actor_id=user_id, request_id=str(body.request_id), payload_hash=digest,
            query=body.query, replacement=body.replacement, status="applied",
            chapter_count=len(plans), hit_count=len(body.hits),
        )
        db.add(batch)
        await db.flush()
        for slot, (chapter, changes) in enumerate(plans, start=1):
            await _write_chapter(db, chapter, changes, body.query, body.replacement)
            for change in changes:
                db.add(RevisionBatchScene(
                    batch_id=batch.id, chapter_id=chapter.id, scene_id=change.scene.id, chapter_slot=slot,
                    chapter_no_at_apply=chapter.chapter_no, chapter_title=chapter.title,
                    applied_revision=chapter.content_revision, before_document=change.before.model_dump(mode="json"),
                    start_offsets=change.start_offsets, hit_count=len(change.start_offsets),
                ))
        _record_audit(audit, [chapter.id for chapter, _ in plans])
        await audit.commit(flush_fn=version_service.flush_versioned)
        return BatchSummary.model_validate(batch, from_attributes=True)


async def _restore_changes(
    db: AsyncSession, chapter: Chapter, details: list[RevisionBatchScene],
) -> list[book_search.SceneChange]:
    if any(detail.applied_revision != chapter.content_revision for detail in details):
        raise ConflictError("章节在本次修订后有过编辑")
    active = {scene.id: scene for scene in await scene_service.list_active_scenes(db, chapter.id)}
    if any(detail.scene_id not in active for detail in details):
        raise ConflictError("修订涉及的场景已移除或归属已变化")
    targets = [active[detail.scene_id] for detail in details]
    current, _ = await capture_documents(db, targets)
    changes = []
    for detail, scene in zip(details, targets, strict=True):
        try:
            before = SceneDocument.model_validate(detail.before_document)
        except ValidationError as exc:
            raise ConflictError("批次的原稿记录不完整") from exc
        changes.append(book_search.SceneChange(
            scene=scene, before=current[scene.id], after=before, start_offsets=detail.start_offsets,
        ))
    return changes


async def _undo_plans(
    db: AsyncSession, batch: RevisionBatch,
) -> list[tuple[Chapter, list[book_search.SceneChange]]]:
    rows = list(await db.scalars(select(RevisionBatchScene).where(RevisionBatchScene.batch_id == batch.id)))
    grouped: dict[int, list[RevisionBatchScene]] = defaultdict(list)
    deleted = set()
    conflicts, plans = [], []
    for detail in rows:
        if detail.chapter_id is None:
            deleted.add((detail.chapter_slot, detail.chapter_no_at_apply, detail.chapter_title))
        else:
            grouped[detail.chapter_id].append(detail)
    for _, number, title in sorted(deleted):
        conflicts.append(_chapter_conflict(None, number, title, "章节已删除"))
    for chapter_id, details in sorted(grouped.items()):
        try:
            chapter = await _lock_chapter(db, batch.project_id, chapter_id)
            changes = await _restore_changes(db, chapter, details)
            plans.append((chapter, changes))
        except (ConflictError, NotFoundError) as exc:
            first = details[0]
            conflicts.append(_chapter_conflict(chapter_id, first.chapter_no_at_apply, first.chapter_title, exc.detail))
    _raise_conflicts(conflicts, "整批撤销未执行，当前修订仍然有效，请核对冲突章节")
    if len(grouped) != batch.chapter_count or sum(row.hit_count for row in rows) != batch.hit_count:
        raise ConflictError("批次明细不完整，无法整批撤销", extra={"code": "batch_revision_conflict", "conflicts": []})
    return plans


async def undo(db: AsyncSession, user_id: int, project_id: int, batch_id: int) -> BatchSummary:
    async with AuditOperation(db, user_id, "chapter.update", "project", project_id, project_id=project_id) as audit:
        project = await lock_manual_project_visibility(db, user_id, project_id)
        audit.target(project)
        batch = await db.scalar(select(RevisionBatch).where(
            RevisionBatch.project_id == project_id, RevisionBatch.id == batch_id,
        ).with_for_update())
        if batch is None:
            raise NotFoundError("修订批次不存在")
        if batch.status == "undone":
            return BatchSummary.model_validate(batch, from_attributes=True)
        plans = await _undo_plans(db, batch)
        for chapter, changes in plans:
            await _write_chapter(db, chapter, changes, batch.query, batch.replacement, undoing=True)
        batch.status, batch.undone_at = "undone", datetime.now(UTC)
        _record_audit(audit, [chapter.id for chapter, _ in plans])
        await audit.commit(flush_fn=version_service.flush_versioned)
        return BatchSummary.model_validate(batch, from_attributes=True)
