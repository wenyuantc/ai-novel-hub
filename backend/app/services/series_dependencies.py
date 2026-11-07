"""解绑前检查会失效的已保存资料；预检与持锁写入共用同一套规则。"""

from collections.abc import Sequence

from sqlalchemy import literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import (
    CareerHistory,
    Chapter,
    ChapterCharacter,
    ChapterPlan,
    ChapterVersion,
    Character,
    CharacterCareer,
    ForeshadowCharacter,
    OrganizationMember,
    OutlineSnapshot,
    PlanningUnit,
    PlanningUnitWorld,
    Relationship,
    SettingAiLink,
    SettingProgression,
    SettingReferenceHandle,
    SettingTextField,
    SettingTimeline,
    WorldSetting,
)
from app.models.planning import PlanningUnitCharacter
from app.schemas.setting_field_value import EntityRef
from app.services import setting_field_service

_CHARACTER_REFS = (
    ("章节出场", ChapterCharacter.character_id),
    ("场景本地角色", PlanningUnitCharacter.character_id),
    ("规划视角", PlanningUnit.pov_character_id),
    ("组织成员", OrganizationMember.character_id),
    ("职业任职", CharacterCareer.character_id),
    ("职业历史", CareerHistory.character_id),
    ("伏笔角色", ForeshadowCharacter.character_id),
)
_BOOK_CHAR_BASELINE = {"age": "", "current_state": "", "life_status": "alive"}


def _exists(label, *where):
    return select(literal(label)).where(select(literal(1)).where(*where).exists())


async def _matches_array(db, statement, ids, *, snapshots=False) -> bool:
    rows = await db.stream_scalars(statement)
    try:
        async for row in rows:
            arrays = (
                (plan.get("character_ids") for plan in row.get("plans", ()) if isinstance(plan, dict))
                if snapshots and isinstance(row, dict) and isinstance(row.get("plans"), list)
                else (row,)
            )
            if any(isinstance(values, list) and any(type(value) is int and value in ids for value in values)
                   for values in arrays):
                return True
        return False
    finally:
        await rows.close()


async def blocking_reasons(db: AsyncSession, project_id: int, refs: Sequence[EntityRef]) -> list[str]:
    """只统计当前作品使用，快照和停用事件不能被旧资料清理悄悄删除。"""
    if not refs:
        return []
    chars = {ref.id for ref in refs if ref.kind == "character"}
    worlds = {ref.id for ref in refs if ref.kind == "world"}
    own_timelines = select(SettingTimeline.id).where(
        SettingTimeline.project_id == project_id,
        or_(
            (SettingTimeline.subject_kind == "character") & SettingTimeline.character_id.in_(chars),
            (SettingTimeline.subject_kind == "world") & SettingTimeline.world_setting_id.in_(worlds),
        ),
    )
    handles = select(SettingReferenceHandle.id).where(
        SettingReferenceHandle.project_id == project_id,
        or_(SettingReferenceHandle.character_id.in_(chars), SettingReferenceHandle.world_setting_id.in_(worlds)),
    )
    statements = [
        _exists("剧情变化", SettingProgression.timeline_id.in_(own_timelines)),
        _exists("本书自定义字段", SettingTextField.timeline_id.in_(own_timelines)),
        _exists(
            "AI 关联",
            or_(SettingAiLink.source_handle_id.in_(handles), SettingAiLink.target_handle_id.in_(handles)),
        ),
    ]
    if worlds:
        statements.append(_exists("规划世界观", PlanningUnitWorld.world_id.in_(worlds)))
    if chars:
        statements.extend(_exists(label, column.in_(chars)) for label, column in _CHARACTER_REFS)
        statements.extend([
            _exists("人物关系", or_(Relationship.a_id.in_(chars), Relationship.b_id.in_(chars))),
            _exists(
                "关联剧情历史",
                SettingTimeline.project_id == project_id,
                SettingTimeline.subject_kind != "character",
                or_(SettingTimeline.character_id.in_(chars), SettingTimeline.related_character_id.in_(chars)),
            ),
        ])
    reasons = list(await db.scalars(union_all(*statements)))
    snapshots = await db.stream_scalars(select(ChapterVersion.scene_snapshot).join(
        Chapter, Chapter.id == ChapterVersion.chapter_id
    ).where(Chapter.project_id == project_id, ChapterVersion.scene_snapshot.is_not(None)))
    try:
        async for snapshot in snapshots:
            if _scene_snapshot_references(snapshot, chars, worlds):
                reasons.append("可恢复的场景规划历史")
                break
    finally:
        await snapshots.close()
    if chars:
        for label, statement, snapshots in (
            ("细纲出场", select(ChapterPlan.character_ids).where(ChapterPlan.project_id == project_id), False),
            (
                "待关联规划",
                select(PlanningUnit.detached_character_ids).where(PlanningUnit.project_id == project_id),
                False,
            ),
            ("可恢复的大纲快照", select(OutlineSnapshot.data).where(OutlineSnapshot.project_id == project_id), True),
        ):
            if await _matches_array(db, statement, chars, snapshots=snapshots):
                reasons.append(label)
        rows = await db.execute(select(
            Character.role_type, Character.always_in_context, Character.relations_text, Character.setting_tags,
            Character.age, Character.current_state, Character.life_status,
        ).where(Character.id.in_(chars)))
        if any(role or always or relations or tags or age or state or life != "alive"
               for role, always, relations, tags, age, state, life in rows):
            reasons.append("本书角色资料")
        baselines = await db.scalars(select(SettingTimeline.baseline).where(
            SettingTimeline.id.in_(own_timelines), SettingTimeline.subject_kind == "character",
        ))
        if any(any(base.get(key, default) != default for key, default in _BOOK_CHAR_BASELINE.items())
               for base in baselines):
            reasons.append("本书起始状态")
    if worlds and any(await db.scalars(select(WorldSetting.setting_tags).where(WorldSetting.id.in_(worlds)))):
        reasons.append("本书标签")
    try:
        await setting_field_service.assert_targets_unreferenced(db, project_id, refs)
    except ConflictError as exc:
        if (exc.extra or {}).get("code") != "setting_target_in_use":
            raise
        reasons.append("本书字段引用或历史")
    return reasons


def _scene_snapshot_references(snapshot, chars: set[int], worlds: set[int]) -> bool:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("scenes"), list):
        return False
    for scene in snapshot["scenes"]:
        planning = scene.get("planning") if isinstance(scene, dict) else None
        raw = planning.get("raw") if isinstance(planning, dict) else None
        if not isinstance(raw, dict):
            continue
        if type(raw.get("pov_character_id")) is int and raw["pov_character_id"] in chars:
            return True
        references = raw.get("references")
        if not isinstance(references, list):
            continue
        for ref in references:
            if not isinstance(ref, dict) or type(ref.get("id")) is not int:
                continue
            if ref.get("kind") == "character" and ref["id"] in chars:
                return True
            if ref.get("kind") == "world" and ref["id"] in worlds:
                return True
    return False
