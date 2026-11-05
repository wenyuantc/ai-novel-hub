"""关系时间线的稳定主体和最新关系载体。

此模块只负责关系相关的验证、载体同步和旧审计，不负责剧情事件的求值或提交事务。
"""

from collections.abc import Mapping, Sequence
from typing import Any, cast

from pydantic import JsonValue
from sqlalchemy import or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.models import (
    Character,
    Relationship,
    RelationshipHistory,
    RelationshipType,
    SettingProgression,
    SettingTimeline,
)
from app.schemas.progression import BaselineStatus
from app.services.setting_progression_resolver import EffectiveSnapshot, validate_baseline


def relationship_snapshot(rel: Relationship) -> dict[str, JsonValue]:
    """从当前关系行构造完整基础快照，不以人物姓名反查身份。"""
    if not rel.is_present:
        return {"exists": False}
    return {
        "exists": True,
        "identity": {"a_id": rel.a_id, "b_id": rel.b_id, "type_id": rel.type_id},
        "intimacy": rel.intimacy,
        "status": rel.status,
        "description": rel.description,
    }


def _type_id_from_value(field_key: str | None, value: object) -> int | None:
    """读取关系基础、身份事件或建立事件中的类型引用。"""
    if not isinstance(value, dict):
        return None
    if field_key is None:
        identity = value.get("identity")
    elif field_key == "identity":
        identity = value
    elif field_key == "presence" and value.get("exists") is True:
        state = value.get("state")
        identity = state.get("identity") if isinstance(state, dict) else None
    else:
        return None
    if not isinstance(identity, dict):
        return None
    type_id = identity.get("type_id")
    return type_id if type(type_id) is int else None


async def validate_relationship_type_reference(
    db: AsyncSession, timeline: SettingTimeline, field_key: str | None, value: JsonValue
) -> None:
    """校验关系类型可见性，不能只依赖最新投影以免历史非法引用被遮盖。

    调用方先用共享 schema/reducer 校验结构；field_key=None 表示完整基础快照。
    """
    if timeline.subject_kind != "relationship":
        raise ValueError("必须提供关系时间线")
    type_id = _type_id_from_value(field_key, value)
    if type_id is None:
        return
    visible = await db.scalar(
        select(RelationshipType.id).where(
            RelationshipType.id == type_id,
            or_(RelationshipType.project_id.is_(None), RelationshipType.project_id == timeline.project_id),
        )
    )
    if visible is None:
        raise BadRequestError("关系类型不存在或不属于该项目")


async def validate_relationship_references(
    db: AsyncSession, project_id: int, timelines: Sequence[SettingTimeline]
) -> None:
    """批量校验全部基础及历史类型引用，包括停用和无法定位章节的事件。"""
    if not timelines:
        return
    if any(
        timeline.project_id != project_id or timeline.subject_kind != "relationship" or timeline.id is None
        for timeline in timelines
    ):
        raise ValueError("必须提供同一作品的已保存关系时间线")
    timeline_ids = {timeline.id for timeline in timelines}
    type_ids = {
        type_id for timeline in timelines if (type_id := _type_id_from_value(None, timeline.baseline)) is not None
    }
    rows = await db.execute(
        select(SettingProgression.field_key, SettingProgression.value).where(
            SettingProgression.timeline_id.in_(timeline_ids),
            SettingProgression.field_key.in_(("identity", "presence")),
        )
    )
    type_ids.update(
        type_id for field_key, value in rows if (type_id := _type_id_from_value(field_key, value)) is not None
    )
    if not type_ids:
        return
    visible_types = set(
        (
            await db.scalars(
                select(RelationshipType.id).where(
                    RelationshipType.id.in_(type_ids),
                    or_(RelationshipType.project_id.is_(None), RelationshipType.project_id == project_id),
                )
            )
        ).all()
    )
    if visible_types != type_ids:
        raise BadRequestError("关系类型不存在或不属于该项目")


async def type_referenced_by_story(db: AsyncSession, project_id: int, type_id: int) -> bool:
    """保护仅被基础、已撤销或孤儿剧情事件引用的类型。"""
    baselines = await db.scalars(
        select(SettingTimeline.baseline).where(
            SettingTimeline.project_id == project_id, SettingTimeline.subject_kind == "relationship"
        )
    )
    if any(_type_id_from_value(None, baseline) == type_id for baseline in baselines):
        return True
    rows = await db.execute(
        select(SettingProgression.field_key, SettingProgression.value)
        .join(SettingTimeline, SettingTimeline.id == SettingProgression.timeline_id)
        .where(
            SettingTimeline.project_id == project_id,
            SettingTimeline.subject_kind == "relationship",
            SettingProgression.field_key.in_(("identity", "presence")),
        )
    )
    return any(_type_id_from_value(field_key, value) == type_id for field_key, value in rows)


async def get_or_create_relationship_timeline(
    db: AsyncSession,
    project_id: int,
    a_id: int,
    b_id: int,
    *,
    initial_state: Mapping[str, JsonValue] | None = None,
    baseline_status: BaselineStatus = "confirmed",
) -> SettingTimeline:
    """按无方向人物对取得稳定主体；初始不存在时显式建立空基线，只 flush。"""
    if a_id == b_id:
        raise BadRequestError("不能与自己建立关系")
    low, high = sorted((a_id, b_id))
    characters = set(
        (
            await db.scalars(
                select(Character.id).where(Character.project_id == project_id, Character.id.in_((low, high)))
            )
        ).all()
    )
    if characters != {low, high}:
        raise BadRequestError("角色不存在或不属于该项目")
    timeline = await db.scalar(
        select(SettingTimeline).where(
            SettingTimeline.project_id == project_id,
            SettingTimeline.subject_kind == "relationship",
            SettingTimeline.character_id == low,
            SettingTimeline.related_character_id == high,
        )
    )
    if timeline is not None:
        return timeline
    baseline = dict(initial_state) if initial_state is not None else {"exists": False}
    try:
        validate_baseline("relationship", baseline, subject_identity=(low, high))
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    timeline = SettingTimeline(
        project_id=project_id,
        subject_kind="relationship",
        character_id=low,
        related_character_id=high,
        baseline=baseline,
        baseline_status=baseline_status,
        legacy_compat_allowed=baseline_status == "legacy_unverified",
    )
    await validate_relationship_type_reference(db, timeline, None, baseline)
    db.add(timeline)
    await db.flush()
    return timeline


async def materialize_relationships(
    db: AsyncSession,
    project_id: int,
    timelines: Sequence[SettingTimeline],
    snapshots: Mapping[int, EffectiveSnapshot],
) -> dict[int, Relationship | None]:
    """批量同步关系载体，仅以主体类型为单位查询，不写事件或审计。

    未建立的关系不创建空载体；已有载体隐藏而不删除，以保留旧审计。
    所有输入先校验，再修改载体；调用方拥有项目锁与事务。
    """
    if not timelines:
        return {}
    pairs: dict[int, tuple[int, int]] = {}
    character_ids: set[int] = set()
    type_ids: set[int] = set()
    for timeline in timelines:
        if (
            timeline.project_id != project_id
            or timeline.subject_kind != "relationship"
            or timeline.id is None
            or timeline.character_id is None
            or timeline.related_character_id is None
        ):
            raise ValueError("必须提供同一作品的已保存关系时间线")
        pair = (timeline.character_id, timeline.related_character_id)
        if pair[0] >= pair[1] or timeline.id not in snapshots:
            raise ValueError("关系主体的人物对或有效快照无效")
        try:
            validate_baseline("relationship", snapshots[timeline.id].fields, subject_identity=pair)
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
        pairs[timeline.id] = pair
        character_ids.update(pair)
        if snapshots[timeline.id].fields["exists"] is True:
            identity = cast(dict[str, JsonValue], snapshots[timeline.id].fields["identity"])
            type_ids.add(cast(int, identity["type_id"]))

    characters = set(
        (
            await db.scalars(
                select(Character.id).where(Character.project_id == project_id, Character.id.in_(character_ids))
            )
        ).all()
    )
    if characters != character_ids:
        raise BadRequestError("角色不存在或不属于该项目")
    visible_types = (
        set(
            (
                await db.scalars(
                    select(RelationshipType.id).where(
                        RelationshipType.id.in_(type_ids),
                        or_(RelationshipType.project_id.is_(None), RelationshipType.project_id == project_id),
                    )
                )
            ).all()
        )
        if type_ids
        else set()
    )
    if visible_types != type_ids:
        raise BadRequestError("关系类型不存在或不属于该项目")

    carriers = {
        (rel.pair_low, rel.pair_high): rel
        for rel in (
            await db.scalars(
                select(Relationship).where(tuple_(Relationship.pair_low, Relationship.pair_high).in_(pairs.values()))
            )
        ).all()
    }
    if any(rel.project_id != project_id for rel in carriers.values()):
        raise BadRequestError("关系不属于该项目")

    result: dict[int, Relationship | None] = {}
    for timeline in timelines:
        pair = pairs[timeline.id]
        snapshot = snapshots[timeline.id]
        rel = carriers.get(pair)
        if snapshot.fields["exists"] is False:
            if rel is not None:
                rel.is_present = False
            result[timeline.id] = rel
            continue
        identity = cast(dict[str, JsonValue], snapshot.fields["identity"])
        if rel is None:
            rel = Relationship(project_id=project_id, pair_low=pair[0], pair_high=pair[1])
            db.add(rel)
        rel.a_id = cast(int, identity["a_id"])
        rel.b_id = cast(int, identity["b_id"])
        rel.type_id = cast(int, identity["type_id"])
        rel.intimacy = cast(int, snapshot.fields["intimacy"])
        rel.status = cast(str, snapshot.fields["status"])
        rel.description = cast(str, snapshot.fields["description"])
        rel.is_present = True
        result[timeline.id] = rel
    await db.flush()
    return result


async def materialize_relationship(
    db: AsyncSession, timeline: SettingTimeline, snapshot: EffectiveSnapshot
) -> Relationship | None:
    """单关系兼容入口，复用同一批量物化和校验。"""
    return (await materialize_relationships(db, timeline.project_id, [timeline], {timeline.id: snapshot}))[timeline.id]


async def append_analysis_audit(
    db: AsyncSession,
    relationship_id: int,
    changes: dict[str, Any],
    chapter_no: int,
    note: str = "",
) -> RelationshipHistory:
    """记录分析审计；事件写入、当前投影与事务由调用方负责。"""
    history = RelationshipHistory(
        relationship_id=relationship_id,
        chapter_no=chapter_no,
        source="analysis",
        changes=changes,
        note=note[:200],
    )
    db.add(history)
    await db.flush()
    return history
