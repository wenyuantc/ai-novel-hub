"""关系类型、人物关系、关系历史与关系图谱。

语义（D2）：一对角色只存一条无方向记录，「a 是 b 的 role_a，b 是 a 的 role_b」。
- 从 a 的视角对方显示为 role_b（「徒弟 B」），从 b 的视角对方显示为 role_a（「师父 A」）。
- pair_low / pair_high = min / max(a_id, b_id)，(pair_low, pair_high) 唯一。
- apply_change 编排旧关系入口、手动编辑、卡片与向导；变更事实经统一时间线写入，
  最新载体由关系物化器同步，旧审计单独保留；事务由调用方提交。
"""

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import (
    Chapter,
    Character,
    Organization,
    OrganizationMember,
    Relationship,
    RelationshipHistory,
    RelationshipType,
    SettingProgression,
    SettingTimeline,
)
from app.models.entities import RELATION_CATEGORIES, RELATION_SOURCES, RELATION_STATUSES
from app.schemas.progression import BaselineUpdate, ProgressionCreate, StoryPosition
from app.schemas.relationship import (
    CharacterWithRelations,
    GraphEdge,
    GraphNode,
    GraphOrganization,
    RelationshipBatchCreate,
    RelationshipCreate,
    RelationshipGraph,
    RelationshipHistoryOut,
    RelationshipOut,
    RelationshipTypeCreate,
    RelationshipTypeOut,
    RelationshipUpdate,
)
from app.schemas.story import CharacterOut
from app.services import career_service, setting_progression_service, story_service
from app.services.cast_service import is_protagonist
from app.services.project_service import get_manageable_project
from app.services.relationship_progression_service import (
    get_or_create_relationship_timeline,
    relationship_snapshot,
    type_referenced_by_story,
)

STATUS_LABELS = {"normal": "正常", "broken": "破裂", "past": "过去式", "complicated": "复杂"}
CATEGORY_LABELS = {"family": "家庭", "social": "社交", "hostile": "敌对", "work": "职场", "other": "其他"}
PAIR_CONFLICT = "这两个角色之间已有关系"
TYPE_CREATE_CONFLICT = "关系类型已被其他请求创建，请重试保存"
TRACKED_FIELDS = ("a_id", "b_id", "type_id", "intimacy", "status", "description")
_NEW_DEFAULTS: dict[str, Any] = {"intimacy": 0, "status": "normal", "description": ""}


def type_label(t: RelationshipType) -> str:
    return t.role_a if t.role_a == t.role_b else f"{t.role_a}/{t.role_b}"


def describe_relation(a_name: str, b_name: str, t: RelationshipType) -> str:
    """「A 是 B 的师父」；对称类型为「A 与 B 是朋友」。"""
    if t.role_a == t.role_b:
        return f"{a_name} 与 {b_name} 是{t.role_a}"
    return f"{a_name} 是 {b_name} 的{t.role_a}"


# ---------------- 关系类型 ----------------


def _type_out(t: RelationshipType) -> RelationshipTypeOut:
    return RelationshipTypeOut.model_validate(t).model_copy(update={"builtin": t.project_id is None})


def _visible_types(project_id: int):
    return or_(RelationshipType.project_id.is_(None), RelationshipType.project_id == project_id)


async def get_visible_type(db: AsyncSession, project_id: int, type_id: int) -> RelationshipType:
    t = await db.scalar(select(RelationshipType).where(RelationshipType.id == type_id, _visible_types(project_id)))
    if t is None:
        raise BadRequestError("关系类型不存在或不属于该项目")
    return t


async def find_type(
    db: AsyncSession, project_id: int, role_a: str, role_b: str
) -> tuple[RelationshipType | None, bool]:
    """按称呼匹配预置 / 本项目自定义类型。返回 (类型, flipped)；flipped 表示匹配到的是反向类型（a/b 需互换）。"""
    rows = list(
        await db.scalars(
            select(RelationshipType)
            .where(
                _visible_types(project_id),
                or_(
                    (RelationshipType.role_a == role_a) & (RelationshipType.role_b == role_b),
                    (RelationshipType.role_a == role_b) & (RelationshipType.role_b == role_a),
                ),
            )
            # 预置优先
            .order_by(RelationshipType.project_id.is_not(None), RelationshipType.id)
        )
    )
    for t in rows:
        if t.role_a == role_a and t.role_b == role_b:
            return t, False
    if rows:
        return rows[0], True
    return None, False


def _is_type_conflict(exc: IntegrityError) -> bool:
    """只识别同项目、同称呼的类型唯一约束，其他完整性错误保留原异常。"""
    original = exc.orig
    if (
        getattr(original, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_UNIQUE
        and str(original)
        == (
            "UNIQUE constraint failed: relationship_types.project_id, "
            "relationship_types.role_a, relationship_types.role_b"
        )
    ):
        return True
    return any(
        getattr(error, "sqlstate", None) == "23505"
        and getattr(error, "constraint_name", None) == "uq_relationship_types_project_id"
        for error in (original, getattr(original, "__cause__", None))
    )


async def resolve_type(
    db: AsyncSession,
    project_id: int,
    *,
    type_id: int | None = None,
    role_a: str | None = None,
    role_b: str | None = None,
    category: str = "other",
) -> tuple[RelationshipType, bool]:
    """type_id 优先；否则按称呼匹配，匹配不上则新建本项目自定义类型（flush，不 commit）。"""
    if type_id is not None:
        return await get_visible_type(db, project_id, type_id), False
    ra = (role_a or "").strip()[:20]
    rb = (role_b or "").strip()[:20] or ra
    if not ra:
        raise BadRequestError("请指定关系类型")
    found, flipped = await find_type(db, project_id, ra, rb)
    if found is not None:
        return found, flipped
    max_sort = await db.scalar(select(func.max(RelationshipType.sort)).where(RelationshipType.project_id == project_id))
    t = RelationshipType(
        project_id=project_id,
        category=category if category in RELATION_CATEGORIES else "other",
        role_a=ra,
        role_b=rb,
        sort=(max_sort or 0) + 1,
    )
    db.add(t)
    try:
        await db.flush()
    except IntegrityError as exc:
        if _is_type_conflict(exc):
            # 不回滚调用方的事务；HTTP 请求或外层 SAVEPOINT 统一撤销本批改动。
            raise ConflictError(TYPE_CREATE_CONFLICT) from exc
        raise
    return t, False


async def list_types(db: AsyncSession, user_id: int, project_id: int) -> list[RelationshipTypeOut]:
    await get_manageable_project(db, user_id, project_id)
    rows = await db.scalars(
        select(RelationshipType)
        .where(_visible_types(project_id))
        .order_by(RelationshipType.project_id.is_not(None), RelationshipType.sort, RelationshipType.id)
    )
    return [_type_out(t) for t in rows]


async def create_type(
    db: AsyncSession, user_id: int, project_id: int, data: RelationshipTypeCreate
) -> RelationshipTypeOut:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    ra, rb = data.role_a.strip(), data.role_b.strip()
    if not ra or not rb:
        raise BadRequestError("称呼不能为空")
    found, _ = await find_type(db, project_id, ra, rb)
    if found is not None:
        raise ConflictError("该关系类型已存在")
    t, _ = await resolve_type(db, project_id, role_a=ra, role_b=rb, category=data.category)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("该关系类型已存在") from exc
    await db.refresh(t)
    return _type_out(t)


async def delete_type(db: AsyncSession, user_id: int, project_id: int, type_id: int) -> None:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    t = await db.scalar(select(RelationshipType).where(RelationshipType.id == type_id, _visible_types(project_id)))
    if t is None:
        raise NotFoundError("关系类型不存在")
    if t.project_id is None:
        raise BadRequestError("预置关系类型不能修改或删除")
    in_use = await db.scalar(select(func.count(Relationship.id)).where(Relationship.type_id == t.id))
    if in_use or await type_referenced_by_story(db, project_id, type_id):
        raise ConflictError("该关系类型仍被关系使用，无法删除")
    await db.delete(t)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("该关系类型仍被关系使用，无法删除") from exc


# ---------------- 写入入口 ----------------


async def find_pair_record(db: AsyncSession, a_id: int, b_id: int) -> Relationship | None:
    """查询稳定关系载体，包括已从当前剧情中隐藏的记录。"""
    low, high = min(a_id, b_id), max(a_id, b_id)
    return await db.scalar(select(Relationship).where(Relationship.pair_low == low, Relationship.pair_high == high))


async def find_current_pair(db: AsyncSession, a_id: int, b_id: int) -> Relationship | None:
    """查询当前存在的关系，不把已隐藏的载体当作当前关系。"""
    low, high = min(a_id, b_id), max(a_id, b_id)
    return await db.scalar(
        select(Relationship).where(
            Relationship.pair_low == low,
            Relationship.pair_high == high,
            Relationship.is_present.is_(True),
        )
    )


async def find_pair(db: AsyncSession, a_id: int, b_id: int) -> Relationship | None:
    """兼容旧调用方：判断人物对当前是否有关系。"""
    return await find_current_pair(db, a_id, b_id)


def _is_pair_conflict(exc: IntegrityError) -> bool:
    """只识别角色对唯一约束；外键、类型唯一性等错误保留原异常。"""
    original = exc.orig
    if (
        getattr(original, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_UNIQUE
        and str(original) == "UNIQUE constraint failed: relationships.pair_low, relationships.pair_high"
    ):
        return True
    # asyncpg 原异常通过 SQLAlchemy 适配器的 cause 保留约束名。
    return any(
        getattr(error, "sqlstate", None) == "23505"
        and getattr(error, "constraint_name", None) == "uq_relationships_pair_low"
        for error in (original, getattr(original, "__cause__", None))
    )


@asynccontextmanager
async def _pair_conflict_guard() -> AsyncIterator[None]:
    """统一识别 flush / commit 的角色对冲突；事务由调用方或外层 SAVEPOINT 回滚。"""
    try:
        yield
    except IntegrityError as exc:
        if _is_pair_conflict(exc):
            raise ConflictError(PAIR_CONFLICT) from exc
        raise


async def _story_anchor(
    db: AsyncSession,
    project_id: int,
    chapter_no: int | None,
    chapter_id: int | None,
    boundary: str,
    anchor_kind: str | None,
) -> tuple[dict[str, Any] | None, StoryPosition | None, int | None]:
    """旧章号唯一匹配时关联稳定章节；尚未建章保留为计划章。"""
    if anchor_kind not in (None, "chapter", "planned"):
        raise BadRequestError("章节锚点类型不合法")
    if anchor_kind == "planned" and (chapter_no is None or chapter_id is not None):
        raise BadRequestError("计划章必须指定章节号")
    if anchor_kind == "chapter" and (chapter_id is None or chapter_no is not None):
        raise BadRequestError("已有章节必须指定章节 id")
    if chapter_id is not None and chapter_no is not None:
        raise BadRequestError("请只指定章节 id 或章节号")
    if chapter_id is None and chapter_no is None:
        return None, None, None
    if boundary not in ("start", "end"):
        raise BadRequestError("章节生效位置不合法")
    if anchor_kind == "planned":
        assert chapter_no is not None
        position = StoryPosition(chapter_no=chapter_no, boundary=boundary)
        return {"anchor_kind": "planned", "chapter_no": chapter_no, "boundary": boundary}, position, chapter_no
    if chapter_id is not None:
        chapter = await db.scalar(select(Chapter).where(Chapter.project_id == project_id, Chapter.id == chapter_id))
        if chapter is None:
            raise BadRequestError("章节不存在或不属于该作品")
        chapter_no = chapter.chapter_no
    else:
        matches = list((await db.scalars(
            select(Chapter).where(Chapter.project_id == project_id, Chapter.chapter_no == chapter_no)
        )).all())
        if len(matches) > 1:
            raise BadRequestError("有多个同号章节，请指定章节 id")
        chapter_id = matches[0].id if matches else None
    assert chapter_no is not None
    position = StoryPosition(chapter_id=chapter_id, chapter_no=chapter_no, boundary=boundary)
    if chapter_id is None:
        return {"anchor_kind": "planned", "chapter_no": chapter_no, "boundary": boundary}, position, chapter_no
    return {"anchor_kind": "chapter", "chapter_id": chapter_id, "boundary": boundary}, position, chapter_no


def _tracked_from_snapshot(fields: dict[str, Any]) -> dict[str, Any]:
    if not fields.get("exists"):
        return {field: None for field in TRACKED_FIELDS}
    identity = fields["identity"]
    return {
        "a_id": identity["a_id"],
        "b_id": identity["b_id"],
        "type_id": identity["type_id"],
        "intimacy": fields["intimacy"],
        "status": fields["status"],
        "description": fields["description"],
    }


def _snapshot_from_tracked(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "exists": True,
        "identity": {key: values[key] for key in ("a_id", "b_id", "type_id")},
        "intimacy": values["intimacy"],
        "status": values["status"],
        "description": values["description"],
    }


def _changed_fields(current: dict[str, Any], merged: dict[str, Any]) -> set[str]:
    fields = {name for name in ("intimacy", "status", "description") if current[name] != merged[name]}
    if any(current[name] != merged[name] for name in ("a_id", "b_id", "type_id")):
        fields.add("identity")
    return fields


async def _pair_timeline(db: AsyncSession, project_id: int, a_id: int, b_id: int) -> SettingTimeline | None:
    return await db.scalar(
        select(SettingTimeline).where(
            SettingTimeline.project_id == project_id,
            SettingTimeline.subject_kind == "relationship",
            SettingTimeline.character_id == min(a_id, b_id),
            SettingTimeline.related_character_id == max(a_id, b_id),
        )
    )


def _check_expected_timeline_revision(timeline: SettingTimeline | None, expected_revision: int | None) -> None:
    if expected_revision is None:
        return
    matches = (
        timeline is None if expected_revision == 0 else timeline is not None and timeline.revision == expected_revision
    )
    if not matches:
        raise ConflictError("设定已被其他操作修改，请刷新后重试", extra={"code": "stale_revision"})


async def apply_change(
    db: AsyncSession,
    rel: Relationship,
    values: dict[str, Any],
    *,
    source: str,
    chapter_no: int | None = None,
    chapter_id: int | None = None,
    anchor_kind: str | None = None,
    boundary: str = "start",
    edit_mode: str | None = None,
    expected_revision: int | None = None,
    recreate: bool = False,
    note: str = "",
) -> RelationshipHistory | None:
    """关系唯一写入入口：显式区分基础修正与按故事位置发生的变化。

    只写实际变化的字段；有章节时读取该位置的旧值，随后由统一求值更新最新载体。
    旧审计记录与时间线事实同一事务写入，事务由调用方负责。
    """
    if source not in RELATION_SOURCES:
        raise ValueError(f"未知的关系来源：{source}")
    if edit_mode not in (None, "baseline", "progression"):
        raise BadRequestError("请选择基础修正或剧情变化")
    if edit_mode is not None and expected_revision is None:
        raise BadRequestError("明确修改基础或剧情变化时请提供当前版本")
    unknown = set(values) - set(TRACKED_FIELDS)
    if unknown:
        raise ValueError(f"不支持的关系字段：{sorted(unknown)}")
    anchor, position, history_chapter = await _story_anchor(
        db, rel.project_id, chapter_no, chapter_id, boundary, anchor_kind
    )
    if edit_mode == "baseline" and anchor is not None:
        raise BadRequestError("基础修正不能同时指定剧情位置")
    if edit_mode == "progression" and anchor is None:
        raise BadRequestError("剧情变化必须指定章节")

    is_new_record = rel.id is None
    is_new = is_new_record or recreate
    timeline: SettingTimeline | None = None
    if not is_new_record:
        timeline = await _pair_timeline(db, rel.project_id, rel.pair_low, rel.pair_high)
        _check_expected_timeline_revision(timeline, expected_revision)
        if timeline is None:
            timeline = await get_or_create_relationship_timeline(
                db,
                rel.project_id,
                rel.pair_low,
                rel.pair_high,
                initial_state=relationship_snapshot(rel),
                baseline_status="legacy_unverified",
            )

    if is_new:
        current = {field: None for field in TRACKED_FIELDS}
    else:
        assert timeline is not None
        if edit_mode == "baseline":
            target = timeline.baseline
        else:
            target = (await setting_progression_service.resolve_timelines(db, rel.project_id, [timeline], position))[
                timeline.id
            ].fields
        if not target.get("exists"):
            raise ConflictError("此位置的关系尚未建立，请先记录关系建立")
        current = _tracked_from_snapshot(target)
    merged = {
        field: values.get(field, _NEW_DEFAULTS.get(field) if is_new else current[field]) for field in TRACKED_FIELDS
    }
    a_id, b_id = merged["a_id"], merged["b_id"]
    if a_id is None or b_id is None or merged["type_id"] is None:
        raise BadRequestError("关系缺少角色或类型")
    if a_id == b_id:
        raise BadRequestError("不能与自己建立关系")
    if not is_new_record and {a_id, b_id} != {rel.pair_low, rel.pair_high}:
        raise BadRequestError("关系身份必须使用原有的两个人物")
    lookup = {a_id, b_id} | {i for i in (current["a_id"], current["b_id"]) if i is not None}
    names = dict(
        (
            await db.execute(
                select(Character.id, Character.name).where(
                    Character.id.in_(lookup), Character.project_id == rel.project_id
                )
            )
        ).all()
    )
    if a_id not in names or b_id not in names:
        raise BadRequestError("角色不存在或不属于该项目")
    new_type = await get_visible_type(db, rel.project_id, merged["type_id"])
    intimacy = int(merged["intimacy"])
    if not -100 <= intimacy <= 100:
        raise BadRequestError("亲密度应在 -100～100 之间")
    if merged["status"] not in RELATION_STATUSES:
        raise BadRequestError("关系状态不合法")
    merged["intimacy"] = intimacy
    merged["description"] = merged["description"] or ""

    existing = await find_pair_record(db, a_id, b_id)
    if existing is not None and existing is not rel and existing.id != rel.id:
        raise ConflictError(PAIR_CONFLICT)
    if is_new_record:
        # 分析待确认时可能已先建立空主体，但尚没有关系载体。
        timeline = await _pair_timeline(db, rel.project_id, a_id, b_id)
        _check_expected_timeline_revision(timeline, expected_revision)
    timeline_already_existed = timeline is not None

    changes: dict[str, list[Any]] = {}
    if (current["a_id"], current["b_id"]) != (a_id, b_id):
        old_dir = None
        if not is_new:
            old_dir = f"{names.get(current['a_id'], current['a_id'])}→{names.get(current['b_id'], current['b_id'])}"
        changes["direction"] = [old_dir, f"{names[a_id]}→{names[b_id]}"]
    if current["type_id"] != new_type.id:
        old_label = None
        if current["type_id"] is not None:
            old_t = await db.get(RelationshipType, current["type_id"])
            old_label = type_label(old_t) if old_t is not None else None
        changes["type"] = [old_label, type_label(new_type)]
    for f, new in (("intimacy", intimacy), ("status", merged["status"]), ("description", merged["description"])):
        if current[f] != new:
            changes[f] = [current[f], new]
    if not changes:
        return None

    if is_new_record:
        # 载体与旧审计保留稳定外键；有章节的新建在该章节前仍为 exists=false。
        rel.a_id, rel.b_id = a_id, b_id
        rel.pair_low, rel.pair_high = min(a_id, b_id), max(a_id, b_id)
        rel.type_id = new_type.id
        rel.intimacy = intimacy
        rel.status = merged["status"]
        rel.description = merged["description"]
        rel.is_present = True
        db.add(rel)
    async with _pair_conflict_guard():
        await db.flush()
        if timeline is None:
            timeline = await get_or_create_relationship_timeline(
                db,
                rel.project_id,
                a_id,
                b_id,
                initial_state={"exists": False} if anchor is not None else _snapshot_from_tracked(merged),
            )

        fields = _changed_fields(current, merged)
        if anchor is None and timeline_already_existed and edit_mode is None:
            # 原样重发字段无操作；对已受剧情事件影响的字段必须要求明确意图。
            affecting = await db.scalar(
                select(SettingProgression.id)
                .where(
                    SettingProgression.timeline_id == timeline.id,
                    SettingProgression.active.is_(True),
                    SettingProgression.field_key.in_(fields | {"presence"}),
                )
                .limit(1)
            )
            if affecting is not None:
                raise ConflictError(
                    "该字段已有剧情变化，请选择修正基础设定或指定生效章节",
                    extra={"code": "progression_intent_required"},
                )

        if anchor is not None:
            event_values: dict[str, Any]
            if is_new:
                snapshot = _snapshot_from_tracked(merged)
                event_values = {
                    "presence": {"exists": True, "state": {k: v for k, v in snapshot.items() if k != "exists"}}
                }
            else:
                event_values = {}
                if "identity" in fields:
                    event_values["identity"] = {key: merged[key] for key in ("a_id", "b_id", "type_id")}
                for name in ("intimacy", "status", "description"):
                    if name in fields:
                        event_values[name] = merged[name]
            for field, value in event_values.items():
                event = ProgressionCreate(
                    expected_revision=timeline.revision,
                    field_key=field,
                    operation="replace",
                    value=value,
                    note=note,
                    **anchor,
                )
                await setting_progression_service.apply_progression(
                    db,
                    timeline,
                    event,
                    source="analysis" if source == "analysis" else "manual",
                    protection="automatic" if source == "analysis" else "manual",
                    materialize=False,
                )
            await setting_progression_service.materialize_latest(db, timeline)
        elif timeline_already_existed:
            baseline_changes: dict[str, Any]
            if is_new:
                baseline_changes = _snapshot_from_tracked(merged)
            else:
                baseline_changes = {}
                if "identity" in fields:
                    baseline_changes["identity"] = {key: merged[key] for key in ("a_id", "b_id", "type_id")}
                for name in ("intimacy", "status", "description"):
                    if name in fields:
                        baseline_changes[name] = merged[name]
            await setting_progression_service.apply_baseline(
                db,
                timeline,
                BaselineUpdate(expected_revision=timeline.revision, changes=baseline_changes),
                materialize=False,
            )
            await setting_progression_service.materialize_latest(db, timeline)

        history = RelationshipHistory(
            relationship_id=rel.id, chapter_no=history_chapter, source=source, changes=changes, note=(note or "")[:200]
        )
        db.add(history)
        await db.flush()
    return history


async def create_relation(
    db: AsyncSession,
    project_id: int,
    *,
    a_id: int,
    b_id: int,
    type_id: int | None = None,
    role_a: str | None = None,
    role_b: str | None = None,
    category: str = "other",
    intimacy: int = 0,
    status: str = "normal",
    description: str = "",
    source: str,
    chapter_no: int | None = None,
    chapter_id: int | None = None,
    anchor_kind: str | None = None,
    boundary: str = "start",
    expected_revision: int | None = None,
    note: str = "",
) -> Relationship:
    """解析类型后建立关系；已有隐藏载体时复用以保留审计。不 commit。"""
    t, flipped = await resolve_type(db, project_id, type_id=type_id, role_a=role_a, role_b=role_b, category=category)
    if flipped:
        a_id, b_id = b_id, a_id
    if expected_revision is not None:
        timeline = await _pair_timeline(db, project_id, a_id, b_id)
        _check_expected_timeline_revision(timeline, expected_revision)
    existing = await find_pair_record(db, a_id, b_id)
    if existing is not None and existing.is_present:
        raise ConflictError(PAIR_CONFLICT)
    rel = existing if existing is not None else Relationship(project_id=project_id)
    await apply_change(
        db,
        rel,
        {
            "a_id": a_id,
            "b_id": b_id,
            "type_id": t.id,
            "intimacy": intimacy,
            "status": status,
            "description": description,
        },
        source=source,
        chapter_no=chapter_no,
        chapter_id=chapter_id,
        anchor_kind=anchor_kind,
        boundary=boundary,
        expected_revision=expected_revision,
        recreate=existing is not None,
        note=note,
    )
    return rel


async def _commit(db: AsyncSession) -> None:
    try:
        async with _pair_conflict_guard():
            await db.commit()
    except (ConflictError, IntegrityError):
        await db.rollback()
        raise


# ---------------- 序列化 ----------------


async def to_outs(db: AsyncSession, rels: list[Relationship], viewer_id: int | None = None) -> list[RelationshipOut]:
    if not rels:
        return []
    char_ids = {r.a_id for r in rels} | {r.b_id for r in rels}
    names = dict((await db.execute(select(Character.id, Character.name).where(Character.id.in_(char_ids)))).all())
    types = {
        t.id: t
        for t in await db.scalars(select(RelationshipType).where(RelationshipType.id.in_({r.type_id for r in rels})))
    }
    outs = []
    for r in rels:
        t = types[r.type_id]
        out = RelationshipOut(
            id=r.id,
            project_id=r.project_id,
            a_id=r.a_id,
            b_id=r.b_id,
            a_name=names.get(r.a_id, ""),
            b_name=names.get(r.b_id, ""),
            type_id=t.id,
            category=t.category,
            role_a=t.role_a,
            role_b=t.role_b,
            intimacy=r.intimacy,
            status=r.status,
            description=r.description,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        if viewer_id == r.a_id:
            out = out.model_copy(
                update={"other_id": r.b_id, "other_name": out.b_name, "other_role": t.role_b, "self_role": t.role_a}
            )
        elif viewer_id == r.b_id:
            out = out.model_copy(
                update={"other_id": r.a_id, "other_name": out.a_name, "other_role": t.role_a, "self_role": t.role_b}
            )
        outs.append(out)
    return outs


async def _refreshed_outs(db: AsyncSession, rels: list[Relationship], viewer_id: int | None = None):
    for r in rels:
        await db.refresh(r)
    return await to_outs(db, rels, viewer_id)


# ---------------- HTTP 入口 ----------------


async def get_owned_relationship(
    db: AsyncSession, user_id: int, project_id: int, rel_id: int, *, include_hidden: bool = False
) -> Relationship:
    await get_manageable_project(db, user_id, project_id)
    stmt = select(Relationship).where(Relationship.id == rel_id, Relationship.project_id == project_id)
    if not include_hidden:
        stmt = stmt.where(Relationship.is_present.is_(True))
    rel = await db.scalar(stmt)
    if rel is None:
        raise NotFoundError("关系不存在")
    return rel


async def list_relationships(
    db: AsyncSession, user_id: int, project_id: int, *, character_id: int | None = None
) -> list[RelationshipOut]:
    await get_manageable_project(db, user_id, project_id)
    stmt = select(Relationship).where(Relationship.project_id == project_id, Relationship.is_present.is_(True))
    if character_id is not None:
        exists = await db.scalar(
            select(Character.id).where(Character.id == character_id, Character.project_id == project_id)
        )
        if exists is None:
            raise NotFoundError("角色不存在")
        stmt = stmt.where(or_(Relationship.a_id == character_id, Relationship.b_id == character_id))
    rels = list(await db.scalars(stmt.order_by(Relationship.id)))
    return await to_outs(db, rels, character_id)


def _create_kwargs(data: RelationshipCreate) -> dict[str, Any]:
    return {
        "a_id": data.a_id,
        "b_id": data.b_id,
        "type_id": data.type_id,
        "role_a": data.role_a,
        "role_b": data.role_b,
        "category": data.category,
        "intimacy": data.intimacy,
        "status": data.status,
        "description": data.description,
        "source": data.source,
        "chapter_no": data.chapter_no,
        "chapter_id": data.chapter_id,
        "anchor_kind": data.anchor_kind,
        "boundary": data.boundary,
        "expected_revision": data.expected_revision,
        "note": data.note,
    }


async def create_relationship(
    db: AsyncSession, user_id: int, project_id: int, data: RelationshipCreate
) -> RelationshipOut:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    try:
        rel = await create_relation(db, project_id, **_create_kwargs(data))
    except ConflictError:
        await db.rollback()
        raise
    await _commit(db)
    return (await _refreshed_outs(db, [rel]))[0]


async def create_relationships(
    db: AsyncSession, user_id: int, project_id: int, data: RelationshipBatchCreate
) -> list[RelationshipOut]:
    """批量新建（AI 关系建议预览确认后保存），单事务：任一条失败则全部不写入。"""
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    try:
        rels = [await create_relation(db, project_id, **_create_kwargs(item)) for item in data.items]
    except ConflictError:
        await db.rollback()
        raise
    await _commit(db)
    return await _refreshed_outs(db, rels)


async def update_relationship(
    db: AsyncSession, user_id: int, project_id: int, rel_id: int, data: RelationshipUpdate
) -> RelationshipOut:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    rel = await get_owned_relationship(db, user_id, project_id, rel_id)
    values: dict[str, Any] = {}
    if data.a_id is not None:
        if data.a_id not in (rel.a_id, rel.b_id):
            raise BadRequestError("a_id 必须是这段关系的一方")
        values["a_id"] = data.a_id
        values["b_id"] = rel.b_id if data.a_id == rel.a_id else rel.a_id
    for f in ("type_id", "intimacy", "status", "description"):
        v = getattr(data, f)
        if v is not None:
            values[f] = v
    await apply_change(
        db,
        rel,
        values,
        source="manual",
        chapter_no=data.chapter_no,
        chapter_id=data.chapter_id,
        anchor_kind=data.anchor_kind,
        boundary=data.boundary,
        edit_mode=data.edit_mode,
        expected_revision=data.expected_revision,
        note=data.note,
    )
    await _commit(db)
    return (await _refreshed_outs(db, [rel]))[0]


async def delete_relationship(db: AsyncSession, user_id: int, project_id: int, rel_id: int) -> None:
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    rel = await get_owned_relationship(db, user_id, project_id, rel_id, include_hidden=True)
    timeline = await db.scalar(
        select(SettingTimeline).where(
            SettingTimeline.project_id == project_id,
            SettingTimeline.subject_kind == "relationship",
            SettingTimeline.character_id == rel.pair_low,
            SettingTimeline.related_character_id == rel.pair_high,
        )
    )
    if timeline is not None:
        await db.delete(timeline)
    await db.delete(rel)
    await db.commit()


async def _relationship_history(db: AsyncSession, rel_id: int) -> list[RelationshipHistoryOut]:
    rows = await db.scalars(
        select(RelationshipHistory)
        .where(RelationshipHistory.relationship_id == rel_id)
        .order_by(RelationshipHistory.id.desc())
    )
    return [RelationshipHistoryOut.model_validate(h) for h in rows]


async def list_history(db: AsyncSession, user_id: int, project_id: int, rel_id: int) -> list[RelationshipHistoryOut]:
    rel = await get_owned_relationship(db, user_id, project_id, rel_id, include_hidden=True)
    return await _relationship_history(db, rel.id)


async def list_pair_history(
    db: AsyncSession, user_id: int, project_id: int, character_id: int, related_character_id: int
) -> list[RelationshipHistoryOut]:
    """按稳定人物对读取旧审计，包含隐藏载体；不影响当前关系列表。"""
    await get_manageable_project(db, user_id, project_id)
    if character_id == related_character_id:
        raise BadRequestError("关系两端不能是同一角色")
    ids = {character_id, related_character_id}
    owned = set((await db.scalars(select(Character.id).where(
        Character.project_id == project_id, Character.id.in_(ids)
    ))).all())
    if owned != ids:
        raise NotFoundError("角色不存在")
    rel = await find_pair_record(db, character_id, related_character_id)
    if rel is None:
        return []
    if rel.project_id != project_id:
        raise NotFoundError("关系不存在")
    return await _relationship_history(db, rel.id)


async def create_character_with_relations(
    db: AsyncSession, user_id: int, project_id: int, data: CharacterWithRelations
) -> dict[str, Any]:
    """新建一个角色并同时建立勾选的关系与职业挂接（单事务）。subject_is_a 为真时新角色是 role_a 一方。"""
    await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
    character = await story_service.new_character(db, project_id, data.character.model_dump())
    rels = []
    for d in data.relations:
        a_id, b_id = (character.id, d.target_id) if d.subject_is_a else (d.target_id, character.id)
        rels.append(
            await create_relation(
                db,
                project_id,
                a_id=a_id,
                b_id=b_id,
                type_id=d.type_id,
                role_a=d.role_a,
                role_b=d.role_b,
                category=d.category,
                intimacy=d.intimacy,
                status=d.status,
                description=d.description,
                source="manual",
            )
        )
    assignments = []
    for d in data.careers:
        career = await career_service.get_project_career(db, project_id, d.career_id)
        assignments.append(
            await career_service.assign_new(db, character, career, stage_id=d.stage_id, note=d.note, source="ai")
        )
    await _commit(db)
    await db.refresh(character)
    for a in assignments:
        await db.refresh(a)
    return {
        "character": CharacterOut.model_validate(character),
        "relationships": await _refreshed_outs(db, rels, character.id),
        "careers": await career_service.assignment_outs(db, assignments),
    }


async def relations_among(db: AsyncSession, character_ids: list[int]) -> list[tuple[Relationship, RelationshipType]]:
    """两端都在 character_ids 内的关系（上下文组装用）。"""
    if len(character_ids) < 2:
        return []
    rows = await db.execute(
        select(Relationship, RelationshipType)
        .join(RelationshipType, RelationshipType.id == Relationship.type_id)
        .where(
            Relationship.a_id.in_(character_ids),
            Relationship.b_id.in_(character_ids),
            Relationship.is_present.is_(True),
        )
        .order_by(Relationship.id)
    )
    return [(r, t) for r, t in rows.all()]


async def graph(db: AsyncSession, user_id: int, project_id: int) -> RelationshipGraph:
    await get_manageable_project(db, user_id, project_id)
    characters = await story_service.list_items(db, story_service.CHARACTERS, project_id)
    orgs = list(
        await db.scalars(
            select(Organization)
            .where(Organization.project_id == project_id)
            .order_by(Organization.sort, Organization.id)
        )
    )
    member_rows = (
        await db.execute(
            select(OrganizationMember.character_id, OrganizationMember.organization_id)
            .join(Organization, Organization.id == OrganizationMember.organization_id)
            .where(Organization.project_id == project_id)
            .order_by(OrganizationMember.id)
        )
    ).all()
    by_char: dict[int, list[int]] = {}
    for cid, oid in member_rows:
        by_char.setdefault(cid, []).append(oid)
    rels = (
        await db.execute(
            select(Relationship, RelationshipType)
            .join(RelationshipType, RelationshipType.id == Relationship.type_id)
            .where(Relationship.project_id == project_id, Relationship.is_present.is_(True))
            .order_by(Relationship.id)
        )
    ).all()
    return RelationshipGraph(
        nodes=[
            GraphNode(
                id=c.id,
                name=c.name,
                aliases=list(c.aliases or []),
                role_type=c.role_type,
                is_protagonist=is_protagonist(c),
                life_status=c.life_status,
                organization_ids=by_char.get(c.id, []),
            )
            for c in characters
        ],
        edges=[
            GraphEdge(
                id=r.id,
                source=r.a_id,
                target=r.b_id,
                type_id=t.id,
                category=t.category,
                role_a=t.role_a,
                role_b=t.role_b,
                label=type_label(t),
                intimacy=r.intimacy,
                status=r.status,
                description=r.description,
            )
            for r, t in rels
        ],
        organizations=[
            GraphOrganization(id=o.id, name=o.name, parent_id=o.parent_id, org_type=o.org_type) for o in orgs
        ],
    )
