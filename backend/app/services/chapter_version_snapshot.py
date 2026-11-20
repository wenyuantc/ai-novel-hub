"""章节历史只可展示和恢复已确认属于本书的场景规划引用。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import (
    Career,
    Chapter,
    Character,
    Organization,
    SceneMarker,
    SceneMove,
    SceneSection,
    SettingTimeline,
    WorldSetting,
)
from app.models.planning import PlanningTag, PlanningUnit
from app.models.scenes import Scene
from app.schemas.planning import ScenePlanningRaw
from app.schemas.rich_document import SceneDocument
from app.schemas.scene import SceneSnapshotV2, parse_scene_snapshot
from app.services.scene_move_history import move_touches_chapter, restore_move_conflict


class _PlanningSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unit_id: Annotated[int, Field(strict=True, gt=0)]
    binding_epoch: Annotated[int, Field(strict=True, ge=0)]
    raw: ScenePlanningRaw


def _invalid() -> ConflictError:
    return ConflictError("场景历史关联数据不一致，无法操作", extra={"code": "scene_reference_invalid"})


async def _check_ids(db: AsyncSession, model, ids: set[int], *conditions) -> None:
    ordered = sorted(ids)
    for start in range(0, len(ordered), 400):
        batch = set(ordered[start:start + 400])
        found = set(await db.scalars(select(model.id).where(model.id.in_(batch), *conditions)))
        if found != batch:
            raise _invalid() from None


async def _check_document_keys(db: AsyncSession, model, keys: set[tuple[int, str]]) -> None:
    ordered = sorted(keys)
    for start in range(0, len(ordered), 400):
        batch = set(ordered[start:start + 400])
        rows = await db.execute(select(model.scene_id, model.logical_key).where(
            tuple_(model.scene_id, model.logical_key).in_(batch),
        ))
        if set(rows) != batch:
            raise _invalid() from None


async def _check_scene_history(
    db: AsyncSession, project_id: int, chapter_id: int, scene_ids: set[int], *, for_restore: bool,
) -> None:
    ordered = sorted(scene_ids)
    evidence = select(SceneMove.id).where(SceneMove.scene_id == Scene.id, move_touches_chapter(chapter_id)).exists()
    for start in range(0, len(ordered), 400):
        batch = set(ordered[start:start + 400])
        owners = dict((await db.execute(
            select(Scene.id, Scene.chapter_id).join(Chapter, Chapter.id == Scene.chapter_id).where(
                Scene.id.in_(batch), Chapter.project_id == project_id, or_(Scene.chapter_id == chapter_id, evidence),
            )
        )).all())
        if owners.keys() != batch:
            raise _invalid() from None
        if for_restore and any(owner != chapter_id for owner in owners.values()):
            raise restore_move_conflict("此版本中的场景已移到其他章节，请通过移动入口处理")


async def _check_unit_history(db: AsyncSession, project_id: int, chapter_id: int, unit_ids: set[int]) -> None:
    current_scenes = select(Scene.id).where(Scene.chapter_id == chapter_id)
    history = (
        select(SceneMove.id).join(Scene, Scene.id == SceneMove.scene_id).join(Chapter, Chapter.id == Scene.chapter_id)
        .where(SceneMove.planning_unit_id == PlanningUnit.id, move_touches_chapter(chapter_id),
               Chapter.project_id == project_id).exists()
    )
    await _check_ids(db, PlanningUnit, unit_ids, PlanningUnit.project_id == project_id,
                     PlanningUnit.source_level == "scene", or_(PlanningUnit.scene_id.in_(current_scenes), history))


async def validate_history_snapshot(
    db: AsyncSession, project_id: int, chapter_id: int, snapshot: dict, *, for_restore: bool = False,
) -> None:
    """批量核验 JSON 内的身份；恢复时还需确认当前本地绑定及代次。"""
    try:
        parsed = parse_scene_snapshot(snapshot)
        plans = {
            item.id: _PlanningSnapshot.model_validate(item.planning) if item.planning is not None else None
            for item in parsed.scenes
        }
        documents = {
            item.id: SceneDocument.model_validate({"content": item.content, **item.document})
            for item in parsed.scenes
        } if isinstance(parsed, SceneSnapshotV2) else {}
    except (ValidationError, TypeError, ValueError):
        raise _invalid() from None

    refs: dict[str, set[int]] = {kind: set() for kind in (
        "character", "tag", "world", "organization", "career", "relationship",
    )}
    for plan in plans.values():
        if plan is None:
            continue
        raw = plan.raw
        if raw.pov_character_id is not None:
            refs["character"].add(raw.pov_character_id)
        refs["tag"].update(raw.tag_ids)
        for ref in raw.references:
            refs[ref.kind].add(ref.timeline_id if ref.kind == "relationship" else ref.id)

    with db.no_autoflush:
        await _check_scene_history(db, project_id, chapter_id, set(plans), for_restore=for_restore)
        sections = {(scene_id, str(key)) for scene_id, doc in documents.items()
                    for key in (*[item.key for item in doc.sections], *doc.retired_section_keys)}
        markers = {(scene_id, str(item.key)) for scene_id, doc in documents.items() for item in doc.markers}
        await _check_document_keys(db, SceneSection, sections)
        await _check_document_keys(db, SceneMarker, markers)
        unit_ids = {plan.unit_id for plan in plans.values() if plan is not None}
        await _check_unit_history(db, project_id, chapter_id, unit_ids)
        for kind, model in (
            ("character", Character), ("tag", PlanningTag), ("world", WorldSetting),
            ("organization", Organization), ("career", Career), ("relationship", SettingTimeline),
        ):
            conditions = [model.project_id == project_id]
            if kind == "relationship":
                conditions.append(SettingTimeline.subject_kind == "relationship")
            await _check_ids(db, model, refs[kind], *conditions)

        if for_restore:
            units = {}
            scene_ids = sorted(plans)
            for start in range(0, len(scene_ids), 400):
                rows = await db.execute(select(PlanningUnit.scene_id, PlanningUnit.id, PlanningUnit.binding_epoch)
                                        .where(PlanningUnit.project_id == project_id,
                                               PlanningUnit.scene_id.in_(scene_ids[start:start + 400]),
                                               PlanningUnit.retired.is_(False)))
                units.update({row.scene_id: (row.id, row.binding_epoch) for row in rows})
            for scene_id, plan in plans.items():
                current = units.get(scene_id)
                if plan is None:
                    if current is not None and current[1] != 0:
                        raise ConflictError("旧场景历史不能覆盖后来重新关联的规划，请先处理引用", extra={
                            "code": "scene_reference_conflict",
                        })
                elif current != (plan.unit_id, plan.binding_epoch):
                    raise ConflictError("场景规划身份或绑定代次已变化，请先处理引用", extra={
                        "code": "scene_reference_conflict",
                    })
