"""将章节分析回写编排为时间线事件；事务由调用方拥有。

事件的读写、版本控制和最新投影均委托统一时间线服务。本模块只负责
识别同章旧自动事件及它们与 AnalysisChange 的来源关系。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.models import AnalysisChange, ChapterAnalysis, SettingProgression, SettingTimeline
from app.schemas.progression import ProgressionCreate, StoryPosition
from app.services import setting_progression_service as progressions

REVERT_CONFLICT = "该项已被后续修改，无法撤销"


@dataclass(slots=True)
class PreparedField:
    """当前章初字段，以及同源章节/主体/字段可自动替代的旧事件组。"""

    timeline: SettingTimeline
    field_key: str
    before: Any
    previous: list[SettingProgression]


async def prepare_field(
    db: AsyncSession,
    analysis: ChapterAnalysis,
    timeline: SettingTimeline,
    *,
    source_field_key: str,
    field_key: str,
) -> PreparedField:
    if timeline.project_id != analysis.project_id:
        raise ValueError("分析与设定时间线不属于同一作品")
    previous = list(
        await db.scalars(
            select(SettingProgression)
            .join(AnalysisChange, AnalysisChange.id == SettingProgression.source_change_id)
            .where(
                SettingProgression.timeline_id == timeline.id,
                SettingProgression.field_key == field_key,
                SettingProgression.active.is_(True),
                SettingProgression.source == "analysis",
                SettingProgression.protection == "automatic",
                SettingProgression.anchor_kind == "chapter",
                SettingProgression.chapter_id == analysis.chapter_id,
                SettingProgression.boundary == "end",
                AnalysisChange.project_id == analysis.project_id,
                AnalysisChange.field_key == source_field_key,
            )
            .order_by(SettingProgression.sort_order, SettingProgression.id)
        )
    )
    position = StoryPosition(chapter_id=analysis.chapter_id, chapter_no=analysis.chapter_no, boundary="start")
    resolved = await progressions.resolve_timelines(
        db,
        analysis.project_id,
        [timeline],
        position,
        exclude_event_ids=[event.id for event in previous],
    )
    return PreparedField(timeline, field_key, resolved[timeline.id].fields.get(field_key), previous)


async def apply_field(
    db: AsyncSession,
    analysis: ChapterAnalysis,
    change: AnalysisChange,
    prepared: PreparedField,
    value: Any,
    *,
    protection: str = "automatic",
) -> SettingProgression:
    return (await apply_fields(db, analysis, change, [(prepared, value)], protection=protection))[0]


async def apply_fields(
    db: AsyncSession,
    analysis: ChapterAnalysis,
    change: AnalysisChange,
    fields: Sequence[tuple[PreparedField, Any]],
    *,
    protection: str = "automatic",
) -> list[SettingProgression]:
    """在调用方 SAVEPOINT 内替代旧自动组并创建新来源；重复同一来源幂等。"""
    if change.id is None:
        raise ValueError("写入剧情变化前必须先 flush 分析回写条目")
    if not fields or len({prepared.timeline.id for prepared, _ in fields}) != 1:
        raise ValueError("同一分析组必须属于一个设定主体")
    existing = list(
        await db.scalars(select(SettingProgression).where(SettingProgression.source_change_id == change.id))
    )
    if existing:
        by_field = {event.field_key: event for event in existing}
        if len(existing) == len(fields) and all(
            (event := by_field.get(prepared.field_key)) is not None and event.active and event.value == value
            for prepared, value in fields
        ):
            return [by_field[prepared.field_key] for prepared, _ in fields]
        raise ConflictError("分析回写来源已被修改或替代")

    timeline = fields[0][0].timeline
    events = []
    for prepared, value in fields:
        order = min((event.sort_order for event in prepared.previous), default=0)
        for old in prepared.previous:
            await progressions.revoke_progression(
                db, timeline, old, timeline.revision, protect=False, materialize=False
            )
        data = ProgressionCreate(
            expected_revision=timeline.revision,
            field_key=prepared.field_key,
            operation="replace",
            value=value,
            anchor_kind="chapter",
            chapter_id=analysis.chapter_id,
            boundary="end",
            sort_order=order,
            note=f"第 {analysis.chapter_no} 章分析",
        )
        event = await progressions.apply_progression(
            db,
            timeline,
            data,
            source="analysis",
            protection=protection,
            source_change_id=change.id,
            source_content_hash=analysis.content_hash,
            materialize=False,
        )
        for old in prepared.previous:
            old.superseded_by_id = event.id
        events.append(event)
    await db.flush()
    await progressions.materialize_timeline(db, timeline)
    source_ids = {old.source_change_id for prepared, _ in fields for old in prepared.previous if old.source_change_id}
    for source_id in source_ids:
        old_change = await db.get(AnalysisChange, source_id)
        remaining = await db.scalar(
            select(SettingProgression.id).where(
                SettingProgression.source_change_id == source_id, SettingProgression.active.is_(True)
            ).limit(1)
        )
        if old_change is not None:
            old_change.reason = "部分变化已替代，其余需复核" if remaining is not None else "已被重新分析替代"
    return events


async def revoke_group(db: AsyncSession, change: AnalysisChange) -> bool:
    """停用同一原分析的整组事件；没有来源事件时返回 False，供旧记录兼容分流。"""
    events = list(
        await db.scalars(
            select(SettingProgression)
            .join(SettingTimeline, SettingTimeline.id == SettingProgression.timeline_id)
            .where(SettingTimeline.project_id == change.project_id, SettingProgression.source_change_id == change.id)
            .order_by(SettingProgression.id)
        )
    )
    if not events:
        return False
    if any(not event.active or event.protection == "manual" or event.revision != 1 for event in events):
        raise ConflictError(REVERT_CONFLICT)
    timeline_ids = {event.timeline_id for event in events}
    timelines = {
        timeline.id: timeline
        for timeline in (
            await db.scalars(
                select(SettingTimeline).where(
                    SettingTimeline.project_id == change.project_id,
                    SettingTimeline.id.in_(timeline_ids),
                )
            )
        ).all()
    }
    if set(timelines) != timeline_ids:
        raise ConflictError(REVERT_CONFLICT)
    for event in events:
        timeline = timelines[event.timeline_id]
        await progressions.revoke_progression(
            db, timeline, event, timeline.revision, protect=False, materialize=False
        )
    for timeline in timelines.values():
        await progressions.materialize_timeline(db, timeline)
    return True
