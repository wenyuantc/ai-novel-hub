"""细纲（chapter_plans）：CRUD、整体同步（重新展开的采纳）、建章。

章节号模型：每条大纲占 max(1, 已建章数) 个章节号（未建章的细纲不占号，建章时才插入并平移）。
一条大纲「已展开」= 其下至少有一条细纲：建章按细纲进行，`create_from_outlines` 跳过它；未展开的大纲仍按 1 纲 1 章。
chapter_plans.chapter_id 与 chapters.plan_id 双向冗余，只经本模块维护一致。

整体同步（PUT /outlines/{oid}/plans）：
- 原本已有细纲时先存快照（reexpand）；
- 已建章且章节有正文的细纲不能删除（缺失 → 409「已有正文的细纲不能删除」），只能改描述字段；
- 已建章但章节为空的细纲允许删除：空章节一并删除，其后的章节号整体 -1（shift_chapter_numbers）；
  删除后大纲一章都不剩时，保留大纲占的 1 个章节号（最前面那一章删除时不平移，大纲 chapter_no 设为该号）；
- 未建章的细纲按 id 更新、无 id 新建、缺失删除；最后 seq 按提交顺序重排为 1..N。

建章（POST /outlines/{oid}/plans/build）：
1. 取尚未建章的细纲（按 seq）；
2. 插入点：大纲原以 1 纲 1 章关联了章节 C（C.plan_id 为空）且尚无细纲建章 → C 绑定首个细纲
   （保留正文与章节号），其余插在 C 之后；
   否则插在本大纲已建章细纲的最后一章之后；都没有时插在大纲的 chapter_no；
3. 先 shift_chapter_numbers(from_no=插入点, delta=新建章数)，再建章（outline_id / plan_id / 标题 / 空正文 / draft），
   大纲还没有任何章节、且没有别的章节占用其 chapter_no 时，它本身占着这 1 个号：第一章用掉它，
   平移改为 (插入点 + 1, 新建章数 - 1)；
   细纲的出场角色写入 chapter_characters；
4. 大纲的 chapter_no 设为其第一章的章节号。
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Chapter, ChapterCharacter, ChapterPlan, Character, Outline
from app.schemas.chapter import ChapterBrief
from app.schemas.outline import PlanBuildResult, PlanOut, PlansSync, PlanUpdate
from app.services import memory_service, outline_snapshot_service, planning_lifecycle, setting_progression_service
from app.services.chapter_numbering import shift_chapter_numbers
from app.services.project_service import get_manageable_project

OUTLINE_NOT_FOUND = "大纲条目不存在"
PLAN_NOT_FOUND = "细纲不存在"
CONTENT_PLAN_DELETE = "已有正文的细纲不能删除"
CHARACTERS_INVALID = "部分角色不存在或不属于该项目"
PLAN_EDIT_FIELDS = ("title", "summary", "key_events", "character_ids", "emotion", "goal", "est_words")


async def get_project_outline(db: AsyncSession, user_id: int, project_id: int, outline_id: int) -> Outline:
    await get_manageable_project(db, user_id, project_id)
    outline = await db.scalar(select(Outline).where(Outline.id == outline_id, Outline.project_id == project_id))
    if outline is None:
        raise NotFoundError(OUTLINE_NOT_FOUND)
    return outline


async def project_character_ids(db: AsyncSession, project_id: int) -> set[int]:
    return set(await db.scalars(select(Character.id).where(Character.project_id == project_id)))


async def check_characters(db: AsyncSession, project_id: int, ids: list[int]) -> None:
    if ids and not set(ids) <= await project_character_ids(db, project_id):
        raise BadRequestError(CHARACTERS_INVALID)


async def plans_of(db: AsyncSession, outline_ids: list[int]) -> dict[int, list[ChapterPlan]]:
    if not outline_ids:
        return {}
    rows = await db.scalars(
        select(ChapterPlan).where(ChapterPlan.outline_id.in_(outline_ids)).order_by(ChapterPlan.seq, ChapterPlan.id)
    )
    out: dict[int, list[ChapterPlan]] = {}
    for p in rows:
        out.setdefault(p.outline_id, []).append(p)
    return out


async def plan_outs(db: AsyncSession, project_id: int, plans: list[ChapterPlan]) -> list[PlanOut]:
    """细纲 → PlanOut：过滤已删除的角色，附上章节号与是否已有正文（一次查询）。"""
    if not plans:
        return []
    valid = await project_character_ids(db, project_id)
    chapter_ids = [p.chapter_id for p in plans if p.chapter_id is not None]
    info: dict[int, tuple[int, bool]] = {}
    casts: dict[int, list[int]] = {}
    if chapter_ids:
        rows = await db.execute(
            select(Chapter.id, Chapter.chapter_no, Chapter.word_count).where(Chapter.id.in_(chapter_ids))
        )
        info = {cid: (no, (wc or 0) > 0) for cid, no, wc in rows.all()}
        cast_rows = await db.execute(
            select(ChapterCharacter.chapter_id, ChapterCharacter.character_id)
            .where(ChapterCharacter.chapter_id.in_(chapter_ids)).order_by(ChapterCharacter.character_id)
        )
        for cid, character_id in cast_rows:
            casts.setdefault(cid, []).append(character_id)
    outs = []
    for p in plans:
        out = PlanOut.model_validate(p)
        out.character_ids = [c for c in (p.character_ids or []) if c in valid]
        if p.chapter_id is not None and p.chapter_id in info:
            out.chapter_no, out.has_content = info[p.chapter_id]
            out.character_ids = [cid for cid in casts.get(p.chapter_id, []) if cid in valid]
        outs.append(out)
    return outs


async def _outline_plans(db: AsyncSession, outline_id: int) -> list[ChapterPlan]:
    return (await plans_of(db, [outline_id])).get(outline_id, [])


async def list_plans(db: AsyncSession, user_id: int, project_id: int, outline_id: int) -> list[PlanOut]:
    await get_project_outline(db, user_id, project_id, outline_id)
    return await plan_outs(db, project_id, await _outline_plans(db, outline_id))


async def update_plan(
    db: AsyncSession, user_id: int, project_id: int, outline_id: int, plan_id: int, data: PlanUpdate
) -> PlanOut:
    """单条细纲的行内编辑（不影响建章状态）。"""
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    await get_project_outline(db, user_id, project_id, outline_id)
    plan = await db.scalar(select(ChapterPlan).where(ChapterPlan.id == plan_id, ChapterPlan.outline_id == outline_id))
    if plan is None:
        raise NotFoundError(PLAN_NOT_FOUND)
    values = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    if "character_ids" in values:
        values["character_ids"] = list(dict.fromkeys(values["character_ids"]))
        await check_characters(db, project_id, values["character_ids"])
    if "title" in values:
        values["title"] = values["title"].strip()
        if not values["title"]:
            raise BadRequestError("细纲标题不能为空")
    for k, v in values.items():
        setattr(plan, k, v)
    if "character_ids" in values:
        await planning_lifecycle.sync_plan_cast(db, plan)
    await planning_lifecycle.bump_revision(db, project_id)
    await db.commit()
    await db.refresh(plan)
    return (await plan_outs(db, project_id, [plan]))[0]


async def get_chapter_plan(db: AsyncSession, chapter: Chapter) -> ChapterPlan | None:
    """章节对应的细纲（上下文「本章任务」用）。"""
    if chapter.plan_id is None:
        return None
    return await db.scalar(
        select(ChapterPlan).where(ChapterPlan.id == chapter.plan_id, ChapterPlan.project_id == chapter.project_id)
    )


async def chapter_plan_out(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> PlanOut | None:
    from app.services.chapter_service import get_manageable_chapter

    chapter = await get_manageable_chapter(db, user_id, project_id, chapter_id)
    plan = await get_chapter_plan(db, chapter)
    return (await plan_outs(db, project_id, [plan]))[0] if plan is not None else None


async def sync_plans(
    db: AsyncSession, user_id: int, project_id: int, outline_id: int, data: PlansSync
) -> list[PlanOut]:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    outline = await get_project_outline(db, user_id, project_id, outline_id)
    existing = await _outline_plans(db, outline_id)
    by_id = {p.id: p for p in existing}
    submitted: set[int] = set()
    for item in data.plans:
        if item.id is None:
            continue
        if item.id not in by_id:
            raise BadRequestError("细纲不存在或不属于该大纲")
        if item.id in submitted:
            raise BadRequestError("同一条细纲不能重复提交")
        submitted.add(item.id)
    await check_characters(db, project_id, [c for item in data.plans for c in item.character_ids])

    removed = [p for p in existing if p.id not in submitted]
    removed_chapter_ids = [p.chapter_id for p in removed if p.chapter_id is not None]
    removed_chapters = (
        list(await db.scalars(select(Chapter).where(Chapter.id.in_(removed_chapter_ids))))
        if removed_chapter_ids
        else []
    )
    for ch in removed_chapters:
        if ch.content.strip():
            raise ConflictError(CONTENT_PLAN_DELETE)
        if ch.status == "generating":
            raise ConflictError("该章节正在生成中，请稍候再试")

    if existing:
        await outline_snapshot_service.save_snapshot(db, project_id, "reexpand", f"重新展开「{outline.title}」前")

    # 删除后大纲是否还有章节：没有时保留它占的那一个章节号（与未建章的大纲一致），最前面那一章删除时不平移
    kept_built = any(by_id[i].chapter_id is not None for i in submitted)
    single = await db.scalar(
        select(Chapter.id).where(
            Chapter.project_id == project_id, Chapter.outline_id == outline_id, Chapter.plan_id.is_(None)
        )
    )
    keep_slot = not kept_built and single is None
    await planning_lifecycle.detach_sources(
        db, project_id, plan_ids=[p.id for p in removed], chapter_ids=removed_chapter_ids
    )
    for p in removed:
        await db.delete(p)
    await db.flush()
    # 空章节随细纲删除：从后往前删，其后的章节号整体 -1
    ordered = sorted(removed_chapters, key=lambda c: (c.chapter_no, c.id), reverse=True)
    await memory_service.on_chapters_deleted(db, [c.id for c in ordered])
    for i, ch in enumerate(ordered):
        no = ch.chapter_no
        await db.delete(ch)
        await db.flush()
        if keep_slot and i == len(ordered) - 1:
            outline.chapter_no = no
            continue
        await shift_chapter_numbers(db, project_id, from_no=no + 1, delta=-1)

    result: list[ChapterPlan] = []
    for seq, item in enumerate(data.plans, start=1):
        values = item.model_dump(include=set(PLAN_EDIT_FIELDS))
        values["character_ids"] = list(dict.fromkeys(values["character_ids"]))
        if item.id is not None:
            plan = by_id[item.id]
            for k, v in values.items():
                setattr(plan, k, v)
        else:
            plan = ChapterPlan(project_id=project_id, outline_id=outline_id, **values)
            db.add(plan)
        plan.seq = seq
        result.append(plan)
        if item.id is not None:
            await planning_lifecycle.sync_plan_cast(db, plan)
    if ordered:
        # 多章删除和平移可能产生中间态；整批完成后只验证并重算一次。
        await db.flush()
        await setting_progression_service.on_story_positions_changed(db, project_id)
    await planning_lifecycle.bump_revision(db, project_id)
    await db.commit()
    for p in result:
        await db.refresh(p)
    return await plan_outs(db, project_id, result)


async def _add_cast(db: AsyncSession, chapter_id: int, character_ids: list[int]) -> None:
    current = set(
        await db.scalars(select(ChapterCharacter.character_id).where(ChapterCharacter.chapter_id == chapter_id))
    )
    chapter = await db.get(Chapter, chapter_id)
    await planning_lifecycle.sync_cast(db, chapter, sorted(current | set(character_ids)))


async def build_chapters(db: AsyncSession, user_id: int, project_id: int, outline_id: int) -> PlanBuildResult:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    outline = await get_project_outline(db, user_id, project_id, outline_id)
    plans = await _outline_plans(db, outline_id)
    if not plans:
        raise BadRequestError("该大纲还没有细纲，请先展开")
    pending = [p for p in plans if p.chapter_id is None]
    if not pending:
        raise BadRequestError("细纲都已建章")
    valid = await project_character_ids(db, project_id)
    built_ids = [p.chapter_id for p in plans if p.chapter_id is not None]
    built_nos = list(await db.scalars(select(Chapter.chapter_no).where(Chapter.id.in_(built_ids)))) if built_ids else []

    touched: list[Chapter] = []
    single = None
    if not built_ids:
        single = await db.scalar(
            select(Chapter)
            .where(Chapter.project_id == project_id, Chapter.outline_id == outline_id, Chapter.plan_id.is_(None))
            .order_by(Chapter.chapter_no, Chapter.id)
            .limit(1)
        )
    if single is not None:
        await planning_lifecycle.preflight_build_binding(db, project_id, single.id, pending[0].id)
        first = pending.pop(0)
        single.plan_id = first.id
        first.chapter_id = single.id
        await _add_cast(db, single.id, [c for c in first.character_ids or [] if c in valid])
        await planning_lifecycle.attach_built_plan(db, single, first)
        touched.append(single)
        insert_at = single.chapter_no + 1
    elif built_nos:
        insert_at = max(built_nos) + 1
    else:
        insert_at = outline.chapter_no

    if pending:
        occupied = await db.scalar(
            select(Chapter.id).where(Chapter.project_id == project_id, Chapter.chapter_no == insert_at).limit(1)
        )
        if single is None and not built_nos and occupied is None:
            # 还没有章节的大纲本身占着 1 个章节号（且没有别的章节占用）：第一章用掉这个号，只需为其余章节腾位置
            await shift_chapter_numbers(db, project_id, from_no=insert_at + 1, delta=len(pending) - 1)
        else:
            await shift_chapter_numbers(db, project_id, from_no=insert_at, delta=len(pending))
        created = []
        for i, plan in enumerate(pending):
            chapter = Chapter(
                project_id=project_id,
                outline_id=outline_id,
                plan_id=plan.id,
                chapter_no=insert_at + i,
                title=plan.title,
                content="",
                status="draft",
                word_count=0,
            )
            db.add(chapter)
            created.append((chapter, plan))
        await db.flush()
        for chapter, plan in created:
            plan.chapter_id = chapter.id
            await _add_cast(db, chapter.id, [c for c in plan.character_ids or [] if c in valid])
            await planning_lifecycle.attach_built_plan(db, chapter, plan)
            touched.append(chapter)
    await db.flush()

    first_no = await db.scalar(
        select(func.min(Chapter.chapter_no)).where(
            Chapter.project_id == project_id,
            Chapter.plan_id.in_(select(ChapterPlan.id).where(ChapterPlan.outline_id == outline_id)),
        )
    )
    if first_no is not None:
        outline.chapter_no = first_no
    if pending:
        # 建章时的全部平移完成后，再统一刷新按章节排序的最新投影。
        await db.flush()
        await setting_progression_service.on_story_positions_changed(db, project_id)
    await planning_lifecycle.bump_revision(db, project_id)
    await db.commit()
    for c in touched:
        await db.refresh(c)
    all_plans = await _outline_plans(db, outline_id)
    for p in all_plans:
        await db.refresh(p)
    return PlanBuildResult(
        chapters=[ChapterBrief.model_validate(c) for c in sorted(touched, key=lambda c: (c.chapter_no, c.id))],
        plans=await plan_outs(db, project_id, all_plans),
    )
