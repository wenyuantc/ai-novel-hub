"""章节生成上下文组装。

本章任务（chapter_task，供提示词【本章任务】与选角匹配使用）按优先级取：
  1. 章节对应的细纲（chapters.plan_id）：标题、梗概（细纲梗概为空时退回大纲梗概）、关键事件、叙事目标、
     情感基调、预估字数；
  2. 章节关联的大纲：标题与梗概（与引入细纲前的行为一致）。
  生成请求的目标字数仍由请求决定（前端打开生成面板时用细纲的预估字数预填），后端不改变请求语义。

组成：作品信息 + 世界观（按分类分组）+ 参考资料（知识库）+ 角色部分 + 当前卷大纲 + 伏笔提醒 + 更早章节标题/摘要
+ 相关旧情节（剧情记忆）+ 最近 N 章全文。
检索块（knowledge-base，见 retrieval_service / kb_service / memory_service；都为空时上下文与之前逐字一致）：
  - 【参考资料（来自知识库…）】：查询 = 本章任务标题 + 梗概 + 关键事件 + 涉及角色名；
    项目总开关关闭、没有命中、检索出错时不出现，字数受 kb_settings.max_chars 限制；位于世界观之后。
  - 【相关旧情节】：查询 = 上述 + 叙事目标；只召回本章之前、且不在「最近 N 章全文」中的章节，
    前情提要里已有摘要的章节不再召回摘要类记忆；钉住的优先，按章号排列，字数受 memory_max_chars 限制；
    位于前情提要之后、最近章节正文之前。
更早章节的摘要（前情提要）：优先用该章最近一次章节分析的 summary（批量一条查询取每章 max(id)；
正文已变化、分析已过期时照常使用），没有分析时退回正文前 SUMMARY_CHARS 字。
角色部分（D4，选角见 cast_service.select_cast）：
  - 本章涉及角色（主角 ∪ 手动指定出场 ∪ 梗概 / 上一章结尾提及）：完整角色卡，含所属组织与职位
  - 人物关系：只列涉及角色两两之间的关系
  - 相关组织：涉及角色所属组织的名称、类型、宗旨
  - 职业体系：涉及角色所挂职业的完整阶段阶梯（角色卡中有「职业：剑修·筑基（2/9，瓶颈期）」行）
  - 其他角色：只列「名字（定位）」名单
伏笔提醒（foreshadow_service.reminders，只取注入上下文开启、未回收 / 未废弃的伏笔；项目没有可提醒的伏笔时整段不出现）：
  本章计划埋设（全部）→ 本章必须回收（全部）→ 超期未回收（≤3，最超期在前）→ 即将到期（未来 3 章内，≤3）→
  长线伏笔（没有计划回收章，按重要度 ≤3）；位于当前卷大纲之后、前情提要之前。
超出字符预算时的裁剪顺序：
  0a. 从尾部截断【参考资料】
  0b. 从最不相关的一条开始丢弃【相关旧情节】中未钉住的条目（钉住的只在最后的硬截断中处理）
  1. 从最早开始丢弃「更早章节摘要」
  2. 从较早的一章开始，截掉最近章节正文的开头（保留结尾，利于衔接）
  3. 截断世界观尾部
  4. 伏笔提醒：先整档去掉「长线伏笔」→ 再整档去掉「即将到期」（其余三档只在最后的硬截断中处理）
  5. 角色部分：先从尾部丢弃「其他角色」名单 → 再从尾部去掉关系描述（保留关系本身）→ 再从尾部丢弃关系 →
     截断职业体系 → 截断相关组织 → 最后截断涉及角色卡（排在后面的角色先被截；主角在最前）
  6. 截断当前卷大纲尾部
  7. 最终硬截断兜底，保证不超过预算
"""

from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts import format_project_info
from app.models import (
    Career,
    CareerStage,
    Chapter,
    ChapterAnalysis,
    Character,
    CharacterCareer,
    Organization,
    OrganizationMember,
    Outline,
    Project,
    Relationship,
    RelationshipType,
    SettingTextField,
    SettingTimeline,
)
from app.schemas.progression import StoryPosition
from app.services import (
    career_service,
    cast_service,
    foreshadow_service,
    kb_service,
    memory_service,
    organization_service,
    plan_service,
    relationship_service,
    setting_progression_service,
    story_service,
)
from app.services.project_service import project_info
from app.services.retrieval_service import Embedder, EmbedEnv
from app.services.setting_progression_resolver import EffectiveSnapshot

if TYPE_CHECKING:
    from app.services.planning_context_service import PlanningContext

RECENT_FULL_CHAPTERS = 2
SUMMARY_CHARS = 200
ELLIPSIS = "……"
CAREER_RULES_CHARS = 200
KNOWLEDGE_HEADER = "【参考资料（来自知识库，仅供参考；与项目设定冲突时以设定为准）】"
MEMORY_HEADER = "【相关旧情节】"


@dataclass
class PriorChapter:
    chapter_no: int
    title: str
    content: str
    # 最近一次章节分析的摘要；为空时前情提要退回正文开头
    summary: str = ""


@dataclass
class RelationLine:
    head: str
    description: str = ""
    # 供规划保护块与普通关系投影去重；不改变原有展示文本。
    timeline_id: int | None = None

    def render(self) -> str:
        return f"{self.head}：{self.description}" if self.description else self.head


@dataclass
class CastSection:
    """角色部分素材：cards 按优先级排列（主角在前），roster 为其余角色的「名字（定位）」。"""

    cards: list[str] = field(default_factory=list)
    relations: list[RelationLine] = field(default_factory=list)
    organizations: list[str] = field(default_factory=list)
    roster: list[str] = field(default_factory=list)
    # 涉及角色所挂职业的阶梯（每项一个职业）
    careers: list[str] = field(default_factory=list)
    # 涉及角色的名字（组成检索查询用，不进上下文）
    names: list[str] = field(default_factory=list)


@dataclass
class ForeshadowSection:
    """伏笔提醒五档（每项一行，已带「◆ 档位：」前缀）。"""

    to_plant: list[str] = field(default_factory=list)
    must_resolve: list[str] = field(default_factory=list)
    overdue: list[str] = field(default_factory=list)
    upcoming: list[str] = field(default_factory=list)
    long_term: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        return [*self.to_plant, *self.must_resolve, *self.overdue, *self.upcoming, *self.long_term]


@dataclass
class MemoryLine:
    """【相关旧情节】的一行。rank：相关度名次（越大越不相关，裁剪时先丢）；钉住的不参与裁剪。"""

    text: str
    pinned: bool = False
    rank: int = 0


@dataclass
class MemorySection:
    lines: list[MemoryLine] = field(default_factory=list)


@dataclass
class ContextParts:
    project_text: str
    include_project: bool = True
    world_text: str = ""
    cards_text: str = ""
    relations: list[RelationLine] = field(default_factory=list)
    organizations_text: str = ""
    careers_text: str = ""
    roster: list[str] = field(default_factory=list)
    outline_text: str = ""
    foreshadows: ForeshadowSection = field(default_factory=ForeshadowSection)
    summaries: list[str] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)
    knowledge_text: str = ""
    memories: list[MemoryLine] = field(default_factory=list)

    def render(self) -> str:
        sections = [f"【作品信息】\n{self.project_text}"] if self.include_project else []
        if self.world_text:
            sections.append(f"【世界观】\n{self.world_text}")
        if self.knowledge_text:
            sections.append(f"{KNOWLEDGE_HEADER}\n{self.knowledge_text}")
        if self.cards_text:
            sections.append(f"【本章涉及角色】\n{self.cards_text}")
        if self.relations:
            sections.append("【人物关系】\n" + "\n".join(r.render() for r in self.relations))
        if self.organizations_text:
            sections.append(f"【相关组织】\n{self.organizations_text}")
        if self.careers_text:
            sections.append(f"【职业体系】\n{self.careers_text}")
        if self.roster:
            sections.append("【其他角色（仅名单）】\n" + "，".join(self.roster))
        if self.outline_text:
            sections.append(f"【当前卷大纲】\n{self.outline_text}")
        foreshadow_lines = self.foreshadows.lines()
        if foreshadow_lines:
            sections.append("【伏笔提醒】\n" + "\n".join(foreshadow_lines))
        if self.summaries:
            sections.append("【前情提要（更早章节）】\n" + "\n".join(self.summaries))
        if self.memories:
            sections.append(MEMORY_HEADER + "\n" + "\n".join(m.text for m in self.memories))
        if self.recent:
            sections.append("【最近章节正文】\n" + "\n\n".join(self.recent))
        return "\n\n".join(sections)


def _chapter_heading(no: int, title: str) -> str:
    return f"第{no}章 {title}".rstrip()


def _summary_line(ch: PriorChapter, summary_chars: int) -> str:
    summary = " ".join(ch.summary.split())
    if summary:
        return f"{_chapter_heading(ch.chapter_no, ch.title)}：{summary}"
    text = " ".join(ch.content.split())
    snippet = text[:summary_chars] + (ELLIPSIS if len(text) > summary_chars else "")
    return f"{_chapter_heading(ch.chapter_no, ch.title)}：{snippet}"


def _cut_tail(text: str, keep: int) -> str:
    """保留开头 keep 个字符。"""
    if keep <= 0:
        return ""
    return text if len(text) <= keep else text[: max(0, keep - len(ELLIPSIS))] + ELLIPSIS


def assemble_context(
    *,
    project_text: str,
    world_text: str,
    cast: CastSection | None,
    outline_text: str,
    prior_chapters: list[PriorChapter],
    budget: int,
    recent_count: int = RECENT_FULL_CHAPTERS,
    summary_chars: int = SUMMARY_CHARS,
    foreshadows: ForeshadowSection | None = None,
    knowledge: str = "",
    memories: MemorySection | None = None,
    include_project: bool = True,
) -> str:
    """纯函数：根据素材与字符预算组装上下文。prior_chapters 需按章节号升序。

    knowledge：参考资料正文（不含标题行）；memories：相关旧情节（已按章号排列）。都为空时与不传完全一致。
    """
    prior = [c for c in prior_chapters if c.content.strip()]
    recent_src = prior[-recent_count:] if recent_count > 0 else []
    earlier = prior[: len(prior) - len(recent_src)]

    cast = cast or CastSection()
    parts = ContextParts(
        project_text=project_text,
        include_project=include_project,
        world_text=world_text.strip(),
        cards_text="\n\n".join(c.strip() for c in cast.cards if c.strip()),
        relations=[RelationLine(r.head, r.description) for r in cast.relations],
        organizations_text="\n".join(o.strip() for o in cast.organizations if o.strip()),
        careers_text="\n".join(c.strip() for c in cast.careers if c.strip()),
        roster=list(cast.roster),
        outline_text=outline_text.strip(),
        # 裁剪只整档替换为空列表，浅拷贝即可避免改动调用方的对象
        foreshadows=replace(foreshadows) if foreshadows else ForeshadowSection(),
        summaries=[_summary_line(c, summary_chars) for c in earlier],
        knowledge_text=knowledge.strip(),
        memories=[MemoryLine(m.text, m.pinned, m.rank) for m in (memories.lines if memories else []) if m.text.strip()],
    )
    recent_bodies = [c.content.strip() for c in recent_src]
    recent_heads = [_chapter_heading(c.chapter_no, c.title) for c in recent_src]

    def sync_recent() -> None:
        parts.recent = [f"{h}\n{b}" for h, b in zip(recent_heads, recent_bodies, strict=True) if b]

    sync_recent()

    def over() -> int:
        return len(parts.render()) - budget

    excess = over()

    # 0a. 截断参考资料尾部
    if excess > 0 and parts.knowledge_text:
        keep = len(parts.knowledge_text) - excess
        parts.knowledge_text = _cut_tail(parts.knowledge_text, keep) if keep > len(ELLIPSIS) else ""
        excess = over()

    # 0b. 从最不相关的开始丢弃未钉住的旧情节
    for victim in sorted((m for m in parts.memories if not m.pinned), key=lambda m: m.rank, reverse=True):
        if excess <= 0:
            break
        parts.memories.remove(victim)
        excess = over()

    # 1. 丢弃最早的章节摘要
    while excess > 0 and parts.summaries:
        excess -= len(parts.summaries.pop(0)) + 1
        if not parts.summaries:
            excess = over()
    excess = over()

    # 2. 截掉最近章节正文开头（较早的一章先截），保留结尾
    for i in range(len(recent_bodies)):
        if excess <= 0:
            break
        body = recent_bodies[i]
        keep = len(body) - excess - len(ELLIPSIS)
        recent_bodies[i] = ELLIPSIS + body[-keep:] if keep > 0 else ""
        sync_recent()
        excess = over()

    def cut_attr(attr: str) -> None:
        nonlocal excess
        if excess <= 0:
            return
        text: str = getattr(parts, attr)
        setattr(parts, attr, _cut_tail(text, len(text) - excess))
        excess = over()

    # 3. 截断世界观
    cut_attr("world_text")

    # 4. 伏笔提醒：先整档去掉长线伏笔，再整档去掉即将到期
    for tier in ("long_term", "upcoming"):
        if excess > 0 and getattr(parts.foreshadows, tier):
            setattr(parts.foreshadows, tier, [])
            excess = over()

    # 5. 角色部分：名单 → 关系描述 → 关系 → 职业体系 → 相关组织 → 涉及角色卡
    while excess > 0 and parts.roster:
        parts.roster.pop()
        excess = over()
    for rel in reversed(parts.relations):
        if excess <= 0:
            break
        if rel.description:
            rel.description = ""
            excess = over()
    while excess > 0 and parts.relations:
        parts.relations.pop()
        excess = over()
    cut_attr("careers_text")
    cut_attr("organizations_text")
    cut_attr("cards_text")

    # 6. 截断当前卷大纲
    cut_attr("outline_text")

    rendered = parts.render()
    # 7. 兜底
    return _cut_tail(rendered, budget) if len(rendered) > budget else rendered


def _current_volume_outlines(outlines: list[Outline], chapter: Chapter) -> list[Outline]:
    if not outlines:
        return []
    volume_no: int | None = None
    if chapter.outline_id is not None:
        volume_no = next((o.volume_no for o in outlines if o.id == chapter.outline_id), None)
    if volume_no is None:
        candidates = [o for o in outlines if o.chapter_no <= chapter.chapter_no]
        if candidates:
            volume_no = max(candidates, key=lambda o: o.chapter_no).volume_no
    if volume_no is None:
        volume_no = outlines[0].volume_no
    return [o for o in outlines if o.volume_no == volume_no]


LIFE_STATUS_LABELS = {"dead": "已死亡", "missing": "失踪", "other": "存活状态：其他"}
MEMBER_STATUS_LABELS = {"left": "已离开", "expelled": "已开除", "dead": "已阵亡"}


def format_card(
    c: Character | SimpleNamespace,
    memberships: list[tuple[OrganizationMember | SimpleNamespace, Organization]],
    *,
    include_legacy_relations: bool = False,
    career_line: str = "",
    custom_fields: list[tuple[str, str]] | None = None,
) -> str:
    """涉及角色的完整角色卡。非存活状态在标题醒目标注。"""
    head = f"■ {c.name}"
    if c.aliases:
        head += f"（别名：{'、'.join(c.aliases)}）"
    if c.role_type:
        head += f" · {c.role_type}"
    if c.life_status in LIFE_STATUS_LABELS:
        head += f"【{LIFE_STATUS_LABELS[c.life_status]}】"
    lines = [head]
    basics = [f"{label}：{v.strip()}" for label, v in (("年龄", c.age), ("性别", c.gender)) if v.strip()]
    if basics:
        lines.append("；".join(basics))
    for label, value in (
        ("外貌", c.appearance),
        ("性格", c.personality),
        ("背景", c.background),
        ("简介", c.profile),
        ("当前状态", c.current_state),
    ):
        if value.strip():
            lines.append(f"{label}：{value.strip()}")
    for label, value in custom_fields or []:
        if value.strip():
            lines.append(f"{label}：{value.strip()}")
    if memberships:
        orgs = []
        for m, o in memberships:
            extra = [x for x in (m.position.strip(), MEMBER_STATUS_LABELS.get(m.status, "")) if x]
            orgs.append(o.name + (f"（{'，'.join(extra)}）" if extra else ""))
        lines.append(f"所属组织：{'、'.join(orgs)}")
    if career_line:
        lines.append(career_line)
    # 尚未结构化的旧关系备注仍作为参考（已有结构化关系时不再重复）
    if include_legacy_relations and c.relations_text.strip():
        lines.append(f"关系备注：{c.relations_text.strip()}")
    return "\n".join(lines)


def format_organization(o: Organization) -> str:
    head = f"■ {o.name}" + (f"（{o.org_type}）" if o.org_type else "")
    return f"{head}：{o.purpose.strip()}" if o.purpose.strip() else head


def _career_item(a: CharacterCareer, career: Career, stages: list[CareerStage]) -> str:
    stage = next((s for s in stages if s.id == a.stage_id), None)
    if stage is None:
        return career.name
    extra = f"{stage.position}/{len(stages)}" + (f"，{a.note.strip()}" if a.note.strip() else "")
    return f"{career.name}·{stage.name}（{extra}）"


def format_career_line(items: list[tuple[CharacterCareer, Career]], stages: dict[int, list[CareerStage]]) -> str:
    """「职业：剑修·筑基（2/9，瓶颈期）；副职业：丹师·一品（1/9）」；没有挂接时为空串。"""
    main = [_career_item(a, c, stages.get(c.id, [])) for a, c in items if a.role == "main"]
    sub = [_career_item(a, c, stages.get(c.id, [])) for a, c in items if a.role != "main"]
    parts = []
    if main:
        parts.append(f"职业：{'、'.join(main)}")
    if sub:
        parts.append(f"副职业：{'、'.join(sub)}")
    return "；".join(parts)


def format_career_ladder(career: Career, stages: list[CareerStage]) -> str:
    """「■ 剑修（主修）：练气 → 筑基 → 金丹」，规则非空时附一行（前 200 字）。"""
    text = f"■ {career_service.ladder_line(career, stages)}"
    rules = " ".join(career.rules.split())
    if rules:
        clipped = rules[:CAREER_RULES_CHARS] + (ELLIPSIS if len(rules) > CAREER_RULES_CHARS else "")
        text += f"\n规则：{clipped}"
    return text


def roster_item(c: Character) -> str:
    return c.name + (f"（{c.role_type}）" if c.role_type else "")


@dataclass
class EffectiveCast:
    """供生成与分析共用的只读投影；不把历史字段赋值给 ORM 当前行。"""

    characters: list[Character | SimpleNamespace] = field(default_factory=list)
    memberships: dict[int, list[tuple[OrganizationMember | SimpleNamespace, Organization]]] = field(
        default_factory=dict
    )
    careers_by_character: dict[int, list[tuple[CharacterCareer | SimpleNamespace, Career]]] = field(
        default_factory=dict
    )
    careers: dict[int, Career] = field(default_factory=dict)
    stages: dict[int, list[CareerStage]] = field(default_factory=dict)
    organizations: dict[int, Organization] = field(default_factory=dict)
    relations: list[RelationLine] = field(default_factory=list)
    structured_character_ids: set[int] = field(default_factory=set)
    custom_fields: dict[int, list[tuple[str, str]]] = field(default_factory=dict)


async def _custom_labels(
    db: AsyncSession, timelines: list[SettingTimeline], snapshots: dict[int, EffectiveSnapshot]
) -> dict[int, list[tuple[str, str]]]:
    """批量将稳定自定义字段 ID 映射为显示名称与目标时点的值。"""
    if not timelines:
        return {}
    rows = await db.scalars(
        select(SettingTextField)
        .where(SettingTextField.timeline_id.in_([t.id for t in timelines]))
        .order_by(SettingTextField.sort, SettingTextField.id)
    )
    result: dict[int, list[tuple[str, str]]] = {}
    for item in rows:
        snapshot = snapshots[item.timeline_id]
        value = snapshot.custom_fields.get(item.id, "")
        if value.strip():
            result.setdefault(item.timeline_id, []).append((item.label, value))
    return result


async def effective_world_text(
    db: AsyncSession, project: Project, chapter: Chapter, *, exclude_ids: set[int] | None = None
) -> str:
    """按目标章初组装世界观，保留条目分类与原有顺序。"""
    worlds = await story_service.list_items(db, story_service.WORLD, project.id)
    if exclude_ids:
        worlds = [world for world in worlds if world.id not in exclude_ids]
    if not worlds:
        return ""
    timelines = list(await db.scalars(
        select(SettingTimeline).where(
            SettingTimeline.project_id == project.id, SettingTimeline.subject_kind == "world"
        ).order_by(SettingTimeline.id)
    ))
    snapshots = await setting_progression_service.resolve_timelines(
        db, project.id, timelines, StoryPosition(chapter_id=chapter.id, chapter_no=chapter.chapter_no)
    )
    labels = await _custom_labels(db, timelines, snapshots)
    by_world = {t.world_setting_id: t for t in timelines}
    result = []
    for world in worlds:
        timeline = by_world.get(world.id)
        content = snapshots[timeline.id].fields["content"] if timeline else world.content
        custom = labels.get(timeline.id, []) if timeline else []
        text = "\n".join([str(content), *(f"{name}：{value}" for name, value in custom)]).strip()
        result.append(SimpleNamespace(title=world.title, category=world.category, content=text))
    return story_service.format_world(result)


async def resolve_effective_cast(
    db: AsyncSession, project: Project, chapter: Chapter, involved: list[Character]
) -> EffectiveCast:
    """批量读取目标章的角色、关系、职业和成员，同一逻辑供生成及分析使用。"""
    result = EffectiveCast()
    ids = [c.id for c in involved]
    if not ids:
        return result
    id_set = set(ids)
    timelines = list(await db.scalars(
        select(SettingTimeline).where(
            SettingTimeline.project_id == project.id,
            or_(
                and_(SettingTimeline.subject_kind.in_(("character", "career", "membership")),
                     SettingTimeline.character_id.in_(ids)),
                and_(SettingTimeline.subject_kind == "relationship",
                     or_(SettingTimeline.character_id.in_(ids), SettingTimeline.related_character_id.in_(ids))),
            ),
        ).order_by(SettingTimeline.id)
    ))
    relevant = [
        t for t in timelines
        if t.subject_kind != "relationship" or (t.character_id in id_set and t.related_character_id in id_set)
    ]
    snapshots = await setting_progression_service.resolve_timelines(
        db, project.id, relevant, StoryPosition(chapter_id=chapter.id, chapter_no=chapter.chapter_no)
    )
    char_timelines = {t.character_id: t for t in relevant if t.subject_kind == "character"}
    custom = await _custom_labels(db, list(char_timelines.values()), snapshots)
    for c in involved:
        timeline = char_timelines.get(c.id)
        if timeline is None:
            result.characters.append(c)
            continue
        # 不复制 SQLAlchemy 的内部状态，也不修改 identity map 中的实体。
        values: dict[str, Any] = {
            key: getattr(c, key)
            for key in ("id", "name", "aliases", "role_type", "always_in_context", "relations_text")
        }
        values.update(snapshots[timeline.id].fields)
        result.characters.append(SimpleNamespace(**values))
        result.custom_fields[c.id] = custom.get(timeline.id, [])

    # 关系备注是否回退应以整个角色是否已结构化管理为准，而非当前时点是否存在一条有效关系。
    for t in timelines:
        if t.subject_kind == "relationship":
            result.structured_character_ids.update({t.character_id, t.related_character_id} & id_set)
    pairs = await db.execute(
        select(Relationship.a_id, Relationship.b_id).where(
            Relationship.project_id == project.id,
            or_(Relationship.a_id.in_(ids), Relationship.b_id.in_(ids)),
        )
    )
    for a_id, b_id in pairs:
        result.structured_character_ids.update({a_id, b_id} & id_set)

    # 当前挂接仅作为没有时间线的旧数据兜底；已卸下的历史主体仍由稳定时间线枚举。
    current_members = await organization_service.memberships_of(db, ids)
    members = {(m.character_id, o.id): (m, o) for m, o in current_members}
    member_timelines = [t for t in relevant if t.subject_kind == "membership"]
    organization_ids = [t.organization_id for t in member_timelines if t.organization_id is not None]
    organization_by_id = {o.id: o for _, o in current_members}
    if organization_ids:
        rows = await db.scalars(select(Organization).where(
            Organization.project_id == project.id, Organization.id.in_(organization_ids)
        ))
        organization_by_id.update({o.id: o for o in rows})
    for timeline in member_timelines:
        key = (timeline.character_id, timeline.organization_id)
        members.pop(key, None)
        fields = snapshots[timeline.id].fields
        org = organization_by_id.get(timeline.organization_id)
        if fields.get("exists") and org is not None:
            member = SimpleNamespace(character_id=timeline.character_id, organization_id=org.id,
                                     position=fields["position"], status=fields["status"], note=fields["note"])
            members[key] = (member, org)
    for (character_id, _), (member, org) in sorted(
        members.items(), key=lambda row: (row[1][1].sort, row[1][1].id, row[0][0])
    ):
        result.memberships.setdefault(character_id, []).append((member, org))
        result.organizations[org.id] = org

    current_careers = await career_service.assignments_of(db, ids)
    assignments = {(a.character_id, career.id): (a, career) for a, career in current_careers}
    career_timelines = [t for t in relevant if t.subject_kind == "career"]
    career_ids = [t.career_id for t in career_timelines if t.career_id is not None]
    career_by_id = {career.id: career for _, career in current_careers}
    if career_ids:
        rows = await db.scalars(select(Career).where(Career.project_id == project.id, Career.id.in_(career_ids)))
        career_by_id.update({career.id: career for career in rows})
    for timeline in career_timelines:
        key = (timeline.character_id, timeline.career_id)
        assignments.pop(key, None)
        fields = snapshots[timeline.id].fields
        career = career_by_id.get(timeline.career_id)
        if fields.get("exists") and career is not None:
            assignment = SimpleNamespace(character_id=timeline.character_id, career_id=career.id, role=career.kind,
                                         stage_id=fields["stage_id"], note=fields["note"])
            assignments[key] = (assignment, career)
    for (character_id, _), (assignment, career) in sorted(
        assignments.items(), key=lambda row: (row[1][0].role, row[1][1].sort, row[1][1].id)
    ):
        result.careers_by_character.setdefault(character_id, []).append((assignment, career))
        result.careers[career.id] = career
    result.stages = await career_service.stages_of(db, list(result.careers))

    relation_timelines = [t for t in relevant if t.subject_kind == "relationship"]
    managed_pairs = {(t.character_id, t.related_character_id) for t in relation_timelines}
    relation_values: list[tuple[dict[str, Any], RelationshipType, int | None]] = []
    for relation, relation_type in await relationship_service.relations_among(db, ids):
        key = (relation.pair_low, relation.pair_high)
        if key not in managed_pairs and relation.is_present:
            relation_values.append(({
                "identity": {"a_id": relation.a_id, "b_id": relation.b_id}, "intimacy": relation.intimacy,
                "status": relation.status, "description": relation.description,
            }, relation_type, None))
    type_ids = {
        fields["identity"]["type_id"]
        for t in relation_timelines
        if (fields := snapshots[t.id].fields).get("exists")
    }
    types: dict[int, RelationshipType] = {}
    if type_ids:
        rows = await db.scalars(select(RelationshipType).where(
            RelationshipType.id.in_(type_ids),
            or_(RelationshipType.project_id.is_(None), RelationshipType.project_id == project.id),
        ))
        types = {item.id: item for item in rows}
    for timeline in relation_timelines:
        fields = snapshots[timeline.id].fields
        if fields.get("exists"):
            relation_type = types.get(fields["identity"]["type_id"])
            if relation_type is not None:
                relation_values.append((fields, relation_type, timeline.id))
    names = {c.id: c.name for c in involved}
    for values, relation_type, timeline_id in relation_values:
        identity = values["identity"]
        a_id, b_id = identity["a_id"], identity["b_id"]
        if a_id in names and b_id in names:
            status = values["status"]
            result.relations.append(RelationLine(
                f"{relationship_service.describe_relation(names[a_id], names[b_id], relation_type)}"
                f"（亲密度 {values['intimacy']}，{relationship_service.STATUS_LABELS.get(status, status)}）",
                values["description"].strip(),
                timeline_id=timeline_id,
            ))
    return result


async def build_cast_section(
    db: AsyncSession, project: Project, chapter: Chapter, outline_summary: str, *,
    planning: "PlanningContext | None" = None,
) -> CastSection:
    cast = await cast_service.select_cast(db, project, chapter, outline_summary)
    involved = list(cast.involved)
    extra_ids = planning.additional_character_ids - {c.id for c in involved} if planning else set()
    if extra_ids:
        involved.extend(await db.scalars(select(Character).where(
            Character.project_id == project.id, Character.id.in_(extra_ids)
        ).order_by(Character.sort, Character.id)))
    effective = await resolve_effective_cast(db, project, chapter, involved)
    protected_characters = planning.protected_character_ids if planning else set()
    protected_relations = planning.protected_relationship_ids if planning else set()
    protected_organizations = planning.protected_organization_ids if planning else set()
    protected_careers = planning.protected_career_ids if planning else set()
    return CastSection(
        cards=[
            format_card(
                c,
                effective.memberships.get(c.id, []),
                include_legacy_relations=c.id not in effective.structured_character_ids,
                career_line=format_career_line(effective.careers_by_character.get(c.id, []), effective.stages),
                custom_fields=effective.custom_fields.get(c.id, []),
            )
            for c in effective.characters
            if c.id not in protected_characters
        ],
        relations=[r for r in effective.relations if r.timeline_id not in protected_relations],
        organizations=[
            format_organization(o) for o in effective.organizations.values() if o.id not in protected_organizations
        ],
        careers=[
            format_career_ladder(c, effective.stages.get(c.id, []))
            for c in sorted(effective.careers.values(), key=lambda c: (c.sort, c.id))
            if c.id not in protected_careers
        ],
        roster=[roster_item(c) for c in cast.others if c.id not in protected_characters],
        names=[c.name for c in involved],
    )


async def build_chapter_context(
    db: AsyncSession, project: Project, chapter: Chapter, budget: int, *, embedder: Embedder | None = None,
    planning: "PlanningContext | None" = None,
) -> str:
    """embedder：当前用户的向量模型（None = 检索只用关键词）。"""
    with db.no_autoflush:
        outlines = await story_service.list_items(db, story_service.OUTLINES, project.id)
        task = await chapter_task(db, chapter)
        cast = await build_cast_section(db, project, chapter, task.match_text(), planning=planning)
        prior = await prior_chapters(db, project, chapter)
        knowledge, memories = await retrieval_blocks(db, project, chapter, task, cast.names, prior, embedder=embedder)
        return assemble_context(
            project_text=format_project_info(await project_info(db, project)),
            world_text=await effective_world_text(
                db, project, chapter, exclude_ids=planning.protected_world_ids if planning else None
            ),
            cast=cast,
            outline_text=story_service.format_outlines(_current_volume_outlines(outlines, chapter)),
            prior_chapters=prior,
            budget=budget,
            foreshadows=await build_foreshadow_section(db, project.id, chapter.chapter_no),
            knowledge=knowledge,
            memories=memories,
        )


async def build_setting_context(
    db: AsyncSession, project: Project, chapter: Chapter, *, text_hint: str = "", budget: int = 4000,
    planning: "PlanningContext | None" = None,
) -> str:
    """供局部改写和设定预览使用的章初设定块；不检索知识库或调用模型。"""
    with db.no_autoflush:
        header = "【本章有效设定】\n"
        available = budget - len(header)
        if available <= 0:
            return ""
        task = await chapter_task(db, chapter)
        hint = "\n".join(part for part in (task.match_text(), text_hint) if part)
        cast = await build_cast_section(db, project, chapter, hint, planning=planning)
        body = assemble_context(
            project_text="",
            include_project=False,
            world_text=await effective_world_text(
                db, project, chapter, exclude_ids=planning.protected_world_ids if planning else None
            ),
            cast=cast,
            outline_text="",
            prior_chapters=[],
            budget=available,
        )
        return header + body if body else ""


async def prior_chapters(db: AsyncSession, project: Project, chapter: Chapter) -> list[PriorChapter]:
    rows = await db.execute(
        select(Chapter.id, Chapter.chapter_no, Chapter.title, Chapter.content)
        .where(
            Chapter.project_id == project.id,
            Chapter.id != chapter.id,
            Chapter.chapter_no < chapter.chapter_no,
        )
        .order_by(Chapter.chapter_no, Chapter.id)
    )
    prior_rows = rows.all()
    summaries = await latest_summaries(db, project.id, [r[0] for r in prior_rows])
    return [
        PriorChapter(chapter_no=no, title=title, content=content, summary=summaries.get(cid, ""))
        for cid, no, title, content in prior_rows
    ]


def prior_coverage(prior: list[PriorChapter], recent_count: int = RECENT_FULL_CHAPTERS) -> tuple[set[int], set[int]]:
    """(前情给了全文的章节号, 前情给了分析摘要的章节号)，与 assemble_context 的划分一致（裁剪前）。"""
    nonempty = [c for c in prior if c.content.strip()]
    recent = nonempty[-recent_count:] if recent_count > 0 else []
    earlier = nonempty[: len(nonempty) - len(recent)]
    return {c.chapter_no for c in recent}, {c.chapter_no for c in earlier if " ".join(c.summary.split())}


def retrieval_query(task: "ChapterTask", names: list[str], *, title: str = "", with_goal: bool = False) -> str:
    parts = [title or task.title, task.summary, *task.key_events]
    if with_goal:
        parts.append(task.goal)
    parts.append(" ".join(names))
    return "\n".join(p.strip() for p in parts if p and p.strip())


async def retrieval_blocks(
    db: AsyncSession,
    project: Project,
    chapter: Chapter,
    task: "ChapterTask",
    names: list[str],
    prior: list[PriorChapter],
    *,
    embedder: Embedder | None,
) -> tuple[str, MemorySection]:
    """【参考资料】正文与【相关旧情节】条目（裁剪前）。检索出错时对应块为空，不抛错。"""
    full_nos, summary_nos = prior_coverage(prior)
    knowledge = await kb_service.knowledge_text(
        db, project, retrieval_query(task, names, title=chapter.title), embedder=embedder, purpose="章节生成"
    )
    recalled = await memory_service.recall(
        db,
        project,
        chapter.chapter_no,
        retrieval_query(task, names, title=chapter.title, with_goal=True),
        embedder=embedder,
        excluded_chapter_nos=full_nos,
        summary_chapter_nos=summary_nos,
    )
    return knowledge, MemorySection([MemoryLine(r.line, r.pinned, r.rank) for r in recalled])


async def retrieval_preview(db: AsyncSession, user_id: int, chapter_id: int, *, env: EmbedEnv) -> tuple[str, str]:
    """GET /chapters/{id}/context-preview：本次生成会注入的参考资料与相关旧情节（带标题行，裁剪前；不注入为空串）。"""
    from app.services.chapter_service import get_owned_chapter
    from app.services.project_service import get_owned_project

    chapter = await get_owned_chapter(db, user_id, chapter_id)
    project = await get_owned_project(db, user_id, chapter.project_id)
    embedder = await env.embedder(db, user_id)
    task = await chapter_task(db, chapter)
    cast = await cast_service.select_cast(db, project, chapter, task.match_text())
    prior = await prior_chapters(db, project, chapter)
    knowledge, memories = await retrieval_blocks(
        db, project, chapter, task, [c.name for c in cast.involved], prior, embedder=embedder
    )
    return (
        f"{KNOWLEDGE_HEADER}\n{knowledge}" if knowledge.strip() else "",
        MEMORY_HEADER + "\n" + "\n".join(m.text for m in memories.lines) if memories.lines else "",
    )


async def build_foreshadow_section(db: AsyncSession, project_id: int, chapter_no: int) -> ForeshadowSection:
    rem = await foreshadow_service.reminders(db, project_id, chapter_no)
    if rem.empty():
        return ForeshadowSection()
    return ForeshadowSection(**foreshadow_service.reminder_lines(rem))


async def latest_summaries(db: AsyncSession, project_id: int, chapter_ids: list[int]) -> dict[int, str]:
    """每章最近一次分析的摘要（一条查询：子查询取每章 max(id)）。"""
    if not chapter_ids:
        return {}
    latest = (
        select(func.max(ChapterAnalysis.id))
        .where(ChapterAnalysis.project_id == project_id, ChapterAnalysis.chapter_id.in_(chapter_ids))
        .group_by(ChapterAnalysis.chapter_id)
    )
    rows = await db.execute(
        select(ChapterAnalysis.chapter_id, ChapterAnalysis.summary).where(ChapterAnalysis.id.in_(latest))
    )
    return {cid: summary for cid, summary in rows.all() if (summary or "").strip()}


async def find_chapter_outline(db: AsyncSession, chapter: Chapter) -> Outline | None:
    if chapter.outline_id is None:
        return None
    return await db.scalar(
        select(Outline).where(Outline.id == chapter.outline_id, Outline.project_id == chapter.project_id)
    )


@dataclass
class ChapterTask:
    """【本章任务】素材：来自细纲（source="plan"）或大纲（source="outline"），都没有时为空。"""

    title: str = ""
    summary: str = ""
    key_events: list[str] = field(default_factory=list)
    goal: str = ""
    emotion: str = ""
    est_words: int | None = None
    source: str = ""

    def match_text(self) -> str:
        """选角自动匹配用的文本：梗概 + 关键事件。"""
        return "\n".join([self.summary, *self.key_events])


async def chapter_task(db: AsyncSession, chapter: Chapter) -> ChapterTask:
    outline = await find_chapter_outline(db, chapter)
    plan = await plan_service.get_chapter_plan(db, chapter)
    if plan is not None:
        return ChapterTask(
            title=plan.title,
            summary=plan.summary.strip() or (outline.summary if outline else ""),
            key_events=list(plan.key_events or []),
            goal=plan.goal,
            emotion=plan.emotion,
            est_words=plan.est_words,
            source="plan",
        )
    if outline is not None:
        return ChapterTask(title=outline.title, summary=outline.summary, source="outline")
    return ChapterTask()
