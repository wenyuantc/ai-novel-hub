"""职业等级体系：职业与阶段 CRUD、角色挂接（apply_assignment）与挂接历史。

- 阶段整体同步（sync_stages）：带 id 的更新、不带 id 的新建、缺失的删除（被角色引用 → 409），
  提交顺序即新的 position（从 1 连续）。阶段 id 稳定，重排后角色仍处于原来那一阶。
- apply_assignment 是表单、向导与卡片的挂接写入入口：有变化时写剧情事实并追加审计，不 commit。
  分析写回使用统一事件服务并单独追加审计，避免生成重复事件。
- 每个角色最多 1 个主职业、3 个副职业；挂接的 role 恒等于职业的 kind（主修只能挂为主职业，副修只能挂为副职业）。
"""

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Career, CareerHistory, CareerStage, Chapter, Character, CharacterCareer
from app.models.entities import CAREER_KINDS, CAREER_SOURCES
from app.models.progressions import SettingProgression, SettingTimeline
from app.schemas.career import (
    AssignmentCreate,
    AssignmentOut,
    AssignmentUpdate,
    CareerBatchCreate,
    CareerCreate,
    CareerHistoryOut,
    CareerHolderOut,
    CareerOut,
    CareerUpdate,
    StageIn,
    StageOut,
)
from app.schemas.progression import BaselineUpdate, ProgressionCreate, StoryPosition
from app.schemas.setting_fields import EntityRef
from app.services import (
    career_progression_service,
    setting_ai_visibility,
    setting_catalog_service,
    setting_field_service,
    setting_progression_service,
    story_service,
)
from app.services.project_service import get_manageable_project

if TYPE_CHECKING:
    from app.services.setting_ai_context import AiScope

MAX_MAIN_CAREERS = 1
MAX_SUB_CAREERS = 3
KIND_LABELS = {"main": "主修", "sub": "副修"}

NAME_CONFLICT = "同名职业已存在"
STAGE_IN_USE = "该阶段仍有角色处于其中，无法删除"
STAGES_REQUIRED = "职业至少需要 1 个阶段"
STAGE_NAME_DUP = "同一职业内阶段名称不能重复"
STAGE_NAME_EMPTY = "阶段名称不能为空"
MAIN_LIMIT = "该角色已有主职业"
SUB_LIMIT = f"副职业最多 {MAX_SUB_CAREERS} 个"
ALREADY_ASSIGNED = "该角色已挂该职业"
ROLE_MISMATCH = {"main": "主修职业只能作为主职业", "sub": "副修职业只能作为副职业"}
STAGE_NOT_IN_CAREER = "阶段不属于该职业"
CAREER_NOT_IN_PROJECT = "职业不存在或不属于该项目"


def stage_label(stage: CareerStage) -> str:
    return f"第{stage.position}阶·{stage.name}"


def ladder_line(career: Career, stages: list[CareerStage]) -> str:
    """「剑修（主修）：练气 → 筑基 → 金丹」，用于提示词。"""
    return f"{career.name}（{KIND_LABELS.get(career.kind, career.kind)}）：" + " → ".join(s.name for s in stages)


# ---------------- 查询辅助 ----------------


async def stages_of(db: AsyncSession, career_ids: list[int]) -> dict[int, list[CareerStage]]:
    """按 position 排序的阶段，键为职业 id（没有阶段的职业也有空列表）。"""
    out: dict[int, list[CareerStage]] = {cid: [] for cid in career_ids}
    if not career_ids:
        return out
    rows = await db.scalars(
        select(CareerStage)
        .where(CareerStage.career_id.in_(career_ids))
        .order_by(CareerStage.career_id, CareerStage.position, CareerStage.id)
    )
    for s in rows:
        out.setdefault(s.career_id, []).append(s)
    return out


async def list_project_careers(db: AsyncSession, project_id: int) -> list[Career]:
    rows = await db.scalars(select(Career).where(Career.project_id == project_id).order_by(Career.sort, Career.id))
    return list(rows)


async def assignments_of(db: AsyncSession, character_ids: list[int]) -> list[tuple[CharacterCareer, Career]]:
    """角色的挂接（上下文组装用，一次查询）：主职业在前，再按职业排序。"""
    if not character_ids:
        return []
    rows = await db.execute(
        select(CharacterCareer, Career)
        .join(Career, Career.id == CharacterCareer.career_id)
        .where(CharacterCareer.character_id.in_(character_ids))
        .order_by(CharacterCareer.role, Career.sort, Career.id)
    )
    return [(a, c) for a, c in rows.all()]


async def _holder_counts(db: AsyncSession, career_ids: list[int]) -> dict[int, int]:
    if not career_ids:
        return {}
    rows = await db.execute(
        select(CharacterCareer.career_id, func.count(CharacterCareer.id))
        .where(CharacterCareer.career_id.in_(career_ids))
        .group_by(CharacterCareer.career_id)
    )
    return {cid: int(n) for cid, n in rows.all()}


async def career_outs(db: AsyncSession, careers: list[Career]) -> list[CareerOut]:
    ids = [c.id for c in careers]
    stages = await stages_of(db, ids)
    counts = await _holder_counts(db, ids)
    return [
        CareerOut(
            id=c.id,
            project_id=c.project_id,
            name=c.name,
            kind=c.kind,
            description=c.description,
            requirements=c.requirements,
            abilities=c.abilities,
            rules=c.rules,
            sort=c.sort,
            stages=[StageOut.model_validate(s) for s in stages.get(c.id, [])],
            holder_count=counts.get(c.id, 0),
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in careers
    ]


async def _refreshed_outs(db: AsyncSession, careers: list[Career]) -> list[CareerOut]:
    for c in careers:
        await db.refresh(c)
    return await career_outs(db, careers)


async def get_owned_career(db: AsyncSession, user_id: int, project_id: int, career_id: int) -> Career:
    await get_manageable_project(db, user_id, project_id)
    career = await db.scalar(select(Career).where(Career.id == career_id, Career.project_id == project_id))
    if career is None:
        raise NotFoundError("职业不存在")
    return career


async def get_project_career(db: AsyncSession, project_id: int, career_id: int) -> Career:
    """请求体中的职业 id：须属于本项目，否则 400。"""
    career = await db.scalar(select(Career).where(Career.id == career_id, Career.project_id == project_id))
    if career is None:
        raise BadRequestError(CAREER_NOT_IN_PROJECT)
    return career


async def _commit(db: AsyncSession, conflict: str) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(conflict) from exc


# ---------------- 阶段 ----------------


async def sync_stages(db: AsyncSession, career: Career, items: list[StageIn]) -> list[CareerStage]:
    """整体同步阶段（career 须已 flush）。不 commit。"""
    if not items:
        raise BadRequestError(STAGES_REQUIRED)
    names = [i.name.strip()[:50] for i in items]
    if any(not n for n in names):
        raise BadRequestError(STAGE_NAME_EMPTY)
    if len(set(names)) != len(names):
        raise BadRequestError(STAGE_NAME_DUP)
    existing = {s.id: s for s in (await stages_of(db, [career.id]))[career.id]}
    ids = [i.id for i in items if i.id is not None]
    if len(set(ids)) != len(ids) or any(i not in existing for i in ids):
        raise BadRequestError(STAGE_NOT_IN_CAREER)
    removed = [s for sid, s in existing.items() if sid not in ids]
    if removed:
        in_use = await db.scalar(
            select(func.count(CharacterCareer.id)).where(CharacterCareer.stage_id.in_([s.id for s in removed]))
        )
        if in_use:
            raise ConflictError(STAGE_IN_USE)
        if set(s.id for s in removed) & await career_progression_service.protected_stage_ids(db, career.id):
            raise ConflictError("该阶段仍被历史设定引用，无法删除")
        for s in removed:
            await db.delete(s)
        await db.flush()
    result: list[CareerStage] = []
    for pos, (item, name) in enumerate(zip(items, names, strict=True), start=1):
        if item.id is None:
            stage = CareerStage(career_id=career.id, position=pos, name=name, description=item.description)
            db.add(stage)
        else:
            stage = existing[item.id]
            stage.position, stage.name, stage.description = pos, name, item.description
        result.append(stage)
    await db.flush()
    return result


async def append_stage(db: AsyncSession, career: Career, name: str, description: str = "") -> CareerStage:
    """追加到阶梯末尾（卡片导入补齐阶段）。不 commit。"""
    max_pos = await db.scalar(select(func.max(CareerStage.position)).where(CareerStage.career_id == career.id))
    stage = CareerStage(career_id=career.id, position=(max_pos or 0) + 1, name=name[:50], description=description)
    db.add(stage)
    await db.flush()
    return stage


# ---------------- 职业 ----------------


async def _name_taken(db: AsyncSession, project_id: int, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(Career.id).where(Career.project_id == project_id, Career.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Career.id != exclude_id)
    return await db.scalar(stmt) is not None


async def new_career(db: AsyncSession, project_id: int, data: CareerCreate) -> Career:
    """校验名称后新建职业与阶段并 flush（不 commit）。"""
    name = data.name.strip()
    if not name:
        raise BadRequestError("职业名称不能为空")
    if await _name_taken(db, project_id, name):
        raise ConflictError(NAME_CONFLICT)
    sort = data.sort if data.sort is not None else await story_service.next_sort(db, Career, project_id)
    career = Career(
        project_id=project_id,
        name=name,
        kind=data.kind,
        description=data.description,
        requirements=data.requirements,
        abilities=data.abilities,
        rules=data.rules,
        sort=sort,
    )
    db.add(career)
    await db.flush()
    await sync_stages(db, career, data.stages)
    await setting_catalog_service.ensure_entry_timeline(db, project_id, "career", career.id)
    return career


async def list_careers(db: AsyncSession, user_id: int, project_id: int) -> list[CareerOut]:
    await get_manageable_project(db, user_id, project_id)
    return await career_outs(db, await list_project_careers(db, project_id))


async def get_career(db: AsyncSession, user_id: int, project_id: int, career_id: int) -> CareerOut:
    return (await career_outs(db, [await get_owned_career(db, user_id, project_id, career_id)]))[0]


async def create_career(db: AsyncSession, user_id: int, project_id: int, data: CareerCreate) -> CareerOut:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    career = await new_career(db, project_id, data)
    await _commit(db, NAME_CONFLICT)
    return (await _refreshed_outs(db, [career]))[0]


async def create_careers(db: AsyncSession, user_id: int, project_id: int, data: CareerBatchCreate) -> list[CareerOut]:
    """批量新建（AI 生成预览确认后保存），单事务：任一条失败则全部不写入。"""
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    careers = [await new_career(db, project_id, item) for item in data.careers]
    await _commit(db, NAME_CONFLICT)
    return await _refreshed_outs(db, careers)


async def update_career(
    db: AsyncSession, user_id: int, project_id: int, career_id: int, data: CareerUpdate
) -> CareerOut:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    career = await get_owned_career(db, user_id, project_id, career_id)
    if data.name is not None:
        name = data.name.strip()
        if not name:
            raise BadRequestError("职业名称不能为空")
        if await _name_taken(db, project_id, name, exclude_id=career.id):
            raise ConflictError(NAME_CONFLICT)
        career.name = name
    for f in ("description", "requirements", "abilities", "rules", "sort"):
        v = getattr(data, f)
        if v is not None:
            setattr(career, f, v)
    if data.stages is not None:
        await sync_stages(db, career, data.stages)
    if data.kind is not None and data.kind != career.kind:
        # 先补齐旧挂接主体，再检查该职业曾经出现过的所有角色及其全部区间。
        old_kind = career.kind
        holders = list(
            (
                await db.scalars(
                    select(Character)
                    .join(CharacterCareer, CharacterCareer.character_id == Character.id)
                    .where(CharacterCareer.career_id == career.id)
                    .order_by(Character.id)
                )
            ).all()
        )
        for character in holders:
            await career_progression_service.get_or_create_career_timeline(db, character, career)
        timelines = list(
            (
                await db.scalars(
                    select(SettingTimeline)
                    .where(
                        SettingTimeline.project_id == project_id,
                        SettingTimeline.subject_kind == "career",
                        SettingTimeline.career_id == career.id,
                    )
                    .order_by(SettingTimeline.id)
                )
            ).all()
        )
        character_ids = {timeline.character_id for timeline in timelines if timeline.character_id is not None}
        await career_progression_service.validate_career_intervals(
            db, project_id, character_ids, kind_overrides={career.id: data.kind}
        )
        # 修改定义是对职业类别的全局修正，必须同步基础和保留的存在事件，
        # 而不是在当前章节伪造一条剧情变化。
        for timeline in timelines:
            await setting_progression_service._claim_revision(db, timeline, timeline.revision)
            if timeline.baseline.get("exists") is True:
                baseline = deepcopy(timeline.baseline)
                baseline["role"] = data.kind
                timeline.baseline = baseline
            await db.flush()
        if timelines:
            events = await db.scalars(
                select(SettingProgression).where(
                    SettingProgression.timeline_id.in_([timeline.id for timeline in timelines]),
                    SettingProgression.field_key == "presence",
                )
            )
            for event in events:
                if isinstance(event.value, dict) and event.value.get("exists") is True:
                    value = deepcopy(event.value)
                    value["state"]["role"] = data.kind
                    event.value = value
                    event.revision += 1
        career.kind = data.kind
        await db.flush()
        await career_progression_service.validate_career_intervals(db, project_id, character_ids)
        snapshots = await setting_progression_service.resolve_timelines(db, project_id, timelines)
        await career_progression_service.materialize_career_timelines(db, project_id, timelines, snapshots)
        for character in holders:
            db.add(
                CareerHistory(
                    character_id=character.id,
                    career_id=career.id,
                    career_name=career.name,
                    source="manual",
                    changes={"role": [old_kind, data.kind]},
                    note="",
                )
            )
    await _commit(db, NAME_CONFLICT)
    return (await _refreshed_outs(db, [career]))[0]


async def delete_career(db: AsyncSession, user_id: int, project_id: int, career_id: int) -> None:
    """删除职业：阶段、挂接与挂接历史随之级联删除。"""
    from app.services.planning_lifecycle import bump_revision

    await setting_ai_visibility.lock_author_visibility_write(db, user_id, project_id)
    career = await get_owned_career(db, user_id, project_id, career_id)
    await setting_field_service.cleanup_unreferenced_handles(
        db, project_id, [EntityRef(kind="career", id=career.id)]
    )
    await db.delete(career)
    await bump_revision(db, project_id)
    await db.commit()


async def list_holders(db: AsyncSession, user_id: int, project_id: int, career_id: int) -> list[CareerHolderOut]:
    career = await get_owned_career(db, user_id, project_id, career_id)
    total = len((await stages_of(db, [career.id]))[career.id])
    rows = await db.execute(
        select(CharacterCareer, Character.name, CareerStage)
        .join(Character, Character.id == CharacterCareer.character_id)
        .join(CareerStage, CareerStage.id == CharacterCareer.stage_id)
        .where(CharacterCareer.career_id == career.id)
        .order_by(CareerStage.position.desc(), Character.sort, Character.id)
    )
    return [
        CareerHolderOut(
            id=a.id,
            character_id=a.character_id,
            character_name=name,
            role=a.role,
            stage_id=s.id,
            stage_name=s.name,
            stage_position=s.position,
            stage_total=total,
            note=a.note,
        )
        for a, name, s in rows.all()
    ]


# ---------------- 挂接写入入口 ----------------


async def _story_anchor(
    db: AsyncSession,
    project_id: int,
    chapter_id: int | None,
    chapter_no: int | None,
    anchor_kind: Literal["chapter", "planned"] | None,
) -> tuple[dict[str, Any] | None, StoryPosition | None, int | None]:
    """显式指定锚点类型；旧章节号仅在当前项目唯一匹配时自动绑定。"""
    if anchor_kind not in (None, "chapter", "planned"):
        raise BadRequestError("章节锚点类型不合法")
    if anchor_kind == "chapter" and (chapter_id is None or chapter_no is not None):
        raise BadRequestError("已有章节锚点须指定章节 id")
    if anchor_kind == "planned" and (chapter_no is None or chapter_id is not None):
        raise BadRequestError("计划章节锚点须指定章节号")
    if chapter_id is not None and chapter_no is not None:
        raise BadRequestError("不能同时指定章节 id 和章节号")
    if chapter_id is not None:
        chapter = await db.scalar(select(Chapter).where(Chapter.project_id == project_id, Chapter.id == chapter_id))
        if chapter is None:
            raise BadRequestError("章节不存在或不属于该作品")
        return (
            {"anchor_kind": "chapter", "chapter_id": chapter.id, "chapter_no": None},
            StoryPosition(chapter_id=chapter.id, chapter_no=chapter.chapter_no),
            chapter.chapter_no,
        )
    if chapter_no is None:
        return None, None, None
    if type(chapter_no) is not int or chapter_no < 1 or chapter_no > 2_147_483_647:
        raise BadRequestError("章节号不合法")
    if anchor_kind == "planned":
        return (
            {"anchor_kind": "planned", "chapter_id": None, "chapter_no": chapter_no},
            StoryPosition(chapter_no=chapter_no),
            chapter_no,
        )
    chapters = list(
        (
            await db.scalars(
                select(Chapter).where(Chapter.project_id == project_id, Chapter.chapter_no == chapter_no).limit(2)
            )
        ).all()
    )
    if len(chapters) > 1:
        raise ConflictError("该章节号对应多个章节，请指定章节 id")
    if chapters:
        chapter = chapters[0]
        return (
            {"anchor_kind": "chapter", "chapter_id": chapter.id, "chapter_no": None},
            StoryPosition(chapter_id=chapter.id, chapter_no=chapter_no),
            chapter_no,
        )
    return (
        {"anchor_kind": "planned", "chapter_id": None, "chapter_no": chapter_no},
        StoryPosition(chapter_no=chapter_no),
        chapter_no,
    )


async def _protect_ambiguous_changes(
    db: AsyncSession,
    timeline: SettingTimeline,
    current: CharacterCareer | None,
    *,
    stage_id: int | None,
    note: str | None,
    remove: bool,
) -> None:
    """旧无章请求只修改尚无剧情变化的字段，避免把最新值写回基线。"""
    fields: set[str] = set()
    if remove or current is None:
        fields.add("presence")
    else:
        if stage_id is not None and stage_id != current.stage_id:
            fields.add("stage_id")
        if note is not None and note.strip()[:200] != current.note:
            fields.add("note")
    if not fields:
        return
    stmt = select(SettingProgression.id).where(
        SettingProgression.timeline_id == timeline.id, SettingProgression.active.is_(True)
    )
    # 存在性变化会影响全部字段；删除/新挂也必须检查全部已记录剧情。
    if "presence" not in fields:
        stmt = stmt.where(SettingProgression.field_key.in_(fields | {"presence"}))
    if await db.scalar(stmt.limit(1)) is not None:
        raise ConflictError(
            "该职业已有剧情变化，请选择修改基础设定或指定生效章节",
            extra={"code": "progression_intent_required"},
        )


async def apply_assignment(
    db: AsyncSession,
    character: Character,
    career: Career,
    *,
    stage_id: int | None = None,
    note: str | None = None,
    role: str | None = None,
    remove: bool = False,
    source: str,
    chapter_no: int | None = None,
    history_note: str = "",
    chapter_id: int | None = None,
    anchor_kind: Literal["chapter", "planned"] | None = None,
    boundary: Literal["start", "end"] = "start",
    edit_mode: Literal["baseline", "progression"] | None = None,
    expected_revision: int | None = None,
) -> CareerHistory | None:
    """唯一挂接写入口。基础/剧情事实、区间检查、最新投影和审计在一个保存点内完成。"""
    if source not in CAREER_SOURCES:
        raise ValueError(f"未知的职业挂接来源：{source}")
    if career.project_id != character.project_id:
        raise BadRequestError(CAREER_NOT_IN_PROJECT)
    if career.kind not in CAREER_KINDS:
        raise ValueError(f"未知的职业类别：{career.kind}")
    if role is not None and role != career.kind:
        raise BadRequestError(ROLE_MISMATCH[career.kind])
    if boundary not in ("start", "end"):
        raise BadRequestError("章节边界不合法")
    if edit_mode is not None and expected_revision is None:
        raise BadRequestError("明确修改基础或剧情变化时请提供当前版本")
    anchor, position, history_chapter = await _story_anchor(
        db, character.project_id, chapter_id, chapter_no, anchor_kind
    )
    if edit_mode == "baseline" and anchor is not None:
        raise BadRequestError("基础设定不能指定生效章节")
    if edit_mode == "progression" and anchor is None:
        raise BadRequestError("剧情变化必须指定生效章节")
    if expected_revision is not None and expected_revision < 0:
        raise BadRequestError("设定版本不合法")
    if remove and expected_revision == 0:
        raise BadRequestError("卸下职业时请提供当前版本")

    async with db.begin_nested():
        current = await db.scalar(
            select(CharacterCareer).where(
                CharacterCareer.character_id == character.id, CharacterCareer.career_id == career.id
            )
        )
        missing_legacy_removal = remove and current is None and anchor is None and edit_mode is None
        unchanged_legacy_form = (
            not remove
            and current is not None
            and anchor is None
            and edit_mode is None
            and (stage_id is None or stage_id == current.stage_id)
            and (note is None or note.strip()[:200] == current.note)
        )
        if (missing_legacy_removal or unchanged_legacy_form) and expected_revision is None:
            return None
        stages = (await stages_of(db, [career.id]))[career.id]
        by_id = {s.id: s for s in stages}
        if stage_id is not None and stage_id not in by_id:
            raise BadRequestError(STAGE_NOT_IN_CAREER)
        if not remove and not stages:
            raise BadRequestError(STAGES_REQUIRED)

        if expected_revision is not None:
            existing = await db.scalar(
                select(SettingTimeline).where(
                    SettingTimeline.project_id == character.project_id,
                    SettingTimeline.subject_kind == "career",
                    SettingTimeline.character_id == character.id,
                    SettingTimeline.career_id == career.id,
                )
            )
            if (expected_revision == 0 and existing is not None) or (expected_revision > 0 and existing is None):
                raise ConflictError("设定已被其他操作修改，请刷新后重试", extra={"code": "stale_revision"})
        timeline = await career_progression_service.get_or_create_career_timeline(db, character, career)
        if expected_revision is not None and expected_revision > 0 and expected_revision != timeline.revision:
            raise ConflictError("设定已被其他操作修改，请刷新后重试", extra={"code": "stale_revision"})
        if missing_legacy_removal or unchanged_legacy_form:
            return None
        if anchor is None and edit_mode is None:
            await _protect_ambiguous_changes(db, timeline, current, stage_id=stage_id, note=note, remove=remove)

        if position is None:
            before = deepcopy(timeline.baseline)
        else:
            position.boundary = boundary
            before = (
                await setting_progression_service.resolve_timelines(db, character.project_id, [timeline], position)
            )[timeline.id].fields
        before_exists = before.get("exists") is True
        if remove:
            if not before_exists:
                return None
            after: dict[str, Any] = {"exists": False}
        elif not before_exists:
            after = {
                "exists": True,
                "role": career.kind,
                "stage_id": stage_id if stage_id is not None else stages[0].id,
                "note": (note or "").strip()[:200],
            }
        else:
            after = deepcopy(before)
            if stage_id is not None:
                after["stage_id"] = stage_id
            if note is not None:
                after["note"] = note.strip()[:200]
        if before == after:
            return None

        def label(sid: int | None) -> str | None:
            stage = by_id.get(sid) if sid is not None else None
            return stage_label(stage) if stage is not None else None

        changes: dict[str, list[Any]] = {}
        if not before_exists and after["exists"]:
            changes = {
                "role": [None, career.kind],
                "stage": [None, label(after["stage_id"])],
                "note": [None, after["note"]],
            }
        elif before_exists and not after["exists"]:
            changes["removed"] = [career.name, None]
        else:
            if before.get("stage_id") != after.get("stage_id"):
                changes["stage"] = [label(before.get("stage_id")), label(after.get("stage_id"))]
            if before.get("note") != after.get("note"):
                changes["note"] = [before.get("note"), after.get("note")]
        if not changes:
            return None

        if anchor is None:
            base_changes = (
                after
                if before_exists != after["exists"]
                else {field: after[field] for field in ("stage_id", "note") if before.get(field) != after.get(field)}
            )
            await setting_progression_service.apply_baseline(
                db,
                timeline,
                BaselineUpdate(expected_revision=timeline.revision, changes=base_changes),
                materialize=False,
            )
        else:
            payloads: list[tuple[str, Any]] = []
            if before_exists != after["exists"]:
                state = {key: after[key] for key in ("role", "stage_id", "note")} if after["exists"] else None
                payloads.append(("presence", {"exists": True, "state": state} if state else {"exists": False}))
            else:
                payloads.extend(
                    (field, after[field]) for field in ("stage_id", "note") if before.get(field) != after.get(field)
                )
            for field, value in payloads:
                event_data = ProgressionCreate(
                    expected_revision=timeline.revision,
                    field_key=field,
                    operation="replace",
                    value=value,
                    boundary=boundary,
                    note=(history_note or "")[:200],
                    **anchor,
                )
                await setting_progression_service.apply_progression(
                    db,
                    timeline,
                    event_data,
                    source="analysis" if source == "analysis" else "manual",
                    protection="automatic" if source == "analysis" else "manual",
                    materialize=False,
                )

        await career_progression_service.validate_career_intervals(db, character.project_id, [character.id])
        snapshots = await setting_progression_service.resolve_timelines(db, character.project_id, [timeline])
        await career_progression_service.materialize_career_timelines(db, character.project_id, [timeline], snapshots)
        history = CareerHistory(
            character_id=character.id,
            career_id=career.id,
            career_name=career.name,
            source=source,
            chapter_no=history_chapter,
            changes=changes,
            note=(history_note or "")[:200],
        )
        db.add(history)
        await db.flush()
        return history


async def assign_new(
    db: AsyncSession,
    character: Character,
    career: Career,
    *,
    stage_id: int | None = None,
    note: str = "",
    role: str | None = None,
    source: str,
    chapter_no: int | None = None,
    history_note: str = "",
    chapter_id: int | None = None,
    anchor_kind: Literal["chapter", "planned"] | None = None,
    boundary: Literal["start", "end"] = "start",
    edit_mode: Literal["baseline", "progression"] | None = None,
    expected_revision: int | None = None,
) -> CharacterCareer:
    """新挂（已挂该职业 → 409），经 apply_assignment 写入。不 commit。"""
    exists = await db.scalar(
        select(CharacterCareer.id).where(
            CharacterCareer.character_id == character.id, CharacterCareer.career_id == career.id
        )
    )
    if exists is not None:
        if expected_revision == 0:
            raise ConflictError("设定已被其他操作修改，请刷新后重试", extra={"code": "stale_revision"})
        raise ConflictError(ALREADY_ASSIGNED)
    async with db.begin_nested():
        await apply_assignment(
            db,
            character,
            career,
            stage_id=stage_id,
            note=note,
            role=role,
            source=source,
            chapter_no=chapter_no,
            history_note=history_note,
            chapter_id=chapter_id,
            anchor_kind=anchor_kind,
            boundary=boundary,
            edit_mode=edit_mode,
            expected_revision=expected_revision,
        )
        created = await db.scalar(
            select(CharacterCareer).where(
                CharacterCareer.character_id == character.id, CharacterCareer.career_id == career.id
            )
        )
        if created is None:
            raise ConflictError(
                "该职业在最新剧情中已卸下，请到职业时间线新增变化或校准初始状态",
                extra={"code": "career_history_requires_timeline"},
            )
    return created


# ---------------- 挂接 HTTP 入口 ----------------


async def assignment_outs(db: AsyncSession, rows: list[CharacterCareer]) -> list[AssignmentOut]:
    if not rows:
        return []
    career_ids = list({a.career_id for a in rows})
    careers = {c.id: c for c in await db.scalars(select(Career).where(Career.id.in_(career_ids)))}
    stages = await stages_of(db, career_ids)
    stage_by_id = {s.id: s for ss in stages.values() for s in ss}
    outs = []
    for a in rows:
        c, s = careers[a.career_id], stage_by_id[a.stage_id]
        outs.append(
            AssignmentOut(
                id=a.id,
                character_id=a.character_id,
                career_id=c.id,
                career_name=c.name,
                career_kind=c.kind,
                role=a.role,
                stage_id=s.id,
                stage_name=s.name,
                stage_position=s.position,
                stage_total=len(stages[c.id]),
                note=a.note,
                updated_at=a.updated_at,
            )
        )
    return outs


async def character_assignments(db: AsyncSession, character_id: int) -> list[CharacterCareer]:
    rows = await db.scalars(
        select(CharacterCareer)
        .join(Career, Career.id == CharacterCareer.career_id)
        .where(CharacterCareer.character_id == character_id)
        .order_by(CharacterCareer.role, Career.sort, Career.id)
    )
    return list(rows)


async def _refreshed_assignments(db: AsyncSession, rows: list[CharacterCareer]) -> list[AssignmentOut]:
    for a in rows:
        await db.refresh(a)
    return await assignment_outs(db, rows)


async def _owned_character(db: AsyncSession, user_id: int, project_id: int, character_id: int) -> Character:
    return await story_service.get_manageable(db, story_service.CHARACTERS, user_id, project_id, character_id)


async def _owned_assignment(
    db: AsyncSession, user_id: int, project_id: int, character_id: int, assignment_id: int
) -> tuple[Character, CharacterCareer, Career]:
    character = await _owned_character(db, user_id, project_id, character_id)
    row = (
        await db.execute(
            select(CharacterCareer, Career)
            .join(Career, Career.id == CharacterCareer.career_id)
            .where(CharacterCareer.id == assignment_id, CharacterCareer.character_id == character.id)
        )
    ).first()
    if row is None:
        raise NotFoundError("职业挂接不存在")
    return character, row[0], row[1]


async def list_assignments(db: AsyncSession, user_id: int, project_id: int, character_id: int) -> list[AssignmentOut]:
    character = await _owned_character(db, user_id, project_id, character_id)
    return await assignment_outs(db, await character_assignments(db, character.id))


async def list_project_assignments(db: AsyncSession, user_id: int, project_id: int) -> list[AssignmentOut]:
    """项目内全部角色的挂接（角色卡片汇总用，一次请求）：按角色、主职业在前。"""
    await get_manageable_project(db, user_id, project_id)
    rows = await db.scalars(
        select(CharacterCareer)
        .join(Career, Career.id == CharacterCareer.career_id)
        .where(Career.project_id == project_id)
        .order_by(CharacterCareer.character_id, CharacterCareer.role, Career.sort, Career.id)
    )
    return await assignment_outs(db, list(rows))


async def assign(
    db: AsyncSession, user_id: int, project_id: int, character_id: int, data: AssignmentCreate
) -> AssignmentOut:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    character = await _owned_character(db, user_id, project_id, character_id)
    career = await get_project_career(db, project_id, data.career_id)
    row = await assign_new(
        db,
        character,
        career,
        stage_id=data.stage_id,
        note=data.note,
        role=data.role,
        source="manual",
        chapter_no=data.chapter_no,
        history_note=data.history_note,
        chapter_id=data.chapter_id,
        anchor_kind=data.anchor_kind,
        boundary=data.boundary,
        edit_mode=data.edit_mode,
        expected_revision=data.expected_revision,
    )
    await _commit(db, ALREADY_ASSIGNED)
    return (await _refreshed_assignments(db, [row]))[0]


async def update_assignment(
    db: AsyncSession, user_id: int, project_id: int, character_id: int, assignment_id: int, data: AssignmentUpdate
) -> AssignmentOut:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    character, row, career = await _owned_assignment(db, user_id, project_id, character_id, assignment_id)
    await apply_assignment(
        db,
        character,
        career,
        stage_id=data.stage_id,
        note=data.note,
        source="manual",
        chapter_no=data.chapter_no,
        history_note=data.history_note,
        chapter_id=data.chapter_id,
        anchor_kind=data.anchor_kind,
        boundary=data.boundary,
        edit_mode=data.edit_mode,
        expected_revision=data.expected_revision,
    )
    await db.commit()
    return (await _refreshed_assignments(db, [row]))[0]


async def remove_assignment(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    character_id: int,
    assignment_id: int,
    *,
    chapter_id: int | None = None,
    chapter_no: int | None = None,
    anchor_kind: Literal["chapter", "planned"] | None = None,
    boundary: Literal["start", "end"] = "start",
    edit_mode: Literal["baseline", "progression", "delete"] | None = None,
    expected_revision: int | None = None,
) -> None:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    character, current, career = await _owned_assignment(db, user_id, project_id, character_id, assignment_id)
    if edit_mode == "delete":
        if chapter_id is not None or chapter_no is not None or anchor_kind is not None:
            raise BadRequestError("删除职业资料不能指定生效章节")
        if expected_revision is None or expected_revision < 1:
            raise BadRequestError("删除职业资料时请提供当前版本")
        timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project_id,
                SettingTimeline.subject_kind == "career",
                SettingTimeline.character_id == character.id,
                SettingTimeline.career_id == career.id,
            )
        )
        if timeline is None or timeline.revision != expected_revision:
            raise ConflictError("设定已被其他操作修改，请刷新后重试", extra={"code": "stale_revision"})
        await setting_progression_service._claim_revision(db, timeline, expected_revision)
        await db.delete(timeline)
        await db.delete(current)
        db.add(
            CareerHistory(
                character_id=character.id,
                career_id=career.id,
                career_name=career.name,
                source="manual",
                changes={"removed": [career.name, None]},
            )
        )
        await db.flush()
    else:
        await apply_assignment(
            db,
            character,
            career,
            remove=True,
            source="manual",
            chapter_no=chapter_no,
            chapter_id=chapter_id,
            anchor_kind=anchor_kind,
            boundary=boundary,
            edit_mode=edit_mode,
            expected_revision=expected_revision,
        )
    await db.commit()


async def list_history(db: AsyncSession, user_id: int, project_id: int, character_id: int) -> list[CareerHistoryOut]:
    character = await _owned_character(db, user_id, project_id, character_id)
    rows = await db.scalars(
        select(CareerHistory).where(CareerHistory.character_id == character.id).order_by(CareerHistory.id.desc())
    )
    return [CareerHistoryOut.model_validate(h) for h in rows]


# ---------------- AI 输出规范化（纯函数，career.generate / wizard.careers 共用） ----------------

_KIND_WORDS = (("sub", ("sub", "副", "辅", "secondary")), ("main", ("main", "主", "primary")))


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "；".join(_text(v) for v in value if _text(v))
    if isinstance(value, dict):
        return "；".join(f"{k}：{_text(v)}" for k, v in value.items() if _text(v))
    return str(value).strip()


def normalize_career_kind(value: Any, default: str = "main") -> str:
    v = _text(value).lower()
    if v in CAREER_KINDS:
        return v
    for kind, words in _KIND_WORDS:
        if any(w in v for w in words):
            return kind
    return default


def _career_items(obj: Any) -> list[Any]:
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in ("careers", "items", "professions"):
            if isinstance(obj.get(key), list):
                return obj[key]
        if obj.get("name"):
            return [obj]
    return []


def normalize_career_drafts(obj: Any, *, force_kind: str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """模型输出 → 职业草稿列表（截断到列长度、阶段名去重、职业名去重）与警告。没有阶段的职业被丢弃。"""
    drafts: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for c in _career_items(obj):
        if not isinstance(c, dict):
            continue
        name = _text(c.get("name"))[:100]
        if not name:
            continue
        if name in seen:
            warnings.append(f"重复的职业「{name}」，只保留第一个")
            continue
        raw_stages = c.get("stages") or c.get("levels") or c.get("ranks") or []
        stages: list[dict[str, str]] = []
        names: set[str] = set()
        if isinstance(raw_stages, list):
            for s in raw_stages:
                if isinstance(s, dict):
                    sname, sdesc = _text(s.get("name") or s.get("title"))[:50], _text(s.get("description"))
                else:
                    sname, sdesc = _text(s)[:50], ""
                if sname and sname not in names:
                    names.add(sname)
                    stages.append({"name": sname, "description": sdesc})
        if not stages:
            warnings.append(f"职业「{name}」没有阶段，已忽略")
            continue
        seen.add(name)
        drafts.append(
            {
                "name": name,
                "kind": force_kind or normalize_career_kind(c.get("kind") or c.get("type") or c.get("category")),
                "description": _text(c.get("description")),
                "requirements": _text(c.get("requirements")),
                "abilities": _text(c.get("abilities")),
                "rules": _text(c.get("rules")),
                "stages": stages[:100],
            }
        )
    return drafts, warnings


async def careers_reference(db: AsyncSession, project_id: int, *, scope: "AiScope") -> str:
    """已有职业的名称、类别与阶梯摘要（提示词参考）；没有职业时为空串。"""
    careers = [c for c in await list_project_careers(db, project_id) if scope.includes("career", c.id)]
    if not careers:
        return ""
    stages = await stages_of(db, [c.id for c in careers])
    return "\n".join(
        ladder_line(c, stages[c.id] if scope.field_visible("career", c.id, "stages") else [])
        for c in careers
    )


# 详细参考里每个字段的截断长度：够 AI 看清定位 / 能力 / 规则，又不至于让十几个职业撑爆上下文
DETAIL_FIELD_CHARS = 100


async def careers_detail_reference(db: AsyncSession, project_id: int, *, scope: "AiScope") -> str:
    """生成新职业用的详细参考：阶梯摘要 + 描述 / 特殊能力 / 体系规则（各截断）。

    不含获得条件与阶段描述——对「保持体系一致、避免能力重叠」帮助有限却最占篇幅。
    """
    careers = [c for c in await list_project_careers(db, project_id) if scope.includes("career", c.id)]
    if not careers:
        return ""
    stages = await stages_of(db, [c.id for c in careers])
    blocks: list[str] = []
    for c in careers:
        lines = ["- " + ladder_line(c, stages[c.id] if scope.field_visible("career", c.id, "stages") else [])]
        for key, label in (("description", "描述"), ("abilities", "特殊能力"), ("rules", "体系规则")):
            text = getattr(c, key).strip() if scope.field_visible("career", c.id, key) else ""
            if text:
                clipped = text if len(text) <= DETAIL_FIELD_CHARS else text[:DETAIL_FIELD_CHARS] + "……"
                lines.append(f"  {label}：{clipped}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)
