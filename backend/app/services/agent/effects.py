"""智能体业务差异与撤销；调用方负责归属校验及外层事务的提交。

快照使用列查询，避免数据库级联或 Core UPDATE 后读到 identity map 里的旧值。
正文只保存 hash；局部替换通过版本恢复。唯一会被工具删除的是空章节，
因此只为这些章节附带旧版本，完整恢复删除细纲产生的级联影响。
"""

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import DateTime, and_, delete, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, BadRequestError, ConflictError
from app.models import (
    Base,
    Chapter,
    ChapterPlan,
    ChapterVersion,
    Project,
    RelationshipType,
    RetrievalChunk,
    StoryMemory,
    Task,
)
from app.models.planning import PlanningUnit
from app.services import memory_service, planning_lifecycle, project_lifecycle, retrieval_service, version_service
from app.services.chapter_service import count_words
from app.services.project_transfer.registry import PLANNING_TABLES, PROJECT_RULE, RULES, TableRule

Snapshot = dict[str, dict[str, dict[str, Any]]]
Effect = dict[str, Any]

# 注册表的拓扑顺序用于插入，逆序用于删除；额外包含实际的级联 / 章号平移影响。
EFFECT_TABLES = frozenset(
    {
        "projects",
        "characters",
        "organizations",
        "organization_members",
        "relationship_types",
        "relationships",
        "relationship_history",
        "careers",
        "career_stages",
        "character_careers",
        "career_history",
        "world_settings",
        "outlines",
        "chapter_plans",
        "chapters",
        "foreshadows",
        "foreshadow_characters",
        "foreshadow_history",
        "story_memories",
        "chapter_characters",
        "chapter_analyses",
        "analysis_changes",
        "setting_timelines",
        "setting_text_fields",
        "setting_progressions",
        "chapter_versions",
        "generation_logs",
        "tasks",
        *PLANNING_TABLES,
    }
)
_RULES = (
    PROJECT_RULE,
    *(rule for rule in RULES if rule.table in EFFECT_TABLES),
    TableRule("tasks", Task, lambda pid: Task.project_id == pid),
)
_BY_TABLE = {rule.table: rule for rule in _RULES}
# created_at 创建后不变，须保留在快照中，撤销删除时才能原样恢复。
_IGNORED = frozenset({"updated_at"})
_LINK_ONLY = frozenset({"generation_logs", "tasks"})
_LABELS = {
    "projects": "项目",
    "characters": "角色",
    "organizations": "组织",
    "organization_members": "组织成员",
    "relationship_types": "关系类型",
    "relationships": "人物关系",
    "relationship_history": "关系历史",
    "careers": "职业",
    "career_stages": "职业阶段",
    "character_careers": "角色职业",
    "career_history": "职业历史",
    "world_settings": "世界观",
    "outlines": "大纲",
    "chapter_plans": "细纲",
    "chapters": "章节",
    "foreshadows": "伏笔",
    "foreshadow_characters": "伏笔角色",
    "foreshadow_history": "伏笔历史",
    "story_memories": "剧情记忆",
    "chapter_characters": "章节角色",
    "chapter_analyses": "章节分析",
    "analysis_changes": "分析回写",
    "setting_timelines": "设定时间线",
    "setting_text_fields": "设定文本字段",
    "setting_progressions": "剧情变化",
    "chapter_versions": "章节版本",
    "generation_logs": "生成记录",
    "tasks": "任务",
    "chapter_text": "章节正文",
    "planning_units": "章节规划",
    "planning_tags": "规划标签",
    "planning_unit_tags": "规划标签关联",
    "planning_unit_world": "规划世界观关联",
    "planning_unit_organizations": "规划组织关联",
    "planning_unit_careers": "规划职业关联",
    "planning_unit_relationships": "规划关系关联",
    "subplots": "支线",
    "subplot_nodes": "支线推进",
    "subplot_crossings": "支线交汇",
    "subplot_crossing_participants": "交汇参与支线",
}


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _pk(rule: TableRule, row: dict) -> dict:
    return {col.name: row[col.name] for col in rule.model.__table__.primary_key}


def _key(pk: dict) -> str:
    return str(next(iter(pk.values()))) if len(pk) == 1 else json.dumps(list(pk.values()))


def _where(rule: TableRule, pk: dict):
    table = rule.model.__table__
    return and_(*(table.c[name] == value for name, value in pk.items()))


def _columns(rule: TableRule):
    table = rule.model.__table__
    if rule.table in _LINK_ONLY:
        return [table.c[name] for name in ("id", "project_id", "chapter_id")]
    ignored = (_IGNORED | project_lifecycle.LIFECYCLE_FIELDS | {"planning_revision"}
               if rule.model is Project else _IGNORED)
    return [col for col in table.columns if col.name not in ignored and col.name != "word_count"]


def _row(rule: TableRule, mapping) -> dict:
    row = jsonable_encoder(dict(mapping))
    if rule.table == "chapters":
        content = row.pop("content")
        row["_content_hash"] = content_hash(content)
        # 空白正文也须原样恢复；非空正文只保存 hash，不保存在普通 effects 中。
        if not content.strip():
            row["_empty_content"] = content
    return row


_UNIT_DEPENDENTS = (
    "planning_unit_tags", "planning_unit_world", "planning_unit_organizations", "planning_unit_careers",
    "planning_unit_relationships", "subplot_nodes", "subplot_crossings",
)


def _planning_signature(unit: dict, snapshot_data: Snapshot) -> str:
    """派生签名让源操作的撤销也能识别后来修改的节点／交汇，不新增持久副本。"""
    data = {
        table: {key: row for key, row in snapshot_data.get(table, {}).items() if row["unit_id"] == unit["id"]}
        for table in _UNIT_DEPENDENTS
    }
    crossing_ids = {row["id"] for row in data["subplot_crossings"].values()}
    data["subplot_crossing_participants"] = {
        key: row for key, row in snapshot_data.get("subplot_crossing_participants", {}).items()
        if row["crossing_id"] in crossing_ids
    }
    if unit.get("chapter_id") is not None:
        data["source_cast"] = sorted(row["character_id"] for row in
                                     snapshot_data.get("chapter_characters", {}).values()
                                     if row["chapter_id"] == unit["chapter_id"])
    elif unit.get("plan_id") is not None:
        plan = snapshot_data.get("chapter_plans", {}).get(str(unit["plan_id"]), {})
        data["source_cast"] = sorted(plan.get("character_ids", []))
    else:
        data["source_cast"] = sorted(unit.get("detached_character_ids", []))
    return content_hash(json.dumps(data, sort_keys=True, ensure_ascii=False))


async def _current_planning_signature(db: AsyncSession, unit: dict, project_id: int) -> str:
    data: Snapshot = {}
    for name in _UNIT_DEPENDENTS:
        rule = _BY_TABLE[name]
        rows = (await db.execute(select(*_columns(rule)).where(
            rule.scope(project_id), rule.model.__table__.c.unit_id == unit["id"]
        ).with_for_update())).mappings()
        data[name] = {_key(_pk(rule, row)): _row(rule, row) for row in rows}
    crossing_ids = [row["id"] for row in data["subplot_crossings"].values()]
    rule = _BY_TABLE["subplot_crossing_participants"]
    rows = (await db.execute(select(*_columns(rule)).where(
        rule.model.__table__.c.crossing_id.in_(crossing_ids)
    ).with_for_update())).mappings()
    data[rule.table] = {_key(_pk(rule, row)): _row(rule, row) for row in rows}
    if unit.get("chapter_id") is not None:
        ids = await planning_lifecycle.character_ids(db, unit["chapter_id"])
        data["chapter_characters"] = {str(cid): {"chapter_id": unit["chapter_id"], "character_id": cid} for cid in ids}
    elif unit.get("plan_id") is not None:
        plan_ids = await db.scalar(select(ChapterPlan.character_ids).where(
            ChapterPlan.id == unit["plan_id"], ChapterPlan.project_id == project_id
        ))
        data["chapter_plans"] = {str(unit["plan_id"]): {"character_ids": plan_ids or []}}
    return _planning_signature(unit, data)


def _planning_signatures(snapshot_data: Snapshot) -> None:
    """一次分组，避免对全书每个 unit 重扫全部节点／关联。"""
    grouped: dict[int, Snapshot] = {}
    units = snapshot_data.get("planning_units", {})
    for table in _UNIT_DEPENDENTS:
        for key, row in snapshot_data.get(table, {}).items():
            grouped.setdefault(row["unit_id"], {}).setdefault(table, {})[key] = row
    crossing_units = {row["id"]: row["unit_id"] for row in snapshot_data.get("subplot_crossings", {}).values()}
    for key, row in snapshot_data.get("subplot_crossing_participants", {}).items():
        unit_id = crossing_units.get(row["crossing_id"])
        if unit_id is not None:
            grouped.setdefault(unit_id, {}).setdefault("subplot_crossing_participants", {})[key] = row
    chapters = {row["chapter_id"]: row["id"] for row in units.values() if row.get("chapter_id") is not None}
    for key, row in snapshot_data.get("chapter_characters", {}).items():
        unit_id = chapters.get(row["chapter_id"])
        if unit_id is not None:
            grouped.setdefault(unit_id, {}).setdefault("chapter_characters", {})[key] = row
    for row in units.values():
        if row.get("plan_id") is not None and row.get("chapter_id") is None:
            plan = snapshot_data.get("chapter_plans", {}).get(str(row["plan_id"]))
            if plan is not None:
                grouped.setdefault(row["id"], {})["chapter_plans"] = {str(row["plan_id"]): plan}
        row["_planning_content_hash"] = _planning_signature(row, grouped.get(row["id"], {}))


async def snapshot(db: AsyncSession, project_id: int) -> Snapshot:
    """仅查询当前项目；返回可直接 JSON 序列化的独立数据，不缓存 ORM 实例。"""
    await db.flush()
    result: Snapshot = {}
    for rule in _RULES:
        scope = rule.scope(project_id)
        if rule.table == "relationship_types":
            scope = RelationshipType.project_id == project_id  # 全局预置只读
        if rule.table == "chapter_versions":
            empty_ids = [int(key) for key, row in result["chapters"].items() if "_empty_content" in row]
            scope = and_(scope, ChapterVersion.chapter_id.in_(empty_ids))
        rows = (await db.execute(select(*_columns(rule)).where(scope))).mappings()
        result[rule.table] = {_key(_pk(rule, row)): _row(rule, row) for row in rows}
    _planning_signatures(result)
    return result


def diff(before: Snapshot, after: Snapshot) -> list[Effect]:
    """章节版本只跟随被删除的空章节恢复；文本修改产生的版本本身保留为历史。"""
    removed_chapters = set(before.get("chapters", {})) - set(after.get("chapters", {}))
    result = []
    for rule in _RULES:
        old, new = before.get(rule.table, {}), after.get(rule.table, {})
        for key in sorted(old.keys() | new.keys()):
            previous, current = old.get(key), new.get(key)
            if rule.table == "chapter_versions":
                if previous is None or str(previous["chapter_id"]) not in removed_chapters:
                    continue
            if rule.table in _LINK_ONLY and (previous is None or current is None):
                continue
            if previous != current:
                result.append(
                    {
                        "table": rule.table,
                        "pk": _pk(rule, previous or current),
                        "before": deepcopy(previous),
                        "after": deepcopy(current),
                        "format_version": 2,
                    }
                )
    return result


def chapter_text_effect(
    chapter_id: int, before_version_id: int, after_version_id: int, before_hash: str, after_hash: str
) -> Effect:
    return {
        "table": "chapter_text",
        "pk": {"id": chapter_id},
        "before": {"version_id": before_version_id, "hash": before_hash},
        "after": {"version_id": after_version_id, "hash": after_hash},
        "format_version": 2,
    }


_LEGACY_SETTING_TABLES = frozenset(
    {
        "characters",
        "world_settings",
        "organizations",
        "organization_members",
        "relationship_types",
        "relationships",
        "relationship_history",
        "careers",
        "career_stages",
        "character_careers",
        "career_history",
        "chapter_analyses",
        "analysis_changes",
        "setting_timelines",
        "setting_text_fields",
        "setting_progressions",
    }
)


def _legacy_affects_timeline(effect: Effect) -> bool:
    table = effect.get("table")
    if table in _LEGACY_SETTING_TABLES:
        return True
    if table not in {"chapters", "outlines", "chapter_plans"}:
        return False
    before, after = effect.get("before"), effect.get("after")
    if before is None or after is None:
        return True
    position_fields = {
        "chapters": ("chapter_no",),
        "outlines": ("chapter_no",),
        "chapter_plans": ("chapter_id", "outline_id", "seq"),
    }[table]
    return any(before.get(field) != after.get(field) for field in position_fields)


def validate_effect_versions(effects: list[Effect]) -> None:
    """旧快照缺少基线和变化记录时，不可用 Core 恢复静默破坏时间线。"""
    for effect in effects:
        version = effect.get("format_version", 1)
        if type(version) is not int or version not in (1, 2):
            raise ConflictError("该智能体修改的效果版本不受支持", extra={"code": "unsupported_effect_version"})
        if version == 1 and _legacy_affects_timeline(effect):
            raise ConflictError(
                "旧智能体修改缺少设定时间线信息，请在基础设定中手动修正",
                extra={"code": "legacy_effect_requires_manual_correction"},
            )


def _conflict(effect: Effect, reason: str = "执行后已被修改") -> dict:
    return {"table": effect["table"], "label": f"{_LABELS.get(effect['table'], effect['table'])}：{reason}"}


def _raise_conflicts(conflicts: list[dict]) -> None:
    raise ConflictError("数据已变化，无法撤销，请检查冲突", extra={"code": "undo_conflict", "conflicts": conflicts})


async def _current(db: AsyncSession, rule: TableRule, pk: dict, project_id: int) -> dict | None:
    row = (
        (await db.execute(select(*_columns(rule)).where(_where(rule, pk), rule.scope(project_id)).with_for_update()))
        .mappings()
        .first()
    )
    out = _row(rule, row) if row is not None else None
    if out is not None and rule.table == "planning_units":
        out["_planning_content_hash"] = await _current_planning_signature(db, out, project_id)
    return out


async def _check_text(db: AsyncSession, project_id: int, effect: Effect) -> bool:
    chapter_id = effect["pk"]["id"]
    chapter = (
        await db.execute(
            select(Chapter.content, Chapter.status)
            .where(Chapter.id == chapter_id, Chapter.project_id == project_id)
            .with_for_update()
        )
    ).first()
    if chapter is None or chapter.status == "generating" or content_hash(chapter.content) != effect["after"]["hash"]:
        return False
    for side in ("before", "after"):
        version = await db.scalar(
            select(ChapterVersion.content).where(
                ChapterVersion.id == effect[side]["version_id"], ChapterVersion.chapter_id == chapter_id
            )
        )
        if version is None or content_hash(version) != effect[side]["hash"]:
            return False
    return True


async def _check_dependents(db: AsyncSession, project_id: int, effects: list[Effect]) -> list[dict]:
    """禁止删除本次新建实体时悄悄级联删除 / 解除其后来新增的关联。"""
    changes = {(e["table"], _key(e["pk"])): e for e in effects if e["table"] != "chapter_text"}
    conflicts = []
    for effect in effects:
        if effect["before"] is not None or effect["table"] == "chapter_text":
            continue
        for table in Base.metadata.tables.values():
            for fk in table.foreign_keys:
                if fk.column.table.name != effect["table"]:
                    continue
                value = effect["pk"].get(fk.column.name)
                rows = (
                    await db.execute(select(*table.primary_key.columns).where(fk.parent == value).with_for_update())
                ).mappings()
                for row in rows:
                    child = changes.get((table.name, _key(dict(row))))
                    # 同次插入会被一起删除；既有行只有原始外键不同才允许解除引用。
                    if child is not None and (child["before"] is None or child["before"].get(fk.parent.name) != value):
                        continue
                    conflicts.append(_conflict(effect, "新增了关联记录"))
                    break
        # 细纲的选角存在 JSON 数组中，没有数据库外键；撤销新建角色同样不能留下悬空引用。
        if effect["table"] == "characters":
            character_id = effect["pk"]["id"]
            plans = await db.execute(
                select(ChapterPlan.id, ChapterPlan.character_ids).where(ChapterPlan.project_id == project_id)
            )
            for plan_id, character_ids in plans:
                if character_id not in character_ids:
                    continue
                plan = changes.get(("chapter_plans", str(plan_id)))
                if plan is None or (plan["before"] is not None and character_id in plan["before"]["character_ids"]):
                    conflicts.append(_conflict(effect, "细纲新增了角色引用"))
                    break
            for unit_id, character_ids in await db.execute(select(
                PlanningUnit.id, PlanningUnit.detached_character_ids
            ).where(PlanningUnit.project_id == project_id)):
                if character_id not in character_ids:
                    continue
                unit = changes.get(("planning_units", str(unit_id)))
                if unit is None or (unit["before"] is not None and character_id in
                                    unit["before"].get("detached_character_ids", [])):
                    conflicts.append(_conflict(effect, "失去来源的规划新增了角色引用"))
                    break
    return conflicts


async def _validate(db: AsyncSession, project_id: int, effects: list[Effect]) -> None:
    conflicts = []
    for effect in effects:
        name, before, after = effect["table"], effect["before"], effect["after"]
        if name == "chapter_text":
            if not await _check_text(db, project_id, effect):
                conflicts.append(_conflict(effect, "正文、版本已变化或章节正在生成"))
            continue
        rule = _BY_TABLE.get(name)
        if rule is None or any(
            row is not None and row.get("project_id", project_id) != project_id for row in (before, after)
        ):
            conflicts.append(_conflict(effect, "记录不属于当前项目"))
            continue
        current = await _current(db, rule, effect["pk"], project_id)
        expected_after = after
        if name == "world_settings" and after is not None and "aliases" not in after:
            # 0033前的v2效果没有该列；只接受仍为迁移默认值的记录，作者新增别名后必须冲突。
            expected_after = {**after, "aliases": []}
        # 恢复删除行时必须检查全表主键，不能只看项目范围（id 可能已被其他项目占用）。
        if after is None:
            exists = await db.scalar(
                select(*rule.model.__table__.primary_key.columns).where(_where(rule, effect["pk"])).limit(1)
            )
            if exists is not None:
                conflicts.append(_conflict(effect, "原主键已被占用"))
        elif current != expected_after:
            conflicts.append(_conflict(effect))
        if name == "projects" and (before is None or after is None):
            conflicts.append(_conflict(effect, "不支持创建或删除项目"))
        if name == "chapters" and current is not None and current["status"] == "generating":
            conflicts.append(_conflict(effect, "章节正在生成"))
    if not conflicts:
        conflicts.extend(await _check_dependents(db, project_id, effects))
    if conflicts:
        _raise_conflicts(conflicts)


def _values(rule: TableRule, row: dict) -> dict:
    values = {name: value for name, value in row.items() if name in rule.model.__table__.columns}
    for column in rule.model.__table__.columns:
        if isinstance(column.type, DateTime) and isinstance(values.get(column.name), str):
            values[column.name] = datetime.fromisoformat(values[column.name])
    if rule.table == "chapters" and "_empty_content" in row:
        values["content"] = row["_empty_content"]
        values["word_count"] = 0
    if rule.table == "chapter_versions":
        values["word_count"] = count_words(values["content"])
    if rule.table == "projects":
        values.pop("planning_revision", None)
    return values


async def _restore_rows(db: AsyncSession, effects: list[Effect]) -> None:
    by_table = {rule.table: [e for e in effects if e["table"] == rule.table] for rule in _RULES}
    removing = {(e["table"], name, value) for e in effects if e["before"] is None for name, value in e["pk"].items()}
    next_epochs = {}
    for effect in by_table.get("planning_units", []):
        before, after = effect["before"], effect["after"]
        if before is not None and (after is None or any(
            before.get(field) != after.get(field) for field in ("chapter_id", "plan_id", "retired", "binding_epoch")
        )):
            next_epochs[before["id"]] = max(before.get("binding_epoch", 0), (after or {}).get("binding_epoch", 0)) + 1

    def values_before(rule, effect):
        values = _values(rule, effect["before"])
        if rule.table == "planning_units" and values["id"] in next_epochs:
            values["binding_epoch"] = next_epochs[values["id"]]
        return values
    # 自动承接可能退役一个空 unit；反向恢复时先统一释放来源，不能依赖主键
    # 顺序让原 unit 抢回仍由 survivor 占用的唯一章／细纲。已完成 _validate。
    rebound_units = [effect["pk"]["id"] for effect in by_table.get("planning_units", [])
                     if effect["before"] is not None and effect["after"] is not None and any(
                         effect["before"].get(field) != effect["after"].get(field)
                         for field in ("chapter_id", "plan_id")
                     )]
    if rebound_units:
        await db.execute(update(PlanningUnit.__table__).where(PlanningUnit.id.in_(rebound_units))
                         .values(chapter_id=None, plan_id=None))
    # 先解除既有行对新建父记录的引用，避免删除父记录时把既有行一起级联删掉。
    for rule in _RULES:
        for effect in by_table[rule.table]:
            if effect["before"] is None or effect["after"] is None:
                continue
            values = {}
            for fk in rule.model.__table__.foreign_keys:
                if rule.table == "planning_units" and fk.parent.name in {"chapter_id", "plan_id"}:
                    continue  # 来源在上面统一释放，最后统一恢复。
                value = effect["after"].get(fk.parent.name)
                if (fk.column.table.name, fk.column.name, value) in removing:
                    values[fk.parent.name] = effect["before"][fk.parent.name]
            if values:
                # 自定义字段与内置字段互斥，解除引用时必须同时恢复另一侧。
                if rule.table == "setting_progressions" and "custom_field_id" in values:
                    values["field_key"] = effect["before"]["field_key"]
                await db.execute(update(rule.model.__table__).where(_where(rule, effect["pk"])).values(**values))
    for rule in reversed(_RULES):
        for effect in by_table[rule.table]:
            if effect["before"] is None:
                await db.execute(delete(rule.model.__table__).where(_where(rule, effect["pk"])))
    deferred = []
    for rule in _RULES:
        for effect in by_table[rule.table]:
            if effect["before"] is None or effect["after"] is not None:
                continue
            values = values_before(rule, effect)
            refs = {name: values.pop(name) for name in rule.deferred if values.get(name) is not None}
            if refs:
                deferred.append((rule, effect["pk"], refs))
                values.update(dict.fromkeys(refs))
            await db.execute(rule.model.__table__.insert().values(**values))
    for rule in _RULES:
        for effect in by_table[rule.table]:
            if effect["before"] is not None and effect["after"] is not None:
                await db.execute(
                    update(rule.model.__table__)
                    .where(_where(rule, effect["pk"]))
                    .values(**values_before(rule, effect))
                )
    for rule, pk, refs in deferred:
        await db.execute(update(rule.model.__table__).where(_where(rule, pk)).values(**refs))


async def _refresh_loaded(db: AsyncSession, effects: list[Effect]) -> None:
    """只刷新受影响实例，不能 expire_all：调用方还持有会话、提案等 ORM 对象。"""
    changes = {(e["table"], tuple(e["pk"].values())): e for e in effects if e["table"] != "chapter_text"}
    for obj in list(db.identity_map.values()):
        state = inspect(obj)
        effect = changes.get((state.mapper.local_table.name, state.identity))
        if effect is not None:
            if effect["before"] is None:
                db.expunge(obj)
            else:
                await db.refresh(obj)


async def _restore_text(db: AsyncSession, project_id: int, effect: Effect) -> None:
    chapter = await db.scalar(
        select(Chapter)
        .where(Chapter.id == effect["pk"]["id"], Chapter.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    version = await db.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.id == effect["before"]["version_id"])
        .execution_options(populate_existing=True)
    )
    chapter.content = version.content
    chapter.word_count = count_words(version.content)
    await version_service.record(db, chapter, "restore", note=f"撤销智能体修改，恢复自 v{version.seq}")


async def undo(db: AsyncSession, project_id: int, effects: list[Effect]) -> bool:
    """原子撤销一项，不 commit。返回是否需在外层提交后 enqueue_memory。

    整轮由调用方逆 seq 调用，并在任一异常时回滚外层事务。内层保存点保证即使
    还原遇到外键 / 唯一约束竞争，单项也不会留下部分改动。
    """
    validate_effect_versions(effects)
    await db.flush()
    # SQLite legacy transaction mode 的 SELECT 不会真正 BEGIN；直接 SAVEPOINT 后
    # RELEASE 会提交它，使调用方无法回滚整轮。无值变化 UPDATE 同时取得项目写锁。
    locked = await db.execute(
        update(Project)
        .where(Project.id == project_id)
        .values(id=Project.id, updated_at=Project.updated_at)
        .execution_options(synchronize_session=False)
    )
    if locked.rowcount != 1:
        _raise_conflicts([{"table": "projects", "label": "项目不存在"}])
    try:
        async with db.begin_nested():
            await _validate(db, project_id, effects)
            await _restore_rows(db, effects)
            await _refresh_loaded(db, effects)
            try:
                await planning_lifecycle.validate_project(db, project_id)
            except BadRequestError as exc:
                raise ConflictError("规划关联已变化，无法撤销", extra={"code": "undo_conflict"}) from exc
            if any(_legacy_affects_timeline(effect) for effect in effects):
                # 同步最新投影与跨主体约束，所有修改留在当前保存点和项目事务中。
                from app.services.setting_progression_service import materialize_project

                try:
                    await materialize_project(db, project_id)
                except (AppError, ValueError) as exc:
                    reason = exc.detail if isinstance(exc, AppError) else str(exc)
                    raise ConflictError(
                        "关联设定已变化，无法撤销",
                        extra={"code": "undo_conflict", "conflicts": [{"table": "setting_timelines", "label": reason}]},
                    ) from exc
            for effect in effects:
                if effect["table"] == "chapter_text":
                    await _restore_text(db, project_id, effect)
            memories = [e for e in effects if e["table"] == "story_memories"]
            if memories:
                removed = [e["pk"]["id"] for e in memories if e["before"] is None]
                await retrieval_service.delete_refs(db, "memory", removed)
                restored = [e["pk"]["id"] for e in memories if e["before"] is not None]
                rows = list(
                    await db.scalars(
                        select(StoryMemory)
                        .where(StoryMemory.id.in_(restored), StoryMemory.project_id == project_id)
                        .execution_options(populate_existing=True)
                    )
                )
                # set_flags 使用 synchronize_session=False；刷新已加载的派生行，避免
                # upsert_many 因 identity map 中的旧值误判「无需更新」。
                await db.execute(
                    select(RetrievalChunk)
                    .where(
                        RetrievalChunk.project_id == project_id,
                        RetrievalChunk.source == "memory",
                        RetrievalChunk.ref_id.in_(restored),
                    )
                    .execution_options(populate_existing=True)
                )
                await memory_service.index_memories(db, project_id, rows)
                retrieval_service.invalidate(project_id)
            await db.flush()
            # 全部恢复与跨主体校验成功后再失效规划版本；失败的保存点不应提前
            # 修改并 expire 调用方已加载的 Project，破坏后续的异步冲突处理。
            if any(effect["table"] in EFFECT_TABLES - {"projects", "generation_logs", "tasks"} for effect in effects):
                await planning_lifecycle.bump_revision(db, project_id)
    except IntegrityError as exc:
        raise ConflictError(
            "关联数据已变化，无法撤销",
            extra={"code": "undo_conflict", "conflicts": [_conflict(e, "关联记录或唯一字段已变化") for e in effects]},
        ) from exc
    return bool(memories)
