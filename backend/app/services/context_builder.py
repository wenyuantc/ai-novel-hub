"""章节生成上下文组装（MVP 不依赖向量检索）。

本章任务（chapter_task，供提示词【本章任务】与选角匹配使用）按优先级取：
  1. 章节对应的细纲（chapters.plan_id）：标题、梗概（细纲梗概为空时退回大纲梗概）、关键事件、叙事目标、
     情感基调、预估字数；
  2. 章节关联的大纲：标题与梗概（与引入细纲前的行为一致）。
  生成请求的目标字数仍由请求决定（前端打开生成面板时用细纲的预估字数预填），后端不改变请求语义。

组成：作品信息 + 世界观（按分类分组）+ 角色部分 + 当前卷大纲 + 伏笔提醒 + 更早章节标题/摘要 + 最近 N 章全文。
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

from sqlalchemy import func, select
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
)
from app.services import (
    career_service,
    cast_service,
    foreshadow_service,
    organization_service,
    plan_service,
    relationship_service,
    story_service,
)
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
    # 最近一次章节分析的摘要；为空时前情提要退回正文开头
    summary: str = ""


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
class ContextParts:
    project_text: str
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
        foreshadow_lines = self.foreshadows.lines()
        if foreshadow_lines:
            sections.append("【伏笔提醒】\n" + "\n".join(foreshadow_lines))
        if self.summaries:
            sections.append("【前情提要（更早章节）】\n" + "\n".join(self.summaries))
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
        # 裁剪只整档替换为空列表，浅拷贝即可避免改动调用方的对象
        foreshadows=replace(foreshadows) if foreshadows else ForeshadowSection(),
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
    task = await chapter_task(db, chapter)
    cast = await build_cast_section(db, project, chapter, task.match_text())
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
    prior = [
        PriorChapter(chapter_no=no, title=title, content=content, summary=summaries.get(cid, ""))
        for cid, no, title, content in prior_rows
    ]
    return assemble_context(
        project_text=format_project_info(await project_info(db, project)),
        world_text=story_service.format_world(world),
        cast=cast,
        outline_text=story_service.format_outlines(_current_volume_outlines(outlines, chapter)),
        prior_chapters=prior,
        budget=budget,
        foreshadows=await build_foreshadow_section(db, project.id, chapter.chapter_no),
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
