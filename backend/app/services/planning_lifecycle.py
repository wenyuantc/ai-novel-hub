"""规划与旧故事来源之间的事务内衔接；所有 helper 都不 commit。

调用方持项目写屏障。源删除只解除绑定，永不按重用的整数 ID 猜测重连；
章 cast 是已建章角色的唯一事实，未建章细纲及孤立规划各有明确的接管点。
"""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.models import Chapter, ChapterCharacter, ChapterPlan, Character, Outline, Project
from app.models.planning import (
    PlanningUnit,
    PlanningUnitCareer,
    PlanningUnitOrganization,
    PlanningUnitRelationship,
    PlanningUnitTag,
    PlanningUnitWorld,
    SubplotCrossing,
    SubplotNode,
)

UNIT_ASSOCIATIONS = (
    PlanningUnitTag,
    PlanningUnitWorld,
    PlanningUnitOrganization,
    PlanningUnitCareer,
    PlanningUnitRelationship,
    SubplotNode,
    SubplotCrossing,
)


async def bump_revision(db: AsyncSession, project_id: int) -> None:
    """源结构／旧选角入口的版本失效；新规划 API 的 CAS 自己递增，不重复调用。"""
    # 外层可能需要经版本／审计边界 flush；版本失效不能提前写入那些待保存对象。
    with db.no_autoflush:
        await db.execute(
            update(Project).where(Project.id == project_id)
            .values(planning_revision=Project.planning_revision + 1)
            .execution_options(synchronize_session="fetch")
        )


async def character_ids(db: AsyncSession, chapter_id: int) -> list[int]:
    return list(await db.scalars(
        select(ChapterCharacter.character_id).where(ChapterCharacter.chapter_id == chapter_id)
        .order_by(ChapterCharacter.character_id)
    ))


async def sync_cast(db: AsyncSession, chapter: Chapter, ids: Iterable[int]) -> None:
    """替换章 cast 并同步已绑定细纲；不提交，也不独立改变项目 revision。"""
    wanted = list(dict.fromkeys(ids))
    if wanted:
        found = set(await db.scalars(select(Character.id).where(
            Character.project_id == chapter.project_id, Character.id.in_(wanted)
        )))
        if set(wanted) != found:
            raise BadRequestError("部分角色不存在或不属于该项目")
    await db.execute(delete(ChapterCharacter).where(ChapterCharacter.chapter_id == chapter.id))
    db.add_all(ChapterCharacter(chapter_id=chapter.id, character_id=cid) for cid in wanted)
    if chapter.plan_id is not None:
        plan = await db.scalar(select(ChapterPlan).where(
            ChapterPlan.id == chapter.plan_id, ChapterPlan.project_id == chapter.project_id
        ))
        if plan is not None:
            plan.character_ids = wanted
    await db.flush()


async def sync_plan_cast(db: AsyncSession, plan: ChapterPlan) -> None:
    """细纲的人工角色写入：已建章时实际写章 cast，未建章时本行就是权威。"""
    if plan.chapter_id is None:
        return
    chapter = await db.scalar(select(Chapter).where(
        Chapter.id == plan.chapter_id, Chapter.project_id == plan.project_id,
        Chapter.plan_id == plan.id,
    ))
    if chapter is not None:
        await sync_cast(db, chapter, plan.character_ids or [])


async def _remember(db: AsyncSession, unit: PlanningUnit) -> list[int]:
    chapter = await db.scalar(select(Chapter).where(
        Chapter.id == unit.chapter_id, Chapter.project_id == unit.project_id
    )) if unit.chapter_id is not None else None
    plan = await db.scalar(select(ChapterPlan).where(
        ChapterPlan.id == unit.plan_id, ChapterPlan.project_id == unit.project_id
    )) if unit.plan_id is not None else None
    outline_id = chapter.outline_id if chapter is not None else plan.outline_id if plan is not None else None
    if chapter is not None:
        unit.last_source_label = f"第{chapter.chapter_no}章 {chapter.title}"[:300]
        ids = await character_ids(db, chapter.id)
    elif plan is not None:
        unit.last_source_label = f"细纲{plan.seq} {plan.title}"[:300]
        ids = list(plan.character_ids or [])
    else:
        ids = list(unit.detached_character_ids or [])
    if outline_id is not None:
        outline = await db.scalar(select(Outline).where(
            Outline.id == outline_id, Outline.project_id == unit.project_id
        ))
        if outline is not None:
            unit.last_volume_label = f"第{outline.volume_no}卷 {outline.volume_title}"[:300]
    return ids


async def detach_sources(
    db: AsyncSession, project_id: int, *, chapter_ids: Iterable[int] = (), plan_ids: Iterable[int] = ()
) -> None:
    """在源删除前保存可读位置和角色；SET NULL 仅作为数据库兜底。"""
    removed_chapters, removed_plans = set(chapter_ids), set(plan_ids)
    if not removed_chapters and not removed_plans:
        return
    units = list(await db.scalars(select(PlanningUnit).where(
        PlanningUnit.project_id == project_id, PlanningUnit.retired.is_(False),
        or_(PlanningUnit.chapter_id.in_(removed_chapters), PlanningUnit.plan_id.in_(removed_plans)),
    )))
    # 即使没有持久 unit，删除章仍须把当前 cast 交还给保留的细纲。
    plans = list(await db.scalars(select(ChapterPlan).where(
        ChapterPlan.project_id == project_id, ChapterPlan.chapter_id.in_(removed_chapters),
        ChapterPlan.id.not_in(removed_plans),
    ))) if removed_chapters else []
    for plan in plans:
        plan.character_ids = await character_ids(db, plan.chapter_id)
    for unit in units:
        ids = await _remember(db, unit)
        if unit.chapter_id in removed_chapters:
            unit.chapter_id = None
        if unit.plan_id in removed_plans:
            unit.plan_id = None
        if unit.chapter_id is None and unit.plan_id is None:
            unit.detached_character_ids = ids
    await db.flush()


async def detach_outlines(db: AsyncSession, project_id: int, outline_ids: Iterable[int] | None = None) -> None:
    query = select(ChapterPlan.id).where(ChapterPlan.project_id == project_id)
    if outline_ids is not None:
        query = query.where(ChapterPlan.outline_id.in_(list(outline_ids)))
    await detach_sources(db, project_id, plan_ids=list(await db.scalars(query)))


async def detach_chapter_plan(db: AsyncSession, chapter: Chapter) -> None:
    """章改挂大纲时规划随章，旧细纲不复制一份支线／POV。"""
    unit = await db.scalar(select(PlanningUnit).where(
        PlanningUnit.project_id == chapter.project_id, PlanningUnit.chapter_id == chapter.id,
        PlanningUnit.retired.is_(False),
    ))
    if unit is not None:
        await _remember(db, unit)
        unit.plan_id = None
    if chapter.plan_id is not None:
        plan = await db.scalar(select(ChapterPlan).where(
            ChapterPlan.project_id == chapter.project_id, ChapterPlan.id == chapter.plan_id
        ))
        if plan is not None:
            plan.character_ids = await character_ids(db, chapter.id)
    await db.flush()


async def remove_character_references(db: AsyncSession, project_id: int, ids: Iterable[int]) -> None:
    """JSON 中没有 FK，真删除角色前清理，避免旧 ID 被重用后错误恢复引用。"""
    removed = set(ids)
    if not removed:
        return
    for plan in await db.scalars(select(ChapterPlan).where(ChapterPlan.project_id == project_id)):
        if removed.intersection(plan.character_ids or []):
            plan.character_ids = [cid for cid in plan.character_ids if cid not in removed]
    for unit in await db.scalars(select(PlanningUnit).where(PlanningUnit.project_id == project_id)):
        if removed.intersection(unit.detached_character_ids or []):
            unit.detached_character_ids = [cid for cid in unit.detached_character_ids if cid not in removed]
    await db.flush()


async def has_content(db: AsyncSession, unit: PlanningUnit) -> bool:
    if unit.pov_character_id is not None or unit.detached_character_ids:
        return True
    for model in UNIT_ASSOCIATIONS:
        if await db.scalar(select(model.unit_id).where(model.unit_id == unit.id).limit(1)) is not None:
            return True
    return False


async def _binding_units(
    db: AsyncSession, project_id: int, chapter_id: int, plan_id: int
) -> tuple[PlanningUnit | None, PlanningUnit | None]:
    units = list(await db.scalars(select(PlanningUnit).where(
        PlanningUnit.project_id == project_id, PlanningUnit.retired.is_(False),
        or_(PlanningUnit.chapter_id == chapter_id, PlanningUnit.plan_id == plan_id),
    )))
    return (next((u for u in units if u.chapter_id == chapter_id), None),
            next((u for u in units if u.plan_id == plan_id), None))


async def preflight_build_binding(db: AsyncSession, project_id: int, chapter_id: int, plan_id: int) -> None:
    chapter_unit, plan_unit = await _binding_units(db, project_id, chapter_id, plan_id)
    if chapter_unit is None or plan_unit is None or chapter_unit.id == plan_unit.id:
        return
    if await has_content(db, chapter_unit) and await has_content(db, plan_unit):
        from app.services.planning_service import merge_conflict

        raise await merge_conflict(db, project_id, chapter_unit, plan_unit)


async def attach_built_plan(db: AsyncSession, chapter: Chapter, plan: ChapterPlan) -> None:
    """真实双向建章完成后承接稳定 unit；空单元只退役，不重用旧身份。"""
    await db.flush()
    chapter_unit, plan_unit = await _binding_units(db, chapter.project_id, chapter.id, plan.id)
    if chapter_unit is None and plan_unit is None:
        return
    await preflight_build_binding(db, chapter.project_id, chapter.id, plan.id)
    survivor = chapter_unit or plan_unit
    if chapter_unit is not None and plan_unit is not None and chapter_unit.id != plan_unit.id:
        if await has_content(db, plan_unit):
            survivor, retired = plan_unit, chapter_unit
        else:
            survivor, retired = chapter_unit, plan_unit
        retired.chapter_id = retired.plan_id = None
        retired.retired = True
        retired.binding_epoch += 1
        await db.flush()  # 先释放两个唯一来源 FK。
    survivor.chapter_id, survivor.plan_id = chapter.id, plan.id
    survivor.detached_character_ids = []
    await _remember(db, survivor)
    await db.flush()


async def snapshot_bindings(db: AsyncSession, project_id: int) -> list[dict[str, int]]:
    rows = await db.execute(select(PlanningUnit.id, PlanningUnit.plan_id, PlanningUnit.binding_epoch).where(
        PlanningUnit.project_id == project_id, PlanningUnit.retired.is_(False), PlanningUnit.plan_id.is_not(None),
    ).order_by(PlanningUnit.id))
    return [{"unit_id": uid, "plan_id": pid, "binding_epoch": epoch} for uid, pid, epoch in rows]


async def chapter_anchors(db: AsyncSession, project_id: int) -> dict[int, int]:
    """必须在快照删除／重建来源前取证，不能事后用恢复后的 plan 链接冒充身份。"""
    rows = await db.execute(select(PlanningUnit.id, PlanningUnit.chapter_id).where(
        PlanningUnit.project_id == project_id, PlanningUnit.retired.is_(False), PlanningUnit.chapter_id.is_not(None),
    ))
    return dict(rows.all())


async def restore_bindings(
    db: AsyncSession, project_id: int, bindings: Any, plans: dict[int, ChapterPlan], anchors: dict[int, int]
) -> list[str]:
    warnings: list[str] = []
    if not isinstance(bindings, list):
        if await db.scalar(select(PlanningUnit.id).where(
            PlanningUnit.project_id == project_id, PlanningUnit.retired.is_(False),
            PlanningUnit.chapter_id.is_(None), PlanningUnit.plan_id.is_(None),
        ).limit(1)) is not None:
            warnings.append("旧快照缺少规划绑定，失去来源的规划保留在待重新关联列表")
        return warnings
    seen: set[int] = set()
    for binding in bindings:
        if not isinstance(binding, dict) or type(binding.get("unit_id")) is not int:
            warnings.append("快照中的规划绑定无效，已跳过")
            continue
        unit = await db.scalar(select(PlanningUnit).where(
            PlanningUnit.id == binding["unit_id"], PlanningUnit.project_id == project_id,
        ))
        plan = plans.get(binding.get("plan_id"))
        valid = (unit is not None and plan is not None and not unit.retired
                 and type(binding.get("binding_epoch")) is int and unit.binding_epoch == binding["binding_epoch"]
                 and unit.id not in seen)
        if valid:
            occupied = await db.scalar(select(PlanningUnit.id).where(
                PlanningUnit.plan_id == plan.id, PlanningUnit.id != unit.id,
            ))
            valid = occupied is None
        if valid and plan.chapter_id is not None:
            valid = anchors.get(unit.id) == plan.chapter_id and unit.chapter_id == plan.chapter_id
        elif valid:
            valid = unit.chapter_id is None and unit.plan_id is None
        if not valid:
            warnings.append(f"规划 #{binding['unit_id']} 的位置已变化，保留现有内容，请按需重新关联")
            continue
        unit.plan_id = plan.id
        if plan.chapter_id is not None:
            plan.character_ids = await character_ids(db, plan.chapter_id)
        else:
            # 空数组也是作者的有效选择，不能用快照中的旧角色回填。
            plan.character_ids = list(unit.detached_character_ids or [])
        unit.detached_character_ids = []
        seen.add(unit.id)
        await db.flush()
    return warnings


async def validate_project(db: AsyncSession, project_id: int) -> None:
    """导入／智能体 Core 恢复后的跨表检查；不读取章节正文，也不隐式修正用户数据。"""
    from app.services.project_transfer.planning import validate_planning_tables
    from app.services.project_transfer.registry import PLANNING_TABLES, RULES_BY_TABLE

    fields = {
        "chapters": ("id", "plan_id"), "chapter_plans": ("id", "chapter_id"),
        "characters": ("id",), "world_settings": ("id",), "organizations": ("id",), "careers": ("id",),
        "setting_timelines": ("id", "subject_kind"),
    }
    tables = {}
    for name in (*fields, *PLANNING_TABLES):
        rule = RULES_BY_TABLE[name]
        columns = rule.model.__table__.columns
        selected = [columns[key] for key in fields[name]] if name in fields else list(columns)
        tables[name] = [dict(row) for row in
                        (await db.execute(select(*selected).where(rule.scope(project_id)))).mappings()]
    errors: list[str] = []
    validate_planning_tables(tables, errors.append)
    if errors:
        raise BadRequestError("规划关联数据不一致：" + "；".join(errors[:10]))
