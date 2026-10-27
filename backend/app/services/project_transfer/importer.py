"""项目备份导入：解析校验 → 预览（令牌缓存）→ 单事务导入为新项目。

解析（parse_backup，纯函数，CPU 密集，路由放进线程）：大小 ≤ 100MB → UTF-8 → json.loads（嵌套深度 ≤ MAX_DEPTH）→
format / version（更新的版本 400，migrate 逐级升级，目前只有 v1）→ 结构与列类型 / 长度 / 必填 → 文件内引用完整性
（每个外键都能在文件内找到，风格 ref 都能在 styles[] 中找到）。任何一项不通过 → 400（响应体 errors 列出前 20 条）。

预览（preview）：不写库；解析结果放进进程内缓存（按用户隔离，TOKEN_TTL 秒，每个用户最多 MAX_TOKENS_PER_USER 份），
返回 token、统计、风格对应方式与警告。确认导入时凭 token 取回；过期 → 400，提示重新选择文件。

导入（import_backup，单事务）：
1. 风格：预设按 preset_key 对应（缺失 → 复制为导入者的自定义风格 + 警告）；
   自定义风格在导入者库里有同名且字段全同的 → 复用，否则新建（同名不同内容加「（导入）」）。
2. 新建项目（重名时书名加「（导入）」「（导入2）」…），按注册表顺序逐表插入：由数据库分配新 id，
   外键按「旧 id → 新 id」映射改写，
   JSON / 字符串列内的 id 经 remap 改写；deferred 列（chapters.plan_id、organizations.parent_id）全部插入后回填，
   项目的 reading_progress（含章节 id）也在全部表插入后改写。
   全局关系类型按 (role_a, role_b) 对应，找不到 → 建为项目自定义类型 + 警告。
3. 知识库文档从 text 重新分块并写关键词检索单元（kb_index_service.chunk_document），记忆写检索单元
   （memory_service.index_memories，保留钉住 / 屏蔽）；向量留空。
4. 提交；导入者有默认向量模型时排队一个重建模式的 kb.index 任务。
任何一步出错整体回滚，400 并带上出错的表与行序号。PostgreSQL 序列：一律由数据库分配新 id，无需校正。
"""

import json
import math
import re
import secrets
import time
import types
import typing
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from typing import Any

from pydantic import ValidationError
from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, Integer, String, Text, func, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, BadRequestError
from app.models import KbDocument, Project, RelationshipType, StoryMemory, WritingStyle
from app.models import entities as E
from app.models.entities import PROJECT_STATUSES
from app.schemas.project import WizardProgress
from app.schemas.transfer import BackupOptions, ImportPreview, ImportResult, ImportStyleBrief
from app.services import kb_index_service, memory_service, project_service, retrieval_service
from app.services.project_transfer.exporter import FORMAT, STYLE_FIELDS, VERSION
from app.services.project_transfer.registry import PROJECT_RULE, RULES, RULES_BY_TABLE, TableRule
from app.services.project_transfer.remap import RemapContext
from app.services.style_service import FIELD_LIMITS, IMPORT_SUFFIX, NAME_LIMIT, _unique_name

MAX_BYTES = 100 * 1024 * 1024
TOO_LARGE = "备份文件过大，上限为 100MB"
MAX_DEPTH = 40
MAX_ERRORS = 20
TOKEN_TTL = 600
MAX_TOKENS_PER_USER = 2
# 全实例最多缓存的预览份数：解析后的备份可能是文件体积的数倍，超出时淘汰最早到期的（被淘汰者确认时提示重新选择文件）
MAX_TOKENS_TOTAL = 6
TOKEN_EXPIRED = "导入凭据已过期，请重新选择备份文件"
TITLE_SUFFIX = "（导入）"
TITLE_LIMIT = 200
INSERT_BATCH = 500

TABLE_LABELS: dict[str, str] = {
    "projects": "项目",
    "world_settings": "世界观",
    "characters": "角色",
    "organizations": "组织",
    "organization_members": "组织成员",
    "relationship_types": "关系类型",
    "relationships": "关系",
    "relationship_history": "关系历史",
    "careers": "职业",
    "career_stages": "职业阶段",
    "character_careers": "角色职业",
    "career_history": "职业历史",
    "outlines": "大纲",
    "chapters": "章节",
    "chapter_plans": "细纲",
    "chapter_characters": "出场角色",
    "foreshadows": "伏笔",
    "foreshadow_characters": "伏笔角色",
    "foreshadow_history": "伏笔历史",
    "generation_logs": "生成记录",
    "chapter_versions": "章节版本",
    "chapter_analyses": "章节分析",
    "analysis_changes": "分析回写条目",
    "story_memories": "剧情记忆",
    "kb_settings": "知识库设置",
    "kb_documents": "知识库文档",
    "outline_snapshots": "大纲快照",
    "inspiration_sessions": "灵感会话",
}


# 取值受限的字符串列（读取时 Out schema 为 Literal，非法值会让列表接口 500）
ENUM_COLUMNS: dict[tuple[str, str], tuple[str, ...]] = {
    ("world_settings", "category"): E.WORLD_CATEGORIES,
    ("characters", "life_status"): E.LIFE_STATUSES,
    ("organization_members", "status"): E.MEMBER_STATUSES,
    ("relationship_types", "category"): E.RELATION_CATEGORIES,
    ("relationships", "status"): E.RELATION_STATUSES,
    ("careers", "kind"): E.CAREER_KINDS,
    ("character_careers", "role"): E.CAREER_KINDS,
    ("chapters", "status"): E.CHAPTER_STATUSES,
    ("generation_logs", "kind"): E.GENERATION_KINDS,
    ("generation_logs", "status"): E.GENERATION_STATUSES,
    ("chapter_versions", "source"): E.VERSION_SOURCES,
    ("analysis_changes", "kind"): E.ANALYSIS_CHANGE_KINDS,
    ("analysis_changes", "status"): E.ANALYSIS_CHANGE_STATUSES,
    ("foreshadows", "status"): E.FORESHADOW_STATUSES,
    ("foreshadows", "source"): E.FORESHADOW_SOURCES,
    ("story_memories", "kind"): E.MEMORY_KINDS,
    ("story_memories", "origin"): E.MEMORY_ORIGINS,
    ("outline_snapshots", "reason"): E.OUTLINE_SNAPSHOT_REASONS,
    ("inspiration_sessions", "status"): E.INSPIRATION_STATUSES,
}
_INT32 = 2**31
_INT64 = 2**63


def label(table: str) -> str:
    return TABLE_LABELS.get(table, table)


@dataclass
class Backup:
    data: dict[str, Any]
    version: int
    tables: dict[str, list[dict[str, Any]]]
    styles: list[dict[str, Any]]
    project: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {t: len(self.tables.get(t) or []) for t in (r.table for r in RULES) if self.tables.get(t)}

    @property
    def options(self) -> BackupOptions:
        raw = self.data.get("options") if isinstance(self.data.get("options"), dict) else {}
        values = {k: bool(raw.get(k)) for k in BackupOptions.model_fields}
        # 以实际包含的数据为准
        for rule in RULES:
            if rule.option is not None and self.tables.get(rule.table):
                values[rule.option] = True
        return BackupOptions(**values)


# ---------------- 解析与校验 ----------------


class _Errors:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, msg: str) -> None:
        if len(self.items) < MAX_ERRORS:
            self.items.append(msg)

    def raise_if_any(self) -> None:
        if self.items:
            raise BadRequestError("备份文件校验失败：" + "；".join(self.items[:3]), extra={"errors": self.items})


def _too_deep(obj: Any, limit: int) -> bool:
    stack: list[tuple[Any, int]] = [(obj, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > limit:
            return True
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)
    return False


def migrate(data: dict[str, Any], version: int) -> dict[str, Any]:
    """按版本逐级升级到当前版本（以后新增 v2 时在这里补 v1 → v2 的转换）。"""
    steps: dict[int, Any] = {}
    while version < VERSION:
        data = steps[version](data)
        version += 1
    return data


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


@cache
def _json_shape(model: type, col_name: str) -> tuple[type | None, type | None]:
    """JSON 列按模型注解期望的容器类型与元素类型：Mapped[list[str]] → (list, str)；Mapped[Any] → (None, None)。"""
    hint = typing.get_args(typing.get_type_hints(model).get(col_name)) or (Any,)
    inner = hint[0]
    is_union = typing.get_origin(inner) in (typing.Union, types.UnionType)
    args = [a for a in typing.get_args(inner) if a is not type(None)] if is_union else []
    if args:
        inner = args[0]
    origin = typing.get_origin(inner) or inner
    if origin not in (list, dict):
        return None, None
    elem = typing.get_args(inner)[0] if origin is list and typing.get_args(inner) else None
    return origin, elem if elem in (str, int) else None


def _check_json(model: type, col_name: str, v: Any) -> str | None:
    container, elem = _json_shape(model, col_name)
    if container is None:
        return None
    if not isinstance(v, container):
        return "应为数组" if container is list else "应为对象"
    if elem is str and not all(isinstance(x, str) for x in v):
        return "应为字符串数组"
    if elem is int and not all(_is_int(x) for x in v):
        return "应为整数数组"
    return None


def _check_value(col, v: Any) -> str | None:
    """返回错误描述；None = 合法。"""
    if v is None:
        return None if col.nullable else "不能为空"
    t = col.type
    if isinstance(t, Boolean):
        return None if isinstance(v, bool) else "应为布尔值"
    if isinstance(t, Integer):
        if not _is_int(v):
            return "应为整数"
        bound = _INT64 if isinstance(t, BigInteger) else _INT32
        return None if -bound <= v < bound else "超出整数范围"
    if isinstance(t, Float):
        if not (_is_int(v) or isinstance(v, float)):
            return "应为数字"
        return None if math.isfinite(v) else "应为有限数字"
    if isinstance(t, DateTime):
        if not isinstance(v, str):
            return "应为时间字符串"
        try:
            datetime.fromisoformat(v)
        except ValueError:
            return "时间格式不正确"
        return None
    if isinstance(t, String | Text):
        if not isinstance(v, str):
            return "应为字符串"
        length = getattr(t, "length", None)
        if length is not None and len(v) > length:
            return f"超过长度上限 {length}"
        return None
    return None


def _has_default(col) -> bool:
    return col.nullable or col.default is not None or col.server_default is not None


def _check_rows(rule: TableRule, rows: Any, errors: _Errors) -> None:
    name = label(rule.table)
    if not isinstance(rows, list):
        errors.add(f"{name}（{rule.table}）应为数组")
        return
    table = rule.model.__table__
    seen: set[int] = set()
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            errors.add(f"{name}第 {i} 行应为对象")
            continue
        for col_name in rule.columns:
            col = table.columns[col_name]
            if col_name not in row:
                if col_name == "id" or not _has_default(col):
                    errors.add(f"{name}第 {i} 行缺少字段 {col_name}")
                continue
            v = row[col_name]
            problem = _check_value(col, v)
            if problem is None and v is not None:
                if isinstance(col.type, JSON):
                    problem = _check_json(rule.model, col_name, v)
                elif (allowed := ENUM_COLUMNS.get((rule.table, col_name))) is not None and v not in allowed:
                    problem = f"取值 {str(v)[:20]} 不合法"
            if problem:
                errors.add(f"{name}第 {i} 行字段 {col_name} {problem}")
        if rule.has_id and _is_int(row.get("id")):
            if row["id"] in seen:
                errors.add(f"{name}第 {i} 行的 id {row['id']} 重复")
            seen.add(row["id"])


def _id_sets(project: dict[str, Any], tables: dict[str, list[dict[str, Any]]]) -> dict[str, set[int]]:
    sets = {"projects": {project["id"]} if _is_int(project.get("id")) else set()}
    for rule in RULES:
        if rule.has_id:
            sets[rule.table] = {r["id"] for r in tables.get(rule.table) or [] if isinstance(r, dict)}
    return sets


def _check_refs(backup: Backup, errors: _Errors) -> None:
    ids = _id_sets(backup.project, backup.tables)
    refs = {s.get("ref") for s in backup.styles}
    ref = backup.project.get("default_style_ref")
    if ref is not None and ref not in refs:
        errors.add(f"项目引用的风格 {ref} 不在 styles 中")
    for rule in RULES:
        name = label(rule.table)
        for i, row in enumerate(backup.tables.get(rule.table) or [], start=1):
            for col, target in rule.fk.items():
                v = row.get(col)
                # 指向其他项目的灵感会话引用导入时置空，不算断裂
                if v is None or target == "projects":
                    continue
                if v not in ids.get(target, set()):
                    errors.add(f"{name}第 {i} 行的 {col} 引用了不存在的{label(target)} #{v}")
            if rule.table == "chapters" and row.get("style_ref") is not None and row["style_ref"] not in refs:
                errors.add(f"{name}第 {i} 行引用的风格 {row['style_ref']} 不在 styles 中")


def _check_styles(styles: Any, errors: _Errors) -> list[dict[str, Any]]:
    if styles is None:
        return []
    if not isinstance(styles, list):
        errors.add("styles 应为数组")
        return []
    out = []
    refs: set[str] = set()
    for i, s in enumerate(styles, start=1):
        if not isinstance(s, dict) or not isinstance(s.get("ref"), str) or not s["ref"]:
            errors.add(f"风格第 {i} 项缺少 ref")
            continue
        if s["ref"] in refs:
            errors.add(f"风格 ref {s['ref']} 重复")
        refs.add(s["ref"])
        if not isinstance(s.get("name"), str) or not s["name"].strip():
            errors.add(f"风格第 {i} 项缺少名称")
        if s.get("preset_key") is not None and not isinstance(s.get("preset_key"), str):
            errors.add(f"风格第 {i} 项的 preset_key 应为字符串")
        for f in FIELD_LIMITS:
            if s.get(f) is not None and not isinstance(s.get(f), str):
                errors.add(f"风格第 {i} 项字段 {f} 应为字符串")
        out.append(s)
    return out


def _reject_constant(name: str) -> Any:
    # json.loads 默认接受 NaN / Infinity（不是合法 JSON），写进库后响应编码会失败
    raise ValueError(f"不支持的数值 {name}")


def parse_backup(raw: bytes) -> Backup:
    """解析并校验备份文件（不访问数据库）。不合法 → BadRequestError。"""
    if len(raw) > MAX_BYTES:
        raise BadRequestError(TOO_LARGE)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BadRequestError("备份文件不是 UTF-8 编码的 JSON") from exc
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except RecursionError as exc:
        raise BadRequestError(f"备份文件嵌套层级过深（上限 {MAX_DEPTH} 层）") from exc
    except ValueError as exc:
        raise BadRequestError(f"备份文件不是合法的 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise BadRequestError("备份文件格式不正确：应为 JSON 对象")
    if data.get("format") != FORMAT:
        raise BadRequestError(f"不是有效的项目备份文件（format 应为 {FORMAT}）")
    version = data.get("version")
    if not _is_int(version) or version < 1:
        raise BadRequestError(f"不支持的备份版本：{version}")
    if version > VERSION:
        raise BadRequestError(f"备份来自更新的版本（v{version}，当前支持 v{VERSION}），请升级后再导入")
    if _too_deep(data, MAX_DEPTH):
        raise BadRequestError(f"备份文件嵌套层级过深（上限 {MAX_DEPTH} 层）")
    data = migrate(data, version)

    errors = _Errors()
    project = data.get("project")
    if not isinstance(project, dict):
        raise BadRequestError("备份文件格式不正确：缺少 project")
    tables = data.get("tables")
    if not isinstance(tables, dict):
        raise BadRequestError("备份文件格式不正确：缺少 tables")
    _check_rows(PROJECT_RULE, [project], errors)
    if isinstance(project.get("wizard_progress"), dict):
        try:
            WizardProgress.model_validate(project["wizard_progress"])
        except ValidationError:
            errors.add("项目字段 wizard_progress 结构不正确")
    styles = _check_styles(data.get("styles"), errors)
    warnings: list[str] = []
    unknown = sorted(k for k in tables if k not in RULES_BY_TABLE)
    if unknown:
        warnings.append(f"备份中有当前版本不认识的数据，已忽略：{'、'.join(unknown)}")
    clean: dict[str, list[dict[str, Any]]] = {}
    for rule in RULES:
        rows = tables.get(rule.table)
        if rows is None:
            continue
        _check_rows(rule, rows, errors)
        if isinstance(rows, list):
            clean[rule.table] = rows
    if rule_rows := clean.get("relationship_types"):
        for i, row in enumerate(rule_rows, start=1):
            if isinstance(row, dict) and not isinstance(row.get("global", False), bool):
                errors.add(f"关系类型第 {i} 行的 global 应为布尔值")
    errors.raise_if_any()
    backup = Backup(data=data, version=version, tables=clean, styles=styles, project=project, warnings=warnings)
    _check_refs(backup, errors)
    errors.raise_if_any()
    return backup


# ---------------- 令牌缓存 ----------------


@dataclass
class _Entry:
    user_id: int
    expires_at: float
    backup: Backup


_CACHE: dict[str, _Entry] = {}


def _now() -> float:
    return time.monotonic()


def _purge() -> None:
    now = _now()
    for token in [t for t, e in _CACHE.items() if e.expires_at <= now]:
        _CACHE.pop(token, None)


def put_token(user_id: int, backup: Backup) -> str:
    _purge()
    mine = sorted((e.expires_at, t) for t, e in _CACHE.items() if e.user_id == user_id)
    for _, token in mine[: max(0, len(mine) - MAX_TOKENS_PER_USER + 1)]:
        _CACHE.pop(token, None)
    overflow = len(_CACHE) - MAX_TOKENS_TOTAL + 1
    if overflow > 0:
        for _, token in sorted((e.expires_at, t) for t, e in _CACHE.items())[:overflow]:
            _CACHE.pop(token, None)
    token = secrets.token_urlsafe(24)
    _CACHE[token] = _Entry(user_id=user_id, expires_at=_now() + TOKEN_TTL, backup=backup)
    return token


def get_token(user_id: int, token: str) -> Backup:
    _purge()
    entry = _CACHE.get(token)
    if entry is None or entry.user_id != user_id:
        raise BadRequestError(TOKEN_EXPIRED)
    return entry.backup


def drop_token(token: str) -> None:
    _CACHE.pop(token, None)


def clear_tokens() -> None:
    _CACHE.clear()


# ---------------- 风格与书名 ----------------


@dataclass
class _StylePlan:
    ref: str
    action: str  # preset / reuse / create / copy
    name: str
    preset_key: str | None
    style_id: int | None
    values: dict[str, Any]


def _style_values(item: dict[str, Any]) -> dict[str, Any]:
    values = {"name": str(item.get("name") or "").strip()[:NAME_LIMIT] or "未命名风格"}
    for f, limit in FIELD_LIMITS.items():
        values[f] = str(item.get(f) or "")[:limit]
    return values


async def _plan_styles(db: AsyncSession, user_id: int, styles: list[dict[str, Any]]) -> list[_StylePlan]:
    presets = {
        s.preset_key: s for s in await db.scalars(select(WritingStyle).where(WritingStyle.preset_key.is_not(None)))
    }
    mine = list(await db.scalars(select(WritingStyle).where(WritingStyle.user_id == user_id)))
    taken = {s.name for s in mine}
    plans: list[_StylePlan] = []
    for item in styles:
        values = _style_values(item)
        key = item.get("preset_key")
        if key:
            preset = presets.get(key)
            if preset is not None:
                plans.append(_StylePlan(item["ref"], "preset", preset.name, key, preset.id, values))
                continue
            values["name"] = _unique_name(values["name"], taken, IMPORT_SUFFIX)
            taken.add(values["name"])
            plans.append(_StylePlan(item["ref"], "copy", values["name"], key, None, values))
            continue
        same = next((s for s in mine if all(getattr(s, f) == values[f] for f in STYLE_FIELDS)), None)
        if same is not None:
            plans.append(_StylePlan(item["ref"], "reuse", same.name, None, same.id, values))
            continue
        values["name"] = _unique_name(values["name"], taken, IMPORT_SUFFIX)
        taken.add(values["name"])
        plans.append(_StylePlan(item["ref"], "create", values["name"], None, None, values))
    return plans


def _style_warnings(plans: list[_StylePlan]) -> list[str]:
    return [
        f"预设风格「{p.preset_key}」在当前版本中不存在，已复制为自定义风格「{p.name}」"
        for p in plans
        if p.action == "copy"
    ]


async def _final_title(db: AsyncSession, user_id: int, title: str) -> str:
    title = (title or "").strip()[:TITLE_LIMIT] or "未命名作品"
    taken = set(await db.scalars(select(Project.title).where(Project.user_id == user_id)))
    if title not in taken:
        return title
    n = 1
    while True:
        tail = TITLE_SUFFIX if n == 1 else f"{TITLE_SUFFIX[:-1]}{n}{TITLE_SUFFIX[-1]}"
        candidate = title[: TITLE_LIMIT - len(tail)] + tail
        if candidate not in taken:
            return candidate
        n += 1


# ---------------- 预览 ----------------


def _missing_warnings(missing: Counter[str]) -> list[str]:
    return [f"{where}中有 {n} 处引用在备份中找不到，已置空或去掉" for where, n in sorted(missing.items())]


def _dry_run_missing(backup: Backup) -> Counter[str]:
    ids = _id_sets(backup.project, backup.tables)
    ctx = RemapContext({t: {i: i for i in s} for t, s in ids.items()}, identity=True)
    for col, fn in PROJECT_RULE.json_refs.items():
        if col in backup.project:
            fn(backup.project[col], ctx)
    for rule in RULES:
        if not (rule.json_refs or rule.str_refs):
            continue
        for row in backup.tables.get(rule.table) or []:
            for col, fn in (*rule.json_refs.items(), *rule.str_refs.items()):
                if col in row:
                    fn(row[col], ctx)
    return ctx.missing


async def _missing_global_types(db: AsyncSession, backup: Backup) -> list[tuple[str, str]]:
    rows = [r for r in backup.tables.get("relationship_types") or [] if r.get("global")]
    if not rows:
        return []
    existing = set(
        (
            await db.execute(
                select(RelationshipType.role_a, RelationshipType.role_b).where(RelationshipType.project_id.is_(None))
            )
        ).all()
    )
    return [(r["role_a"], r["role_b"]) for r in rows if (r["role_a"], r["role_b"]) not in existing]


def _global_type_warning(a: str, b: str) -> str:
    return f"预置关系类型「{a}/{b}」在当前版本中不存在，已改为项目自定义类型"


async def preview(db: AsyncSession, user_id: int, backup: Backup) -> ImportPreview:
    plans = await _plan_styles(db, user_id, backup.styles)
    warnings = list(backup.warnings)
    warnings += _style_warnings(plans)
    warnings += [_global_type_warning(a, b) for a, b in await _missing_global_types(db, backup)]
    warnings += _missing_warnings(_dry_run_missing(backup))
    title = str(backup.project.get("title") or "")
    token = put_token(user_id, backup)
    return ImportPreview(
        token=token,
        title=title,
        final_title=await _final_title(db, user_id, title),
        version=backup.version,
        exported_at=backup.data.get("exported_at") if isinstance(backup.data.get("exported_at"), str) else None,
        app_schema=backup.data.get("app_schema") if isinstance(backup.data.get("app_schema"), str) else None,
        options=backup.options,
        counts=backup.counts,
        styles=[ImportStyleBrief(name=p.name, preset_key=p.preset_key, action=p.action) for p in plans],
        warnings=warnings,
        expires_in=TOKEN_TTL,
    )


# ---------------- 导入 ----------------


class _RowError(Exception):
    def __init__(self, table: str, start: int, end: int, cause: Exception) -> None:
        super().__init__(str(cause))
        self.table, self.start, self.end, self.cause = table, start, end, cause


def _convert(col, v: Any) -> Any:
    if v is not None and isinstance(col.type, DateTime):
        return datetime.fromisoformat(v)
    return v


_ERR_LINE_RE = re.compile(r"\s+")


def _short(exc: Exception) -> str:
    orig = getattr(exc, "orig", None) or exc
    return _ERR_LINE_RE.sub(" ", str(orig)).strip()[:200]


class _Importer:
    def __init__(self, db: AsyncSession, user_id: int, backup: Backup) -> None:
        self.db = db
        self.user_id = user_id
        self.backup = backup
        self.project_id = 0
        self.maps: dict[str, dict[int, int]] = {}
        self.ctx = RemapContext(self.maps)
        self.warnings: list[str] = []
        self.style_ids: dict[str, int] = {}
        # (rule, 新 id, 列, 旧值, updated_at)
        self.deferred: list[tuple[TableRule, int, str, int, datetime | None]] = []
        self.objects: dict[str, list[Any]] = {}

    def _values(self, rule: TableRule, row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        table = rule.model.__table__
        values: dict[str, Any] = {}
        later: dict[str, int] = {}
        for col_name in rule.columns:
            if col_name == "id" or col_name not in row:
                continue
            v = row[col_name]
            if col_name in rule.fk:
                target = rule.fk[col_name]
                if col_name in rule.deferred:
                    if v is not None:
                        later[col_name] = v
                    v = None
                elif target == "projects":
                    v = self.maps["projects"].get(v) if v is not None else None
                elif v is not None:
                    v = self.maps[target][v]
            elif col_name in rule.json_refs:
                v = rule.json_refs[col_name](v, self.ctx)
            elif col_name in rule.str_refs:
                v = rule.str_refs[col_name](v, self.ctx)
            else:
                v = _convert(table.columns[col_name], v)
            values[col_name] = v
        for col_name, kind in rule.fill.items():
            values[col_name] = self.project_id if kind == "project" else self.user_id
        return values, later

    async def _styles(self) -> None:
        plans = await _plan_styles(self.db, self.user_id, self.backup.styles)
        for plan in plans:
            if plan.style_id is None:
                style = WritingStyle(user_id=self.user_id, **plan.values)
                self.db.add(style)
                await self.db.flush()
                plan.style_id = style.id
            self.style_ids[plan.ref] = plan.style_id
        self.warnings += _style_warnings(plans)

    async def _project(self, title: str | None) -> Project:
        # 项目先于其他表插入：含 id 的 JSON 列（reading_progress）留到全部表插入后由 _project_refs 改写
        row = {k: v for k, v in self.backup.project.items() if k not in PROJECT_RULE.json_refs}
        values, _ = self._values(PROJECT_RULE, row)
        values.pop("user_id", None)
        if values.get("status") not in PROJECT_STATUSES:
            values["status"] = "planning"
        values["title"] = await _final_title(
            self.db, self.user_id, title if title and title.strip() else str(self.backup.project.get("title") or "")
        )
        ref = self.backup.project.get("default_style_ref")
        project = Project(user_id=self.user_id, default_style_id=self.style_ids.get(ref) if ref else None, **values)
        self.db.add(project)
        await self.db.flush()
        self.project_id = project.id
        self.maps["projects"] = {}
        if _is_int(self.backup.project.get("id")):
            self.maps["projects"][self.backup.project["id"]] = project.id
        return project

    async def _relationship_types(self, rows: list[dict[str, Any]]) -> None:
        presets = {
            (t.role_a, t.role_b): t.id
            for t in await self.db.scalars(select(RelationshipType).where(RelationshipType.project_id.is_(None)))
        }
        custom: dict[tuple[str, str], int] = {}
        mapping: dict[int, int] = {}
        objs = []
        for row in rows:
            key = (row["role_a"], row["role_b"])
            if row.get("global"):
                if key in presets:
                    mapping[row["id"]] = presets[key]
                    continue
                self.warnings.append(_global_type_warning(*key))
            if key in custom:
                mapping[row["id"]] = custom[key]
                continue
            values, _ = self._values(RULES_BY_TABLE["relationship_types"], row)
            t = RelationshipType(project_id=self.project_id, **values)
            self.db.add(t)
            await self.db.flush()
            custom[key] = t.id
            mapping[row["id"]] = t.id
            objs.append(t)
        self.maps["relationship_types"] = mapping
        self.objects["relationship_types"] = objs

    def _hook(self, rule: TableRule, row: dict[str, Any], values: dict[str, Any]) -> None:
        if rule.table == "relationships":
            values["pair_low"], values["pair_high"] = (
                min(values["a_id"], values["b_id"]),
                max(values["a_id"], values["b_id"]),
            )
        elif rule.table == "chapters":
            ref = row.get("style_ref")
            values["style_id"] = self.style_ids.get(ref) if ref else None
            # 生成中的状态不能带到新项目（没有对应的任务）
            if values.get("status") == "generating":
                values["status"] = "draft"
        elif rule.table == "kb_documents":
            values["status"] = "ready_keyword_only"
            values["chunk_count"] = 0

    async def _table(self, rule: TableRule, rows: list[dict[str, Any]]) -> None:
        if rule.table == "outline_snapshots":
            for t in ("outlines", "chapter_plans"):
                model = RULES_BY_TABLE[t].model
                self.ctx.synthetic_base[t] = int(await self.db.scalar(select(func.max(model.id))) or 0)
        objs = []
        laters = []
        for row in rows:
            values, later = self._values(rule, row)
            self._hook(rule, row, values)
            objs.append(rule.model(**values))
            laters.append((later, values.get("updated_at")))
        for start in range(0, len(objs), INSERT_BATCH):
            batch = objs[start : start + INSERT_BATCH]
            self.db.add_all(batch)
            try:
                await self.db.flush()
            except SQLAlchemyError as exc:
                raise _RowError(rule.table, start + 1, start + len(batch), exc) from exc
        if rule.has_id:
            self.maps[rule.table] = {row["id"]: obj.id for row, obj in zip(rows, objs, strict=True)}
            for obj, (later, updated_at) in zip(objs, laters, strict=True):
                for col, old in later.items():
                    self.deferred.append((rule, obj.id, col, old, updated_at))
        self.objects[rule.table] = objs

    async def _project_refs(self) -> None:
        values = {
            col: fn(self.backup.project[col], self.ctx)
            for col, fn in PROJECT_RULE.json_refs.items()
            if col in self.backup.project
        }
        if values:
            # 显式写回原更新时间，避免 onupdate 改成导入时间
            await self.db.execute(
                update(Project)
                .where(Project.id == self.project_id)
                .values(**values, updated_at=Project.updated_at)
                .execution_options(synchronize_session=False)
            )

    async def _backfill(self) -> None:
        for rule, new_id, col, old, updated_at in self.deferred:
            target = self.maps[rule.fk[col]].get(old)
            values: dict[str, Any] = {col: target}
            if updated_at is not None:
                # 显式写回原值，避免 onupdate 把更新时间改成导入时间
                values["updated_at"] = updated_at
            model = rule.model
            await self.db.execute(
                update(model).where(model.id == new_id).values(**values).execution_options(synchronize_session=False)
            )

    async def _retrieval(self) -> None:
        docs = [d for d in self.objects.get("kb_documents") or [] if isinstance(d, KbDocument)]
        # 只读已在会话里的值（没有原值时属性已过期，访问会触发隐式查询）
        originals = {d.id: sa_inspect(d).dict.get("updated_at") for d in docs}
        for doc in docs:
            await kb_index_service.chunk_document(self.db, doc)
        memories = [m for m in self.objects.get("story_memories") or [] if isinstance(m, StoryMemory)]
        await memory_service.index_memories(self.db, self.project_id, memories)
        await self.db.flush()
        # 写分块数会触发 onupdate：把文档的更新时间改回备份中的原值
        for doc_id, updated_at in originals.items():
            if updated_at is not None:
                await self.db.execute(
                    update(KbDocument)
                    .where(KbDocument.id == doc_id)
                    .values(updated_at=updated_at)
                    .execution_options(synchronize_session=False)
                )

    async def run(self, title: str | None) -> Project:
        await self._styles()
        project = await self._project(title)
        for rule in RULES:
            rows = self.backup.tables.get(rule.table) or []
            if rule.table == "relationship_types":
                await self._relationship_types(rows)
                continue
            await self._table(rule, rows)
        await self._backfill()
        await self._project_refs()
        await self._retrieval()
        await self.db.flush()
        self.warnings += _missing_warnings(self.ctx.missing)
        return project


async def import_backup(db: AsyncSession, engine, user_id: int, backup: Backup, title: str | None) -> ImportResult:
    importer = _Importer(db, user_id, backup)
    try:
        project = await importer.run(title)
        await db.commit()
    except _RowError as exc:
        await db.rollback()
        rows = f"第 {exc.start} 行" if exc.start == exc.end else f"第 {exc.start}–{exc.end} 行"
        raise BadRequestError(
            f"导入失败：{label(exc.table)}（{exc.table}）{rows}附近出错：{_short(exc.cause)}",
            extra={"table": exc.table, "row_start": exc.start, "row_end": exc.end},
        ) from exc
    except AppError:
        await db.rollback()
        raise
    except (SQLAlchemyError, KeyError, ValueError, TypeError) as exc:
        await db.rollback()
        raise BadRequestError(f"导入失败：{_short(exc)}") from exc
    await db.refresh(project)
    retrieval_service.invalidate(project.id)
    task_id = None
    if await retrieval_service.default_embedding_config(db, user_id) is not None:
        from app.services import kb_service

        try:
            task = await kb_service.start_rebuild(db, engine, user_id, project.id)
            task_id = task.id
        except AppError as exc:
            importer.warnings.append(f"重建索引任务排队失败（{exc.detail}），可稍后在知识库页面手动重建")
    return ImportResult(
        project=await project_service.project_out(db, project),
        counts=backup.counts,
        warnings=importer.warnings,
        task_id=task_id,
    )
