"""伏笔管理：CRUD、状态流转、统计、章节提醒（上下文【伏笔提醒】与编辑器「本章伏笔」同源）、分析清单。

- 写入入口唯一：apply_foreshadow（手动 CRUD、快捷操作、章节分析回写 / 确认 / 撤销都经它），有变化才写历史，不 commit。
- 章节字段存章节号（不做外键），可以指向尚未创建的章节。
- 派生属性：is_long_term（已埋 / 部分回收且没有计划回收章）、
  overdue_by（已埋 / 部分回收且计划回收章 < 参照章时的差值）。
  列表 / 统计的参照章 = 项目中已有正文的最大章节号（current_chapter）；提醒以目标章节为参照。
- 关联角色必须属于本项目（400），删除角色只删除关联行（外键 CASCADE）。
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, NotFoundError
from app.models import Chapter, Character, Foreshadow, ForeshadowCharacter, ForeshadowHistory
from app.models.entities import FORESHADOW_SOURCES, FORESHADOW_STATUSES
from app.schemas.foreshadow import (
    ForeshadowCreate,
    ForeshadowHistoryOut,
    ForeshadowOut,
    ForeshadowReminders,
    ForeshadowStats,
    ForeshadowUpdate,
    PlantRequest,
    ResolveRequest,
)
from app.services.chapter_service import get_manageable_chapter
from app.services.project_service import get_manageable_project

NOT_FOUND = "伏笔不存在"
CHARACTER_INVALID = "关联角色不存在或不属于该项目"
PLANT_REQUIRED = "请填写实际埋设章节"
RESOLVE_REQUIRED = "请填写实际回收章节"
TITLE_REQUIRED = "伏笔标题不能为空"

OPEN_STATUSES = ("planted", "partial")
CLOSED_STATUSES = ("resolved", "abandoned")
STATUS_LABELS = {
    "pending": "待埋",
    "planted": "已埋",
    "resolved": "已回收",
    "partial": "部分回收",
    "abandoned": "已废弃",
}
TEXT_FIELDS = ("title", "content", "plant_hint", "reveal_hint", "note")
CHAPTER_FIELDS = ("planned_plant_chapter", "actual_plant_chapter", "planned_resolve_chapter", "actual_resolve_chapter")
TRACKED_FIELDS = (*TEXT_FIELDS, "status", *CHAPTER_FIELDS, "importance", "include_in_context")
DEFAULTS: dict[str, Any] = {
    "title": "",
    "content": "",
    "plant_hint": "",
    "reveal_hint": "",
    "note": "",
    "status": "pending",
    "planned_plant_chapter": None,
    "actual_plant_chapter": None,
    "planned_resolve_chapter": None,
    "actual_resolve_chapter": None,
    "importance": 3,
    "include_in_context": True,
}

UPCOMING_WINDOW = 3
TIER_LIMIT = 3
ANALYSIS_LIMIT = 30
SNIPPET_CHARS = 60


# ---------------- 派生属性 ----------------


def is_long_term(fs: Foreshadow) -> bool:
    return fs.status in OPEN_STATUSES and fs.planned_resolve_chapter is None


def overdue_by(fs: Foreshadow, current_no: int) -> int:
    if (
        fs.status in OPEN_STATUSES
        and fs.planned_resolve_chapter is not None
        and fs.planned_resolve_chapter < current_no
    ):
        return current_no - fs.planned_resolve_chapter
    return 0


async def current_chapter(db: AsyncSession, project_id: int) -> int:
    """已有正文（去空白后非空）的最大章节号；没有时为 0。"""
    value = await db.scalar(
        select(func.max(Chapter.chapter_no)).where(Chapter.project_id == project_id, func.trim(Chapter.content) != "")
    )
    return int(value or 0)


# ---------------- 关联角色 ----------------


async def character_links(db: AsyncSession, foreshadow_ids: list[int]) -> dict[int, list[tuple[int, str]]]:
    """伏笔 id → [(角色 id, 名字)]，按角色排序（一次查询）。"""
    if not foreshadow_ids:
        return {}
    rows = await db.execute(
        select(ForeshadowCharacter.foreshadow_id, Character.id, Character.name)
        .join(Character, Character.id == ForeshadowCharacter.character_id)
        .where(ForeshadowCharacter.foreshadow_id.in_(foreshadow_ids))
        .order_by(Character.sort, Character.id)
    )
    result: dict[int, list[tuple[int, str]]] = {}
    for fid, cid, name in rows.all():
        result.setdefault(fid, []).append((cid, name))
    return result


async def _validate_characters(db: AsyncSession, project_id: int, character_ids: list[int]) -> list[tuple[int, str]]:
    ids = list(dict.fromkeys(character_ids))
    if not ids:
        return []
    rows = await db.execute(
        select(Character.id, Character.name)
        .where(Character.id.in_(ids), Character.project_id == project_id)
        .order_by(Character.sort, Character.id)
    )
    found = [(cid, name) for cid, name in rows.all()]
    if len(found) != len(ids):
        raise BadRequestError(CHARACTER_INVALID)
    return found


# ---------------- 写入入口 ----------------


def _clean(field_name: str, value: Any) -> Any:
    if field_name in TEXT_FIELDS:
        text = (value or "").strip()
        return text[:200] if field_name == "title" else text
    if field_name in CHAPTER_FIELDS:
        if value is None:
            return None
        n = int(value)
        if n < 1:
            raise BadRequestError("章节号必须大于 0")
        return n
    if field_name == "importance":
        n = int(value)
        if not 1 <= n <= 5:
            raise BadRequestError("重要度必须在 1–5 之间")
        return n
    if field_name == "include_in_context":
        return bool(value)
    if field_name == "status":
        if value not in FORESHADOW_STATUSES:
            raise BadRequestError("伏笔状态不合法")
        return value
    return value


async def apply_foreshadow(
    db: AsyncSession,
    fs: Foreshadow,
    values: dict[str, Any],
    *,
    source: str,
    chapter_no: int | None = None,
    note: str = "",
) -> ForeshadowHistory | None:
    """伏笔唯一写入入口。fs.id 为空视为新建（须先设 project_id），未给的字段用默认值。

    values 可含 TRACKED_FIELDS 与 character_ids（整体替换，须属于本项目）。
    校验：状态为已埋时须有实际埋设章、已回收 / 部分回收时须有实际回收章（按合并后的值判断）。
    有变化才写一条历史 {字段: [旧, 新]}（新建时旧值全为 null；关联角色以名字列表记在 characters 键）。不 commit。
    """
    if source not in FORESHADOW_SOURCES:
        raise ValueError(f"未知的伏笔来源：{source}")
    unknown = set(values) - {*TRACKED_FIELDS, "character_ids"}
    if unknown:
        raise ValueError(f"不支持的伏笔字段：{sorted(unknown)}")
    is_new = fs.id is None
    current = {f: (None if is_new else getattr(fs, f)) for f in TRACKED_FIELDS}
    merged = {f: (DEFAULTS[f] if is_new else current[f]) for f in TRACKED_FIELDS}
    for f in TRACKED_FIELDS:
        if f in values:
            merged[f] = _clean(f, values[f])
    if not merged["title"]:
        raise BadRequestError(TITLE_REQUIRED)
    if merged["status"] == "planted" and merged["actual_plant_chapter"] is None:
        raise BadRequestError(PLANT_REQUIRED)
    if merged["status"] in ("resolved", "partial") and merged["actual_resolve_chapter"] is None:
        raise BadRequestError(RESOLVE_REQUIRED)

    new_links: list[tuple[int, str]] | None = None
    if "character_ids" in values:
        new_links = await _validate_characters(db, fs.project_id, list(values["character_ids"] or []))
    old_links: list[tuple[int, str]] = []
    if new_links is not None and not is_new:
        old_links = (await character_links(db, [fs.id])).get(fs.id, [])

    changes: dict[str, list[Any]] = {}
    for f in TRACKED_FIELDS:
        if is_new or merged[f] != current[f]:
            changes[f] = [current[f], merged[f]]
            setattr(fs, f, merged[f])
    links_changed = new_links is not None and [c for c, _ in new_links] != [c for c, _ in old_links]
    if links_changed or (is_new and new_links):
        changes["characters"] = [None if is_new else [n for _, n in old_links], [n for _, n in new_links or []]]
    if not changes:
        return None
    if is_new:
        db.add(fs)
    await db.flush()
    if links_changed:
        await db.execute(delete(ForeshadowCharacter).where(ForeshadowCharacter.foreshadow_id == fs.id))
        for cid, _ in new_links or []:
            db.add(ForeshadowCharacter(foreshadow_id=fs.id, character_id=cid))
    history = ForeshadowHistory(
        foreshadow_id=fs.id, source=source, chapter_no=chapter_no, changes=changes, note=(note or "")[:200]
    )
    db.add(history)
    await db.flush()
    return history


def snapshot(fs: Foreshadow, character_ids: list[int] | None = None) -> dict[str, Any]:
    """撤销比对用的字段快照。"""
    data = {f: getattr(fs, f) for f in TRACKED_FIELDS}
    if character_ids is not None:
        data["character_ids"] = sorted(character_ids)
    return data


# ---------------- 输出 ----------------


def to_out(fs: Foreshadow, links: list[tuple[int, str]], current_no: int) -> ForeshadowOut:
    return ForeshadowOut(
        id=fs.id,
        project_id=fs.project_id,
        title=fs.title,
        content=fs.content,
        plant_hint=fs.plant_hint,
        reveal_hint=fs.reveal_hint,
        status=fs.status,  # type: ignore[arg-type]
        planned_plant_chapter=fs.planned_plant_chapter,
        actual_plant_chapter=fs.actual_plant_chapter,
        planned_resolve_chapter=fs.planned_resolve_chapter,
        actual_resolve_chapter=fs.actual_resolve_chapter,
        importance=fs.importance,
        include_in_context=fs.include_in_context,
        note=fs.note,
        source=fs.source,
        character_ids=[c for c, _ in links],
        character_names=[n for _, n in links],
        is_long_term=is_long_term(fs),
        overdue_by=overdue_by(fs, current_no),
        created_at=fs.created_at,
        updated_at=fs.updated_at,
    )


async def outs(db: AsyncSession, items: list[Foreshadow], current_no: int) -> list[ForeshadowOut]:
    links = await character_links(db, [f.id for f in items])
    return [to_out(f, links.get(f.id, []), current_no) for f in items]


async def _out(db: AsyncSession, fs: Foreshadow) -> ForeshadowOut:
    await db.refresh(fs)
    return (await outs(db, [fs], await current_chapter(db, fs.project_id)))[0]


# ---------------- CRUD ----------------


async def get_owned_foreshadow(db: AsyncSession, user_id: int, project_id: int, foreshadow_id: int) -> Foreshadow:
    await get_manageable_project(db, user_id, project_id)
    fs = await db.scalar(select(Foreshadow).where(Foreshadow.id == foreshadow_id, Foreshadow.project_id == project_id))
    if fs is None:
        raise NotFoundError(NOT_FOUND)
    return fs


async def project_foreshadows(db: AsyncSession, project_id: int) -> list[Foreshadow]:
    return list(await db.scalars(select(Foreshadow).where(Foreshadow.project_id == project_id).order_by(Foreshadow.id)))


def _resolve_key(fs: Foreshadow) -> tuple:
    return (fs.planned_resolve_chapter is None, fs.planned_resolve_chapter or 0, fs.id)


async def list_foreshadows(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    *,
    status: str | None = None,
    overdue: bool = False,
    long_term: bool = False,
    character_id: int | None = None,
    sort: str = "planned_resolve",
) -> list[ForeshadowOut]:
    """筛选：状态、仅超期、仅长线、关联角色；排序：计划回收章（空值在后）或重要度（高在前）。"""
    await get_manageable_project(db, user_id, project_id)
    stmt = select(Foreshadow).where(Foreshadow.project_id == project_id)
    if status is not None:
        stmt = stmt.where(Foreshadow.status == status)
    if character_id is not None:
        stmt = stmt.where(
            Foreshadow.id.in_(
                select(ForeshadowCharacter.foreshadow_id).where(ForeshadowCharacter.character_id == character_id)
            )
        )
    items = list(await db.scalars(stmt))
    current_no = await current_chapter(db, project_id)
    if overdue:
        items = [f for f in items if overdue_by(f, current_no) > 0]
    if long_term:
        items = [f for f in items if is_long_term(f)]
    if sort == "importance":
        items.sort(key=lambda f: (-f.importance, *_resolve_key(f)))
    else:
        items.sort(key=_resolve_key)
    return await outs(db, items, current_no)


async def get_foreshadow(db: AsyncSession, user_id: int, project_id: int, foreshadow_id: int) -> ForeshadowOut:
    fs = await get_owned_foreshadow(db, user_id, project_id, foreshadow_id)
    return (await outs(db, [fs], await current_chapter(db, project_id)))[0]


async def create_foreshadow(db: AsyncSession, user_id: int, project_id: int, data: ForeshadowCreate) -> ForeshadowOut:
    await get_manageable_project(db, user_id, project_id, lock=True)
    fs = Foreshadow(project_id=project_id, source="manual")
    await apply_foreshadow(db, fs, data.model_dump(), source="manual")
    await db.commit()
    return await _out(db, fs)


async def update_foreshadow(
    db: AsyncSession, user_id: int, project_id: int, foreshadow_id: int, data: ForeshadowUpdate
) -> ForeshadowOut:
    await get_manageable_project(db, user_id, project_id, lock=True)
    fs = await get_owned_foreshadow(db, user_id, project_id, foreshadow_id)
    fields = data.model_fields_set
    values: dict[str, Any] = {}
    for f in TRACKED_FIELDS:
        if f not in fields:
            continue
        value = getattr(data, f)
        # 章节字段显式 null = 清空；其他字段 null 视为不变
        if value is None and f not in CHAPTER_FIELDS:
            continue
        values[f] = value
    if "character_ids" in fields and data.character_ids is not None:
        values["character_ids"] = data.character_ids
    await apply_foreshadow(db, fs, values, source="manual", chapter_no=data.chapter_no, note=data.history_note)
    await db.commit()
    return await _out(db, fs)


async def delete_foreshadow(db: AsyncSession, user_id: int, project_id: int, foreshadow_id: int) -> None:
    await get_manageable_project(db, user_id, project_id, lock=True)
    fs = await get_owned_foreshadow(db, user_id, project_id, foreshadow_id)
    await db.delete(fs)
    await db.commit()


async def plant(
    db: AsyncSession, user_id: int, project_id: int, foreshadow_id: int, data: PlantRequest
) -> ForeshadowOut:
    await get_manageable_project(db, user_id, project_id, lock=True)
    fs = await get_owned_foreshadow(db, user_id, project_id, foreshadow_id)
    values: dict[str, Any] = {"status": "planted"}
    if data.chapter_no is not None:
        values["actual_plant_chapter"] = data.chapter_no
    await apply_foreshadow(db, fs, values, source="manual", chapter_no=data.chapter_no)
    await db.commit()
    return await _out(db, fs)


async def resolve(
    db: AsyncSession, user_id: int, project_id: int, foreshadow_id: int, data: ResolveRequest
) -> ForeshadowOut:
    await get_manageable_project(db, user_id, project_id, lock=True)
    fs = await get_owned_foreshadow(db, user_id, project_id, foreshadow_id)
    values: dict[str, Any] = {"status": "partial" if data.partial else "resolved"}
    if data.chapter_no is not None:
        values["actual_resolve_chapter"] = data.chapter_no
    await apply_foreshadow(db, fs, values, source="manual", chapter_no=data.chapter_no)
    await db.commit()
    return await _out(db, fs)


async def abandon(db: AsyncSession, user_id: int, project_id: int, foreshadow_id: int) -> ForeshadowOut:
    await get_manageable_project(db, user_id, project_id, lock=True)
    fs = await get_owned_foreshadow(db, user_id, project_id, foreshadow_id)
    await apply_foreshadow(db, fs, {"status": "abandoned"}, source="manual")
    await db.commit()
    return await _out(db, fs)


async def list_history(
    db: AsyncSession, user_id: int, project_id: int, foreshadow_id: int
) -> list[ForeshadowHistoryOut]:
    fs = await get_owned_foreshadow(db, user_id, project_id, foreshadow_id)
    rows = await db.scalars(
        select(ForeshadowHistory).where(ForeshadowHistory.foreshadow_id == fs.id).order_by(ForeshadowHistory.id.desc())
    )
    return [
        ForeshadowHistoryOut(
            id=h.id,
            foreshadow_id=h.foreshadow_id,
            source=h.source,
            chapter_no=h.chapter_no,
            changes=h.changes,
            note=h.note,
            created_at=h.created_at,
        )
        for h in rows
    ]


async def stats(db: AsyncSession, user_id: int, project_id: int) -> ForeshadowStats:
    await get_manageable_project(db, user_id, project_id)
    items = await project_foreshadows(db, project_id)
    current_no = await current_chapter(db, project_id)
    result = ForeshadowStats(current_chapter=current_no)
    for f in items:
        setattr(result, f.status, getattr(result, f.status) + 1)
        if overdue_by(f, current_no) > 0:
            result.overdue += 1
        if is_long_term(f):
            result.long_term += 1
    return result


# ---------------- 章节提醒（D2） ----------------


@dataclass
class Reminders:
    """某一章的五档提醒（只含注入上下文开启、且未回收 / 未废弃的伏笔）。"""

    chapter_no: int
    to_plant: list[Foreshadow] = field(default_factory=list)
    must_resolve: list[Foreshadow] = field(default_factory=list)
    overdue: list[Foreshadow] = field(default_factory=list)
    upcoming: list[Foreshadow] = field(default_factory=list)
    long_term: list[Foreshadow] = field(default_factory=list)
    links: dict[int, list[tuple[int, str]]] = field(default_factory=dict)

    def empty(self) -> bool:
        return not (self.to_plant or self.must_resolve or self.overdue or self.upcoming or self.long_term)


def select_reminders(items: list[Foreshadow], n: int) -> Reminders:
    """纯函数：按 D2 分档与上限选取。"""
    active = [f for f in items if f.include_in_context and f.status not in CLOSED_STATUSES]

    def by_importance(f: Foreshadow) -> tuple[int, int]:
        return (-f.importance, f.id)

    rem = Reminders(chapter_no=n)
    rem.to_plant = sorted(
        (f for f in active if f.status == "pending" and f.planned_plant_chapter == n), key=by_importance
    )
    opened = [f for f in active if f.status in OPEN_STATUSES]
    rem.must_resolve = sorted((f for f in opened if f.planned_resolve_chapter == n), key=by_importance)
    rem.overdue = sorted(
        (f for f in opened if f.planned_resolve_chapter is not None and f.planned_resolve_chapter < n),
        key=lambda f: (f.planned_resolve_chapter, -f.importance, f.id),
    )[:TIER_LIMIT]
    rem.upcoming = sorted(
        (
            f
            for f in opened
            if f.planned_resolve_chapter is not None and n < f.planned_resolve_chapter <= n + UPCOMING_WINDOW
        ),
        key=lambda f: (f.planned_resolve_chapter, -f.importance, f.id),
    )[:TIER_LIMIT]
    rem.long_term = sorted((f for f in opened if f.planned_resolve_chapter is None), key=by_importance)[:TIER_LIMIT]
    return rem


async def reminders(db: AsyncSession, project_id: int, chapter_no: int) -> Reminders:
    items = list(
        await db.scalars(
            select(Foreshadow).where(
                Foreshadow.project_id == project_id,
                Foreshadow.include_in_context.is_(True),
                Foreshadow.status.not_in(CLOSED_STATUSES),
            )
        )
    )
    rem = select_reminders(items, chapter_no)
    if not rem.empty():
        ids = [
            f.id for tier in (rem.to_plant, rem.must_resolve, rem.overdue, rem.upcoming, rem.long_term) for f in tier
        ]
        rem.links = await character_links(db, ids)
    return rem


def _one_line(text: str, limit: int | None = None) -> str:
    flat = " ".join((text or "").split())
    if limit is not None and len(flat) > limit:
        return flat[:limit] + "……"
    return flat


def _linked(rem: Reminders, fs: Foreshadow) -> str:
    names = [n for _, n in rem.links.get(fs.id, [])]
    return f"（关联：{'、'.join(names)}）" if names else ""


def reminder_lines(rem: Reminders) -> dict[str, list[str]]:
    """渲染五档提醒（每条一行，带档位前缀）。"""

    def head(fs: Foreshadow) -> str:
        return f"#{fs.id} {fs.title}"

    def detail(fs: Foreshadow, hint: str, label: str) -> str:
        if hint.strip():
            return f" —— {label}：{_one_line(hint)}"
        brief = _one_line(fs.content, SNIPPET_CHARS)
        return f" —— {brief}" if brief else ""

    return {
        "to_plant": [
            f"◆ 本章计划埋设：{head(f)}{detail(f, f.plant_hint, '埋法')}{_linked(rem, f)}" for f in rem.to_plant
        ],
        "must_resolve": [
            f"◆ 本章必须回收：{head(f)}{detail(f, f.reveal_hint, '回收')}{_linked(rem, f)}" for f in rem.must_resolve
        ],
        "overdue": [
            f"◆ 超期未回收：{head(f)}（已超期 {overdue_by(f, rem.chapter_no)} 章）{_linked(rem, f)}"
            for f in rem.overdue
        ],
        "upcoming": [
            f"◆ 即将到期（可适当铺垫）：{head(f)}（第 {f.planned_resolve_chapter} 章）{_linked(rem, f)}"
            for f in rem.upcoming
        ],
        "long_term": [
            f"◆ 长线伏笔：{head(f)}"
            + (f"：{_one_line(f.content, SNIPPET_CHARS)}" if f.content.strip() else "")
            + _linked(rem, f)
            for f in rem.long_term
        ],
    }


async def chapter_reminders(db: AsyncSession, user_id: int, project_id: int, chapter_id: int) -> ForeshadowReminders:
    chapter = await get_manageable_chapter(db, user_id, project_id, chapter_id)
    rem = await reminders(db, project_id, chapter.chapter_no)
    n = chapter.chapter_no

    def tier(items: list[Foreshadow]) -> list[ForeshadowOut]:
        return [to_out(f, rem.links.get(f.id, []), n) for f in items]

    return ForeshadowReminders(
        chapter_no=n,
        to_plant=tier(rem.to_plant),
        must_resolve=tier(rem.must_resolve),
        overdue=tier(rem.overdue),
        upcoming=tier(rem.upcoming),
        long_term=tier(rem.long_term),
    )


# ---------------- 分析清单（R2） ----------------


async def analysis_candidates(db: AsyncSession, project_id: int, chapter_no: int) -> list[Foreshadow]:
    """交给分析模型的伏笔：已埋 / 部分回收的全部 + 计划埋设章 ≤ 本章（或未定）的待埋。

    按重要度、计划回收章排序，最多 30 条。
    """
    items = [
        f
        for f in await project_foreshadows(db, project_id)
        if f.status in OPEN_STATUSES
        or (f.status == "pending" and (f.planned_plant_chapter is None or f.planned_plant_chapter <= chapter_no))
    ]
    items.sort(key=lambda f: (-f.importance, *_resolve_key(f)))
    return items[:ANALYSIS_LIMIT]


def analysis_line(fs: Foreshadow) -> str:
    """「#ID 标题｜内容摘要（≤60 字）｜状态｜计划回收第N章」。"""
    parts = [f"#{fs.id} {fs.title}"]
    brief = _one_line(fs.content, SNIPPET_CHARS)
    if brief:
        parts.append(brief)
    status = STATUS_LABELS.get(fs.status, fs.status)
    if fs.status == "pending" and fs.planned_plant_chapter is not None:
        status += f"（计划第{fs.planned_plant_chapter}章埋设）"
    parts.append(status)
    parts.append(f"计划回收第{fs.planned_resolve_chapter}章" if fs.planned_resolve_chapter else "未定回收章")
    return "｜".join(parts)
