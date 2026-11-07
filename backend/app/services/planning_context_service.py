"""同章规划的只读投影：实际生成、局部重写和预览共享来源与过滤逻辑。"""

from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

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
from app.models.scenes import Scene
from app.schemas.planning import EntityReference, PlanningContextPreview, PlanningReference, PlanningSource, SourceKind
from app.schemas.planning import RelationshipReference as PlanningRelationshipReference
from app.schemas.progression import SettingContextOut, StoryPosition
from app.schemas.setting_fields import EntityRef
from app.services import relationship_service, setting_field_service, setting_progression_service
from app.services.ai_dispatch import VisibilityStamp, read_visibility_stamp, require_visibility_stamp
from app.services.mentions.matcher import EntryKey
from app.services.setting_ai_context import AiScope, build_scope
from app.services.setting_field_formatting import format_entry_fields

if TYPE_CHECKING:
    from app.services.context_builder import ChapterSettings

MAX_PLANNING_CHARS = 20000
MIN_ORDINARY_CONTEXT_CHARS = 200
TRUNCATED_SUFFIX = "……[已截断]"

type SettingReferences = tuple[list[WorldSetting], list[Organization], list[Career], list[SettingTimeline]]


@dataclass
class PlanningContext:
    revision: int
    source: PlanningSource
    # 与普通设定块共用目录版本；非持久化规划版本，不进入规划 API 的 revision。
    fields_revision: int | None = None
    setting_scope: "ChapterSettings | None" = None
    unit_id: int | None = None
    chapter_id: int | None = None
    plan_id: int | None = None
    scene_id: int | None = None
    position: StoryPosition | None = None
    manual_character_ids: list[int] | None = None
    content_revision: int | None = None
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
            "scene_id": self.scene_id,
            "content_revision": self.content_revision,
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


async def _chapter_unit(db: AsyncSession, project_id: int, chapter_id: int) -> PlanningUnit | None:
    return await db.scalar(select(PlanningUnit).where(
        PlanningUnit.project_id == project_id, PlanningUnit.chapter_id == chapter_id, PlanningUnit.retired.is_(False),
    ))


async def planning_revision(db: AsyncSession, project_id: int) -> int:
    with db.no_autoflush:
        revision = await db.scalar(select(Project.planning_revision).where(Project.id == project_id))
    if revision is None:
        raise NotFoundError("作品不存在")
    return revision


async def check_planning_revision(db: AsyncSession, project_id: int, revision: int) -> None:
    if await planning_revision(db, project_id) != revision:
        # 不在当前 identity map 中重试，避免把缓存的旧节点配上新的 revision。
        raise ConflictError("规划在读取期间发生变化，请刷新后重试", extra={"code": "planning_context_changed"})


async def _source(
    db: AsyncSession, project: Project, source_kind: SourceKind, source_id: int
) -> tuple[PlanningUnit | None, Chapter | None, ChapterPlan | None]:
    chapter = None
    plan = None
    if source_kind == "chapter":
        chapter = await db.scalar(select(Chapter).where(Chapter.id == source_id, Chapter.project_id == project.id))
        if chapter is None:
            raise NotFoundError("章节不存在")
        unit = await _chapter_unit(db, project.id, chapter.id)
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
    elif source_kind == "scene":
        chapter = await db.scalar(select(Chapter).join(Scene, Scene.chapter_id == Chapter.id).where(
            Chapter.project_id == project.id, Scene.id == source_id, Scene.active.is_(True),
        ))
        if chapter is None:
            raise NotFoundError("场景不存在")
        unit = await db.scalar(select(PlanningUnit).where(
            PlanningUnit.project_id == project.id, PlanningUnit.scene_id == source_id,
            PlanningUnit.retired.is_(False), PlanningUnit.source_level == "scene",
        ))
    else:
        unit = await db.scalar(
            select(PlanningUnit).where(
                PlanningUnit.id == source_id,
                PlanningUnit.project_id == project.id,
                PlanningUnit.retired.is_(False),
                PlanningUnit.chapter_id.is_(None),
                PlanningUnit.plan_id.is_(None),
                PlanningUnit.scene_id.is_(None),
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
    out.subplot_ids = sorted(set(out.subplot_ids) | subplot_ids)
    return items


async def _references(
    db: AsyncSession, project: Project, unit: PlanningUnit,
) -> SettingReferences:
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
    return worlds, organizations, careers, relationships


def _explicit_entries(unit: PlanningUnit, rows: SettingReferences) -> list[EntryKey]:
    worlds, organizations, careers, relationships = rows
    entries = [
        *(("world", row.id) for row in worlds),
        *(("organization", row.id) for row in organizations),
        *(("career", row.id) for row in careers),
        *(("character", ident) for row in relationships
          for ident in (row.character_id, row.related_character_id) if ident is not None),
    ]
    if unit.pov_character_id is not None:
        entries.append(("character", unit.pov_character_id))
    return entries


async def chapter_explicit_entries(db: AsyncSession, project: Project, chapter: Chapter) -> list[EntryKey]:
    """分析等不渲染规划文本的入口复用作者指定；调用者在包含手动 cast 的整个读取前后验版本。"""
    with db.no_autoflush:
        unit = await _chapter_unit(db, project.id, chapter.id)
        return _explicit_entries(unit, await _references(db, project, unit)) if unit is not None else []


def _fields(*values: tuple[str, str]) -> str:
    return "\n".join(f"{label}：{value.strip()}" for label, value in values if value and value.strip())


async def _setting_items(
    db: AsyncSession, project: Project, chapter: Chapter | None, unit: PlanningUnit, out: PlanningContext,
    rows: SettingReferences,
    scope: AiScope,
) -> list[PlanningTextItem]:
    # 局部导入避免普通上下文与只读规划投影之间出现模块初始化环。
    from app.services import context_builder

    worlds, organizations, careers, relationships = rows
    worlds = [row for row in worlds if scope.includes("world", row.id)]
    organizations = [row for row in organizations if scope.includes("organization", row.id)]
    careers = [row for row in careers if scope.includes("career", row.id)]
    character_ids = {
        cid
        for timeline in relationships
        for cid in (timeline.character_id, timeline.related_character_id)
        if cid is not None
    }
    if unit.pov_character_id is not None:
        character_ids.add(unit.pov_character_id)
    character_ids.intersection_update(scope.ids("character"))
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

    position = out.position or StoryPosition(chapter_id=chapter.id, chapter_no=chapter.chapter_no)
    assert out.setting_scope is not None
    effective = out.setting_scope.effective
    for character in effective.characters:
        if character.id not in names:
            continue
        label = "视角人物" if character.id == unit.pov_character_id else "关系参考人物（不额外要求出场）"
        body = context_builder.format_card(
            character,
            effective.memberships.get(character.id, []),
            include_legacy_relations=character.id not in effective.structured_character_ids,
            career_line=context_builder.format_career_line(
                effective.careers_by_character.get(character.id, []), effective.stages
            ),
            field_text=format_entry_fields(effective.entry_fields["character", character.id]),
            hidden=scope.hidden("character", character.id),
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
    entry_fields = await setting_field_service.resolve_entry_fields(
        db, project.id, [
            *[EntityRef(kind="world", id=world.id) for world in worlds],
            *[EntityRef(kind="organization", id=organization.id) for organization in organizations],
            *[EntityRef(kind="career", id=career.id) for career in careers],
        ], position,
    )
    by_world = {timeline.world_setting_id: timeline for timeline in world_timelines}
    for world in worlds:
        timeline = by_world.get(world.id)
        content = ""
        if scope.field_visible("world", world.id, "content"):
            content = str(snapshots[timeline.id].fields["content"]) if timeline else world.content
        fields = format_entry_fields(scope.entry_fields(entry_fields["world", world.id]))
        body = "\n".join(part for part in (content, fields) if part).strip()
        items.append(PlanningTextItem(f"指定世界观 #{world.id}：{world.title}（{world.category}）", body))
        out.protected_world_ids.add(world.id)
    for organization in organizations:
        fields = format_entry_fields(scope.entry_fields(entry_fields["organization", organization.id]))
        note = "静态档案；字段按章初" if fields else "当前静态档案"
        details = _fields(*(
            (label, getattr(organization, key)) for key, label in (
                ("org_type", "类型"), ("purpose", "宗旨"), ("location", "所在地"), ("description", "说明"),
            ) if scope.field_visible("organization", organization.id, key)
        ))
        items.append(
            PlanningTextItem(
                f"指定组织 #{organization.id}：{organization.name}（{note}）",
                "\n".join(part for part in (details, fields) if part),
            )
        )
        out.protected_organization_ids.add(organization.id)
    stages = effective.stages
    for career in careers:
        fields = format_entry_fields(scope.entry_fields(entry_fields["career", career.id]))
        note = "静态体系；字段按章初" if fields else "当前静态体系"
        stage_text = "\n".join(
            f"{s.position}. {s.name}" + (f"：{s.description}" if s.description else "")
            for s in stages.get(career.id, [])
        )
        details = _fields(*(
            (label, getattr(career, key)) for key, label in (
                ("kind", "类型"), ("description", "说明"), ("requirements", "获得条件"),
                ("abilities", "能力"), ("rules", "规则"),
            ) if scope.field_visible("career", career.id, key)
        ), ("阶段", stage_text))
        items.append(
            PlanningTextItem(
                f"指定职业 #{career.id}：{career.name}（{note}）",
                "\n".join(part for part in (details, fields) if part),
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
            out.warnings.append(f"人物关系 #{timeline.id} 的参考角色不可用于 AI 或已不存在，已省略")
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
    text_hint: str = "",
    legacy_text_hint: str = "",
    stamp: VisibilityStamp | None = None,
    preceding_text: str | None = None,
) -> PlanningContext:
    """不写入 unit／设定，不依赖 UI 当前列或支线筛选；权限由上层项目入口校验。"""
    from app.services import context_builder

    with db.no_autoflush:
        stamp = stamp if stamp is not None else await read_visibility_stamp(db, project.id)
        fields_revision = await setting_field_service.fields_revision(db, project.id)
        revision = await planning_revision(db, project.id)
        unit, chapter, plan = await _source(db, project, source_kind, source_id)
        if source_kind == "scene":
            return await _load_scene_context(
                db, project, source_id, chapter, unit, revision=revision, fields_revision=fields_revision,
                scope=scope, budget=budget, text_hint=text_hint, legacy_text_hint=legacy_text_hint, stamp=stamp,
                preceding_text=preceding_text,
            )
        out = PlanningContext(
            revision=revision,
            source=PlanningSource(kind=source_kind, id=source_id),
            fields_revision=fields_revision,
            unit_id=unit.id if unit else None,
            chapter_id=chapter.id if chapter else None,
            plan_id=plan.id if plan else (chapter.plan_id if chapter else None),
        )
        explicit: list[EntryKey] = []
        if unit is not None:
            out.plan_id = unit.plan_id or out.plan_id
            story = await _story_items(db, project, unit, out)
            rows = await _references(db, project, unit)
            worlds, organizations, careers, relationships = rows
            explicit.extend(_explicit_entries(unit, rows))
            for kind, entities in (("world", worlds), ("organization", organizations), ("career", careers)):
                out.references.extend(EntityReference(kind=kind, id=entity.id) for entity in entities)
            out.references.extend(
                PlanningRelationshipReference(kind="relationship", timeline_id=row.id) for row in relationships
            )
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
            explicit.extend(("character", ident) for ident in manual)
            out.references[0:0] = [EntityReference(kind="character", id=cid) for cid in sorted(set(manual))]
        if chapter is not None:
            task = await context_builder.chapter_task(db, chapter)
            out.setting_scope = await context_builder.prepare_chapter_settings(
                db, project, chapter, task.match_text(), explicit=explicit, stamp=stamp, extra_text=text_hint,
                legacy_extra_text=legacy_text_hint,
                preceding_text=preceding_text,
            )
        if unit is not None:
            ai_scope = out.setting_scope.scope if out.setting_scope is not None else await build_scope(
                db, project.id, explicit=explicit, stamp=stamp,
            )
            settings = await _setting_items(db, project, chapter, unit, out, rows, ai_scope)
            # 规划为空时不改变预算；有规划才预留正文上下文和锁定块前的两个换行。
            limit = min(MAX_PLANNING_CHARS, budget - MIN_ORDINARY_CONTEXT_CHARS - 2)
            out.text, out.truncated = fit_planning_context(
                settings + story, scope=scope, budget=limit, planned=chapter is None
            )
            if out.truncated:
                out.warnings.append("部分规划或指定设定因上下文预算被截断，完整内容仍保留在规划中")
        await check_planning_revision(db, project.id, revision)
        await setting_field_service.check_fields_revision(db, project.id, fields_revision)
        await require_visibility_stamp(db, stamp)
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
        scene_id=out.scene_id,
        content_revision=out.content_revision,
        text=out.text,
        warnings=out.warnings,
        node_ids=out.node_ids,
        crossing_ids=out.crossing_ids,
        subplot_ids=out.subplot_ids,
        references=out.references,
        truncated=out.truncated,
    )


async def _load_scene_context(
    db, project, scene_id, chapter, unit, *, revision, fields_revision, scope, budget,
    text_hint, legacy_text_hint, stamp, preceding_text=None,
) -> PlanningContext:
    from app.services import context_builder
    from app.services.planning_scene_service import resolve_scene_planning

    content_revision = await db.scalar(select(Chapter.content_revision).where(
        Chapter.id == chapter.id, Chapter.project_id == project.id
    ))
    planning = await resolve_scene_planning(db, project.id, scene_id)
    effective = planning.effective
    position = await setting_progression_service.resolve_scene_position(db, project.id, scene_id)
    scene_text = await db.scalar(select(Scene.content).where(Scene.id == scene_id, Scene.chapter_id == chapter.id))
    manual = [ref.id for ref in effective.references if ref.kind == "character"]
    out = PlanningContext(
        revision=revision, source=PlanningSource(kind="scene", id=scene_id), fields_revision=fields_revision,
        unit_id=unit.id if unit else None, chapter_id=chapter.id, plan_id=chapter.plan_id,
        scene_id=scene_id, position=position, manual_character_ids=manual,
        content_revision=content_revision,
        references=list(effective.references),
    )
    # 本地覆盖先收敛为唯一有效意图，再进入资格/字段门禁，不能把章默认偷偷 union 回来。
    rows = []
    for kind, model in (("world", WorldSetting), ("organization", Organization), ("career", Career),
                        ("relationship", SettingTimeline)):
        ids = [ref.timeline_id if ref.kind == "relationship" else ref.id
               for ref in effective.references if ref.kind == kind]
        statement = select(model).where(model.project_id == project.id, model.id.in_(ids)).order_by(model.id)
        if kind == "relationship":
            statement = statement.where(SettingTimeline.subject_kind == "relationship")
        rows.append(list(await db.scalars(statement)))
    view = SimpleNamespace(pov_character_id=effective.pov_character_id)
    explicit = _explicit_entries(view, tuple(rows))
    explicit.extend(("character", identity) for identity in manual)
    task = await context_builder.chapter_task(db, chapter)
    out.setting_scope = await context_builder.prepare_chapter_settings(
        db, project, chapter, task.match_text(), explicit=explicit, stamp=stamp, extra_text=text_hint,
        legacy_extra_text=legacy_text_hint, position=position, manual_character_ids=manual, source_text=scene_text,
        preceding_text=preceding_text,
    )
    story = []
    parent = await _chapter_unit(db, project.id, chapter.id)
    if parent is not None:
        story += [replace(item, title=f"本章共同背景（非本场景完成目标） · {item.title}")
                  for item in await _story_items(db, project, parent, out)]
    if unit is not None:
        story += await _story_items(db, project, unit, out)
    settings = await _setting_items(db, project, chapter, view, out, tuple(rows), out.setting_scope.scope)
    limit = min(MAX_PLANNING_CHARS, budget - MIN_ORDINARY_CONTEXT_CHARS - 2)
    out.text, out.truncated = fit_planning_context(settings + story, scope=scope, budget=limit)
    if out.truncated:
        out.warnings.append("部分场景规划或指定设定因上下文预算被截断，完整内容仍保留在规划中")
    await check_planning_revision(db, project.id, revision)
    await setting_field_service.check_fields_revision(db, project.id, fields_revision)
    await require_visibility_stamp(db, stamp)
    if content_revision != await db.scalar(select(Chapter.content_revision).where(
        Chapter.id == chapter.id, Chapter.project_id == project.id
    )):
        raise ConflictError("场景上下文读取期间已变化，请重新预览", extra={"code": "scene_context_changed"})
    return out


async def scene_setting_context(
    db: AsyncSession, user_id: int, project_id: int, chapter_id: int, scene_id: int
) -> SettingContextOut:
    """人工只读预览复用真实出站的统一选择与时点，不返回完整作者档案。"""
    from app.services import context_builder
    from app.services.chapter_service import get_manageable_chapter
    from app.services.project_service import get_manageable_project
    from app.services.scene_service import require_scene

    with db.no_autoflush:
        chapter = await get_manageable_chapter(db, user_id, project_id, chapter_id)
        await require_scene(db, chapter, scene_id)
        project = await get_manageable_project(db, user_id, project_id)
        planning = await load_planning_context(db, project, "scene", scene_id)
        text = await context_builder.build_setting_context(
            db, project, chapter, planning=planning, include_protected=True,
        )
        unverified = await setting_progression_service.count_unverified_timelines(db, user_id, project_id)
        warnings = list(planning.warnings)
        if unverified:
            warnings.append(f"该作品有 {unverified} 条旧设定尚未校准，早期设定可能不准确。")
        return SettingContextOut(position=planning.position, text=text, legacy_unverified_count=unverified,
                                 warnings=warnings)
