"""大纲快照（outline-pro R6 / D3）：整体性改动前保存，可一键恢复。

data = {
  "outlines": [{id, volume_no, volume_title, chapter_no, title, summary, sort}],
  "plans":    [{id, outline_id, seq, title, summary, key_events, character_ids, emotion, goal, est_words}],
  "links":    [{chapter_id, outline_id, plan_id, chapter_no}],   # 章节与大纲 / 细纲的关联，chapter_no 为保存时的章节号
}
自动保存时机：向导覆盖（wizard_replace）、导入替换（import_replace）、重新展开（reexpand）、手动（manual）、恢复前（restore）。
每个项目保留最近 MAX_SNAPSHOTS 个（保存时裁剪）。

恢复（单事务）：先为当前状态存快照（restore）→ 删除项目全部大纲（细纲级联删除，章节的 outline_id / plan_id 置空，
章节本身与正文不受影响）→ 按原始 id 重新插入大纲与细纲（原 id 已被其他行占用时改用新 id 并重映射）→
PostgreSQL 校正序列 → 按 links 恢复关联（只恢复章节仍存在、且快照中对应大纲 / 细纲存在的关联，
回填 chapter_plans.chapter_id）。
恢复不改章节号：快照保存后发生过章节号平移时，大纲的 chapter_no 以快照为准，并给出提示。
"""

from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import Chapter, ChapterPlan, Outline, OutlineSnapshot
from app.schemas.outline import (
    SnapshotDetail,
    SnapshotOut,
    SnapshotOutlineBrief,
    SnapshotRestoreResult,
)
from app.services.project_service import get_manageable_project

MAX_SNAPSHOTS = 20
NOT_FOUND = "快照不存在"
REASON_LABELS = {
    "wizard_replace": "向导覆盖前",
    "import_replace": "导入替换前",
    "reexpand": "重新展开前",
    "manual": "手动保存",
    "restore": "恢复快照前",
}
OUTLINE_FIELDS = ("volume_no", "volume_title", "chapter_no", "title", "summary", "sort")
PLAN_FIELDS = ("seq", "title", "summary", "key_events", "character_ids", "emotion", "goal", "est_words")


async def collect(db: AsyncSession, project_id: int) -> dict[str, Any]:
    """序列化项目当前的大纲、细纲与章节关联（只读列查询，不把实体载入会话）。"""
    outline_rows = await db.execute(
        select(Outline.id, *(getattr(Outline, f) for f in OUTLINE_FIELDS))
        .where(Outline.project_id == project_id)
        .order_by(Outline.volume_no, Outline.sort, Outline.chapter_no, Outline.id)
    )
    outlines = [dict(zip(("id", *OUTLINE_FIELDS), row, strict=True)) for row in outline_rows.all()]
    plan_rows = await db.execute(
        select(ChapterPlan.id, ChapterPlan.outline_id, *(getattr(ChapterPlan, f) for f in PLAN_FIELDS))
        .where(ChapterPlan.project_id == project_id)
        .order_by(ChapterPlan.outline_id, ChapterPlan.seq, ChapterPlan.id)
    )
    plans = [dict(zip(("id", "outline_id", *PLAN_FIELDS), row, strict=True)) for row in plan_rows.all()]
    link_rows = await db.execute(
        select(Chapter.id, Chapter.outline_id, Chapter.plan_id, Chapter.chapter_no)
        .where(
            Chapter.project_id == project_id,
            (Chapter.outline_id.is_not(None)) | (Chapter.plan_id.is_not(None)),
        )
        .order_by(Chapter.chapter_no, Chapter.id)
    )
    links = [
        {"chapter_id": cid, "outline_id": oid, "plan_id": pid, "chapter_no": no}
        for cid, oid, pid, no in link_rows.all()
    ]
    return {"outlines": outlines, "plans": plans, "links": links}


async def prune(db: AsyncSession, project_id: int, *, keep: frozenset[int] = frozenset()) -> None:
    """裁剪到最近 MAX_SNAPSHOTS 个；keep 中的快照（如正在恢复的目标）不被裁掉，改为裁掉其余最旧的。"""
    query = select(OutlineSnapshot.id).where(OutlineSnapshot.project_id == project_id)
    if keep:
        query = query.where(OutlineSnapshot.id.not_in(keep))
    stale = list(await db.scalars(query.order_by(OutlineSnapshot.id.desc()).offset(max(MAX_SNAPSHOTS - len(keep), 0))))
    if stale:
        await db.execute(delete(OutlineSnapshot).where(OutlineSnapshot.id.in_(stale)))


async def save_snapshot(
    db: AsyncSession, project_id: int, reason: str, note: str = "", *, keep: frozenset[int] = frozenset()
) -> OutlineSnapshot:
    """保存当前状态并裁剪到 MAX_SNAPSHOTS 个（keep 中的快照不被裁掉）。flush，不 commit。"""
    snap = OutlineSnapshot(
        project_id=project_id,
        reason=reason,
        note=(note or "").strip()[:200],
        data=await collect(db, project_id),
    )
    db.add(snap)
    await db.flush()
    await prune(db, project_id, keep=keep)
    return snap


def _counts(data: dict[str, Any]) -> tuple[int, int, int]:
    return len(data.get("outlines") or []), len(data.get("plans") or []), len(data.get("links") or [])


def to_out(snap: OutlineSnapshot) -> SnapshotOut:
    outlines, plans, links = _counts(snap.data or {})
    return SnapshotOut(
        id=snap.id,
        reason=snap.reason,  # type: ignore[arg-type]
        note=snap.note,
        created_at=snap.created_at,
        outline_count=outlines,
        plan_count=plans,
        link_count=links,
    )


async def _get(db: AsyncSession, user_id: int, project_id: int, snapshot_id: int) -> OutlineSnapshot:
    await get_manageable_project(db, user_id, project_id)
    snap = await db.scalar(
        select(OutlineSnapshot).where(OutlineSnapshot.id == snapshot_id, OutlineSnapshot.project_id == project_id)
    )
    if snap is None:
        raise NotFoundError(NOT_FOUND)
    return snap


async def list_snapshots(db: AsyncSession, user_id: int, project_id: int) -> list[SnapshotOut]:
    await get_manageable_project(db, user_id, project_id)
    rows = await db.scalars(
        select(OutlineSnapshot).where(OutlineSnapshot.project_id == project_id).order_by(OutlineSnapshot.id.desc())
    )
    return [to_out(s) for s in rows]


async def create_manual(db: AsyncSession, user_id: int, project_id: int, note: str) -> SnapshotOut:
    await get_manageable_project(db, user_id, project_id)
    snap = await save_snapshot(db, project_id, "manual", note)
    await db.commit()
    await db.refresh(snap)
    return to_out(snap)


async def delete_snapshot(db: AsyncSession, user_id: int, project_id: int, snapshot_id: int) -> None:
    snap = await _get(db, user_id, project_id, snapshot_id)
    await db.delete(snap)
    await db.commit()


async def _link_warnings(db: AsyncSession, project_id: int, links: list[dict[str, Any]]) -> list[str]:
    """快照保存后章节号是否平移过、关联的章节是否已删除。"""
    ids = [int(link["chapter_id"]) for link in links if link.get("chapter_id") is not None]
    current: dict[int, int] = {}
    if ids:
        rows = await db.execute(
            select(Chapter.id, Chapter.chapter_no).where(Chapter.project_id == project_id, Chapter.id.in_(ids))
        )
        current = dict(rows.all())
    warnings: list[str] = []
    moved = [
        (link["chapter_no"], current[link["chapter_id"]])
        for link in links
        if link.get("chapter_id") in current and link.get("chapter_no") != current[link["chapter_id"]]
    ]
    if moved:
        old, new = moved[0]
        warnings.append(
            f"快照保存后章节号发生过变化（如第 {old} 章现为第 {new} 章），恢复后大纲的章节号以快照为准，章节号不会改动"
        )
    missing = len([i for i in ids if i not in current])
    if missing:
        warnings.append(f"快照中有 {missing} 个关联的章节已被删除，这些关联不会恢复")
    return warnings


async def get_detail(db: AsyncSession, user_id: int, project_id: int, snapshot_id: int) -> SnapshotDetail:
    snap = await _get(db, user_id, project_id, snapshot_id)
    data = snap.data or {}
    plan_counts: dict[int, int] = {}
    for p in data.get("plans") or []:
        plan_counts[p["outline_id"]] = plan_counts.get(p["outline_id"], 0) + 1
    return SnapshotDetail(
        **to_out(snap).model_dump(),
        outlines=[
            SnapshotOutlineBrief(
                volume_no=o["volume_no"],
                volume_title=o["volume_title"],
                chapter_no=o["chapter_no"],
                title=o["title"],
                plan_count=plan_counts.get(o["id"], 0),
            )
            for o in data.get("outlines") or []
        ],
        warnings=await _link_warnings(db, project_id, data.get("links") or []),
    )


async def _taken_ids(db: AsyncSession, model: Any, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    return set(await db.scalars(select(model.id).where(model.id.in_(ids))))


async def _fix_sequences(db: AsyncSession) -> None:
    """PostgreSQL：显式 id 插入不推进序列，插入后把序列校正到 max(id)。SQLite 不需要。

    只前移、不回退：原 id 都由该序列分配过，序列通常已在其后；setval 不受事务保护，回退序列可能让并发事务
    （尚未提交、本事务看不到其 max(id)）的 id 与随后分配的 id 冲突。序列从未使用过（is_called=false）时也要校正。
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    for table in ("outlines", "chapter_plans"):  # 常量表名，非用户输入
        seq = await db.scalar(text(f"SELECT pg_get_serial_sequence('{table}', 'id')"))
        if seq is None:
            continue
        await db.execute(
            text(
                f"SELECT setval(CAST(:seq AS regclass), x.m) FROM (SELECT MAX(id) AS m FROM {table}) AS x, {seq} AS s "
                "WHERE x.m IS NOT NULL AND (x.m > s.last_value OR NOT s.is_called)"
            ),
            {"seq": seq},
        )


async def _insert_keeping_ids(db: AsyncSession, model: Any, objs: dict[int, Any]) -> None:
    """按原 id 插入；原 id 已被其他行占用的改用新 id。

    先插保留原 id 的行并校正序列，再插改用新 id 的行，避免自动分配的新 id 撞上随后插入的原 id。
    """
    taken = await _taken_ids(db, model, list(objs))
    for old_id, obj in objs.items():
        if old_id not in taken:
            obj.id = old_id
            db.add(obj)
    await db.flush()
    await _fix_sequences(db)
    remapped = [obj for old_id, obj in objs.items() if old_id in taken]
    if remapped:
        db.add_all(remapped)
        await db.flush()


async def restore_data(db: AsyncSession, project_id: int, data: dict[str, Any]) -> tuple[int, int, int]:
    """删除项目全部大纲后按 data 重建并恢复关联。不 commit。返回 (大纲数, 细纲数, 恢复的关联数)。"""
    # 细纲随之级联删除，章节的 outline_id / plan_id 由外键置空
    await db.execute(delete(Outline).where(Outline.project_id == project_id))

    outlines = data.get("outlines") or []
    outline_objs: dict[int, Outline] = {}
    for o in outlines:
        outline_objs[int(o["id"])] = Outline(project_id=project_id, **{f: o[f] for f in OUTLINE_FIELDS if f in o})
    await _insert_keeping_ids(db, Outline, outline_objs)

    plans = [p for p in data.get("plans") or [] if int(p["outline_id"]) in outline_objs]
    plan_objs: dict[int, ChapterPlan] = {}
    for p in plans:
        plan_objs[int(p["id"])] = ChapterPlan(
            project_id=project_id,
            outline_id=outline_objs[int(p["outline_id"])].id,
            **{f: p[f] for f in PLAN_FIELDS if f in p},
        )
    await _insert_keeping_ids(db, ChapterPlan, plan_objs)

    links = data.get("links") or []
    ids = [int(link["chapter_id"]) for link in links if link.get("chapter_id") is not None]
    chapters = (
        {
            c.id: c
            for c in await db.scalars(select(Chapter).where(Chapter.project_id == project_id, Chapter.id.in_(ids)))
        }
        if ids
        else {}
    )
    restored = 0
    used_plans: set[int] = set()
    for link in links:
        chapter = chapters.get(link.get("chapter_id"))
        if chapter is None:
            continue
        outline = outline_objs.get(link["outline_id"]) if link.get("outline_id") is not None else None
        plan = plan_objs.get(link["plan_id"]) if link.get("plan_id") is not None else None
        if plan is not None and plan.id in used_plans:
            plan = None
        if outline is None and plan is None:
            continue
        chapter.outline_id = outline.id if outline is not None else None
        chapter.plan_id = plan.id if plan is not None else None
        if plan is not None:
            plan.chapter_id = chapter.id
            used_plans.add(plan.id)
        restored += 1
    await db.flush()
    return len(outline_objs), len(plan_objs), restored


async def restore_snapshot(db: AsyncSession, user_id: int, project_id: int, snapshot_id: int) -> SnapshotRestoreResult:
    snap = await _get(db, user_id, project_id, snapshot_id)
    data = dict(snap.data or {})
    warnings = await _link_warnings(db, project_id, data.get("links") or [])
    # 被恢复的目标即使是最旧的一个也保留（否则存 backup 后的裁剪会把它删掉，用户无法再次恢复它）
    backup = await save_snapshot(db, project_id, "restore", f"恢复快照 #{snap.id} 前", keep=frozenset({snap.id}))
    outlines, plans, restored = await restore_data(db, project_id, data)
    await db.commit()
    await db.refresh(backup)
    return SnapshotRestoreResult(
        backup=to_out(backup), outlines=outlines, plans=plans, links_restored=restored, warnings=warnings
    )
