"""同章规划的只读投影：实际生成、局部重写和预览共享来源与过滤逻辑。"""

from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts.planning import PlanningScope, PlanningTextItem, render_planning_context
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import (
    Career,
    Chapter,
    ChapterCharacter,
    ChapterPlan,
    Character,
    Organization,
    Outline,
    Project,
    RelationshipType,
    SettingTimeline,
    WorldSetting,
)
from app.models.planning import (
    PlanningUnit,
    PlanningUnitCareer,
    PlanningUnitOrganization,
    PlanningUnitRelationship,
    PlanningUnitWorld,
    Subplot,
    SubplotCrossing,
    SubplotCrossingParticipant,
    SubplotNode,
)
from app.schemas.planning import EntityReference, PlanningContextPreview, PlanningReference, PlanningSource, SourceKind
from app.schemas.planning import RelationshipReference as PlanningRelationshipReference
from app.schemas.progression import StoryPosition
from app.services import career_service, relationship_service, setting_progression_service

MAX_PLANNING_CHARS = 20000
MIN_ORDINARY_CONTEXT_CHARS = 200
TRUNCATED_SUFFIX = "……[已截断]"


@dataclass
class PlanningContext:
    revision: int
    source: PlanningSource
    unit_id: int | None = None
    chapter_id: int | None = None
    plan_id: int | None = None
    pov_character_id: int | None = None
    text: str = ""
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    node_ids: list[int] = field(default_factory=list)
    crossing_ids: list[int] = field(default_factory=list)
    subplot_ids: list[int] = field(default_factory=list)
    crossing_participants: dict[int, list[int]] = field(default_factory=dict)
    references: list[PlanningReference] = field(default_factory=list)
    # 同一投影输出保护块后，普通参考资料排除这些详情，避免相同资料重复。
    protected_character_ids: set[int] = field(default_factory=set)
    protected_world_ids: set[int] = field(default_factory=set)
    protected_organization_ids: set[int] = field(default_factory=set)
    protected_career_ids: set[int] = field(default_factory=set)
    protected_relationship_ids: set[int] = field(default_factory=set)
    additional_character_ids: set[int] = field(default_factory=set)

    @property
    def reserved_chars(self) -> int:
        return len(self.text) + 2 if self.text else 0

    def log_data(self, scope: PlanningScope) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "revision": self.revision,
            "scope": scope,
            "pov_character_id": self.pov_character_id,
            "node_ids": self.node_ids,
            "crossing_ids": self.crossing_ids,
            "crossing_participants": {str(key): value for key, value in self.crossing_participants.items()},
            "references": [item.model_dump() for item in self.references],
            "truncated": self.truncated,
            "warnings": self.warnings,
        }


def fit_planning_context(
    items: list[PlanningTextItem], *, scope: PlanningScope, budget: int, planned: bool = False
) -> tuple[str, bool]:
    """身份不裁、正文公平分配；渲染前估算长度，避免先展开巨大的条目集合。"""
    if not items:
        return "", False
    if sum(len(item.title) for item in items) > budget:
        raise BadRequestError("上下文预算不足以保留本章规划标识，请增大上下文预算或减少本章关联与目标")
    empty = [replace(item, body="") for item in items]
    full_size = len(render_planning_context(empty, scope=scope, planned=planned)) + sum(len(i.body) for i in items)
    if full_size <= budget:
        rendered = render_planning_context(items, scope=scope, planned=planned)
        if len(rendered) <= budget:
            return rendered, False
    overhead = len(render_planning_context(empty, scope=scope, truncated=True, planned=planned))
    available = budget - overhead
    if available < len(items) * len(TRUNCATED_SUFFIX):
        raise BadRequestError("上下文预算不足以保留本章规划标识，请增大上下文预算或减少本章关联与目标")
    quotas = [0] * len(items)
    pending = set(range(len(items)))
    while pending:
        share = available // len(pending)
        small = [i for i in pending if len(items[i].body) <= share]
        if not small:
            for i in sorted(pending):
                quota = available // len(pending)
                quotas[i] = quota
                available -= quota
                pending.remove(i)
            break
        for i in small:
            quotas[i] = len(items[i].body)
            available -= quotas[i]
            pending.remove(i)
    clipped = [
        replace(
            item,
            body=item.body
            if len(item.body) <= quota
            else item.body[: quota - len(TRUNCATED_SUFFIX)] + TRUNCATED_SUFFIX,
        )
        for item, quota in zip(items, quotas, strict=True)
    ]
    rendered = render_planning_context(clipped, scope=scope, truncated=True, planned=planned)
    # 空正文尾部被 strip 掉的换行也占正式块预算，按实际渲染长度再收紧正文。
    excess = len(rendered) - budget
    if excess > 0:
        for i in reversed(range(len(clipped))):
            body = clipped[i].body
            cut = min(excess, max(0, len(body) - len(TRUNCATED_SUFFIX)))
            if cut:
                keep = len(body) - cut - len(TRUNCATED_SUFFIX)
                clipped[i] = replace(clipped[i], body=body[:keep] + TRUNCATED_SUFFIX)
                excess -= cut
            if excess <= 0:
                break
        rendered = render_planning_context(clipped, scope=scope, truncated=True, planned=planned)
    if len(rendered) > budget:
        raise BadRequestError("上下文预算不足以保留本章规划标识，请增大上下文预算")
    return rendered, True


async def _source(
    db: AsyncSession, project: Project, source_kind: SourceKind, source_id: int
) -> tuple[PlanningUnit | None, Chapter | None, ChapterPlan | None]:
    chapter = None
    plan = None
    if source_kind == "chapter":
        chapter = await db.scalar(select(Chapter).where(Chapter.id == source_id, Chapter.project_id == project.id))
        if chapter is None:
            raise NotFoundError("章节不存在")
        unit = await db.scalar(
            select(PlanningUnit).where(
                PlanningUnit.project_id == project.id,
                PlanningUnit.chapter_id == chapter.id,
                PlanningUnit.retired.is_(False),
            )
        )
    elif source_kind == "plan":
        plan = await db.scalar(
            select(ChapterPlan)
            .join(Outline, Outline.id == ChapterPlan.outline_id)
            .where(
                ChapterPlan.id == source_id,
                ChapterPlan.project_id == project.id,
                Outline.project_id == project.id,
            )
        )
        if plan is None:
            raise NotFoundError("细纲不存在")
        if plan.chapter_id is not None:
            chapter = await db.scalar(
                select(Chapter).where(
                    Chapter.id == plan.chapter_id, Chapter.project_id == project.id, Chapter.plan_id == plan.id
                )
            )
            if chapter is None:
                raise ConflictError("细纲与章节的关联已变化，请刷新后重试")
        target = PlanningUnit.chapter_id == chapter.id if chapter is not None else PlanningUnit.plan_id == plan.id
        unit = await db.scalar(
            select(PlanningUnit).where(PlanningUnit.project_id == project.id, PlanningUnit.retired.is_(False), target)
        )
    else:
        unit = await db.scalar(
            select(PlanningUnit).where(
                PlanningUnit.id == source_id,
                PlanningUnit.project_id == project.id,
                PlanningUnit.retired.is_(False),
                PlanningUnit.chapter_id.is_(None),
                PlanningUnit.plan_id.is_(None),
            )
        )
        if unit is None:
            raise NotFoundError("待重新关联的规划不存在")
    return unit, chapter, plan


async def _story_items(
    db: AsyncSession, project: Project, unit: PlanningUnit, out: PlanningContext
) -> list[PlanningTextItem]:
    rows = await db.execute(
        select(SubplotNode, Subplot)
        .join(Subplot, Subplot.id == SubplotNode.subplot_id)
        .where(SubplotNode.unit_id == unit.id, Subplot.project_id == project.id)
        .order_by(Subplot.sort, Subplot.id, SubplotNode.id)
    )
    items = []
    subplot_ids = set()
    for node, subplot in rows:
        out.node_ids.append(node.id)
        subplot_ids.add(subplot.id)
        items.append(PlanningTextItem(f"支线目标 #{node.id}：{subplot.name}", node.content.strip()))
    crossings = list(
        await db.scalars(
            select(SubplotCrossing)
            .where(SubplotCrossing.project_id == project.id, SubplotCrossing.unit_id == unit.id)
            .order_by(SubplotCrossing.sort, SubplotCrossing.id)
        )
    )
    by_event: dict[int, list[Subplot]] = {}
    if crossings:
        rows = await db.execute(
            select(SubplotCrossingParticipant.crossing_id, Subplot)
            .join(Subplot, Subplot.id == SubplotCrossingParticipant.subplot_id)
            .where(
                SubplotCrossingParticipant.crossing_id.in_([event.id for event in crossings]),
                Subplot.project_id == project.id,
            )
            .order_by(Subplot.sort, Subplot.id)
        )
        for event_id, subplot in rows:
            by_event.setdefault(event_id, []).append(subplot)
    for event in crossings:
        participants = by_event.get(event.id, [])
        if not 2 <= len(participants) <= 20:
            raise ConflictError("交汇事件的参与支线已变化，请先检查规划")
        out.crossing_ids.append(event.id)
        out.crossing_participants[event.id] = [p.id for p in participants]
        subplot_ids.update(out.crossing_participants[event.id])
        names = "、".join(p.name for p in participants)
        items.append(
            PlanningTextItem(f"明确交汇 #{event.id}：{event.title}｜参与支线：{names}", event.description.strip())
        )
    out.subplot_ids = sorted(subplot_ids)
    return items


async def _references(
    db: AsyncSession, project: Project, unit: PlanningUnit, out: PlanningContext
) -> tuple[list[WorldSetting], list[Organization], list[Career], list[SettingTimeline]]:
    worlds = list(
        await db.scalars(
            select(WorldSetting)
            .join(PlanningUnitWorld, PlanningUnitWorld.world_id == WorldSetting.id)
            .where(PlanningUnitWorld.unit_id == unit.id, WorldSetting.project_id == project.id)
            .order_by(WorldSetting.sort, WorldSetting.id)
        )
    )
    organizations = list(
        await db.scalars(
            select(Organization)
            .join(PlanningUnitOrganization, PlanningUnitOrganization.organization_id == Organization.id)
            .where(PlanningUnitOrganization.unit_id == unit.id, Organization.project_id == project.id)
            .order_by(Organization.sort, Organization.id)
        )
    )
    careers = list(
        await db.scalars(
            select(Career)
            .join(PlanningUnitCareer, PlanningUnitCareer.career_id == Career.id)
            .where(PlanningUnitCareer.unit_id == unit.id, Career.project_id == project.id)
            .order_by(Career.sort, Career.id)
        )
    )
    relationships = list(
        await db.scalars(
            select(SettingTimeline)
            .join(PlanningUnitRelationship, PlanningUnitRelationship.timeline_id == SettingTimeline.id)
            .where(
                PlanningUnitRelationship.unit_id == unit.id,
                SettingTimeline.project_id == project.id,
                SettingTimeline.subject_kind == "relationship",
            )
            .order_by(SettingTimeline.id)
        )
    )
    for kind, entities in (("world", worlds), ("organization", organizations), ("career", careers)):
        out.references.extend(EntityReference(kind=kind, id=entity.id) for entity in entities)
    out.references.extend(PlanningRelationshipReference(kind="relationship", timeline_id=t.id) for t in relationships)
    return worlds, organizations, careers, relationships


def _fields(*values: tuple[str, str]) -> str:
    return "\n".join(f"{label}：{value.strip()}" for label, value in values if value and value.strip())


async def _setting_items(
    db: AsyncSession, project: Project, chapter: Chapter | None, unit: PlanningUnit, out: PlanningContext
) -> list[PlanningTextItem]:
    # 局部导入避免普通上下文与只读规划投影之间出现模块初始化环。
    from app.services import context_builder

    worlds, organizations, careers, relationships = await _references(db, project, unit, out)
    character_ids = {
        cid
        for timeline in relationships
        for cid in (timeline.character_id, timeline.related_character_id)
        if cid is not None
    }
    if unit.pov_character_id is not None:
        character_ids.add(unit.pov_character_id)
    characters = (
        list(
            await db.scalars(
                select(Character)
                .where(Character.project_id == project.id, Character.id.in_(character_ids))
                .order_by(Character.sort, Character.id)
            )
        )
        if character_ids
        else []
    )
    names = {c.id: c.name for c in characters}
    if unit.pov_character_id in names:
        out.pov_character_id = unit.pov_character_id
    items: list[PlanningTextItem] = []
    if chapter is None:
        if unit.pov_character_id in names:
            items.append(PlanningTextItem(f"视角人物 #{unit.pov_character_id}：{names[unit.pov_character_id]}", ""))
        items.extend(PlanningTextItem(f"指定世界观 #{world.id}：{world.title}", "") for world in worlds)
        items.extend(PlanningTextItem(f"指定组织 #{org.id}：{org.name}", "") for org in organizations)
        items.extend(PlanningTextItem(f"指定职业 #{career.id}：{career.name}", "") for career in careers)
        for timeline in relationships:
            if timeline.character_id in names and timeline.related_character_id in names:
                pair = f"{names[timeline.character_id]}／{names[timeline.related_character_id]}"
                items.append(PlanningTextItem(f"指定人物关系（时间线 #{timeline.id}）：{pair}", ""))
        out.warnings.append("尚未绑定实际章节，仅预览规划指令；设定资料将在建章后按章初位置解析")
        return items

    position = StoryPosition(chapter_id=chapter.id, chapter_no=chapter.chapter_no)
    effective = await context_builder.resolve_effective_cast(db, project, chapter, characters)
    for character in effective.characters:
        label = "视角人物" if character.id == unit.pov_character_id else "关系参考人物（不额外要求出场）"
        body = context_builder.format_card(
            character,
            effective.memberships.get(character.id, []),
            include_legacy_relations=character.id not in effective.structured_character_ids,
            career_line=context_builder.format_career_line(
                effective.careers_by_character.get(character.id, []), effective.stages
            ),
            custom_fields=effective.custom_fields.get(character.id, []),
        )
        items.append(PlanningTextItem(f"{label} #{character.id}：{character.name}", body))
    out.additional_character_ids = set(names)
    out.protected_character_ids = set(names)

    world_timelines = (
        list(
            await db.scalars(
                select(SettingTimeline).where(
                    SettingTimeline.project_id == project.id,
                    SettingTimeline.subject_kind == "world",
                    SettingTimeline.world_setting_id.in_([world.id for world in worlds]),
                )
            )
        )
        if worlds
        else []
    )
    snapshots = await setting_progression_service.resolve_timelines(
        db, project.id, [*world_timelines, *relationships], position
    )
    custom = await context_builder._custom_labels(db, world_timelines, snapshots)
    by_world = {timeline.world_setting_id: timeline for timeline in world_timelines}
    for world in worlds:
        timeline = by_world.get(world.id)
        content = str(snapshots[timeline.id].fields["content"]) if timeline else world.content
        fields = custom.get(timeline.id, []) if timeline else []
        body = "\n".join([content, *(f"{label}：{value}" for label, value in fields)]).strip()
        items.append(PlanningTextItem(f"指定世界观 #{world.id}：{world.title}（{world.category}）", body))
        out.protected_world_ids.add(world.id)
    for organization in organizations:
        items.append(
            PlanningTextItem(
                f"指定组织 #{organization.id}：{organization.name}（当前静态档案）",
                _fields(
                    ("类型", organization.org_type),
                    ("宗旨", organization.purpose),
                    ("所在地", organization.location),
                    ("说明", organization.description),
                ),
            )
        )
        out.protected_organization_ids.add(organization.id)
    stages = await career_service.stages_of(db, [career.id for career in careers])
    for career in careers:
        stage_text = "\n".join(
            f"{s.position}. {s.name}" + (f"：{s.description}" if s.description else "")
            for s in stages.get(career.id, [])
        )
        items.append(
            PlanningTextItem(
                f"指定职业 #{career.id}：{career.name}（当前静态体系）",
                _fields(
                    ("类型", career.kind),
                    ("说明", career.description),
                    ("获得条件", career.requirements),
                    ("能力", career.abilities),
                    ("规则", career.rules),
                    ("阶段", stage_text),
                ),
            )
        )
        out.protected_career_ids.add(career.id)
    type_ids = {
        snapshots[t.id].fields["identity"]["type_id"] for t in relationships if snapshots[t.id].fields.get("exists")
    }
    types = (
        {
            t.id: t
            for t in await db.scalars(
                select(RelationshipType).where(
                    RelationshipType.id.in_(type_ids),
                    or_(RelationshipType.project_id.is_(None), RelationshipType.project_id == project.id),
                )
            )
        }
        if type_ids
        else {}
    )
    for timeline in relationships:
        if timeline.character_id not in names or timeline.related_character_id not in names:
            out.warnings.append(f"人物关系 #{timeline.id} 的参考角色已不存在，已省略")
            continue
        fields = snapshots[timeline.id].fields
        pair = f"{names[timeline.character_id]}／{names[timeline.related_character_id]}"
        body = "本章开始时没有有效关系记录。"
        if fields.get("exists"):
            identity = fields["identity"]
            relation_type = types.get(identity["type_id"])
            if relation_type is None:
                raise ConflictError("人物关系类型已变化，请刷新规划后重试")
            description = relationship_service.describe_relation(
                names[identity["a_id"]], names[identity["b_id"]], relation_type
            )
            body = _fields(
                ("关系", description),
                ("亲密度", str(fields["intimacy"])),
                ("状态", relationship_service.STATUS_LABELS.get(fields["status"], fields["status"])),
                ("说明", fields["description"]),
            )
        items.append(PlanningTextItem(f"指定人物关系（时间线 #{timeline.id}）：{pair}", body))
        out.protected_relationship_ids.add(timeline.id)
    return items


async def load_planning_context(
    db: AsyncSession,
    project: Project,
    source_kind: SourceKind,
    source_id: int,
    *,
    scope: PlanningScope = "chapter",
    budget: int = 4000,
) -> PlanningContext:
    """不写入 unit／设定，不依赖 UI 当前列或支线筛选；权限由上层项目入口校验。"""
    with db.no_autoflush:
        revision = await db.scalar(select(Project.planning_revision).where(Project.id == project.id))
        if revision is None:
            raise NotFoundError("作品不存在")
        unit, chapter, plan = await _source(db, project, source_kind, source_id)
        out = PlanningContext(
            revision=revision,
            source=PlanningSource(kind=source_kind, id=source_id),
            unit_id=unit.id if unit else None,
            chapter_id=chapter.id if chapter else None,
            plan_id=plan.id if plan else (chapter.plan_id if chapter else None),
        )
        if unit is not None:
            out.plan_id = unit.plan_id or out.plan_id
            story = await _story_items(db, project, unit, out)
            settings = await _setting_items(db, project, chapter, unit, out)
            if chapter is not None:
                manual = list(
                    await db.scalars(
                        select(ChapterCharacter.character_id)
                        .join(Character, Character.id == ChapterCharacter.character_id)
                        .where(ChapterCharacter.chapter_id == chapter.id, Character.project_id == project.id)
                    )
                )
            else:
                manual_ids = plan.character_ids if plan is not None else unit.detached_character_ids
                manual = (
                    list(
                        await db.scalars(
                            select(Character.id).where(Character.project_id == project.id, Character.id.in_(manual_ids))
                        )
                    )
                    if manual_ids
                    else []
                )
            out.references[0:0] = [EntityReference(kind="character", id=cid) for cid in sorted(set(manual))]
            # 规划为空时不改变预算；有规划才预留正文上下文和锁定块前的两个换行。
            limit = min(MAX_PLANNING_CHARS, budget - MIN_ORDINARY_CONTEXT_CHARS - 2)
            out.text, out.truncated = fit_planning_context(
                settings + story, scope=scope, budget=limit, planned=chapter is None
            )
            if out.truncated:
                out.warnings.append("部分规划或指定设定因上下文预算被截断，完整内容仍保留在规划中")
        after = await db.scalar(select(Project.planning_revision).where(Project.id == project.id))
        if after != revision:
            # 不在当前 identity map 中重试，避免把缓存的旧节点配上新的 revision。
            raise ConflictError("规划在读取期间发生变化，请刷新后重试", extra={"code": "planning_context_changed"})
        return out


async def context_preview(
    db: AsyncSession,
    project: Project,
    source_kind: SourceKind,
    source_id: int,
    *,
    scope: PlanningScope = "chapter",
    budget: int = 4000,
) -> PlanningContextPreview:
    out = await load_planning_context(db, project, source_kind, source_id, scope=scope, budget=budget)
    return PlanningContextPreview(
        revision=out.revision,
        unit_id=out.unit_id,
        source=out.source,
        chapter_id=out.chapter_id,
        plan_id=out.plan_id,
        text=out.text,
        warnings=out.warnings,
        node_ids=out.node_ids,
        crossing_ids=out.crossing_ids,
        subplot_ids=out.subplot_ids,
        references=out.references,
        truncated=out.truncated,
    )
