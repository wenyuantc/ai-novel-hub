"""规划的只读投影；所有位置来自真实来源，不在 GET 中物化规划单元。"""

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Career,
    Chapter,
    ChapterCharacter,
    ChapterPlan,
    Character,
    Organization,
    Outline,
    Project,
    Relationship,
    SettingTimeline,
    WorldSetting,
)
from app.models.planning import (
    PlanningTag,
    PlanningUnit,
    PlanningUnitCareer,
    PlanningUnitOrganization,
    PlanningUnitRelationship,
    PlanningUnitTag,
    PlanningUnitWorld,
    SubplotCrossing,
    SubplotCrossingParticipant,
    SubplotNode,
)
from app.schemas.planning import (
    EntityReference,
    PlanningEntry,
    PlanningReference,
    PlanningRow,
    PlanningSource,
    PlanningTagOut,
    RelationshipReference,
    SubplotCrossingOut,
)
from app.services.mentions.context import MentionCandidate, load_candidates

REFERENCE_JOINS = {
    "world": (PlanningUnitWorld, "world_id", WorldSetting),
    "organization": (PlanningUnitOrganization, "organization_id", Organization),
    "career": (PlanningUnitCareer, "career_id", Career),
    "relationship": (PlanningUnitRelationship, "timeline_id", SettingTimeline),
}


def reference_key(reference: PlanningReference) -> str:
    identity = reference.timeline_id if reference.kind == "relationship" else reference.id
    return f"{reference.kind}:{identity}"


def source_key(source: PlanningSource) -> str:
    return f"{source.kind}:{source.id}"


async def revision(db: AsyncSession, project_id: int) -> int:
    return await db.scalar(select(Project.planning_revision).where(Project.id == project_id)) or 0


async def entries(
    db: AsyncSession, project_id: int, *, candidates: list[MentionCandidate] | None = None,
) -> list[PlanningEntry]:
    """只加载目录身份；关系选择包含隐藏及尚无当前载体的时间线。"""
    if candidates is None:
        candidates = await load_candidates(db, project_id)
    names = {item.ref.id: item.ref.name for item in candidates if item.ref.kind == "character"}
    result = [
        PlanningEntry(
            reference=EntityReference(kind=item.ref.kind, id=item.ref.id),
            name=item.ref.name,
            category=item.ref.category if item.ref.kind in ("character", "world") else None,
            tracking_supported=item.tracking.enabled,
        )
        for item in candidates
    ]
    carriers = {
        (row.pair_low, row.pair_high): row
        for row in await db.execute(
            select(Relationship.id, Relationship.pair_low, Relationship.pair_high, Relationship.is_present).where(
                Relationship.project_id == project_id
            )
        )
    }
    timelines = await db.execute(
        select(SettingTimeline.id, SettingTimeline.character_id, SettingTimeline.related_character_id).where(
            SettingTimeline.project_id == project_id, SettingTimeline.subject_kind == "relationship"
        )
    )
    for row in timelines:
        if row.character_id not in names or row.related_character_id not in names:
            continue
        carrier = carriers.get((row.character_id, row.related_character_id))
        result.append(
            PlanningEntry(
                reference=RelationshipReference(kind="relationship", timeline_id=row.id),
                name=f"{names[row.character_id]} ↔ {names[row.related_character_id]}",
                relationship_id=carrier.id if carrier else None,
                character_ids=[row.character_id, row.related_character_id],
                is_present=bool(carrier and carrier.is_present),
            )
        )
    return sorted(result, key=lambda item: (item.reference.kind, item.name.casefold(), reference_key(item.reference)))


async def rows(db: AsyncSession, project_id: int) -> list[PlanningRow]:
    chapters = list(
        await db.execute(
            select(
                Chapter.id, Chapter.title, Chapter.chapter_no, Chapter.outline_id, Chapter.plan_id, Chapter.word_count
            )
            .where(Chapter.project_id == project_id)
            .order_by(Chapter.chapter_no, Chapter.id)
        )
    )
    plans = list(
        await db.execute(
            select(
                ChapterPlan.id,
                ChapterPlan.title,
                ChapterPlan.seq,
                ChapterPlan.chapter_id,
                ChapterPlan.outline_id,
                ChapterPlan.character_ids,
            )
            .join(Outline, ChapterPlan.outline_id == Outline.id)
            .where(Outline.project_id == project_id)
        )
    )
    outlines = {
        row.id: row
        for row in await db.execute(
            select(
                Outline.id, Outline.title, Outline.volume_no, Outline.volume_title, Outline.sort, Outline.chapter_no
            ).where(Outline.project_id == project_id)
        )
    }
    units = list(
        await db.scalars(
            select(PlanningUnit)
            .where(PlanningUnit.project_id == project_id, PlanningUnit.retired.is_(False))
            .execution_options(populate_existing=True)
        )
    )
    by_chapter = {unit.chapter_id: unit for unit in units if unit.chapter_id is not None}
    by_plan = {unit.plan_id: unit for unit in units if unit.plan_id is not None and unit.chapter_id is None}
    character_names = dict(
        (await db.execute(select(Character.id, Character.name).where(Character.project_id == project_id))).all()
    )
    cast: dict[int, list[int]] = defaultdict(list)
    for chapter_id, character_id in await db.execute(
        select(ChapterCharacter.chapter_id, ChapterCharacter.character_id)
        .join(Chapter, Chapter.id == ChapterCharacter.chapter_id)
        .join(Character, Character.id == ChapterCharacter.character_id)
        .where(Chapter.project_id == project_id, Character.project_id == project_id)
    ):
        cast[chapter_id].append(character_id)
    tags: dict[int, list[PlanningTagOut]] = defaultdict(list)
    for unit_id, tag in await db.execute(
        select(PlanningUnitTag.unit_id, PlanningTag)
        .join(PlanningTag, PlanningTag.id == PlanningUnitTag.tag_id)
        .join(PlanningUnit, PlanningUnit.id == PlanningUnitTag.unit_id)
        .where(PlanningUnit.project_id == project_id, PlanningTag.project_id == project_id)
    ):
        tags[unit_id].append(PlanningTagOut.model_validate(tag))
    references: dict[int, list[PlanningReference]] = defaultdict(list)
    for kind, (join, column_name, target) in REFERENCE_JOINS.items():
        column = getattr(join, column_name)
        query = (
            select(join.unit_id, column)
            .join(PlanningUnit, PlanningUnit.id == join.unit_id)
            .join(target, target.id == column)
            .where(PlanningUnit.project_id == project_id, target.project_id == project_id)
        )
        if kind == "relationship":
            query = query.where(SettingTimeline.subject_kind == "relationship")
        for unit_id, identity in await db.execute(query):
            references[unit_id].append(
                RelationshipReference(kind="relationship", timeline_id=identity)
                if kind == "relationship"
                else EntityReference(kind=kind, id=identity)
            )
    occupied = set(
        await db.scalars(select(SubplotNode.unit_id).join(PlanningUnit).where(PlanningUnit.project_id == project_id))
    ) | set(await db.scalars(select(SubplotCrossing.unit_id).where(SubplotCrossing.project_id == project_id)))

    def build(source, title, chapter=None, plan=None, unit=None):
        outline_id = chapter.outline_id if chapter else plan.outline_id if plan else None
        outline = outlines.get(outline_id)
        character_ids = cast[chapter.id] if chapter else plan.character_ids if plan else unit.detached_character_ids
        refs = [
            EntityReference(kind="character", id=identity)
            for identity in sorted(set(character_ids or []))
            if identity in character_names
        ]
        if unit:
            refs += references[unit.id]
        return PlanningRow(
            key=source_key(source),
            unit_id=unit.id if unit else None,
            source=source,
            chapter_id=chapter.id if chapter else None,
            plan_id=chapter.plan_id if chapter else plan.id if plan else None,
            title=title,
            chapter_no=chapter.chapter_no if chapter else None,
            outline_id=outline_id,
            volume_no=outline.volume_no if outline else None,
            volume_title=outline.volume_title if outline else unit.last_volume_label if unit else "",
            seq=plan.seq if plan else None,
            has_content=bool(chapter and chapter.word_count > 0),
            pov_character_id=unit.pov_character_id if unit and unit.pov_character_id in character_names else None,
            pov_character_name=character_names.get(unit.pov_character_id) if unit else None,
            tags=sorted(tags[unit.id], key=lambda tag: tag.id) if unit else [],
            references=sorted(refs, key=reference_key),
        )

    result = [
        build(
            PlanningSource(kind="chapter", id=chapter.id),
            chapter.title,
            chapter=chapter,
            unit=by_chapter.get(chapter.id),
        )
        for chapter in chapters
    ]

    def plan_order(plan):
        outline = outlines[plan.outline_id]
        return outline.volume_no, outline.sort, outline.chapter_no, outline.id, plan.seq, plan.id

    for plan in sorted(plans, key=plan_order):
        if plan.chapter_id is None:
            result.append(
                build(PlanningSource(kind="plan", id=plan.id), plan.title, plan=plan, unit=by_plan.get(plan.id))
            )
    for unit in units:
        if (
            unit.chapter_id is None
            and unit.plan_id is None
            and (
                unit.pov_character_id is not None
                or unit.detached_character_ids
                or tags[unit.id]
                or references[unit.id]
                or unit.id in occupied
            )
        ):
            result.append(build(PlanningSource(kind="detached", id=unit.id), unit.last_source_label, unit=unit))
    return result


async def crossings(db: AsyncSession, project_id: int, row_list: list[PlanningRow]) -> list[SubplotCrossingOut]:
    by_unit = {row.unit_id: row for row in row_list if row.unit_id is not None}
    if not by_unit:
        return []
    conditions = [SubplotCrossing.project_id == project_id]
    # 矩阵窗口只读相关事件；完整目录调用以项目条件避免无界 IN 参数。
    if len(by_unit) <= 500:
        conditions.append(SubplotCrossing.unit_id.in_(by_unit))
    participants: dict[int, list[int]] = defaultdict(list)
    for crossing_id, subplot_id in await db.execute(
        select(SubplotCrossingParticipant.crossing_id, SubplotCrossingParticipant.subplot_id)
        .join(SubplotCrossing, SubplotCrossing.id == SubplotCrossingParticipant.crossing_id)
        .where(*conditions)
        .order_by(SubplotCrossingParticipant.subplot_id)
    ):
        participants[crossing_id].append(subplot_id)
    result = []
    for item in await db.scalars(
        select(SubplotCrossing)
        .where(*conditions)
        .order_by(SubplotCrossing.sort, SubplotCrossing.id)
        .execution_options(populate_existing=True)
    ):
        row = by_unit.get(item.unit_id)
        if row is not None:
            result.append(
                SubplotCrossingOut(
                    id=item.id,
                    unit_id=item.unit_id,
                    source=row.source,
                    title=item.title,
                    description=item.description,
                    sort=item.sort,
                    subplot_ids=participants[item.id],
                )
            )
    return result
