"""章节生成上下文组装（MVP 不依赖向量检索）。

组成：作品信息 + 世界观（按分类分组）+ 角色部分 + 当前卷大纲 + 更早章节标题/摘要 + 最近 N 章全文。
角色部分（D4，选角见 cast_service.select_cast）：
  - 本章涉及角色（主角 ∪ 手动指定出场 ∪ 梗概 / 上一章结尾提及）：完整角色卡，含所属组织与职位
  - 人物关系：只列涉及角色两两之间的关系
  - 相关组织：涉及角色所属组织的名称、类型、宗旨
  - 职业体系：涉及角色所挂职业的完整阶段阶梯（角色卡中有「职业：剑修·筑基（2/9，瓶颈期）」行）
  - 其他角色：只列「名字（定位）」名单
超出字符预算时的裁剪顺序：
  1. 从最早开始丢弃「更早章节摘要」
  2. 从较早的一章开始，截掉最近章节正文的开头（保留结尾，利于衔接）
  3. 截断世界观尾部
  4. 角色部分：先从尾部丢弃「其他角色」名单 → 再从尾部去掉关系描述（保留关系本身）→ 再从尾部丢弃关系 →
     截断职业体系 → 截断相关组织 → 最后截断涉及角色卡（排在后面的角色先被截；主角在最前）
  5. 截断当前卷大纲尾部
  6. 最终硬截断兜底，保证不超过预算
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts import format_project_info
from app.models import (
    Career,
    CareerStage,
    Chapter,
    Character,
    CharacterCareer,
    Organization,
    OrganizationMember,
    Outline,
    Project,
)
from app.services import career_service, cast_service, organization_service, relationship_service, story_service
from app.services.project_service import project_info

RECENT_FULL_CHAPTERS = 2
SUMMARY_CHARS = 200
ELLIPSIS = "……"
CAREER_RULES_CHARS = 200


@dataclass
class PriorChapter:
    chapter_no: int
    title: str
    content: str


@dataclass
class RelationLine:
    head: str
    description: str = ""

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


@dataclass
class ContextParts:
    project_text: str
    world_text: str = ""
    cards_text: str = ""
    relations: list[RelationLine] = field(default_factory=list)
    organizations_text: str = ""
    careers_text: str = ""
    roster: list[str] = field(default_factory=list)
    outline_text: str = ""
    summaries: list[str] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)

    def render(self) -> str:
        sections = [f"【作品信息】\n{self.project_text}"]
        if self.world_text:
            sections.append(f"【世界观】\n{self.world_text}")
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
        if self.summaries:
            sections.append("【前情提要（更早章节）】\n" + "\n".join(self.summaries))
        if self.recent:
            sections.append("【最近章节正文】\n" + "\n\n".join(self.recent))
        return "\n\n".join(sections)


def _chapter_heading(no: int, title: str) -> str:
    return f"第{no}章 {title}".rstrip()


def _summary_line(ch: PriorChapter, summary_chars: int) -> str:
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
) -> str:
    """纯函数：根据素材与字符预算组装上下文。prior_chapters 需按章节号升序。"""
    prior = [c for c in prior_chapters if c.content.strip()]
    recent_src = prior[-recent_count:] if recent_count > 0 else []
    earlier = prior[: len(prior) - len(recent_src)]

    cast = cast or CastSection()
    parts = ContextParts(
        project_text=project_text,
        world_text=world_text.strip(),
        cards_text="\n\n".join(c.strip() for c in cast.cards if c.strip()),
        relations=[RelationLine(r.head, r.description) for r in cast.relations],
        organizations_text="\n".join(o.strip() for o in cast.organizations if o.strip()),
        careers_text="\n".join(c.strip() for c in cast.careers if c.strip()),
        roster=list(cast.roster),
        outline_text=outline_text.strip(),
        summaries=[_summary_line(c, summary_chars) for c in earlier],
    )
    recent_bodies = [c.content.strip() for c in recent_src]
    recent_heads = [_chapter_heading(c.chapter_no, c.title) for c in recent_src]

    def sync_recent() -> None:
        parts.recent = [f"{h}\n{b}" for h, b in zip(recent_heads, recent_bodies, strict=True) if b]

    sync_recent()

    def over() -> int:
        return len(parts.render()) - budget

    # 1. 丢弃最早的章节摘要
    excess = over()
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

    # 4. 角色部分：名单 → 关系描述 → 关系 → 职业体系 → 相关组织 → 涉及角色卡
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

    # 5. 截断当前卷大纲
    cut_attr("outline_text")

    rendered = parts.render()
    # 6. 兜底
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
    c: Character,
    memberships: list[tuple[OrganizationMember, Organization]],
    *,
    include_legacy_relations: bool = False,
    career_line: str = "",
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


async def build_cast_section(db: AsyncSession, project: Project, chapter: Chapter, outline_summary: str) -> CastSection:
    cast = await cast_service.select_cast(db, project, chapter, outline_summary)
    ids = cast.involved_ids
    memberships = await organization_service.memberships_of(db, ids)
    by_char: dict[int, list[tuple[OrganizationMember, Organization]]] = {}
    orgs: dict[int, Organization] = {}
    for m, o in memberships:
        by_char.setdefault(m.character_id, []).append((m, o))
        orgs.setdefault(o.id, o)
    # 职业挂接与阶梯：各一次查询，避免按角色逐个查询
    assignments = await career_service.assignments_of(db, ids)
    careers_by_char: dict[int, list[tuple[CharacterCareer, Career]]] = {}
    involved_careers: dict[int, Career] = {}
    for a, career in assignments:
        careers_by_char.setdefault(a.character_id, []).append((a, career))
        involved_careers.setdefault(career.id, career)
    stages = await career_service.stages_of(db, list(involved_careers))
    rels = await relationship_service.relations_among(db, ids)
    names = {c.id: c.name for c in cast.involved}
    related = {r.a_id for r, _ in rels} | {r.b_id for r, _ in rels}
    relation_lines = [
        RelationLine(
            f"{relationship_service.describe_relation(names[r.a_id], names[r.b_id], t)}"
            f"（亲密度 {r.intimacy}，{relationship_service.STATUS_LABELS.get(r.status, r.status)}）",
            r.description.strip(),
        )
        for r, t in rels
    ]
    return CastSection(
        cards=[
            format_card(
                c,
                by_char.get(c.id, []),
                include_legacy_relations=c.id not in related,
                career_line=format_career_line(careers_by_char.get(c.id, []), stages),
            )
            for c in cast.involved
        ],
        relations=relation_lines,
        organizations=[format_organization(o) for o in orgs.values()],
        careers=[
            format_career_ladder(c, stages.get(c.id, []))
            for c in sorted(involved_careers.values(), key=lambda c: (c.sort, c.id))
        ],
        roster=[roster_item(c) for c in cast.others],
    )


async def build_chapter_context(db: AsyncSession, project: Project, chapter: Chapter, budget: int) -> str:
    world = await story_service.list_items(db, story_service.WORLD, project.id)
    outlines = await story_service.list_items(db, story_service.OUTLINES, project.id)
    outline = await find_chapter_outline(db, chapter)
    cast = await build_cast_section(db, project, chapter, outline.summary if outline else "")
    rows = await db.execute(
        select(Chapter.chapter_no, Chapter.title, Chapter.content)
        .where(
            Chapter.project_id == project.id,
            Chapter.id != chapter.id,
            Chapter.chapter_no < chapter.chapter_no,
        )
        .order_by(Chapter.chapter_no, Chapter.id)
    )
    prior = [PriorChapter(chapter_no=no, title=title, content=content) for no, title, content in rows.all()]
    return assemble_context(
        project_text=format_project_info(project_info(project)),
        world_text=story_service.format_world(world),
        cast=cast,
        outline_text=story_service.format_outlines(_current_volume_outlines(outlines, chapter)),
        prior_chapters=prior,
        budget=budget,
    )


async def find_chapter_outline(db: AsyncSession, chapter: Chapter) -> Outline | None:
    if chapter.outline_id is None:
        return None
    return await db.scalar(
        select(Outline).where(Outline.id == chapter.outline_id, Outline.project_id == chapter.project_id)
    )
