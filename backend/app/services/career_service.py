"""职业等级体系：职业与阶段 CRUD、角色挂接（apply_assignment）与挂接历史。

- 阶段整体同步（sync_stages）：带 id 的更新、不带 id 的新建、缺失的删除（被角色引用 → 409），
  提交顺序即新的 position（从 1 连续）。阶段 id 稳定，重排后角色仍处于原来那一阶。
- apply_assignment 是挂接的唯一写入入口：新挂 / 换阶 / 改备注 / 主副同步 / 卸下都经由它，
  有变化才写一条历史（阶段以「第N阶·名称」记录）；不 commit。接口、向导、卡片导入、AI 生成角色保存，
  以及以后的 analysis-memory 都必须经由它（新挂用 assign_new，已挂时 409）。
- 每个角色最多 1 个主职业、3 个副职业；挂接的 role 恒等于职业的 kind（主修只能挂为主职业，副修只能挂为副职业）。
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Career, CareerHistory, CareerStage, Character, CharacterCareer
from app.models.entities import CAREER_KINDS, CAREER_SOURCES
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
from app.services import story_service
from app.services.project_service import get_owned_project

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
    await get_owned_project(db, user_id, project_id)
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
    return career


async def list_careers(db: AsyncSession, user_id: int, project_id: int) -> list[CareerOut]:
    await get_owned_project(db, user_id, project_id)
    return await career_outs(db, await list_project_careers(db, project_id))


async def get_career(db: AsyncSession, user_id: int, project_id: int, career_id: int) -> CareerOut:
    return (await career_outs(db, [await get_owned_career(db, user_id, project_id, career_id)]))[0]


async def create_career(db: AsyncSession, user_id: int, project_id: int, data: CareerCreate) -> CareerOut:
    await get_owned_project(db, user_id, project_id)
    career = await new_career(db, project_id, data)
    await _commit(db, NAME_CONFLICT)
    return (await _refreshed_outs(db, [career]))[0]


async def create_careers(db: AsyncSession, user_id: int, project_id: int, data: CareerBatchCreate) -> list[CareerOut]:
    """批量新建（AI 生成预览确认后保存），单事务：任一条失败则全部不写入。"""
    await get_owned_project(db, user_id, project_id)
    careers = [await new_career(db, project_id, item) for item in data.careers]
    await _commit(db, NAME_CONFLICT)
    return await _refreshed_outs(db, careers)


async def update_career(
    db: AsyncSession, user_id: int, project_id: int, career_id: int, data: CareerUpdate
) -> CareerOut:
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
        career.kind = data.kind
        await db.flush()
        # 类别变化：持有人的主 / 副同步变化（经 apply_assignment 校验上限并写历史）
        holders = await db.scalars(
            select(Character)
            .join(CharacterCareer, CharacterCareer.character_id == Character.id)
            .where(CharacterCareer.career_id == career.id)
            .order_by(Character.id)
        )
        for character in list(holders):
            await apply_assignment(db, character, career, source="manual")
    await _commit(db, NAME_CONFLICT)
    return (await _refreshed_outs(db, [career]))[0]


async def delete_career(db: AsyncSession, user_id: int, project_id: int, career_id: int) -> None:
    """删除职业：阶段、挂接与挂接历史随之级联删除。"""
    career = await get_owned_career(db, user_id, project_id, career_id)
    await db.delete(career)
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


async def _check_limit(db: AsyncSession, character_id: int, role: str, exclude_id: int | None = None) -> None:
    stmt = select(func.count(CharacterCareer.id)).where(
        CharacterCareer.character_id == character_id, CharacterCareer.role == role
    )
    if exclude_id is not None:
        stmt = stmt.where(CharacterCareer.id != exclude_id)
    n = await db.scalar(stmt) or 0
    if role == "main" and n >= MAX_MAIN_CAREERS:
        raise ConflictError(MAIN_LIMIT)
    if role == "sub" and n >= MAX_SUB_CAREERS:
        raise ConflictError(SUB_LIMIT)


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
) -> CareerHistory | None:
    """挂接唯一写入入口。未挂 = 新挂（默认第 1 阶）；已挂 = 更新；remove=True = 卸下。

    校验：职业与角色同项目、阶段属于该职业、role（给出时）等于职业 kind、主 1 副 3 上限。
    有变化时写一条历史并返回；无变化返回 None。不 commit。
    """
    if source not in CAREER_SOURCES:
        raise ValueError(f"未知的职业挂接来源：{source}")
    if career.project_id != character.project_id:
        raise BadRequestError(CAREER_NOT_IN_PROJECT)
    if career.kind not in CAREER_KINDS:
        raise ValueError(f"未知的职业类别：{career.kind}")
    if role is not None and role != career.kind:
        raise BadRequestError(ROLE_MISMATCH[career.kind])
    current = await db.scalar(
        select(CharacterCareer).where(
            CharacterCareer.character_id == character.id, CharacterCareer.career_id == career.id
        )
    )
    stages = (await stages_of(db, [career.id]))[career.id]
    by_id = {s.id: s for s in stages}

    def label(sid: int | None) -> str | None:
        s = by_id.get(sid) if sid is not None else None
        return stage_label(s) if s is not None else None

    changes: dict[str, list[Any]] = {}
    if remove:
        if current is None:
            return None
        changes["removed"] = [career.name, None]
        await db.delete(current)
    else:
        if stage_id is not None and stage_id not in by_id:
            raise BadRequestError(STAGE_NOT_IN_CAREER)
        new_note = (note or "").strip()[:200] if note is not None else None
        if current is None:
            if not stages:
                raise BadRequestError(STAGES_REQUIRED)
            await _check_limit(db, character.id, career.kind)
            sid = stage_id if stage_id is not None else stages[0].id
            current = CharacterCareer(
                character_id=character.id, career_id=career.id, role=career.kind, stage_id=sid, note=new_note or ""
            )
            db.add(current)
            changes = {"role": [None, career.kind], "stage": [None, label(sid)], "note": [None, new_note or ""]}
        else:
            if current.role != career.kind:
                await _check_limit(db, character.id, career.kind, exclude_id=current.id)
                changes["role"] = [current.role, career.kind]
                current.role = career.kind
            if stage_id is not None and stage_id != current.stage_id:
                changes["stage"] = [label(current.stage_id), label(stage_id)]
                current.stage_id = stage_id
            if new_note is not None and new_note != current.note:
                changes["note"] = [current.note, new_note]
                current.note = new_note
            if not changes:
                return None
    await db.flush()
    history = CareerHistory(
        character_id=character.id,
        career_id=career.id,
        career_name=career.name,
        source=source,
        chapter_no=chapter_no,
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
) -> CharacterCareer:
    """新挂（已挂该职业 → 409），经 apply_assignment 写入。不 commit。"""
    exists = await db.scalar(
        select(CharacterCareer.id).where(
            CharacterCareer.character_id == character.id, CharacterCareer.career_id == career.id
        )
    )
    if exists is not None:
        raise ConflictError(ALREADY_ASSIGNED)
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
    )
    created = await db.scalar(
        select(CharacterCareer).where(
            CharacterCareer.character_id == character.id, CharacterCareer.career_id == career.id
        )
    )
    assert created is not None
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
    return await story_service.get_owned(db, story_service.CHARACTERS, user_id, project_id, character_id)


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
    await get_owned_project(db, user_id, project_id)
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
    )
    await _commit(db, ALREADY_ASSIGNED)
    return (await _refreshed_assignments(db, [row]))[0]


async def update_assignment(
    db: AsyncSession, user_id: int, project_id: int, character_id: int, assignment_id: int, data: AssignmentUpdate
) -> AssignmentOut:
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
    )
    await db.commit()
    return (await _refreshed_assignments(db, [row]))[0]


async def remove_assignment(
    db: AsyncSession, user_id: int, project_id: int, character_id: int, assignment_id: int
) -> None:
    character, _, career = await _owned_assignment(db, user_id, project_id, character_id, assignment_id)
    await apply_assignment(db, character, career, remove=True, source="manual")
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


async def careers_reference(db: AsyncSession, project_id: int) -> str:
    """已有职业的名称、类别与阶梯摘要（提示词参考）；没有职业时为空串。"""
    careers = await list_project_careers(db, project_id)
    if not careers:
        return ""
    stages = await stages_of(db, [c.id for c in careers])
    return "\n".join(ladder_line(c, stages[c.id]) for c in careers)
