"""章节分析回写设定（D3 / R4）：把分析结果转换为回写条目（analysis_changes），并负责应用、撤销、确认、拒绝。

规则：
- 模型输出变化后的绝对值；after 等于当前值时不生成条目（重复分析幂等）。
- 名字按「名字 → 别名」精确匹配本项目角色（组织 / 职业 / 阶段按名称精确匹配）；对不上 → skipped，只做提示。
- 更晚章节优先：同一 field_key 已有更晚章节的 applied 条目 → skipped「已有第 N 章的更新」。
- 常规变化自动写入（applied）：角色当前状态、已有关系的亲密度 / 状态 / 描述（每个字段一条）、境界、组织成员与职位。
  重大变化进入待确认（pending）：存活状态（life_status）、新建关系。
- 每条写入用 SAVEPOINT（begin_nested）包住；失败只把该条记为 failed，不影响其他条目。
- 写入入口：关系 relationship_service.apply_change / create_relation、境界 career_service.apply_assignment、
  成员 organization_service.apply_membership、角色状态 set_character_field（本模块）；来源 analysis，带章节号。
- 撤销：当前值仍等于 after 才经同一入口写回 before（note「撤销第 N 章分析」），否则 409「该项已被后续修改，无法撤销」。
- 确认 / 拒绝只作用于 pending；确认时按当前状态重新校验后写入。
- 伏笔（foreshadows）：按编号 ref_id 匹配本项目伏笔。回收已埋 / 部分回收的伏笔（foreshadow_resolve，fs:{id}:resolve）与
  计划中的待埋伏笔在本章埋下（foreshadow_plant_planned，fs:{id}:plant）自动写入；没有编号的新埋设（foreshadow_new，
  field_key 为空）进入待确认，确认时可修改字段（overrides），
  同项目同一实际埋设章已有同名伏笔 → 409「本章已有同名伏笔」；
  撤销已确认的新伏笔 = 删除该伏笔（已被修改 → 409）。编号无效 / 状态不符 / 回收没有编号 → skipped，只做提示。
  写入入口 foreshadow_service.apply_foreshadow。
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.models import (
    AnalysisChange,
    Career,
    CareerStage,
    ChapterAnalysis,
    Character,
    CharacterCareer,
    Foreshadow,
    Organization,
    Project,
    Relationship,
)
from app.models.entities import LIFE_STATUSES
from app.schemas.analysis import AnalysisChangeOut
from app.schemas.foreshadow import ForeshadowOverrides
from app.services import career_service, foreshadow_service, organization_service, relationship_service

logger = logging.getLogger(__name__)

SOURCE = "analysis"
CHARACTER_FIELDS = ("current_state", "life_status")
REL_FIELDS = ("intimacy", "status", "description")
PENDING_KINDS = ("life_status", "relationship_create", "foreshadow_new")
FORESHADOW_AUTO_KINDS = ("foreshadow_resolve", "foreshadow_plant_planned")
FS_NEW_TITLE_CHARS = 20
FS_DUPLICATE = "本章已有同名伏笔"
REVERT_CONFLICT = "该项已被后续修改，无法撤销"
CONFIRM_CHANGED = "该项对应的设定已被修改，无法确认"
TARGET_GONE = "对应的设定已不存在"


# ---------------- 角色状态写入入口 ----------------


async def set_character_field(db: AsyncSession, character: Character, field: str, value: str) -> str:
    """写入角色 current_state / life_status，返回写入前的值。不 commit。"""
    if field not in CHARACTER_FIELDS:
        raise ValueError(f"不支持的角色字段：{field}")
    if field == "life_status" and value not in LIFE_STATUSES:
        raise BadRequestError("存活状态不合法")
    before = getattr(character, field)
    setattr(character, field, value)
    await db.flush()
    return before


# ---------------- 名字解析 ----------------


@dataclass(frozen=True)
class _Named:
    id: int
    name: str
    aliases: tuple[str, ...]


def resolve_name(name: str, items: list[_Named], label: str = "角色") -> tuple[int | None, str]:
    """按名字、再按别名精确匹配。返回 (id, reason)；id 为空时 reason 说明原因。"""
    name = name.strip()
    by_name = [c for c in items if c.name == name]
    if len(by_name) == 1:
        return by_name[0].id, ""
    if len(by_name) > 1:
        return None, f"有多个名为「{name}」的{label}，无法确定"
    by_alias = [c for c in items if name in c.aliases]
    if len(by_alias) == 1:
        return by_alias[0].id, ""
    if len(by_alias) > 1:
        return None, f"别名「{name}」对应多个{label}，无法确定"
    return None, f"未找到名为「{name}」的{label}"


async def _fresh(db: AsyncSession, model: Any, ident: int) -> Any:
    # populate_existing：SAVEPOINT 回滚后被过期的对象重新加载，避免异步下的隐式查询
    return await db.get(model, ident, populate_existing=True)


# ---------------- 生成与应用条目 ----------------


async def _latest_applied_chapter(
    db: AsyncSession, project_id: int, field_key: str, *, exclude_id: int | None = None
) -> int | None:
    stmt = select(func.max(AnalysisChange.chapter_no)).where(
        AnalysisChange.project_id == project_id,
        AnalysisChange.field_key == field_key,
        AnalysisChange.status == "applied",
    )
    if exclude_id is not None:
        stmt = stmt.where(AnalysisChange.id != exclude_id)
    return await db.scalar(stmt)


class _Writer:
    def __init__(self, db: AsyncSession, analysis: ChapterAnalysis):
        self.db = db
        self.analysis = analysis
        self.no = analysis.chapter_no
        self.note = f"第 {analysis.chapter_no} 章分析"
        self.changes: list[AnalysisChange] = []

    def _add(self, kind: str, target: dict, field_key: str, before: Any, after: Any, status: str, reason: str = ""):
        row = AnalysisChange(
            analysis_id=self.analysis.id,
            project_id=self.analysis.project_id,
            chapter_no=self.no,
            kind=kind,
            target=target,
            field_key=field_key,
            before=before,
            after=after,
            status=status,
            reason=reason[:200],
        )
        if status == "applied":
            row.applied_at = func.now()
        self.db.add(row)
        self.changes.append(row)
        return row

    def skip(self, kind: str, target: dict, after: Any, reason: str) -> None:
        self._add(kind, target, "", None, after, "skipped", reason)

    async def plan(
        self,
        kind: str,
        target: dict,
        field_key: str,
        before: Any,
        after: Any,
        apply: Callable[[], Awaitable[Any]] | None,
    ) -> None:
        """无变化 → 不建条目；更晚章节已写入 → skipped；重大变化 → pending；否则在 SAVEPOINT 中应用。"""
        if before == after:
            return
        later = await _latest_applied_chapter(self.db, self.analysis.project_id, field_key)
        if later is not None and later > self.no:
            self._add(kind, target, field_key, before, after, "skipped", f"已有第 {later} 章的更新")
            return
        if kind in PENDING_KINDS or apply is None:
            self._add(kind, target, field_key, before, after, "pending")
            return
        try:
            async with self.db.begin_nested():
                await apply()
        except AppError as exc:
            self._add(kind, target, field_key, before, after, "failed", exc.detail)
        except Exception:  # noqa: BLE001
            logger.exception("分析 %s 回写失败：%s %s", self.analysis.id, kind, field_key)
            self._add(kind, target, field_key, before, after, "failed", "写入失败：服务器内部错误")
        else:
            self._add(kind, target, field_key, before, after, "applied")


async def apply_analysis(db: AsyncSession, analysis: ChapterAnalysis, data: dict[str, Any]) -> list[AnalysisChange]:
    """按规范化后的分析结果生成并应用回写条目（analysis 须已 flush）。不 commit。"""
    w = _Writer(db, analysis)
    pid = analysis.project_id
    rows = await db.execute(select(Character.id, Character.name, Character.aliases).where(Character.project_id == pid))
    characters = [_Named(i, n, tuple(a or [])) for i, n, a in rows.all()]
    names = {c.id: c.name for c in characters}

    for c in data.get("character_changes", []):
        cid, reason = resolve_name(c["name"], characters)
        for field in CHARACTER_FIELDS:
            value = c.get(field)
            if value is None:
                continue
            kind = "character_state" if field == "current_state" else "life_status"
            if cid is None:
                w.skip(kind, {"name": c["name"]}, value, reason)
                continue
            character = await _fresh(db, Character, cid)
            target = {"character_id": cid, "name": character.name}

            async def _apply(cid: int = cid, field: str = field, value: str = value) -> None:
                await set_character_field(db, await _fresh(db, Character, cid), field, value)

            await w.plan(kind, target, f"char:{cid}:{field}", getattr(character, field), value, _apply)

    for r in data.get("relationship_changes", []):
        a_id, a_reason = resolve_name(r["a"], characters)
        b_id, b_reason = resolve_name(r["b"], characters)
        raw_target = {"a_name": r["a"], "b_name": r["b"]}
        updates = {f: r.get(f) for f in REL_FIELDS if r.get(f) is not None}
        if a_id is None or b_id is None:
            w.skip("relationship_update", raw_target, updates or r.get("new_relation"), a_reason or b_reason)
            continue
        if a_id == b_id:
            w.skip("relationship_update", raw_target, updates or r.get("new_relation"), "不能与自己建立关系")
            continue
        rel = await relationship_service.find_pair(db, a_id, b_id)
        if rel is None:
            new = r.get("new_relation")
            if new is None:
                reason = f"「{names[a_id]}」与「{names[b_id]}」之间没有已有关系"
                w.skip("relationship_update", raw_target, updates, reason)
                continue
            after = {
                **new,
                "intimacy": updates.get("intimacy", 0),
                "status": updates.get("status", "normal"),
                "description": updates.get("description", ""),
            }
            target = {"a_id": a_id, "b_id": b_id, "a_name": names[a_id], "b_name": names[b_id]}
            await w.plan(
                "relationship_create", target, f"relpair:{min(a_id, b_id)}:{max(a_id, b_id)}", None, after, None
            )
            continue
        rel_id = rel.id
        for field, value in updates.items():
            rel = await _fresh(db, Relationship, rel_id)
            target = {
                "relationship_id": rel.id,
                "a_id": rel.a_id,
                "b_id": rel.b_id,
                "a_name": names.get(rel.a_id, ""),
                "b_name": names.get(rel.b_id, ""),
                "field": field,
            }

            async def _apply_rel(rid: int = rel.id, field: str = field, value: Any = value) -> None:
                await relationship_service.apply_change(
                    db, await _fresh(db, Relationship, rid), {field: value}, source=SOURCE, chapter_no=w.no, note=w.note
                )

            await w.plan("relationship_update", target, f"rel:{rel.id}:{field}", getattr(rel, field), value, _apply_rel)

    if data.get("career_changes"):
        careers = {c.name: c for c in await career_service.list_project_careers(db, pid)}
        for c in data["career_changes"]:
            raw_target = {"name": c["name"], "career_name": c["career"]}
            cid, reason = resolve_name(c["name"], characters)
            career = careers.get(c["career"])
            if cid is None:
                w.skip("career_stage", raw_target, {"stage": c["stage"]}, reason)
                continue
            if career is None:
                w.skip("career_stage", raw_target, {"stage": c["stage"]}, f"未找到名为「{c['career']}」的职业")
                continue
            assignment = await db.scalar(
                select(CharacterCareer).where(
                    CharacterCareer.character_id == cid, CharacterCareer.career_id == career.id
                )
            )
            if assignment is None:
                reason = f"「{names[cid]}」未挂接职业「{career.name}」"
                w.skip("career_stage", raw_target, {"stage": c["stage"]}, reason)
                continue
            stages = (await career_service.stages_of(db, [career.id]))[career.id]
            stage = next((s for s in stages if s.name == c["stage"]), None)
            if stage is None:
                reason = f"职业「{career.name}」没有名为「{c['stage']}」的阶段"
                w.skip("career_stage", raw_target, {"stage": c["stage"]}, reason)
                continue
            current = next((s for s in stages if s.id == assignment.stage_id), None)
            target = {"character_id": cid, "name": names[cid], "career_id": career.id, "career_name": career.name}
            before = _stage_value(current) if current else {"stage_id": assignment.stage_id, "stage": None}

            async def _apply_career(cid: int = cid, career_id: int = career.id, stage_id: int = stage.id) -> None:
                await career_service.apply_assignment(
                    db,
                    await _fresh(db, Character, cid),
                    await _fresh(db, Career, career_id),
                    stage_id=stage_id,
                    source=SOURCE,
                    chapter_no=w.no,
                    history_note=w.note,
                )

            await w.plan(
                "career_stage", target, f"career:{cid}:{career.id}", before, _stage_value(stage), _apply_career
            )

    if data.get("organization_changes"):
        orgs = {o.name: o for o in await db.scalars(select(Organization).where(Organization.project_id == pid))}
        for o in data["organization_changes"]:
            raw_target = {"name": o["name"], "organization_name": o["organization"]}
            cid, reason = resolve_name(o["name"], characters)
            org = orgs.get(o["organization"])
            raw_after = {k: o.get(k) for k in ("action", "position", "status")}
            if cid is None:
                w.skip("org_membership", raw_target, raw_after, reason)
                continue
            if org is None:
                w.skip("org_membership", raw_target, raw_after, f"未找到名为「{o['organization']}」的组织")
                continue
            member = await organization_service.find_membership(db, org.id, cid)
            before = organization_service.membership_snapshot(member)
            after = _membership_after(o, before)
            if after is None:
                continue
            target = {"organization_id": org.id, "organization_name": org.name, "character_id": cid, "name": names[cid]}

            async def _apply_org(org_id: int = org.id, cid: int = cid, after: dict = after) -> None:
                await organization_service.apply_membership(
                    db, await _fresh(db, Organization, org_id), await _fresh(db, Character, cid), after
                )

            await w.plan("org_membership", target, f"org:{org.id}:{cid}", before, after, _apply_org)

    if data.get("foreshadows"):
        await _plan_foreshadows(db, w, data["foreshadows"])

    await db.flush()
    return w.changes


def _fs_state(fs: Foreshadow, kind: str) -> dict[str, Any]:
    if kind == "foreshadow_resolve":
        return {"status": fs.status, "actual_resolve_chapter": fs.actual_resolve_chapter}
    return {"status": fs.status, "actual_plant_chapter": fs.actual_plant_chapter}


def _fs_new_title(item: dict[str, Any]) -> str:
    return (item.get("keyword") or item.get("text", "")[:FS_NEW_TITLE_CHARS]).strip()[:200]


async def _same_title_exists(db: AsyncSession, project_id: int, chapter_no: int, title: str) -> bool:
    rows = await db.scalars(
        select(Foreshadow.title).where(
            Foreshadow.project_id == project_id, Foreshadow.actual_plant_chapter == chapter_no
        )
    )
    return any(t.strip() == title.strip() for t in rows)


async def _plan_foreshadows(db: AsyncSession, w: _Writer, items: list[dict[str, Any]]) -> None:
    pid = w.analysis.project_id
    for item in items:
        raw_target = {"text": item.get("text", ""), "keyword": item.get("keyword", "")}
        ref_id = item.get("ref_id")
        if item["action"] == "plant" and ref_id is None:
            title = _fs_new_title(item)
            if not title or await _same_title_exists(db, pid, w.no, title):
                continue  # 本章已有同名伏笔（重复分析）：视为无变化
            after = {
                "title": title,
                "content": item.get("text", ""),
                "plant_hint": "",
                "planned_resolve_chapter": item.get("expected_resolve_chapter"),
                "importance": 3,
            }
            w._add("foreshadow_new", {"title": title}, "", None, after, "pending")
            continue
        kind = "foreshadow_resolve" if item["action"] == "resolve" else "foreshadow_plant_planned"
        if ref_id is None:
            w.skip(kind, raw_target, None, "未引用伏笔编号，无法回收")
            continue
        fs = await db.scalar(select(Foreshadow).where(Foreshadow.id == ref_id, Foreshadow.project_id == pid))
        if kind == "foreshadow_resolve":
            after = {"status": "partial" if item.get("partial") else "resolved", "actual_resolve_chapter": w.no}
            allowed = foreshadow_service.OPEN_STATUSES
        else:
            after = {"status": "planted", "actual_plant_chapter": w.no}
            allowed = ("pending",)
        if fs is not None and _fs_state(fs, kind) == after:
            continue  # 重复分析：已是该结果
        suffix = "resolve" if kind == "foreshadow_resolve" else "plant"
        field_key = f"fs:{ref_id}:{suffix}"
        if fs is not None:
            # 更晚章节优先：先于状态判断，已被更晚章节回收 / 埋下时给出更明确的原因
            later = await _latest_applied_chapter(db, pid, field_key)
            if later is not None and later > w.no:
                target = {"foreshadow_id": fs.id, "title": fs.title}
                w._add(kind, target, field_key, _fs_state(fs, kind), after, "skipped", f"已有第 {later} 章的更新")
                continue
        if fs is None or fs.status not in allowed:
            w.skip(kind, {**raw_target, "foreshadow_id": ref_id}, after, f"伏笔 #{ref_id} 不存在或状态不符")
            continue
        target = {"foreshadow_id": fs.id, "title": fs.title}

        async def _apply_fs(fid: int = fs.id, after: dict = after) -> None:
            await foreshadow_service.apply_foreshadow(
                db, await _fresh(db, Foreshadow, fid), after, source=SOURCE, chapter_no=w.no, note=w.note
            )

        await w.plan(kind, target, field_key, _fs_state(fs, kind), after, _apply_fs)


def _stage_value(stage: CareerStage) -> dict[str, Any]:
    return {"stage_id": stage.id, "stage": career_service.stage_label(stage)}


def _membership_after(change: dict[str, Any], before: dict[str, str] | None) -> dict[str, str] | None:
    """join / leave / update → 变化后的成员快照；未是成员时 leave 无意义（None）。"""
    position, status = change.get("position"), change.get("status")
    action = change.get("action") or "update"
    if action == "leave":
        if before is None:
            return None
        return {"position": before["position"], "status": status if status in ("left", "expelled", "dead") else "left"}
    if before is None or action == "join":
        return {
            "position": (position if position is not None else (before["position"] if before else ""))[:50],
            "status": status or "active",
        }
    return {
        "position": (position if position is not None else before["position"])[:50],
        "status": status or before["status"],
    }


# ---------------- 查询 / 撤销 / 确认 / 拒绝 ----------------


async def list_changes(db: AsyncSession, analysis_id: int) -> list[AnalysisChange]:
    rows = await db.scalars(
        select(AnalysisChange).where(AnalysisChange.analysis_id == analysis_id).order_by(AnalysisChange.id)
    )
    return list(rows)


async def get_owned_change(db: AsyncSession, user_id: int, change_id: int) -> AnalysisChange:
    change = await db.scalar(
        select(AnalysisChange)
        .join(Project, Project.id == AnalysisChange.project_id)
        .where(AnalysisChange.id == change_id, Project.user_id == user_id)
    )
    if change is None:
        raise NotFoundError("回写条目不存在")
    return change


async def _project_character(db: AsyncSession, project_id: int, character_id: Any) -> Character | None:
    if not isinstance(character_id, int):
        return None
    c = await db.get(Character, character_id)
    return c if c is not None and c.project_id == project_id else None


async def _project_foreshadow(db: AsyncSession, project_id: int, foreshadow_id: Any) -> Foreshadow | None:
    if not isinstance(foreshadow_id, int):
        return None
    fs = await db.get(Foreshadow, foreshadow_id)
    return fs if fs is not None and fs.project_id == project_id else None


async def _finish(db: AsyncSession, change: AnalysisChange) -> AnalysisChangeOut:
    await db.commit()
    await db.refresh(change)
    return AnalysisChangeOut.model_validate(change)


async def revert_change(db: AsyncSession, user_id: int, change_id: int) -> AnalysisChangeOut:
    change = await get_owned_change(db, user_id, change_id)
    if change.status != "applied":
        raise ConflictError("只能撤销已写入的条目")
    pid, t = change.project_id, change.target or {}
    note = f"撤销第 {change.chapter_no} 章分析"
    if change.kind in ("character_state", "life_status"):
        field = "current_state" if change.kind == "character_state" else "life_status"
        character = await _project_character(db, pid, t.get("character_id"))
        if character is None or getattr(character, field) != change.after:
            raise ConflictError(REVERT_CONFLICT)
        await set_character_field(db, character, field, change.before)
    elif change.kind == "relationship_update":
        rel = await db.get(Relationship, t.get("relationship_id") or 0)
        field = t.get("field")
        if rel is None or rel.project_id != pid or field not in REL_FIELDS or getattr(rel, field) != change.after:
            raise ConflictError(REVERT_CONFLICT)
        await relationship_service.apply_change(
            db, rel, {field: change.before}, source=SOURCE, chapter_no=change.chapter_no, note=note
        )
    elif change.kind == "relationship_create":
        rel = await db.get(Relationship, t.get("relationship_id") or 0)
        after = change.after or {}
        if (
            rel is None
            or rel.project_id != pid
            or any(
                getattr(rel, f) != after.get(f)
                for f in ("a_id", "b_id", "type_id", "intimacy", "status", "description")
            )
        ):
            raise ConflictError(REVERT_CONFLICT)
        await db.delete(rel)
        await db.flush()
    elif change.kind == "career_stage":
        character = await _project_character(db, pid, t.get("character_id"))
        career = await db.get(Career, t.get("career_id") or 0)
        assignment = None
        if character is not None and career is not None and career.project_id == pid:
            assignment = await db.scalar(
                select(CharacterCareer).where(
                    CharacterCareer.character_id == character.id, CharacterCareer.career_id == career.id
                )
            )
        if assignment is None or assignment.stage_id != (change.after or {}).get("stage_id"):
            raise ConflictError(REVERT_CONFLICT)
        await career_service.apply_assignment(
            db,
            character,
            career,
            stage_id=(change.before or {}).get("stage_id"),
            source=SOURCE,
            chapter_no=change.chapter_no,
            history_note=note,
        )
    elif change.kind == "org_membership":
        character = await _project_character(db, pid, t.get("character_id"))
        org = await db.get(Organization, t.get("organization_id") or 0)
        if character is None or org is None or org.project_id != pid:
            raise ConflictError(REVERT_CONFLICT)
        current = organization_service.membership_snapshot(
            await organization_service.find_membership(db, org.id, character.id)
        )
        if current != change.after:
            raise ConflictError(REVERT_CONFLICT)
        await organization_service.apply_membership(db, org, character, change.before)
    elif change.kind in FORESHADOW_AUTO_KINDS:
        fs = await _project_foreshadow(db, pid, t.get("foreshadow_id"))
        if fs is None or _fs_state(fs, change.kind) != change.after:
            raise ConflictError(REVERT_CONFLICT)
        await foreshadow_service.apply_foreshadow(
            db, fs, dict(change.before or {}), source=SOURCE, chapter_no=change.chapter_no, note=note
        )
    elif change.kind == "foreshadow_new":
        fs = await _project_foreshadow(db, pid, t.get("foreshadow_id"))
        if fs is None:
            raise ConflictError(REVERT_CONFLICT)
        links = (await foreshadow_service.character_links(db, [fs.id])).get(fs.id, [])
        if foreshadow_service.snapshot(fs, [c for c, _ in links]) != change.after:
            raise ConflictError(REVERT_CONFLICT)
        await db.delete(fs)
        await db.flush()
    else:  # pragma: no cover - 未知类型属数据错误
        raise ConflictError(REVERT_CONFLICT)
    change.status = "reverted"
    change.resolved_at = func.now()
    return await _finish(db, change)


async def confirm_change(
    db: AsyncSession, user_id: int, change_id: int, overrides: ForeshadowOverrides | None = None
) -> AnalysisChangeOut:
    """确认待确认条目。overrides 只对 foreshadow_new 生效（其他类型忽略）。"""
    change = await get_owned_change(db, user_id, change_id)
    if change.status != "pending":
        raise ConflictError("只能确认待确认的条目")
    pid, t = change.project_id, dict(change.target or {})
    # field_key 为空（新伏笔）不参与「更晚章节优先」
    later = await _latest_applied_chapter(db, pid, change.field_key, exclude_id=change.id) if change.field_key else None
    if later is not None and later > change.chapter_no:
        raise ConflictError(f"已有第 {later} 章的更新，无法确认")
    if change.kind == "life_status":
        character = await _project_character(db, pid, t.get("character_id"))
        if character is None:
            raise ConflictError(TARGET_GONE)
        if character.life_status != change.before:
            raise ConflictError(CONFIRM_CHANGED)
        await set_character_field(db, character, "life_status", change.after)
    elif change.kind == "relationship_create":
        a = await _project_character(db, pid, t.get("a_id"))
        b = await _project_character(db, pid, t.get("b_id"))
        if a is None or b is None:
            raise ConflictError(TARGET_GONE)
        if await relationship_service.find_pair(db, a.id, b.id) is not None:
            raise ConflictError(relationship_service.PAIR_CONFLICT)
        after = dict(change.after or {})
        rel = await relationship_service.create_relation(
            db,
            pid,
            a_id=a.id,
            b_id=b.id,
            role_a=after.get("role_a"),
            role_b=after.get("role_b"),
            category=after.get("category") or "other",
            intimacy=int(after.get("intimacy") or 0),
            status=after.get("status") or "normal",
            description=after.get("description") or "",
            source=SOURCE,
            chapter_no=change.chapter_no,
            note=f"确认第 {change.chapter_no} 章分析",
        )
        # 记录实际写入的值（类型反向匹配时 a / b 会互换），供撤销比对
        after.update(a_id=rel.a_id, b_id=rel.b_id, type_id=rel.type_id)
        change.after = after
        change.target = {**t, "relationship_id": rel.id}
    elif change.kind == "foreshadow_new":
        await _confirm_foreshadow(db, change, overrides)
    else:
        raise ConflictError("只能确认待确认的条目")
    change.status = "applied"
    change.applied_at = func.now()
    change.resolved_at = func.now()
    return await _finish(db, change)


async def _confirm_foreshadow(db: AsyncSession, change: AnalysisChange, overrides: ForeshadowOverrides | None) -> None:
    after = dict(change.after or {})
    values: dict[str, Any] = {
        "title": after.get("title") or "",
        "content": after.get("content") or "",
        "plant_hint": after.get("plant_hint") or "",
        "planned_resolve_chapter": after.get("planned_resolve_chapter"),
        "importance": after.get("importance") or 3,
    }
    if overrides is not None:
        for key in overrides.model_fields_set:
            value = getattr(overrides, key)
            if value is None and key != "planned_resolve_chapter":
                continue
            values[key] = value
    values.update(status="planted", actual_plant_chapter=change.chapter_no)
    title = (values["title"] or "").strip()
    if title and await _same_title_exists(db, change.project_id, change.chapter_no, title):
        raise ConflictError(FS_DUPLICATE)
    fs = Foreshadow(project_id=change.project_id, source=SOURCE)
    await foreshadow_service.apply_foreshadow(
        db, fs, values, source=SOURCE, chapter_no=change.chapter_no, note=f"确认第 {change.chapter_no} 章分析"
    )
    # 记录实际创建的值，供撤销比对（伏笔已被修改则不能撤销）
    change.after = foreshadow_service.snapshot(fs, list(dict.fromkeys(values.get("character_ids") or [])))
    change.target = {"title": fs.title, "foreshadow_id": fs.id}


async def reject_change(db: AsyncSession, user_id: int, change_id: int) -> AnalysisChangeOut:
    change = await get_owned_change(db, user_id, change_id)
    if change.status != "pending":
        raise ConflictError("只能拒绝待确认的条目")
    change.status = "rejected"
    change.resolved_at = func.now()
    return await _finish(db, change)
