"""连载工作台的人工写操作。作品写锁串行化排期与登记，章锁保证定稿读取稳定正文。"""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import (
    Chapter,
    Scene,
    SerializationEdition,
    SerializationEntry,
    SerializationGoal,
    SerializationPublication,
)
from app.schemas.serialization import (
    EditionBrief,
    EntryMutation,
    EntryUpdate,
    FinalizeRequest,
    GoalOut,
    GoalUpdate,
    PublicationReceipt,
    PublishRequest,
)
from app.services import project_access, scene_service
from app.services.audit_writer import AuditOperation
from app.services.chapter_service import get_manual_chapter
from app.services.serialization_snapshot import capture_draft


def _check_revision(actual: int, expected: int) -> None:
    if actual != expected:
        raise ConflictError("连载信息已变化，请刷新后重试", extra={
            "code": "stale_serialization_revision", "current_revision": actual,
        })


async def _check_chapter_identity(db: AsyncSession, chapter_id: int, expected_scene_id: int) -> None:
    # SQLite 可复用已删除章节的数字 ID；退场的场景仍然可识别本章。
    owner = await db.scalar(select(Scene.chapter_id).where(Scene.id == expected_scene_id))
    if owner != chapter_id:
        raise ConflictError("预览中的章节已被替换，请刷新后重试", extra={"code": "stale_chapter_identity"})


async def _chapter_entry(db: AsyncSession, project_id: int, chapter_id: int) -> SerializationEntry | None:
    return await db.scalar(select(SerializationEntry).join(Chapter, Chapter.id == SerializationEntry.chapter_id).where(
        SerializationEntry.project_id == project_id, Chapter.project_id == project_id, Chapter.id == chapter_id,
    ).execution_options(populate_existing=True))


def _new_entry(db: AsyncSession, project_id: int, chapter_id: int) -> SerializationEntry:
    entry = SerializationEntry(project_id=project_id, chapter_id=chapter_id, stage="revision", revision=0)
    db.add(entry)
    return entry


async def set_goal(db: AsyncSession, user_id: int, project_id: int, body: GoalUpdate) -> GoalOut:
    async with AuditOperation(db, user_id, "project.update", "project", project_id, project_id=project_id) as audit:
        project = await project_access.require_project(db, user_id, project_id, "content.edit", for_update=True)
        audit.target(project, label="设置连载日更目标")
        goal = await db.get(SerializationGoal, project_id, populate_existing=True)
        revision = goal.revision if goal is not None else 0
        _check_revision(revision, body.expected_revision)
        if (goal.daily_goal if goal is not None else None) != body.daily_goal:
            if goal is None:
                goal = SerializationGoal(project_id=project_id)
                db.add(goal)
            revision += 1
            goal.daily_goal = body.daily_goal
            goal.revision = revision
        await audit.commit()
        return GoalOut(daily_goal=body.daily_goal, revision=revision)


async def update_entry(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, body: EntryUpdate,
) -> EntryMutation:
    async with AuditOperation(db, user_id, "chapter.update", "chapter", chapter_id, project_id=project_id) as audit:
        chapter = await get_manual_chapter(db, user_id, project_id, chapter_id, "content.edit", for_update=True)
        audit.target(chapter, label=f"调整编辑状态或排期 · {chapter.title}")
        await _check_chapter_identity(db, chapter_id, body.expected_scene_id)
        entry = await _chapter_entry(db, project_id, chapter_id)
        revision = entry.revision if entry is not None else 0
        _check_revision(revision, body.expected_revision)
        stage = entry.stage if entry is not None else "revision"
        planned = entry.planned_date if entry is not None else None
        next_stage = body.stage if "stage" in body.model_fields_set else stage
        next_date = body.planned_date if "planned_date" in body.model_fields_set else planned
        if (next_stage, next_date) != (stage, planned):
            if entry is None:
                entry = _new_entry(db, project_id, chapter_id)
            revision += 1
            entry.stage = next_stage
            entry.planned_date = next_date
            entry.revision = revision
        await audit.commit()
        return EntryMutation(revision=revision)


async def finalize(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, body: FinalizeRequest,
) -> EditionBrief:
    async with AuditOperation(db, user_id, "chapter.update", "chapter", chapter_id, project_id=project_id) as audit:
        chapter = await scene_service.get_manual_write_chapter(db, user_id, project_id, chapter_id)
        audit.target(chapter, label=f"确认连载定稿 · {chapter.title}")
        await _check_chapter_identity(db, chapter_id, body.expected_scene_id)
        entry = await _chapter_entry(db, project_id, chapter_id)
        _check_revision(entry.revision if entry is not None else 0, body.expected_revision)
        draft = await capture_draft(db, project_id, chapter_id)
        if draft.content_revision != body.expected_content_revision or draft.fingerprint != body.expected_fingerprint:
            raise ConflictError("预览后草稿已变化，请重新核对定稿", extra={"code": "stale_serialization_draft"})
        if not draft.content.strip():
            raise BadRequestError("正文为空，无法确认定稿")
        if entry is None:
            entry = _new_entry(db, project_id, chapter_id)
            await db.flush()
        last_seq = await db.scalar(select(func.max(SerializationEdition.seq)).where(
            SerializationEdition.entry_id == entry.id,
        ))
        edition = SerializationEdition(
            entry_id=entry.id, seq=(last_seq or 0) + 1, chapter_no=draft.chapter_no, title=draft.title,
            content=draft.content, blocks=draft.blocks, fingerprint=draft.fingerprint,
        )
        db.add(edition)
        entry.stage = "final"
        entry.revision += 1
        await audit.commit()
        return EditionBrief.model_validate(edition, from_attributes=True)


async def publish(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, body: PublishRequest,
) -> PublicationReceipt:
    # 统一时间偏移后判别重放，路由中的章节也是请求的一部分。
    try:
        published_at = body.published_at.astimezone(UTC)
    except OverflowError:
        raise BadRequestError("实际发布时间超出可登记范围") from None
    channel = body.channel.strip()
    digest_input = [chapter_id, body.edition_id, body.expected_revision, channel, published_at.isoformat()]
    digest = hashlib.sha256(json.dumps(digest_input, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    async with AuditOperation(db, user_id, "chapter.update", "chapter", chapter_id, project_id=project_id) as audit:
        # 父行授权和写锁必须在幂等查询之前。已成功请求即使随后改稿，也可取回原收据。
        await project_access.require_project(db, user_id, project_id, "content.edit", for_update=True)
        existing = await db.scalar(select(SerializationPublication).where(
            SerializationPublication.project_id == project_id, SerializationPublication.actor_id == user_id,
            SerializationPublication.request_id == str(body.request_id),
        ))
        if existing is not None:
            if existing.payload_hash != digest:
                raise ConflictError("该请求已登记过其他发布内容，请重新确认", extra={
                    "code": "serialization_request_conflict",
                })
            receipt = PublicationReceipt(id=existing.id)
            await db.commit()
            return receipt
        if published_at > datetime.now(UTC):
            raise BadRequestError("实际发布时间不能晚于现在，请使用排期安排未来发布")

        entry = await _chapter_entry(db, project_id, chapter_id)
        if entry is None:
            raise NotFoundError("未找到该章节的定稿")
        _check_revision(entry.revision, body.expected_revision)
        edition = await db.scalar(select(SerializationEdition.id).where(
            SerializationEdition.entry_id == entry.id, SerializationEdition.id == body.edition_id,
        ))
        if edition is None:
            raise NotFoundError("该章节的定稿不存在")
        latest = await db.scalar(select(SerializationEdition.id).where(SerializationEdition.entry_id == entry.id)
                                 .order_by(SerializationEdition.seq.desc()).limit(1))
        if entry.stage != "final" or latest != body.edition_id:
            already_published = await db.scalar(select(SerializationPublication.id).where(
                SerializationPublication.project_id == project_id,
                SerializationPublication.edition_id == body.edition_id,
            ).limit(1))
            if already_published is None:
                raise ConflictError("请选择当前有效定稿，或补录已发布过的版本")
        row = SerializationPublication(
            project_id=project_id, edition_id=body.edition_id, channel=channel, published_at=published_at,
            actor_id=user_id, request_id=str(body.request_id), payload_hash=digest,
        )
        db.add(row)
        audit.values["chapter_id"] = chapter_id
        audit.values["target_label"] = "手动登记连载发布"
        await audit.commit()
        return PublicationReceipt(id=row.id)
