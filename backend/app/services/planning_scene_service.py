"""场景本地规划意图及统一继承投影；底层写入只 flush，不接管章投影或提交。"""

from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Chapter, ChapterVersion, Character, SettingProgression, SettingTimeline
from app.models.planning import (
    PlanningTag,
    PlanningUnit,
    PlanningUnitCharacter,
    PlanningUnitTag,
    SubplotCrossing,
    SubplotNode,
)
from app.models.scenes import Scene
from app.schemas import planning as s
from app.schemas.setting_fields import EntityRef
from app.services import planning_queries as query

PLANNING_FIELDS = ("pov", "tags", "character", "world", "organization", "career", "relationship")


async def _scene(db: AsyncSession, project_id: int, scene_id: int, *, active: bool = True) -> tuple[Scene, Chapter]:
    statement = select(Scene, Chapter).join(Chapter, Chapter.id == Scene.chapter_id).where(
        Scene.id == scene_id, Chapter.project_id == project_id
    )
    if active:
        statement = statement.where(Scene.active.is_(True))
    result = (await db.execute(statement)).first()
    if result is None:
        raise NotFoundError("场景不存在或不属于该作品")
    return result[0], result[1]


async def _raw_values(db: AsyncSession, units: Sequence[PlanningUnit]) -> dict[int, s.ScenePlanningRaw]:
    if not units:
        return {}
    if len(units) > 400:
        result = {}
        for start in range(0, len(units), 400):
            result.update(await _raw_values(db, units[start:start + 400]))
        return result
    ids = [unit.id for unit in units]
    tags: dict[int, list[int]] = {identity: [] for identity in ids}
    refs: dict[int, list[s.PlanningReference]] = {identity: [] for identity in ids}
    for uid, tag_id in await db.execute(select(PlanningUnitTag.unit_id, PlanningUnitTag.tag_id).where(
        PlanningUnitTag.unit_id.in_(ids)
    )):
        tags[uid].append(tag_id)
    joins = {"character": (PlanningUnitCharacter, "character_id", Character), **query.REFERENCE_JOINS}
    for kind, (model, column, _) in joins.items():
        for uid, value in await db.execute(select(model.unit_id, getattr(model, column)).where(model.unit_id.in_(ids))):
            refs[uid].append(
                s.RelationshipReference(kind="relationship", timeline_id=value)
                if kind == "relationship" else s.EntityReference(kind=kind, id=value)
            )
    result = {}
    for unit in units:
        modes = s.ScenePlanningModes(**{field: getattr(unit, f"{field}_mode") for field in PLANNING_FIELDS})
        result[unit.id] = s.ScenePlanningRaw(
            modes=modes,
            pov_character_id=unit.pov_character_id if modes.pov == "override" else None,
            tag_ids=sorted(tags[unit.id]) if modes.tags == "override" else [],
            references=sorted(
                (ref for ref in refs[unit.id] if getattr(modes, ref.kind) == "override"), key=query.reference_key
            ),
        )
    return result


async def resolve_scene_plannings(
    db: AsyncSession, project_id: int, scene_ids: Sequence[int], *, chapter_rows: list[s.PlanningRow] | None = None
) -> dict[int, s.ScenePlanningOut]:
    """批量只读快照，矩阵与单场景卡片共用；没有本地行时保持虚拟继承。"""
    if not scene_ids:
        return {}
    with db.no_autoflush:
        before = await query.revision(db, project_id)
        if len(scene_ids) > 400:
            parents = chapter_rows if chapter_rows is not None else await query.rows(db, project_id)
            result = {}
            for start in range(0, len(scene_ids), 400):
                result.update(await resolve_scene_plannings(
                    db, project_id, scene_ids[start:start + 400], chapter_rows=parents,
                ))
            if await query.revision(db, project_id) != before:
                raise ConflictError("场景规划读取期间已变化，请重新预览", extra={"code": "planning_context_changed"})
            return result
        scenes = list(await db.execute(select(Scene.id, Scene.chapter_id)
                                      .join(Chapter, Chapter.id == Scene.chapter_id).where(
            Chapter.project_id == project_id, Scene.id.in_(scene_ids), Scene.active.is_(True)
        )))
        if {scene.id for scene in scenes} != set(scene_ids):
            raise NotFoundError("场景不存在或不属于该作品")
        units = list(await db.scalars(select(PlanningUnit).where(
            PlanningUnit.project_id == project_id, PlanningUnit.scene_id.in_(scene_ids),
            PlanningUnit.source_level == "scene", PlanningUnit.retired.is_(False),
        )))
        units_by_scene = {unit.scene_id: unit for unit in units}
        raw_by_unit = await _raw_values(db, units)
        parents = {row.chapter_id: row for row in (
            chapter_rows if chapter_rows is not None else await query.rows(db, project_id)
        ) if row.source.kind == "chapter"}
        names = dict((await db.execute(select(Character.id, Character.name).where(
            Character.project_id == project_id
        ))).all())
        tag_ids = {tag_id for raw in raw_by_unit.values() for tag_id in raw.tag_ids}
        tags = {}
        ordered_tags = sorted(tag_ids)
        for start in range(0, len(ordered_tags), 400):
            page = await db.scalars(select(PlanningTag).where(
                PlanningTag.project_id == project_id, PlanningTag.id.in_(ordered_tags[start:start + 400])
            ))
            tags.update({tag.id: s.PlanningTagOut.model_validate(tag) for tag in page})
        result = {}
        for scene in scenes:
            parent = parents.get(scene.chapter_id)
            if parent is None:
                raise NotFoundError("场景所属章节不存在")
            unit = units_by_scene.get(scene.id)
            raw = raw_by_unit[unit.id] if unit else s.ScenePlanningRaw()
            sources = {field: "scene" if getattr(raw.modes, field) == "override" else "chapter"
                       for field in PLANNING_FIELDS}
            pov = raw.pov_character_id if sources["pov"] == "scene" else parent.pov_character_id
            references = [ref for ref in parent.references if sources[ref.kind] == "chapter"]
            references += [ref for ref in raw.references if sources[ref.kind] == "scene"]
            result[scene.id] = s.ScenePlanningOut(
                revision=before, unit_id=unit.id if unit else None,
                binding_epoch=unit.binding_epoch if unit else None, raw=raw,
                effective=s.ScenePlanningEffective(
                    pov_character_id=pov if pov in names else None, pov_character_name=names.get(pov),
                    tags=([tags[tag_id] for tag_id in raw.tag_ids if tag_id in tags]
                          if sources["tags"] == "scene" else parent.tags),
                    references=sorted(references, key=query.reference_key),
                    sources=s.ScenePlanningSources(**sources),
                ),
            )
        if await query.revision(db, project_id) != before:
            raise ConflictError("场景规划读取期间已变化，请重新预览", extra={"code": "planning_context_changed"})
        return result


async def resolve_scene_planning(db: AsyncSession, project_id: int, scene_id: int) -> s.ScenePlanningOut:
    return (await resolve_scene_plannings(db, project_id, [scene_id]))[scene_id]


async def capture_scene_planning(
    db: AsyncSession, project_id: int, scene_ids: list[int]
) -> dict[int, dict[str, Any] | None]:
    with db.no_autoflush:
        if len(scene_ids) > 400:
            result = {}
            for start in range(0, len(scene_ids), 400):
                result.update(await capture_scene_planning(db, project_id, scene_ids[start:start + 400]))
            return result
        owned = set(await db.scalars(select(Scene.id).join(Chapter, Chapter.id == Scene.chapter_id).where(
            Chapter.project_id == project_id, Scene.id.in_(scene_ids)
        )))
        if owned != set(scene_ids):
            raise NotFoundError("场景不存在或不属于该作品")
        units = list(await db.scalars(select(PlanningUnit).where(
            PlanningUnit.project_id == project_id, PlanningUnit.scene_id.in_(scene_ids),
            PlanningUnit.source_level == "scene", PlanningUnit.retired.is_(False),
        )))
        raw = await _raw_values(db, units)
        result = dict.fromkeys(scene_ids)
        for unit in units:
            result[unit.scene_id] = {"unit_id": unit.id, "binding_epoch": unit.binding_epoch,
                                     "raw": raw[unit.id].model_dump(mode="json")}
        return result


async def _validate_raw(db: AsyncSession, project_id: int, raw: s.ScenePlanningRaw) -> None:
    from app.services.planning_service import _validate_ids

    await _validate_ids(db, project_id, Character, [raw.pov_character_id] if raw.pov_character_id else [])
    await _validate_ids(db, project_id, PlanningTag, raw.tag_ids)
    joins = {"character": (PlanningUnitCharacter, "character_id", Character), **query.REFERENCE_JOINS}
    for kind, (_, _, target) in joins.items():
        ids = [ref.timeline_id if ref.kind == "relationship" else ref.id for ref in raw.references if ref.kind == kind]
        await _validate_ids(db, project_id, target, ids, relationship=kind == "relationship")


async def _write_raw(db: AsyncSession, unit: PlanningUnit, raw: s.ScenePlanningRaw) -> None:
    from app.services.planning_service import _set_join

    await _validate_raw(db, unit.project_id, raw)
    for field in PLANNING_FIELDS:
        setattr(unit, f"{field}_mode", getattr(raw.modes, field))
    unit.pov_character_id = raw.pov_character_id
    await _set_join(db, PlanningUnitTag, "tag_id", unit.id, raw.tag_ids)
    joins = {"character": (PlanningUnitCharacter, "character_id", Character), **query.REFERENCE_JOINS}
    for kind, (model, column, _) in joins.items():
        ids = [ref.timeline_id if ref.kind == "relationship" else ref.id for ref in raw.references if ref.kind == kind]
        await _set_join(db, model, column, unit.id, ids)
    await db.flush()


async def restore_scene_planning(
    db: AsyncSession, project_id: int, scene_id: int, snapshot: dict | None
) -> None:
    """结构历史只恢复本地意图；独立节点与时间线从不写回。"""
    scene, _ = await _scene(db, project_id, scene_id, active=False)
    unit = await db.scalar(select(PlanningUnit).where(
        PlanningUnit.project_id == project_id, PlanningUnit.scene_id == scene.id, PlanningUnit.retired.is_(False)
    ))
    try:
        raw = s.ScenePlanningRaw.model_validate(snapshot["raw"]) if snapshot else s.ScenePlanningRaw()
    except (ValidationError, KeyError, TypeError) as exc:
        raise BadRequestError("场景历史中的规划数据无效") from exc
    if snapshot is None and unit is not None and unit.binding_epoch != 0:
        raise ConflictError("旧场景历史不能覆盖后来重新关联的规划，请先处理引用", extra={
            "code": "scene_reference_conflict",
        })
    if snapshot is not None and (
        set(snapshot) != {"unit_id", "binding_epoch", "raw"}
        or unit is None or unit.id != snapshot["unit_id"] or unit.binding_epoch != snapshot["binding_epoch"]
        or type(snapshot["unit_id"]) is not int or type(snapshot["binding_epoch"]) is not int
    ):
        raise ConflictError("场景规划身份或绑定代次已变化，请先处理引用", extra={"code": "scene_reference_conflict"})
    if unit is not None:
        await _write_raw(db, unit, raw)


async def clone_scene_planning(
    db: AsyncSession, project_id: int, source_scene_id: int, target_scene_id: int
) -> None:
    """拆分／明确选择元数据后复制意图；保留双方身份，不复制独立剧情事实。"""
    source, _ = await _scene(db, project_id, source_scene_id, active=False)
    target, _ = await _scene(db, project_id, target_scene_id, active=False)
    if source.chapter_id != target.chapter_id:
        raise BadRequestError("场景本地规划只能在同章结构操作中复制")
    if source_scene_id == target_scene_id:
        return
    snapshot = (await capture_scene_planning(db, project_id, [source_scene_id]))[source_scene_id]
    raw = s.ScenePlanningRaw.model_validate(snapshot["raw"]) if snapshot else s.ScenePlanningRaw()
    unit = await db.scalar(select(PlanningUnit).where(
        PlanningUnit.project_id == project_id, PlanningUnit.scene_id == target.id, PlanningUnit.retired.is_(False)
    ))
    if unit is None and snapshot is not None:
        unit = PlanningUnit(project_id=project_id, source_level="scene", scene_id=target.id,
                            last_source_label=target.title)
        db.add(unit)
        await db.flush()
    if unit is not None:
        await _write_raw(db, unit, raw)


async def scene_reference_blockers(db: AsyncSession, project_id: int, scene_ids: list[int]) -> list[dict]:
    """保留历史也属于引用；不能因为事件暂未启用就悄悄移动或吞并边界。"""
    if not scene_ids:
        return []
    with db.no_autoflush:
        if len(scene_ids) > 400:
            result = []
            for start in range(0, len(scene_ids), 400):
                result.extend(await scene_reference_blockers(db, project_id, scene_ids[start:start + 400]))
            return sorted(result, key=lambda row: (row["scene_id"], row["kind"], row["id"]))
        result = [{"kind": "progression", "id": row.id, "scene_id": row.scene_id,
                   "label": row.anchor_label or "场景设定变化"} for row in await db.execute(
            select(SettingProgression.id, SettingProgression.scene_id, SettingProgression.anchor_label)
            .join(SettingTimeline, SettingTimeline.id == SettingProgression.timeline_id)
            .where(SettingTimeline.project_id == project_id, SettingProgression.scene_id.in_(scene_ids))
        )]
        for model, kind, label in ((SubplotNode, "node", "独立支线节点"),
                                   (SubplotCrossing, "crossing", "独立支线交汇")):
            for identity, scene_id in await db.execute(select(model.id, PlanningUnit.scene_id).join(
                PlanningUnit, PlanningUnit.id == model.unit_id
            ).where(PlanningUnit.project_id == project_id, PlanningUnit.scene_id.in_(scene_ids))):
                result.append({"kind": kind, "id": identity, "scene_id": scene_id, "label": label})
        return sorted(result, key=lambda row: (row["scene_id"], row["kind"], row["id"]))


async def retire_scene_planning(db: AsyncSession, project_id: int, scene_ids: list[int]) -> None:
    """退场保持 scene FK 与本地意图；只保存最后标签供待关联目录显示。"""
    for unit, title in await db.execute(select(PlanningUnit, Scene.title).join(Scene, Scene.id == PlanningUnit.scene_id)
                                      .join(Chapter, Chapter.id == Scene.chapter_id).where(
        PlanningUnit.project_id == project_id, Chapter.project_id == project_id, Scene.id.in_(scene_ids),
    )):
        unit.last_source_label = title[:300]
    await db.flush()


async def assert_tag_unreferenced(db: AsyncSession, project_id: int, tag_id: int) -> None:
    """标签真删除不能使场景本地意图或其结构历史成为无法导入的坏核心图。"""
    with db.no_autoflush:
        sources = [{"kind": "scene_planning", "unit_id": row.id, "scene_id": row.scene_id}
                   for row in await db.execute(select(PlanningUnit.id, PlanningUnit.scene_id).join(
                       PlanningUnitTag, PlanningUnitTag.unit_id == PlanningUnit.id,
                   ).where(PlanningUnit.project_id == project_id, PlanningUnit.source_level == "scene",
                           PlanningUnitTag.tag_id == tag_id).order_by(PlanningUnit.id).limit(20))]
        snapshots = await db.stream(select(ChapterVersion.id, ChapterVersion.scene_snapshot).join(
            Chapter, Chapter.id == ChapterVersion.chapter_id,
        ).where(Chapter.project_id == project_id, ChapterVersion.scene_snapshot.is_not(None))
                                   .order_by(ChapterVersion.id))
        try:
            async for version_id, snapshot in snapshots:
                if len(sources) >= 20:
                    break
                if not isinstance(snapshot, dict) or not isinstance(snapshot.get("scenes"), list):
                    continue
                for scene in snapshot["scenes"]:
                    planning = scene.get("planning") if isinstance(scene, dict) else None
                    raw = planning.get("raw") if isinstance(planning, dict) else None
                    if isinstance(raw, dict) and tag_id in raw.get("tag_ids", []):
                        sources.append({"kind": "scene_history", "version_id": version_id,
                                        "scene_id": scene.get("id")})
                        break
        finally:
            await snapshots.close()
        if sources:
            raise ConflictError("该标签仍被场景本地规划或可恢复结构历史引用，请先处理相关资料", extra={
                "code": "planning_tag_in_use", "sources": sources,
            })


async def assert_settings_unreferenced(db: AsyncSession, project_id: int, targets: Sequence[EntityRef]) -> None:
    """真删除／覆盖不能级联吞掉本地意图，也不能使可恢复结构中的引用失效。"""
    wanted = {(ref.kind, ref.id) for ref in targets}
    if not wanted:
        return
    with db.no_autoflush:
        units = list(await db.scalars(select(PlanningUnit).where(
            PlanningUnit.project_id == project_id, PlanningUnit.source_level == "scene"
        )))
        raws = await _raw_values(db, units)
        relationship_ids = set(await db.scalars(select(SettingTimeline.id).where(
            SettingTimeline.project_id == project_id, SettingTimeline.subject_kind == "relationship",
            (SettingTimeline.character_id.in_([identity for kind, identity in wanted if kind == "character"]))
            | (SettingTimeline.related_character_id.in_(
                [identity for kind, identity in wanted if kind == "character"]
            )),
        )))

        def used(raw: dict) -> bool:
            if ("character", raw.get("pov_character_id")) in wanted:
                return True
            return any(
                (ref.get("kind"), ref.get("id")) in wanted
                or (ref.get("kind") == "relationship" and ref.get("timeline_id") in relationship_ids)
                for ref in raw.get("references", []) if isinstance(ref, dict)
            )

        sources = [{"kind": "scene_planning", "unit_id": unit.id, "scene_id": unit.scene_id}
                   for unit in units if used(raws[unit.id].model_dump(mode="json"))]
        snapshots = await db.stream(select(ChapterVersion.id, ChapterVersion.scene_snapshot).join(
            Chapter, Chapter.id == ChapterVersion.chapter_id
        ).where(Chapter.project_id == project_id, ChapterVersion.scene_snapshot.is_not(None)))
        try:
            async for version_id, snapshot in snapshots:
                if not isinstance(snapshot, dict) or not isinstance(snapshot.get("scenes"), list):
                    continue
                for scene in snapshot["scenes"]:
                    planning = scene.get("planning") if isinstance(scene, dict) else None
                    raw = planning.get("raw") if isinstance(planning, dict) else None
                    if isinstance(raw, dict) and used(raw):
                        sources.append({"kind": "scene_history", "version_id": version_id,
                                        "scene_id": scene.get("id")})
                        break
                if len(sources) >= 20:
                    break
        finally:
            await snapshots.close()
        if sources:
            raise ConflictError("该条目仍被场景本地规划或可恢复结构历史引用，请先处理相关资料", extra={
                "code": "setting_target_in_use", "sources": sources[:20],
            })
