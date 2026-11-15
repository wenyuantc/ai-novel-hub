"""章节历史只可展示和恢复已确认属于本书的场景规划引用。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import Career, Character, Organization, SceneMarker, SceneSection, SettingTimeline, WorldSetting
from app.models.planning import PlanningTag, PlanningUnit
from app.models.scenes import Scene
from app.schemas.planning import ScenePlanningRaw
from app.schemas.rich_document import SceneDocument
from app.schemas.scene import SceneSnapshotV2, parse_scene_snapshot


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
        await _check_ids(db, Scene, set(plans), Scene.chapter_id == chapter_id)
        sections = {(scene_id, str(key)) for scene_id, doc in documents.items()
                    for key in (*[item.key for item in doc.sections], *doc.retired_section_keys)}
        markers = {(scene_id, str(item.key)) for scene_id, doc in documents.items() for item in doc.markers}
        await _check_document_keys(db, SceneSection, sections)
        await _check_document_keys(db, SceneMarker, markers)
        await _check_ids(db, PlanningUnit, {plan.unit_id for plan in plans.values() if plan is not None},
                         PlanningUnit.project_id == project_id)
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
