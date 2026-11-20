"""同书场景跨章移动。预览只读，提交持双章写锁并保全两端历史。"""

import hmac
import json
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, UnauthorizedError
from app.models import Chapter, Project, ProjectCollaborator, Scene, SceneSection, User
from app.models.planning import PlanningUnit
from app.models.progressions import SettingProgression, SettingTimeline
from app.schemas.planning import ScenePlanningEffective
from app.schemas.rich_document import is_text_boundary
from app.schemas.scene import SceneBlocker
from app.schemas.scene_move import (
    SceneMove,
    SceneMoveChapter,
    SceneMoveOut,
    SceneMovePlanningChange,
    SceneMovePreview,
    SceneMovePreviewOut,
    SceneMoveSeam,
    SceneMoveTimelineEvent,
)
from app.services import planning_queries, project_access, scene_service, setting_progression_service, version_service
from app.services.audit_writer import AuditOperation
from app.services.chapter_service import lock_manual_project_visibility
from app.services.planning_lifecycle import bump_revision
from app.services.planning_scene_service import preview_scene_planning_move
from app.services.scene_move_history import chapter_identity, chapter_identity_expression, record_scene_move
from app.services.setting_ai_visibility import bump_visibility_revision

_EXCERPT_SIZE = 80


@dataclass(frozen=True)
class MovePlan:
    scene: Scene
    source_scenes: list[Scene]
    target_scenes: list[Scene]
    planning_unit_id: int | None
    preview: SceneMovePreviewOut


def _preview_changed() -> ConflictError:
    return ConflictError(
        "场景移动预览已变化，请重新核对",
        extra={"code": "scene_move_preview_changed"},
    )


async def _chapters(
    db: AsyncSession, project_id: int, source_id: int, target_id: int, *, lock: bool = False,
) -> tuple[Chapter, Chapter]:
    if source_id == target_id:
        raise scene_service.SceneParameterError("请选择另一章作为移动目标")
    statement = select(Chapter).where(
        Chapter.project_id == project_id, Chapter.id.in_((source_id, target_id)),
    ).order_by(Chapter.id).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update()
    chapters = {chapter.id: chapter for chapter in await db.scalars(statement)}
    if len(chapters) != 2:
        raise NotFoundError("章节不存在")
    return chapters[source_id], chapters[target_id]


def _summary(chapter: Chapter, scene_count: int) -> SceneMoveChapter:
    return SceneMoveChapter(
        chapter_id=chapter.id, chapter_no=chapter.chapter_no, title=chapter.title,
        content_revision=chapter.content_revision, scene_count=scene_count,
    )


def _position(scenes: list[Scene], scene_id: int) -> int | None:
    return next((index for index, scene in enumerate(scenes) if scene.id == scene_id), None)


def _excerpt(content: str, *, tail: bool = False) -> str:
    if tail:
        start = max(0, len(content) - _EXCERPT_SIZE)
        return content[start if is_text_boundary(content, start) else start + 1:]
    end = min(len(content), _EXCERPT_SIZE)
    return content[:end if is_text_boundary(content, end) else end - 1]


def _seam(scenes: list[Scene], index: int) -> SceneMoveSeam:
    text, ranges = scene_service.project_scenes(scenes, ordered=True)
    offset = ranges[index].separator_start if index < len(ranges) else len(text)
    return SceneMoveSeam(before=_excerpt(text[:offset], tail=True), after=_excerpt(text[offset:]))


async def _projection_blockers(
    db: AsyncSession, chapter: Chapter, scenes: list[Scene], moved_id: int,
) -> list[SceneBlocker]:
    text, ranges = scene_service.project_scenes(scenes, ordered=True)
    try:
        await scene_service.validate_projection_boundaries(db, scenes, text, ranges)
    except scene_service.SceneParameterError as exc:
        return [SceneBlocker(
            kind="rich_document", id=chapter.id, scene_id=moved_id, label=f"第{chapter.chapter_no}章：{exc.detail}",
        )]
    return []


async def _planning_changes(
    db: AsyncSession, project_id: int, before: ScenePlanningEffective, after: ScenePlanningEffective,
) -> list[SceneMovePlanningChange]:
    changes = []
    if before.pov_character_id != after.pov_character_id:
        changes.append(SceneMovePlanningChange(
            field="pov", before=[before.pov_character_name] if before.pov_character_name is not None else [],
            after=[after.pov_character_name] if after.pov_character_name is not None else [],
        ))
    if {tag.id for tag in before.tags} != {tag.id for tag in after.tags}:
        changes.append(SceneMovePlanningChange(
            field="tags", before=[tag.name for tag in before.tags], after=[tag.name for tag in after.tags],
        ))
    old = {planning_queries.reference_key(ref) for ref in before.references}
    new = {planning_queries.reference_key(ref) for ref in after.references}
    if old == new:
        return changes
    names = {
        planning_queries.reference_key(entry.reference): entry.name
        for entry in await planning_queries.entries(db, project_id)
    }
    for kind in ("character", "world", "organization", "career", "relationship"):
        prefix = f"{kind}:"
        previous = sorted(key for key in old if key.startswith(prefix))
        updated = sorted(key for key in new if key.startswith(prefix))
        if previous != updated:
            changes.append(SceneMovePlanningChange(
                field=kind, before=[names.get(key, f"#{key[len(prefix):]}") for key in previous],
                after=[names.get(key, f"#{key[len(prefix):]}") for key in updated],
            ))
    return changes


async def _timeline_events(
    db: AsyncSession, project_id: int, scene_id: int,
) -> tuple[list[SceneMoveTimelineEvent], list[tuple[int, int, int, int]]]:
    rows = (await db.execute(
        select(
            SettingProgression.id, SettingProgression.anchor_label, SettingProgression.boundary,
            SettingProgression.active, SettingProgression.revision, SettingTimeline.id.label("timeline_id"),
            SettingTimeline.revision.label("timeline_revision"), SettingTimeline.project_id,
        ).outerjoin(SettingTimeline, SettingTimeline.id == SettingProgression.timeline_id).where(
            SettingProgression.scene_id == scene_id,
        ).order_by(SettingProgression.id),
    )).all()
    if any(row.project_id != project_id for row in rows):
        raise ConflictError("场景演变关联数据不一致", extra={"code": "scene_reference_invalid"})
    return (
        [SceneMoveTimelineEvent(id=row.id, label=row.anchor_label or "场景设定变化", boundary=row.boundary)
         for row in rows if row.active],
        [(row.id, row.revision, row.timeline_id, row.timeline_revision) for row in rows],
    )


async def _project_stamp(db: AsyncSession, project_id: int) -> tuple[int, int, str | None]:
    row = (await db.execute(select(
        Project.planning_revision, Project.ai_visibility_revision, Project.last_deleted_at,
    ).where(Project.id == project_id))).one_or_none()
    if row is None:
        raise NotFoundError("项目不存在")
    deleted_at = row.last_deleted_at.isoformat() if row.last_deleted_at is not None else None
    return row.planning_revision, row.ai_visibility_revision, deleted_at


async def _sign(
    db: AsyncSession, project_id: int, user_id: int, data: SceneMovePreviewOut, *,
    signing_secret: bytes, identities: tuple[int, int], stamps: tuple[int, int, str | None],
    timeline_stamp: list[tuple[int, int, int, int]],
) -> str:
    actor = (await db.execute(
        select(User.token_version, User.created_at, ProjectCollaborator.invitation_id, ProjectCollaborator.revision)
        .outerjoin(ProjectCollaborator, and_(
            ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == User.id,
        )).where(User.id == user_id),
    )).one_or_none()
    if actor is None:
        raise UnauthorizedError("账号已变化，请重新登录")
    message = json.dumps(
        ["ainovel/scene-move/v1", project_id, user_id, actor.token_version, actor.created_at.isoformat(),
         actor.invitation_id, actor.revision, identities, stamps, timeline_stamp,
         data.model_dump(mode="json", exclude={"signature"})],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()
    return hmac.digest(signing_secret, message, "sha256").hex()


async def _plan(
    db: AsyncSession, user_id: int, source: Chapter, target: Chapter, scene_id: int,
    data: SceneMovePreview, signing_secret: bytes,
) -> MovePlan:
    project_id = source.project_id
    stamps = await _project_stamp(db, project_id)
    identities = (await chapter_identity(db, source.id), await chapter_identity(db, target.id))
    source_scenes = await scene_service.list_active_scenes(db, source.id)
    target_scenes = await scene_service.list_active_scenes(db, target.id)
    source_index = _position(source_scenes, scene_id)
    if source_index is None:
        raise NotFoundError("场景不存在")
    target_index = (
        len(target_scenes) if data.insert_before_scene_id is None
        else _position(target_scenes, data.insert_before_scene_id)
    )
    if target_index is None:
        if isinstance(data, SceneMove):
            raise _preview_changed()
        raise scene_service.SceneParameterError("插入位置必须是目标章的活动场景")
    moved = source_scenes[source_index]
    remaining = source_scenes[:source_index] + source_scenes[source_index + 1:]
    inserted = [*target_scenes[:target_index], moved, *target_scenes[target_index:]]
    blockers = []
    for chapter, projected in ((source, remaining), (target, inserted)):
        try:
            await scene_service.ensure_manual_write_available(db, chapter)
        except ConflictError as exc:
            blockers.append(SceneBlocker(
                kind="chapter", id=chapter.id, scene_id=scene_id, label=f"第{chapter.chapter_no}章：{exc.detail}",
            ))
        blockers.extend(await _projection_blockers(db, chapter, projected, scene_id))
    if await db.scalar(select(PlanningUnit.id).where(
        PlanningUnit.scene_id == scene_id, PlanningUnit.project_id != project_id,
    ).limit(1)) is not None:
        raise ConflictError("场景规划关联数据不一致", extra={"code": "scene_reference_invalid"})

    planning, after = await preview_scene_planning_move(db, project_id, scene_id, target.id)
    changes = await _planning_changes(db, project_id, planning.effective, after)
    events, timeline_stamp = await _timeline_events(db, project_id, scene_id)
    moved_text = moved.separator_before + moved.content
    preview = SceneMovePreviewOut(
        scene_id=scene_id, source=_summary(source, len(source_scenes)), target=_summary(target, len(target_scenes)),
        source_position=source_index + 1, target_position=target_index + 1,
        insert_before_scene_id=data.insert_before_scene_id, planning_revision=planning.revision, signature="",
        source_will_be_empty=not remaining, planning_changes=changes, timeline_events=events, blockers=blockers,
        source_seam=_seam(remaining, source_index), target_seam=_seam(target_scenes, target_index),
        moved_start=_excerpt(moved_text), moved_end=_excerpt(moved_text, tail=True),
    )
    # PostgreSQL 读提交下同一会话的后续 SELECT 可以看见新稿。
    chapter_rows = (await db.execute(select(
        Chapter.id, Chapter.content_revision, Chapter.chapter_no, Chapter.title, Chapter.status,
        chapter_identity_expression(),
    ).where(Chapter.project_id == project_id, Chapter.id.in_((source.id, target.id))))).all()
    if (
        set(chapter_rows) != {
            (chapter.id, chapter.content_revision, chapter.chapter_no, chapter.title, chapter.status, identity)
            for chapter, identity in zip((source, target), identities, strict=True)
        }
        or planning.revision != stamps[0]
        or stamps != await _project_stamp(db, project_id)
    ):
        raise _preview_changed()
    preview.signature = await _sign(
        db, project_id, user_id, preview, signing_secret=signing_secret,
        identities=identities, stamps=stamps, timeline_stamp=timeline_stamp,
    )
    return MovePlan(moved, remaining, inserted, planning.unit_id, preview)


async def preview_move(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, scene_id: int,
    data: SceneMovePreview, *, signing_secret: bytes,
) -> SceneMovePreviewOut:
    with db.no_autoflush:
        await project_access.require_project(db, user_id, project_id, "content.edit")
        source, target = await _chapters(db, project_id, chapter_id, data.target_chapter_id)
        return (await _plan(db, user_id, source, target, scene_id, data, signing_secret)).preview


async def _relocate(db: AsyncSession, source: Chapter, target: Chapter, plan: MovePlan) -> None:
    # 暂时退出活动顺序的唯一约束，直到两章排序均已完成。
    plan.scene.active = False
    plan.scene.chapter_id = target.id
    await db.flush()
    if plan.source_scenes:
        await scene_service._set_order(db, plan.source_scenes)
    else:
        db.add(Scene(chapter_id=source.id, sort=0))
    await scene_service._set_order(db, plan.target_scenes)
    plan.scene.active = True
    await db.flush()


async def move(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, scene_id: int,
    data: SceneMove, *, signing_secret: bytes,
) -> SceneMoveOut:
    async with AuditOperation(db, user_id, "chapter.update", "project", project_id, project_id=project_id) as audit:
        project = await lock_manual_project_visibility(db, user_id, project_id)
        audit.target(project)
        source, target = await _chapters(db, project_id, chapter_id, data.target_chapter_id, lock=True)
        with db.no_autoflush:
            plan = await _plan(db, user_id, source, target, scene_id, data, signing_secret)
        if plan.preview.blockers:
            raise ConflictError("场景当前无法移动，请检查章节或文档状态", extra={
                "code": "scene_move_blocked",
                "blockers": [blocker.model_dump() for blocker in plan.preview.blockers],
            })
        if not hmac.compare_digest(plan.preview.signature, data.signature):
            raise _preview_changed()
        for chapter in (source, target):
            await version_service.ensure_baseline(db, chapter, force=True)
        await _relocate(db, source, target, plan)
        revision = max(source.content_revision, target.content_revision) + 1
        for chapter in (source, target):
            await scene_service.refresh_projection(db, chapter, next_revision=revision)
        await setting_progression_service.on_story_positions_changed(db, project_id)
        await bump_revision(db, project_id)
        moved_ai_section = await db.scalar(select(SceneSection.id).where(
            SceneSection.scene_id == scene_id, SceneSection.active.is_(True), SceneSection.include_in_ai.is_(True),
        ).limit(1))
        await bump_visibility_revision(db, project_id, tighten=moved_ai_section is not None)
        out_version = await version_service.record(db, source, "manual", note=f"移动场景至第{target.chapter_no}章")
        in_version = await version_service.record(db, target, "manual", note=f"从第{source.chapter_no}章移入场景")
        await record_scene_move(
            db, scene_id=scene_id, from_chapter_id=source.id, to_chapter_id=target.id,
            planning_unit_id=plan.planning_unit_id, from_version_seq=out_version.seq, to_version_seq=in_version.seq,
        )
        audit.affected([source.id, target.id])
        audit.summary.chapter_count = 2
        audit.summary.changed_fields = ["content"]
        audit.version(in_version, project_id)
        result = SceneMoveOut(
            scene_id=scene_id, source_chapter_id=source.id, target_chapter_id=target.id,
            source_content_revision=source.content_revision, target_content_revision=target.content_revision,
            planning_revision=await planning_queries.revision(db, project_id),
        )
        await audit.commit(flush_fn=version_service.flush_versioned)
        return result
