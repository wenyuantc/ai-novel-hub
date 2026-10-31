"""关系类型、人物关系、关系历史与关系图谱。

语义（D2）：一对角色只存一条无方向记录，「a 是 b 的 role_a，b 是 a 的 role_b」。
- 从 a 的视角对方显示为 role_b（「徒弟 B」），从 b 的视角对方显示为 role_a（「师父 A」）。
- pair_low / pair_high = min / max(a_id, b_id)，(pair_low, pair_high) 唯一。
- apply_change 是关系的唯一写入入口：比较新旧值，有变化时更新并写一条历史；不 commit。
  手动编辑、卡片导入、AI 解析、向导，以及以后的 analysis-memory，都经由它写入。
"""

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Character, Organization, OrganizationMember, Relationship, RelationshipHistory, RelationshipType
from app.models.entities import RELATION_CATEGORIES, RELATION_SOURCES, RELATION_STATUSES
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
from app.services import career_service, story_service
from app.services.cast_service import is_protagonist
from app.services.project_service import get_manageable_project

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
    await get_manageable_project(db, user_id, project_id, lock=True)
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
    await get_manageable_project(db, user_id, project_id, lock=True)
    t = await db.scalar(select(RelationshipType).where(RelationshipType.id == type_id, _visible_types(project_id)))
    if t is None:
        raise NotFoundError("关系类型不存在")
    if t.project_id is None:
        raise BadRequestError("预置关系类型不能修改或删除")
    in_use = await db.scalar(select(func.count(Relationship.id)).where(Relationship.type_id == t.id))
    if in_use:
        raise ConflictError("该关系类型仍被关系使用，无法删除")
    await db.delete(t)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("该关系类型仍被关系使用，无法删除") from exc


# ---------------- 写入入口 ----------------


async def find_pair(db: AsyncSession, a_id: int, b_id: int) -> Relationship | None:
    low, high = min(a_id, b_id), max(a_id, b_id)
    return await db.scalar(select(Relationship).where(Relationship.pair_low == low, Relationship.pair_high == high))


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


async def apply_change(
    db: AsyncSession,
    rel: Relationship,
    values: dict[str, Any],
    *,
    source: str,
    chapter_no: int | None = None,
    note: str = "",
) -> RelationshipHistory | None:
    """关系唯一写入入口。rel.id 为空 = 新建（须已设置 project_id）。

    校验：两端是本项目的不同角色、类型对本项目可见、亲密度 -100～100、状态合法、角色对唯一（409）。
    有变化时更新字段并写一条历史（新建时记录全部初始值），返回历史；无变化返回 None。不 commit。
    """
    if source not in RELATION_SOURCES:
        raise ValueError(f"未知的关系来源：{source}")
    is_new = rel.id is None
    current = {f: (None if is_new else getattr(rel, f)) for f in TRACKED_FIELDS}
    merged = {f: values.get(f, _NEW_DEFAULTS.get(f) if is_new else current[f]) for f in TRACKED_FIELDS}
    unknown = set(values) - set(TRACKED_FIELDS)
    if unknown:
        raise ValueError(f"不支持的关系字段：{sorted(unknown)}")

    a_id, b_id = merged["a_id"], merged["b_id"]
    if a_id is None or b_id is None or merged["type_id"] is None:
        raise BadRequestError("关系缺少角色或类型")
    if a_id == b_id:
        raise BadRequestError("不能与自己建立关系")
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
    description = merged["description"] or ""

    existing = await find_pair(db, a_id, b_id)
    if existing is not None and existing is not rel and existing.id != rel.id:
        raise ConflictError(PAIR_CONFLICT)

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
    for f, new in (("intimacy", intimacy), ("status", merged["status"]), ("description", description)):
        if current[f] != new:
            changes[f] = [current[f], new]
    if not changes:
        return None

    rel.a_id, rel.b_id = a_id, b_id
    rel.pair_low, rel.pair_high = min(a_id, b_id), max(a_id, b_id)
    rel.type_id = new_type.id
    rel.intimacy = intimacy
    rel.status = merged["status"]
    rel.description = description
    if is_new:
        db.add(rel)
    async with _pair_conflict_guard():
        await db.flush()
        history = RelationshipHistory(
            relationship_id=rel.id, chapter_no=chapter_no, source=source, changes=changes, note=(note or "")[:200]
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
    note: str = "",
) -> Relationship:
    """解析类型（反向匹配时互换 a/b）后经 apply_change 新建。不 commit。"""
    t, flipped = await resolve_type(db, project_id, type_id=type_id, role_a=role_a, role_b=role_b, category=category)
    if flipped:
        a_id, b_id = b_id, a_id
    rel = Relationship(project_id=project_id)
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


async def get_owned_relationship(db: AsyncSession, user_id: int, project_id: int, rel_id: int) -> Relationship:
    await get_manageable_project(db, user_id, project_id)
    rel = await db.scalar(select(Relationship).where(Relationship.id == rel_id, Relationship.project_id == project_id))
    if rel is None:
        raise NotFoundError("关系不存在")
    return rel


async def list_relationships(
    db: AsyncSession, user_id: int, project_id: int, *, character_id: int | None = None
) -> list[RelationshipOut]:
    await get_manageable_project(db, user_id, project_id)
    stmt = select(Relationship).where(Relationship.project_id == project_id)
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
        "note": data.note,
    }


async def create_relationship(
    db: AsyncSession, user_id: int, project_id: int, data: RelationshipCreate
) -> RelationshipOut:
    await get_manageable_project(db, user_id, project_id, lock=True)
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
    await get_manageable_project(db, user_id, project_id, lock=True)
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
    await get_manageable_project(db, user_id, project_id, lock=True)
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
    await apply_change(db, rel, values, source="manual", chapter_no=data.chapter_no, note=data.note)
    await _commit(db)
    return (await _refreshed_outs(db, [rel]))[0]


async def delete_relationship(db: AsyncSession, user_id: int, project_id: int, rel_id: int) -> None:
    await get_manageable_project(db, user_id, project_id, lock=True)
    rel = await get_owned_relationship(db, user_id, project_id, rel_id)
    await db.delete(rel)
    await db.commit()


async def list_history(db: AsyncSession, user_id: int, project_id: int, rel_id: int) -> list[RelationshipHistoryOut]:
    rel = await get_owned_relationship(db, user_id, project_id, rel_id)
    rows = await db.scalars(
        select(RelationshipHistory)
        .where(RelationshipHistory.relationship_id == rel.id)
        .order_by(RelationshipHistory.id.desc())
    )
    return [RelationshipHistoryOut.model_validate(h) for h in rows]


async def create_character_with_relations(
    db: AsyncSession, user_id: int, project_id: int, data: CharacterWithRelations
) -> dict[str, Any]:
    """新建一个角色并同时建立勾选的关系与职业挂接（单事务）。subject_is_a 为真时新角色是 role_a 一方。"""
    await get_manageable_project(db, user_id, project_id, lock=True)
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
        .where(Relationship.a_id.in_(character_ids), Relationship.b_id.in_(character_ids))
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
            .where(Relationship.project_id == project_id)
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
