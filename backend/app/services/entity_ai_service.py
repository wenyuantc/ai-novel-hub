"""AI 单体生成角色 / 组织 / 职业、构思人物关系与解析旧关系备注。

任务只产出草稿，不写任何设定数据，也不写 generation_logs（与向导一致）。保存由前端预览确认后调用
`characters/with-relations`、`organizations/with-members`、`careers/batch`、`relationships/batch` 完成。
职业草稿与已有职业（或同批中更早的草稿）同名时标记 conflict=true，需改名后才能保存。
生成角色时若项目已有职业，提示词附上职业阶梯，草稿带 career_suggestions（按职业名 / 阶段名精确匹配）。

关系建议从「主体」（新角色 / 被解析的角色）视角给出：
- self_role = 主体的身份，target_role = 对方的身份；
- 按 (self_role, target_role) 匹配已有类型：正向匹配 subject_is_a = True（主体是 role_a 一方），
  反向匹配 subject_is_a = False；匹配不上 type.type_id = None（保存时新建自定义类型），subject_is_a = True。
- 名字按「名字 → 别名」精确匹配已有角色；匹配不上 / 有歧义 / 指向自己 / 两人已有关系 → resolved = False，附 reason。
批量关系构思单独按所选 ID 校验双方，不使用名字推断身份。
"""

import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts import (
    build_career_generate_messages,
    build_character_generate_messages,
    build_organization_generate_messages,
    build_relations_generate_messages,
    build_relations_parse_messages,
    format_project_info,
)
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.security import SecretCipher
from app.models import (
    Career,
    CareerStage,
    Character,
    KbChunk,
    KbDocument,
    KbSettings,
    Organization,
    OrganizationMember,
    Relationship,
    RelationshipType,
    WorldSetting,
)
from app.models.entities import RELATION_CATEGORIES, RELATION_STATUSES
from app.schemas.entity_ai import RelationsGenerateRequest
from app.schemas.story import MAX_ALIAS_LEN, MAX_ALIASES
from app.services import (
    ai_config_service,
    authorization_service,
    career_service,
    collaboration_ai_guard,
    kb_service,
    organization_service,
    project_access,
    project_ai_config,
    prompt_service,
    relationship_service,
    retrieval_service,
    story_service,
)
from app.services.ai_dispatch import VisibilityPolicyError, VisibilityStamp, guard_provider, visibility_sessionmaker
from app.services.ai_usage_recording import meter_provider
from app.services.cast_service import is_protagonist_role
from app.services.context_builder import MEMBER_STATUS_LABELS, format_card
from app.services.generation_service import Event
from app.services.project_service import get_execution_project, project_info
from app.services.setting_ai_context import AiScope, build_scope, current_character_text, current_world_text
from app.services.task_engine.execution_identity import TaskExecutionIdentity, require_write_execution

logger = logging.getLogger(__name__)

EntityKind = Literal[
    "character.generate", "organization.generate", "career.generate", "relations.parse", "relations.generate"
]
REFERENCE_WORLD_CHARS = 4000
REFERENCE_ROSTER_CHARS = 3000
REFERENCE_ORGS_CHARS = 6000
REFERENCE_CAREERS_CHARS = 6000
REFERENCE_CARD_CHARS = 800
REFERENCE_RELATIONS_CHARS = 6000

_CATEGORY_ALIASES = {
    "家庭": "family",
    "家人": "family",
    "亲属": "family",
    "血缘": "family",
    "社交": "social",
    "情感": "social",
    "师徒": "social",
    "友情": "social",
    "爱情": "social",
    "敌对": "hostile",
    "仇敌": "hostile",
    "敌人": "hostile",
    "职场": "work",
    "工作": "work",
    "上下级": "work",
    "其他": "other",
}


@dataclass(repr=False)
class EntityJob:
    kind: EntityKind
    project_id: int
    provider: LLMProvider
    model: str
    messages: list[ChatMessage]
    thinking: ThinkingLevel = "off"
    max_tokens: int | None = None
    # relations.parse 的主体角色
    character_id: int | None = None
    # career.generate 的 count / kind；relations.generate 的原选择、身份快照与有效 count
    options: dict[str, Any] = field(default_factory=dict)
    send_guard: collaboration_ai_guard.SendGuard | None = field(default=None, repr=False)
    result_object: Any = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class _EntitySourceGroup:
    kind: str
    parents: tuple[int, ...]
    extra: tuple[int, ...]
    rows: tuple[tuple[int, str], ...]


@dataclass(frozen=True, slots=True, repr=False)
class EntityStreamGroup:
    """独立接收进程只保留选择条件、该组摘要及复核跨书移动所需的旧行 ID。"""

    kind: str
    parents: tuple[int, ...]
    extra: tuple[int, ...]
    prior_ids: tuple[int, ...]
    fingerprint: str

    @classmethod
    def from_source(cls, group: _EntitySourceGroup) -> "EntityStreamGroup":
        ids = tuple(key for key, _ in group.rows) if group.kind in ("relation", "type") else ()
        return cls(group.kind, group.parents, group.extra, ids, _group_fingerprint(group.rows))


def _group_fingerprint(rows: tuple[tuple[int, str], ...]) -> str:
    # 输入为已排序的 ID 与 SHA-256，不携带原提示词字段。
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class EntitySourceSnapshot:
    """与原领取轮次绑定的私有提示词来源；正文只以摘要保留。"""

    execution: TaskExecutionIdentity
    groups: tuple[_EntitySourceGroup, ...]


_SOURCE_FIELDS = {
    "character": (Character, ("id", "project_id", "name", "aliases", "role_type", "profile", "relations_text",
                              "age", "gender", "appearance", "personality", "background", "current_state",
                              "life_status", "sort", "ai_mode", "ai_hidden_builtin_fields")),
    "world": (WorldSetting, ("id", "project_id", "title", "category", "content", "sort", "ai_mode",
                             "ai_hidden_builtin_fields")),
    "organization": (Organization, ("id", "project_id", "name", "org_type", "purpose", "location",
                                    "power_level", "parent_id", "description", "sort", "ai_mode",
                                    "ai_hidden_builtin_fields")),
    "career": (Career, ("id", "project_id", "name", "kind", "description", "requirements", "abilities",
                        "rules", "sort", "ai_mode", "ai_hidden_builtin_fields")),
    "stage": (CareerStage, ("id", "career_id", "position", "name", "description")),
    "member": (OrganizationMember, ("id", "organization_id", "character_id", "position", "status", "note")),
    "relation": (Relationship, ("id", "project_id", "a_id", "b_id", "pair_low", "pair_high", "type_id",
                                "intimacy", "status", "description", "is_present")),
    "type": (RelationshipType, ("id", "project_id", "category", "role_a", "role_b", "sort")),
    "document": (KbDocument, ("id", "project_id", "filename", "inject")),
    "chunk": (KbChunk, ("id", "document_id", "heading_path", "content")),
    "kb_settings": (KbSettings, ("project_id", "auto_inject", "top_k", "min_score", "max_chars")),
}


def _source_digest(kind: str, row: Any) -> str:
    fields = _SOURCE_FIELDS[kind][1]
    data = [getattr(row, name) for name in fields]
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def _source_id(kind: str, row: Any) -> int:
    return row.project_id if kind == "kb_settings" else row.id


async def _source_rows(
    db: AsyncSession, project_id: int, kind: str, parents: tuple[int, ...], extra: tuple[int, ...],
    *, for_update: bool | None,
) -> list[Any]:
    if (kind != "type" and not parents) or (kind == "member" and not extra) \
            or (kind == "relation" and len(parents) < 2):
        return []
    if kind == "kb_settings":
        query = select(KbSettings).where(KbSettings.project_id == project_id)
        if for_update is not None:
            query = query.with_for_update(read=not for_update).execution_options(populate_existing=True)
        row = await db.scalar(query)
        return [row if row is not None else kb_service.default_settings(project_id)]
    model = _SOURCE_FIELDS[kind][0]
    query = select(model)
    if kind in ("character", "world", "organization", "career", "document", "chunk"):
        query = query.where(model.id.in_(parents))
    elif kind == "stage":
        query = query.where(CareerStage.career_id.in_(parents))
    elif kind == "member":
        query = query.where(OrganizationMember.organization_id.in_(parents), OrganizationMember.character_id.in_(extra))
    elif kind == "relation":
        query = query.where(Relationship.is_present.is_(True), or_(
            and_(Relationship.a_id.in_(parents), Relationship.b_id.in_(parents)),
            and_(Relationship.pair_low.in_(parents), Relationship.pair_high.in_(parents)),
        ))
    elif kind == "type":
        query = query.where(or_(RelationshipType.project_id.is_(None), RelationshipType.project_id == project_id))
    else:
        raise ValueError("unknown entity source")
    query = query.order_by(model.id)
    if for_update is not None:
        # PG 发送使用共享行锁，写入/发布使用排他行锁；SQLite 的上层可见性门禁提供写屏障。
        query = query.with_for_update(read=not for_update).execution_options(populate_existing=True)
    return list(await db.scalars(query))


def _entity_source_changed() -> VisibilityPolicyError:
    return VisibilityPolicyError("设定来源已变化，请重新发起", extra={"code": "entity_source_changed"})


async def _capture_entity_sources(
    db: AsyncSession, execution: TaskExecutionIdentity, scope: AiScope, selected: list[Character],
) -> tuple[EntitySourceSnapshot, list[Any]]:
    if type(execution.project_id) is not int:
        raise _entity_source_changed()
    characters = tuple(sorted(scope.ids("character")))
    organizations = tuple(sorted(scope.ids("organization")))
    careers = tuple(sorted(scope.ids("career")))
    groups = [
        ("character", characters, ()), ("world", tuple(sorted(scope.ids("world"))), ()),
        ("organization", organizations, ()), ("career", careers, ()), ("stage", careers, ()),
        ("member", organizations, characters),
    ]
    if execution.kind == "relations.generate":
        groups.extend((("relation", tuple(sorted(c.id for c in selected)), ()), ("type", (), ())))
    snapshots = []
    retained: list[Any] = []
    with db.no_autoflush:
        for kind, parents, extra in groups:
            rows = await _source_rows(db, execution.project_id, kind, parents, extra, for_update=None)
            if kind in ("character", "world", "organization", "career"):
                if (tuple(row.id for row in rows) != parents
                        or any(row.project_id != execution.project_id for row in rows)):
                    raise _entity_source_changed()
            elif kind == "relation":
                if any(row.project_id != execution.project_id or row.a_id not in parents or row.b_id not in parents
                       or (row.pair_low, row.pair_high) != (min(row.a_id, row.b_id), max(row.a_id, row.b_id))
                       for row in rows):
                    raise _entity_source_changed()
            retained.extend(rows)
            snapshots.append(_EntitySourceGroup(kind, parents, extra, tuple((row.id, _source_digest(kind, row))
                                                                           for row in rows)))
    return EntitySourceSnapshot(execution, tuple(snapshots)), retained


async def require_entity_source_snapshot(
    db: AsyncSession, snapshot: EntitySourceSnapshot, *, for_update: bool, allow_changes: bool = False,
) -> None:
    """在 Task 锁后逐批锁原来源并校验集合、归属与实际使用字段。"""
    if (type(snapshot) is not EntitySourceSnapshot or type(for_update) is not bool or type(allow_changes) is not bool
            or type(snapshot.execution.project_id) is not int):
        raise _entity_source_changed()
    await _require_source_groups(db, snapshot.execution.project_id, snapshot.groups,
                                 for_update=for_update, allow_changes=allow_changes)


async def _require_source_groups(
    db: AsyncSession, project_id: int, groups: tuple[_EntitySourceGroup | EntityStreamGroup, ...], *,
    for_update: bool, allow_changes: bool,
) -> None:
    with db.no_autoflush:
        for group in groups:
            if type(group) is EntityStreamGroup and (for_update or not allow_changes):
                raise _entity_source_changed()
            rows = await _source_rows(db, project_id, group.kind, group.parents, group.extra,
                                      for_update=for_update)
            current = tuple((_source_id(group.kind, row), _source_digest(group.kind, row)) for row in rows)
            if type(group) is EntityStreamGroup:
                matches = _group_fingerprint(current) == group.fingerprint
            else:
                matches = current == group.rows
            if matches:
                continue
            if not allow_changes or group.kind not in ("relation", "type"):
                raise _entity_source_changed()
            # 已发送的提示词当时已通过原来源校验。响应后允许本书关系/类型变动，结果必须在
            # 同一屏障内重新解析；筛选集合消失的旧行可能被移至他书，须按原 ID 再锁查归属。
            prior_ids = group.prior_ids if type(group) is EntityStreamGroup else (key for key, _ in group.rows)
            missing = set(prior_ids).difference(key for key, _ in current)
            previous = []
            if missing:
                model = _SOURCE_FIELDS[group.kind][0]
                statement = select(model).where(model.id.in_(missing)).order_by(model.id)
                statement = statement.with_for_update(read=not for_update)
                previous = list(await db.scalars(statement))
            current_rows = [*rows, *previous]
            if group.kind == "relation":
                if any(row.project_id != project_id
                       or (row.pair_low, row.pair_high) != (min(row.a_id, row.b_id), max(row.a_id, row.b_id))
                       for row in current_rows):
                    raise _entity_source_changed()
                character_ids = {ident for row in current_rows for ident in (row.a_id, row.b_id)}
                character_statement = select(Character).where(Character.id.in_(character_ids))
                character_statement = character_statement.order_by(Character.id).with_for_update(read=not for_update)
                characters = list(await db.scalars(character_statement))
                if (len(characters) != len(character_ids)
                        or any(row.project_id != project_id for row in characters)):
                    raise _entity_source_changed()
                type_ids = {row.type_id for row in current_rows}
                statement = select(RelationshipType).where(RelationshipType.id.in_(type_ids))
                statement = statement.order_by(RelationshipType.id).with_for_update(read=not for_update)
                types = list(await db.scalars(statement))
                if (len(types) != len(type_ids)
                        or any(row.project_id not in (None, project_id) for row in types)):
                    raise _entity_source_changed()
            elif any(row.project_id not in (None, project_id) for row in current_rows):
                raise _entity_source_changed()


# ---------------- 规范化（纯函数） ----------------


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


def _int(value: Any, default: int | None, low: int, high: int) -> int | None:
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError):
        return default


def normalize_aliases(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [v for v in value.replace("，", ",").replace("、", ",").split(",")]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        v = _text(v)[:MAX_ALIAS_LEN]
        if v and v not in out:
            out.append(v)
    return out[:MAX_ALIASES]


def normalize_category(value: Any) -> str:
    v = _text(value).lower()
    if v in RELATION_CATEGORIES:
        return v
    for key, cat in _CATEGORY_ALIASES.items():
        if key in v:
            return cat
    return "other"


def _pick(obj: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(obj, dict):
        for k in keys:
            if k in obj:
                return obj[k]
    return None


def normalize_relation_items(value: Any) -> list[dict[str, Any]]:
    """关系建议原始项 → {target, self_role, target_role, category, intimacy, description}；缺对方或称呼的丢弃。"""
    if isinstance(value, dict):
        value = _pick(value, ("relationships", "relations", "items"))
    if not isinstance(value, list):
        return []
    rows = []
    for r in value:
        if not isinstance(r, dict):
            continue
        target = _text(r.get("target") or r.get("target_name") or r.get("name"))[:100]
        self_role = _text(r.get("self_role"))[:20]
        target_role = _text(r.get("target_role") or r.get("relation") or r.get("role"))[:20]
        self_role = self_role or target_role
        target_role = target_role or self_role
        if not target or not self_role:
            continue
        rows.append(
            {
                "target": target,
                "self_role": self_role,
                "target_role": target_role,
                "category": normalize_category(r.get("category")),
                "intimacy": _int(r.get("intimacy"), 0, -100, 100),
                "description": _text(r.get("description")),
            }
        )
    return rows


def normalize_character_draft(obj: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        obj = {"character": obj[0]}
    if not isinstance(obj, dict):
        raise JSONExtractError("JSON 结构不符合预期")
    c = obj.get("character") if isinstance(obj.get("character"), dict) else obj
    name = _text(c.get("name"))[:100]
    if not name:
        raise JSONExtractError("未解析到角色名字")
    role_type = _text(c.get("role_type") or c.get("role"))[:50]
    draft = {
        "name": name,
        "aliases": [a for a in normalize_aliases(c.get("aliases")) if a != name],
        "role_type": role_type,
        "age": _text(c.get("age"))[:50],
        "gender": _text(c.get("gender"))[:20],
        "appearance": _text(c.get("appearance")),
        "personality": _text(c.get("personality")),
        "background": _text(c.get("background")),
        "profile": _text(c.get("profile") or c.get("description")),
        "current_state": _text(c.get("current_state")),
        "life_status": "alive",
        "always_in_context": is_protagonist_role(role_type),
    }
    rels = normalize_relation_items(obj.get("relationships") or obj.get("relations") or c.get("relationships"))
    return draft, rels


def normalize_org_draft(obj: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        obj = {"organization": obj[0]}
    if not isinstance(obj, dict):
        raise JSONExtractError("JSON 结构不符合预期")
    o = obj.get("organization") if isinstance(obj.get("organization"), dict) else obj
    name = _text(o.get("name"))[:100]
    if not name:
        raise JSONExtractError("未解析到组织名称")
    draft = {
        "name": name,
        "org_type": _text(o.get("org_type") or o.get("type"))[:50],
        "purpose": _text(o.get("purpose")),
        "location": _text(o.get("location"))[:200],
        "power_level": _int(o.get("power_level"), None, 0, 100),
        "description": _text(o.get("description")),
        "parent_name": _text(o.get("parent") or o.get("parent_name"))[:100] or None,
    }
    members_raw = obj.get("members") or o.get("members") or []
    members = []
    if isinstance(members_raw, list):
        for m in members_raw:
            if isinstance(m, str):
                m = {"name": m}
            if not isinstance(m, dict):
                continue
            mname = _text(m.get("name") or m.get("character_name") or m.get("character"))[:100]
            if mname:
                members.append({"name": mname, "position": _text(m.get("position"))[:50]})
    return draft, members


def normalize_career_assignments(obj: Any) -> list[dict[str, Any]]:
    """角色草稿中的 careers → [{name, stage, role, note}]；缺职业名的丢弃。"""
    if not isinstance(obj, dict):
        return []
    c = obj.get("character") if isinstance(obj.get("character"), dict) else {}
    value = obj.get("careers") or c.get("careers")
    if not isinstance(value, list):
        return []
    rows = []
    for r in value:
        if isinstance(r, str):
            r = {"name": r}
        if not isinstance(r, dict):
            continue
        name = _text(r.get("name") or r.get("career") or r.get("career_name"))[:100]
        if not name:
            continue
        role = _text(r.get("role")).lower()
        rows.append(
            {
                "name": name,
                "stage": _text(r.get("stage") or r.get("stage_name") or r.get("level"))[:50],
                "role": role if role in ("main", "sub") else "",
                "note": _text(r.get("note"))[:200],
            }
        )
    return rows


# ---------------- 名字解析 ----------------


def resolve_name(name: str, characters: list[Character]) -> tuple[int | None, str]:
    """按名字、再按别名精确匹配。返回 (id, reason)；id 为空时 reason 说明原因。"""
    name = name.strip()
    by_name = [c for c in characters if c.name == name]
    if len(by_name) == 1:
        return by_name[0].id, ""
    if len(by_name) > 1:
        return None, f"有多个名为「{name}」的角色，无法确定"
    by_alias = [c for c in characters if name in (c.aliases or [])]
    if len(by_alias) == 1:
        return by_alias[0].id, ""
    if len(by_alias) > 1:
        return None, f"别名「{name}」对应多个角色，无法确定"
    return None, f"未找到名为「{name}」的角色"


async def relation_suggestions(
    db: AsyncSession,
    project_id: int,
    raw: list[dict[str, Any]],
    characters: list[Character],
    subject_id: int | None = None,
    *,
    lock_sources: bool = False,
) -> list[dict[str, Any]]:
    resolved = [(r, *resolve_name(r["target"], characters)) for r in raw]
    existing_pairs: dict[tuple[int, int], Relationship] = {}
    if subject_id is not None:
        ids = [subject_id, *(target for _, target, _ in resolved if target is not None and target != subject_id)]
        for relation, _ in await _relations_for_project(
            db, project_id, ids, lock_sources=lock_sources, subject_id=subject_id,
        ):
            existing_pairs[(relation.pair_low, relation.pair_high)] = relation
    out = []
    for r, target_id, reason in resolved:
        if target_id is not None and target_id == subject_id:
            target_id, reason = None, "不能与自己建立关系"
        existing_id = None
        if target_id is not None and subject_id is not None:
            existing = existing_pairs.get((min(subject_id, target_id), max(subject_id, target_id)))
            if existing is not None:
                existing_id, reason = existing.id, "两人之间已有关系"
        type_ref, subject_is_a = await _suggestion_type(
            db, project_id, r["self_role"], r["target_role"], r["category"], lock_sources=lock_sources,
        )
        out.append(
            {
                "target_name": r["target"],
                "target_id": target_id,
                "resolved": target_id is not None and existing_id is None,
                "reason": reason,
                "existing_relationship_id": existing_id,
                "self_role": r["self_role"],
                "target_role": r["target_role"],
                "subject_is_a": subject_is_a,
                "type": type_ref,
                "intimacy": r["intimacy"],
                "status": "normal",
                "description": r["description"],
            }
        )
    return out


async def _suggestion_type(
    db: AsyncSession, project_id: int, self_role: str, target_role: str, category: str, *, lock_sources: bool = False,
) -> tuple[dict[str, Any], bool]:
    """草稿只查找类型；返回引用与方向，保存前不创建类型。"""
    if lock_sources:
        rows = list(await db.scalars(select(RelationshipType).where(
            or_(RelationshipType.project_id.is_(None), RelationshipType.project_id == project_id),
            or_(
                and_(RelationshipType.role_a == self_role, RelationshipType.role_b == target_role),
                and_(RelationshipType.role_a == target_role, RelationshipType.role_b == self_role),
            ),
        ).order_by(RelationshipType.project_id.is_not(None), RelationshipType.id).with_for_update()))
        direct = next((row for row in rows if row.role_a == self_role and row.role_b == target_role), None)
        t, flipped = (direct, False) if direct is not None else ((rows[0], True) if rows else (None, False))
    else:
        t, flipped = await relationship_service.find_type(db, project_id, self_role, target_role)
    if t is not None:
        return {"type_id": t.id, "role_a": t.role_a, "role_b": t.role_b, "category": t.category}, not flipped
    return {"type_id": None, "role_a": self_role, "role_b": target_role, "category": category}, True


async def generated_relation_suggestions(
    db: AsyncSession, job: EntityJob, obj: Any, characters: list[Character], *, lock_sources: bool = False,
) -> dict[str, Any]:
    """按原选择与当前项目双重校验 ID；正反向重复、过期身份和非法字段均不能保存。"""
    if not isinstance(obj, dict) or not isinstance(obj.get("relationships"), list):
        raise JSONExtractError("JSON 结构不符合预期：缺少 relationships 数组")
    selected = set(job.options["character_ids"])
    current = {c.id: c for c in characters if c.id in selected}
    snapshots = job.options["character_names"]
    existing = {
        (min(r.a_id, r.b_id), max(r.a_id, r.b_id)): r.id
        for r, _ in await _relations_for_project(db, job.project_id, list(current), lock_sources=lock_sources)
    }
    seen: set[tuple[int, int]] = set()
    suggestions: list[dict[str, Any]] = []
    warnings: list[str] = []
    valid_count = 0
    omitted = 0
    for raw in obj["relationships"]:
        if not isinstance(raw, dict):
            warnings.append("已忽略格式不正确的关系建议")
            continue
        a = raw.get("a_id")
        b = raw.get("b_id")
        a = a if type(a) is int and a > 0 else None
        b = b if type(b) is int and b > 0 else None
        self_role, target_role = _text(raw.get("role_a")), _text(raw.get("role_b"))
        category = _text(raw.get("category"))
        intimacy, status = raw.get("intimacy"), _text(raw.get("status"))
        pair = (min(a, b), max(a, b)) if a is not None and b is not None else None
        existing_id = existing.get(pair) if pair else None
        reason = ""
        if a not in selected or b not in selected:
            reason = "关系双方必须是本次所选角色 ID"
        elif a == b:
            reason = "不能与自己建立关系"
        elif a not in current or b not in current:
            reason = "角色已被删除或不属于该项目"
        elif existing_id is not None:
            reason = "两人之间已有关系"
        elif pair in seen:
            reason = "同一角色对重复建议（含反向）"
        elif any(not isinstance(raw.get(field), str) for field in ("role_a", "role_b", "category", "status")):
            reason = "关系称呼、类别和状态必须是文本"
        elif not self_role or not target_role or len(self_role) > 20 or len(target_role) > 20:
            reason = "双方称呼不能为空且不能超过 20 字"
        elif category not in RELATION_CATEGORIES:
            reason = "关系类别无效"
        elif type(intimacy) is not int or not -100 <= intimacy <= 100:
            reason = "亲密度必须是 -100～100 的整数"
        elif status not in RELATION_STATUSES:
            reason = "关系状态无效"
        if pair is not None:
            seen.add(pair)
        if not reason and valid_count >= job.options["count"]:
            omitted += 1
            continue
        if len(suggestions) >= 50:
            omitted += 1
            continue
        if not reason:
            valid_count += 1
        type_ref, subject_is_a = await _suggestion_type(
            db, job.project_id, self_role[:20], target_role[:20], normalize_category(category),
            lock_sources=lock_sources,
        )

        def display_name(cid: int | None) -> str:
            if cid in current:
                return current[cid].name
            return snapshots.get(cid, f"未选角色（ID={cid}）" if cid is not None else "无效角色 ID")

        suggestions.append(
            {
                "subject_id": a,
                "subject_name": display_name(a),
                "target_id": b,
                "target_name": display_name(b),
                "resolved": not reason,
                "reason": reason,
                "existing_relationship_id": existing_id,
                "self_role": self_role[:20],
                "target_role": target_role[:20],
                "subject_is_a": subject_is_a,
                "type": type_ref,
                "intimacy": intimacy if type(intimacy) is int and -100 <= intimacy <= 100 else 0,
                "status": status if status in RELATION_STATUSES else "normal",
                "description": _text(raw.get("description")),
            }
        )
    if omitted:
        warnings.append(f"已忽略 {omitted} 条超出本次数量或预览上限的建议")
    return {
        "character_ids": job.options["character_ids"],
        "suggestions": suggestions,
        "warnings": list(dict.fromkeys(warnings)),
    }


async def career_suggestions(
    db: AsyncSession, project_id: int, raw: list[dict[str, Any]], *, lock_sources: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """职业建议：按职业名精确匹配；阶段名对不上时取第 1 阶并给警告；超出主 1 副 3 的标记 resolved=False。"""
    if lock_sources:
        careers = list(await db.scalars(select(Career).where(Career.project_id == project_id)
                                        .order_by(Career.sort, Career.id).with_for_update()))
    else:
        careers = await career_service.list_project_careers(db, project_id)
    if not careers:
        return [], []
    if lock_sources:
        stages: dict[int, list[CareerStage]] = {c.id: [] for c in careers}
        rows = await db.scalars(select(CareerStage).where(CareerStage.career_id.in_(stages))
                                .order_by(CareerStage.career_id, CareerStage.position, CareerStage.id)
                                .with_for_update())
        for stage in rows:
            stages[stage.career_id].append(stage)
    else:
        stages = await career_service.stages_of(db, [c.id for c in careers])
    by_name = {c.name: c for c in careers}
    counts = {"main": 0, "sub": 0}
    limits = {"main": career_service.MAX_MAIN_CAREERS, "sub": career_service.MAX_SUB_CAREERS}
    out: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[int] = set()
    for r in raw:
        career = by_name.get(r["name"].strip())
        item: dict[str, Any] = {
            "career_name": r["name"],
            "career_id": None,
            "career_kind": None,
            "role": r["role"] or None,
            "stage_name": r["stage"],
            "stage_id": None,
            "stage_position": None,
            "note": r["note"],
            "resolved": False,
            "reason": "",
        }
        if career is None:
            item["reason"] = f"未找到名为「{r['name']}」的职业"
            out.append(item)
            continue
        if career.id in seen:
            continue
        seen.add(career.id)
        ladder = stages.get(career.id, [])
        stage = next((s for s in ladder if s.name == r["stage"]), None)
        if stage is None and ladder:
            stage = ladder[0]
            if r["stage"]:
                warnings.append(f"职业「{career.name}」没有名为「{r['stage']}」的阶段，已改为第 1 阶「{stage.name}」")
        item.update(career_id=career.id, career_kind=career.kind, role=career.kind)
        if stage is not None:
            item.update(stage_id=stage.id, stage_name=stage.name, stage_position=stage.position)
        if stage is None:
            item["reason"] = "该职业没有阶段"
        elif counts[career.kind] >= limits[career.kind]:
            item["reason"] = career_service.MAIN_LIMIT if career.kind == "main" else career_service.SUB_LIMIT
        else:
            counts[career.kind] += 1
            item["resolved"] = True
        out.append(item)
    return out, warnings


# ---------------- 校验 / 组装 ----------------


async def create_entity_task(db: AsyncSession, engine, user_id: int, kind: str, params: dict[str, Any]):
    """任何写锁前完成能力短读、模型预检与 DNS（受理证明），再交中央受理在同一写事务内消费。

    作者与 accepted editor 共用（E4-g3b，同 E4-g3a 大纲入口）：本人模型优先，仅本人无文本配置且作者授权时
    回退作者配置，本人配置无效不回退；viewer 403、待接受/非成员/已撤权/仅管理员身份 404，均不建任务、不调用模型。
    """
    from app.services import task_service
    from app.services.task_engine.admission import prepare_task_admission

    project_id = params.get("project_id")
    if type(project_id) is not int or project_id <= 0:
        raise BadRequestError("项目不存在")
    actor = authorization_service.actor_snapshot(db, user_id)
    if (type(actor) is not authorization_service.AuthenticatedActor or actor.id != user_id
            or type(actor.token_version) is not int or actor.token_version < 0 or db.new or db.dirty or db.deleted):
        raise ConflictError("原认证身份无法验证", extra={"code": "task_actor_unavailable"})
    # 请求认证产生的只读事务在 DNS 前结束，不占用连接或锁。
    await db.rollback()
    ctx = engine.ctx
    async with ctx.sessionmaker() as read:
        await authorization_service.require_authenticated(read, actor)
        await project_access.require_project(read, actor.id, project_id, "ai.use")
    model = await project_ai_config.preflight_project_model(
        ctx.sessionmaker, actor, project_id, "text", cipher=ctx.cipher,
        config_id=params.get("ai_config_id"), allow_private=ctx.settings.allow_private_ai_endpoints,
    )
    proof = await prepare_task_admission(ctx, actor, kind, params, model_preflight=model)
    return await task_service.create_task(db, engine, user_id, kind, params, admission=proof)


async def validate_entity_task(
    db: AsyncSession,
    *,
    user_id: int,
    kind: EntityKind,
    project_id: int,
    character_id: int | None,
    ai_config_id: int | None,
    cipher: SecretCipher,
    allow_private: bool,
    character_ids: list[int] | None = None,
    accepted_model: project_ai_config.ResolvedProjectModel,
    original_model_binding: project_ai_config.ResolvedProjectModel | None = None,
    original_embedding: project_ai_config.ResolvedSharedEmbedding | None = None,
) -> tuple[project_ai_config.ResolvedProjectModel, project_ai_config.ResolvedSharedEmbedding, str]:
    """创建任务时同步校验能力、配置与端点，冻结服务端选定的原模型绑定。

    E4-g3b：作者与 accepted editor 共用；按中央接受的原 grant 在 认证 → 发送写门禁 → 作品 屏障内重验 ai.use。
    """
    binding = accepted_model
    if type(binding) is not project_ai_config.ResolvedProjectModel:
        raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
    with db.no_autoflush:
        project = await project_access.require_project(
            db, user_id, project_id, "ai.use", for_update=True, visibility_write=True,
            grant_identity=binding.grant_identity,
        )
    title = {
        "character.generate": "AI 生成角色",
        "organization.generate": "AI 生成组织",
        "career.generate": "AI 生成职业",
        "relations.generate": "AI 生成关系",
    }.get(kind, "")
    if kind == "relations.parse":
        if character_id is None:
            raise BadRequestError("缺少角色")
        character = await _project_character(db, project.id, character_id)
        if character.ai_mode == "never" or "relations_text" in character.ai_hidden_builtin_fields:
            raise _not_available()
        if not character.relations_text.strip():
            raise BadRequestError("该角色没有旧关系备注，无需解析")
        title = f"解析旧关系 · {character.name}"
    elif kind == "relations.generate":
        await _selected_for_generation(db, project_id, character_ids or [])
    if (binding.actor_id != user_id or binding.project_id != project_id
            or binding.project_owner_id != project.user_id or binding.model_type != "text"
            # 作者本人只可能使用本人配置；作者配置回退（source=owner）只属于带原 grant 的协作者。
            or (binding.grant_identity is None and (binding.source != "actor" or project.user_id != user_id))
            or (binding.grant_identity is not None and project.user_id == user_id)
            or (ai_config_id is not None and ai_config_id != binding.config_id)
            or (original_model_binding is not None and original_model_binding != binding)):
        raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
    if original_embedding is None:
        embedding = await project_ai_config.resolve_shared_embedding(
            db, user_id, project_id, cipher=cipher, allow_private=allow_private, for_update=True,
        )
        if (embedding.grant_identity != binding.grant_identity
                or embedding.project_owner_id != binding.project_owner_id):
            raise ConflictError("任务模型预检身份已变化")
    else:
        embedding = original_embedding
        if (embedding.actor_id != user_id or embedding.project_id != project_id
                or embedding.project_owner_id != project.user_id
                or embedding.grant_identity != binding.grant_identity):
            raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
        await project_ai_config.revalidate_shared_embedding(
            db, embedding, cipher=cipher, allow_private=allow_private, for_update=True, lock_config=True,
        )
    return binding, embedding, title[:200]


async def _project_character(db: AsyncSession, project_id: int, character_id: int) -> Character:
    """调用方已按真实 actor 完成作品能力校验（受理）或按原 grant 读取作品（执行）；角色只按同一作品过滤。"""
    character = await db.scalar(
        select(Character).where(Character.id == character_id, Character.project_id == project_id)
    )
    if character is None:
        raise NotFoundError(story_service.CHARACTERS.not_found)
    return character


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "……"


def _not_available() -> BadRequestError:
    return BadRequestError("所选设定或字段不可用于 AI，请调整可见策略", extra={"code": "setting_not_available"})


async def _relations_for_project(
    db: AsyncSession, project_id: int, character_ids: list[int],
    *, lock_sources: bool = False, subject_id: int | None = None,
) -> list[tuple[Relationship, RelationshipType]]:
    """两端及 pair 唯一键均限定本书；损坏关系或类型不能进入提示词或草稿。"""
    ids = sorted(set(character_ids))
    if len(ids) < 2:
        return []
    with db.no_autoflush:
        query = (select(Relationship).where(Relationship.is_present.is_(True), or_(
                and_(Relationship.a_id.in_(ids), Relationship.b_id.in_(ids)),
                and_(Relationship.pair_low.in_(ids), Relationship.pair_high.in_(ids)),
            )).order_by(Relationship.id).execution_options(populate_existing=True))
        if subject_id is not None:
            query = query.where(or_(Relationship.a_id == subject_id, Relationship.b_id == subject_id,
                                    Relationship.pair_low == subject_id, Relationship.pair_high == subject_id))
        if lock_sources:
            query = query.with_for_update()
        relations = list(await db.scalars(query))
        kinds_query = select(RelationshipType).where(
            RelationshipType.id.in_({relation.type_id for relation in relations}),
        ).order_by(RelationshipType.id).execution_options(populate_existing=True)
        if lock_sources:
            kinds_query = kinds_query.with_for_update()
        kinds = {kind.id: kind for kind in await db.scalars(kinds_query)}
    rows = [(relation, kinds.get(relation.type_id)) for relation in relations]
    for relation, kind in rows:
        if (relation.project_id != project_id or kind is None or kind.project_id not in (None, project_id)
                or relation.a_id not in ids or relation.b_id not in ids
                or (relation.pair_low, relation.pair_high) != (min(relation.a_id, relation.b_id),
                                                               max(relation.a_id, relation.b_id))):
            raise ConflictError("关联资料不一致，无法安全处理", extra={"code": "relation_reference_invalid"})
    return [(relation, kind) for relation, kind in rows if kind is not None]


async def _selected_for_generation(
    db: AsyncSession, project_id: int, character_ids: list[int]
) -> tuple[list[Character], list[tuple[Relationship, RelationshipType]], int]:
    """创建与运行共用选择校验，身份查询始终限定当前项目。"""
    characters = list(
        await db.scalars(select(Character).where(Character.project_id == project_id, Character.id.in_(character_ids)))
    )
    by_id = {c.id: c for c in characters}
    if len(character_ids) < 2 or len(by_id) != len(character_ids):
        raise NotFoundError("所选角色不存在或不属于该项目")
    if any(char.ai_mode == "never" for char in characters):
        raise _not_available()
    existing = await _relations_for_project(db, project_id, character_ids)
    missing = len(character_ids) * (len(character_ids) - 1) // 2 - len(existing)
    if missing <= 0:
        raise BadRequestError("所选角色之间已无缺失关系，无需生成")
    return [by_id[cid] for cid in character_ids], existing, missing


async def _relations_generate_context(
    db: AsyncSession,
    project_id: int,
    characters: list[Character],
    existing: list[tuple[Relationship, RelationshipType]],
    scope: AiScope,
) -> tuple[dict[str, str], list[RelationshipType]]:
    """身份与占用对独立保留，详细资料逐角色裁剪，避免长卡片吞掉后面的角色。"""
    memberships = await organization_service.memberships_of(db, [c.id for c in characters])
    scope.include_associations((("character", m.character_id), ("organization", org.id)) for m, org in memberships)
    visible_memberships = [(m, org) for m, org in memberships if scope.includes("organization", org.id)]
    identities = "\n".join(
        f"ID={c.id}｜{c.name}"
        + (f"｜定位：{c.role_type or '未设定'}" if scope.field_visible("character", c.id, "role_type") else "")
        + f"｜别名：{'、'.join(c.aliases or []) or '无'}"
        for c in characters
    )
    cards = "\n\n".join(
        f"ID={c.id}\n"
        + _clip(format_card(
            c, [(m, o) for m, o in visible_memberships if m.character_id == c.id],
            hidden=scope.hidden("character", c.id),
        ), REFERENCE_CARD_CHARS)
        for c in characters
    )
    names = {c.id: c.name for c in characters}
    pairs = "\n".join(f"ID={min(r.a_id, r.b_id)} ↔ ID={max(r.a_id, r.b_id)}" for r, _ in existing) or "无"
    relations = "\n".join(
        f"ID={r.a_id} → ID={r.b_id}：{relationship_service.describe_relation(names[r.a_id], names[r.b_id], t)}"
        f"；亲密度 {r.intimacy}；状态 {r.status}；{_clip(r.description, 100)}"
        for r, t in existing
    )
    # 类型可见范围与 find_type 相同；不调用 resolve_type，避免草稿创建类型。
    visible = list(
        await db.scalars(
            select(RelationshipType)
            .where((RelationshipType.project_id.is_(None)) | (RelationshipType.project_id == project_id))
            .order_by(RelationshipType.project_id.is_not(None), RelationshipType.sort, RelationshipType.id)
        )
    )
    return {
        "identities": identities,
        "characters_text": cards,
        "existing_pairs": pairs,
        "relationships_text": _clip(relations, REFERENCE_RELATIONS_CHARS),
        "types_text": _clip("\n".join(f"{t.role_a} / {t.role_b}（{t.category}）" for t in visible), 3000),
    }, visible


async def _orgs_text(db: AsyncSession, project_id: int, scope: AiScope) -> str:
    """已有组织的完整资料（类型 / 势力 / 上级 / 所在地 / 宗旨 / 描述 / 成员），供 AI 生成角色与组织时参考。"""
    orgs = list(
        await db.scalars(
            select(Organization)
            .where(Organization.project_id == project_id, Organization.id.in_(scope.ids("organization")))
            .order_by(Organization.sort, Organization.id)
        )
    )
    if not orgs:
        return ""
    names = {o.id: o.name for o in orgs}
    members: dict[int, list[str]] = {}
    rows = await db.execute(
        select(OrganizationMember, Character)
        .join(Character, Character.id == OrganizationMember.character_id)
        .where(
            OrganizationMember.organization_id.in_(names), Character.project_id == project_id,
            Character.id.in_(scope.ids("character")),
        )
        .order_by(OrganizationMember.id)
    )
    for m, character in rows:
        extra = [x for x in (m.position.strip(), MEMBER_STATUS_LABELS.get(m.status, "")) if x]
        members.setdefault(m.organization_id, []).append(character.name + (f"（{'，'.join(extra)}）" if extra else ""))
    blocks: list[str] = []
    for o in orgs:
        hidden = scope.hidden("organization", o.id)
        head = [o.name + (f"（{o.org_type}）" if "org_type" not in hidden and o.org_type else "")]
        if "power_level" not in hidden and o.power_level is not None:
            head.append(f"势力 {o.power_level}")
        if o.parent_id in names:
            head.append(f"上级：{names[o.parent_id]}")
        if "location" not in hidden and o.location.strip():
            head.append(f"所在地：{o.location.strip()}")
        lines = ["- " + "｜".join(head)]
        for key, label, text in (("purpose", "宗旨", o.purpose), ("description", "描述", o.description)):
            if key not in hidden and text.strip():
                lines.append(f"  {label}：{text.strip()}")
        if members.get(o.id):
            lines.append(f"  成员：{'、'.join(members[o.id])}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


async def _knowledge_for_execution(
    db: AsyncSession,
    project,
    query: str,
    *,
    execution: TaskExecutionIdentity,
    model_binding: project_ai_config.ResolvedProjectModel,
    selection: project_ai_config.ResolvedSharedEmbedding,
    sources: EntitySourceSnapshot,
    stamp: VisibilityStamp,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
    purpose: str,
) -> tuple[str, EntitySourceSnapshot]:
    """仅使用受理时固定的共享检索模式与向量空间；不能按当前默认模型重新选择。"""
    settings = await kb_service.load_settings(db, project.id)
    settings_group = _EntitySourceGroup(
        "kb_settings", (project.id,), (), ((project.id, _source_digest("kb_settings", settings)),),
    )
    sources = replace(sources, groups=sources.groups + (settings_group,))
    if not settings.auto_inject or not query.strip():
        return "", sources
    if await db.scalar(select(KbDocument.id).where(KbDocument.project_id == project.id).limit(1)) is None:
        return "", sources
    embedder = None
    if (selection.actor_id != execution.actor_id or selection.grant_identity != execution.grant_identity
            or selection.project_owner_id != project.user_id):
        raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
    if selection.mode == "vector":
        # E6-d（裁决 9）：作者与授权编辑者共用受理时冻结的作者共享向量选择；每次查询嵌入经 for_task 守卫
        # 在真实发送租约内重验原执行、原邀请轮次、作者授权、两份模型绑定与已采集来源。
        sessionmaker = visibility_sessionmaker(db)
        async with sessionmaker() as verify:
            await require_write_execution(verify, execution)
            config = await retrieval_service.load_bound_shared_embedding_config(
                verify, selection, cipher=cipher, allow_private=allow_private,
            )
        checks = collaboration_ai_guard.for_task(
            execution, selection, stamp, cipher=cipher, allow_private=allow_private, extra_bindings=(model_binding,),
            entity_sources=sources,
        )
        # 裁决 17：构造普通故障锁内复核原执行、轮次、授权、来源与冻结选择均未变 → 关键词继续；否则失败关闭。
        provider = await collaboration_ai_guard.prepare_shared_embedding(
            checks, sessionmaker, factory, request_timeout=ai_timeout,
        )
        if provider is not None:
            if (provider.provider_name, provider.model) != (config.provider, config.model):
                raise ConflictError("任务检索模型已变化")
            # 裁决 1：只读作者旧键 provider:model:dim（作者与授权编辑者的索引任务都写这个空间）。
            embedder = retrieval_service.guard_embedder(
                retrieval_service.Embedder(
                    provider=provider.provider, provider_name=provider.provider_name, model=provider.model,
                ), stamp, sessionmaker,
            )
    elif execution.grant_identity is not None:
        # 未授权编辑者（或作者无向量）：检索前按原领取身份锁内重验（令牌、accepted editor、原轮次、两份模型
        # 绑定、已采集来源），随即结束该写事务；之后只做关键词检索，作者向量模型零调用。
        checks = collaboration_ai_guard.for_task(
            execution, model_binding, stamp, cipher=cipher, allow_private=allow_private, extra_bindings=(selection,),
            entity_sources=sources,
        )
        async with visibility_sessionmaker(db)() as verify:
            await checks.require_task_write(verify)
            await verify.rollback()
    try:
        hits, _ = await kb_service.search_hits(
            db, project.id, query, top_k=settings.top_k, min_score=settings.min_score, embedder=embedder,
        )
        documents = {}
        chunks = {}
        for hit in hits:
            documents[hit.document.id] = SimpleNamespace(
                id=hit.document.id, project_id=project.id, filename=hit.document.filename, inject=True,
            )
            chunks[hit.chunk.id] = SimpleNamespace(
                id=hit.chunk.id, document_id=hit.document.id, heading_path=hit.chunk.heading_path,
                content=hit.chunk.content,
            )
        groups = []
        for kind, rows in (("document", documents), ("chunk", chunks)):
            ids = tuple(sorted(rows))
            groups.append(_EntitySourceGroup(kind, ids, (), tuple((ident, _source_digest(kind, rows[ident]))
                                                                  for ident in ids)))
        return "\n".join(kb_service.format_lines(hits, settings.max_chars)), replace(
            sources, groups=sources.groups + tuple(groups),
        )
    except (VisibilityPolicyError, ConflictError):
        raise
    except Exception as exc:  # noqa: BLE001  与普通知识检索一致，仅普通检索故障退为无参考资料。
        logger.warning("知识库检索失败 project=%s purpose=%s error_type=%s", project.id, purpose, type(exc).__name__)
        return "", sources


async def build_entity_job(
    db: AsyncSession,
    *,
    user_id: int,
    kind: EntityKind,
    project_id: int,
    params: dict[str, Any],
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
    model_binding: project_ai_config.ResolvedProjectModel | None = None,
    shared_embedding: project_ai_config.ResolvedSharedEmbedding | None = None,
    execution: TaskExecutionIdentity | None = None,
) -> EntityJob:
    # 作者（含无执行身份的直调）严格归属；协作执行只按原领取 grant 读取（协作者无法进入 execution=None 分支）。
    project = await get_execution_project(db, user_id, project_id, execution)
    selected: list[Character] = []
    existing: list[tuple[Relationship, RelationshipType]] = []
    options: dict[str, Any] = {}
    if kind == "relations.generate":
        request = RelationsGenerateRequest.model_validate(params)
        selected, existing, missing = await _selected_for_generation(db, project_id, request.character_ids)
        options = {
            "character_ids": request.character_ids,
            "character_names": {c.id: c.name for c in selected},
            "count": min(request.count, missing),
        }
    grant = execution.grant_identity if execution is not None else None
    if model_binding is not None:
        if (model_binding.actor_id != user_id or model_binding.project_id != project_id
                or model_binding.project_owner_id != project.user_id or model_binding.model_type != "text"
                or model_binding.grant_identity != grant
                or (grant is None and model_binding.source != "actor")):
            raise BadRequestError("模型绑定无效或已失效")
        await project_ai_config.revalidate_project_model(
            db, model_binding, cipher=cipher, allow_private=allow_private, lock_config=False,
        )
        # 作者配置回退（source=owner）的配置行属于作者；只核对实例与指纹，不把配置交给协作者以外的路径。
        cfg = await ai_config_service.resolve_config(db, model_binding.config_owner_id, model_binding.config_id)
        if (cfg.instance_key != model_binding.config_instance_key
                or project_ai_config._fingerprint(cfg) != model_binding.config_fingerprint):
            raise BadRequestError("模型配置已失效，请重新选择")
    else:
        cfg = await ai_config_service.resolve_config(db, user_id, params.get("ai_config_id"))
    if (execution is None) != (shared_embedding is None) or (execution is not None and model_binding is None):
        raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
    info = await project_info(db, project, actor_user_id=user_id)
    template_key = {
        "career.generate": "entity.career",
        "relations.parse": "relations.parse",
        "character.generate": "entity.character",
        "organization.generate": "entity.organization",
        "relations.generate": "relations.generate",
    }[kind]
    override = await prompt_service.get_override(db, user_id, template_key)
    instruction = str(params.get("instruction") or "")
    character_id = int(params["character_id"]) if kind == "relations.parse" else None
    kinds = {
        "relations.generate": ("character", "world", "organization"),
        "relations.parse": ("character",),
        "career.generate": ("world", "career"),
        "character.generate": ("character", "world", "organization", "career"),
        "organization.generate": ("character", "world", "organization"),
    }[kind]
    explicit = [("character", c.id) for c in selected]
    if character_id is not None:
        explicit.append(("character", character_id))
    scope = await build_scope(
        db, project_id, texts=(instruction, format_project_info(info)),
        explicit=explicit,
        kinds=kinds, legacy_kinds=(("world",) if kind == "relations.generate" else set(kinds) - {"character"}),
        legacy_metadata_kinds=() if kind == "relations.generate" else ("character",),
    )
    for char in selected:
        scope.require_entry("character", char.id)
    if character_id is not None:
        scope.require_entry("character", character_id, field="relations_text")
    characters = (
        selected
        if kind == "relations.generate"
        else await story_service.list_items(db, story_service.CHARACTERS, project_id)
    )
    relation_context: dict[str, str] = {}
    visible_types: list[RelationshipType] = []
    if kind == "relations.generate":
        relation_context, visible_types = await _relations_generate_context(db, project_id, selected, existing, scope)
    sources = None
    retained: list[Any] = []
    if execution is not None:
        sources, retained = await _capture_entity_sources(db, execution, scope, selected)
    knowledge = ""
    if kind != "relations.parse":
        # 知识库参考资料：查询 = 生成请求中的描述 + 作品信息
        query = "\n".join([
            instruction, str(params.get("role_type") or ""), "、".join(c.name for c in selected),
            format_project_info(info),
        ])
        if execution is None:
            knowledge = await kb_service.knowledge_for_project(
                db, project, query, cipher=cipher, factory=factory, ai_timeout=ai_timeout,
                allow_private=allow_private, purpose=kind, visibility_stamp=scope.stamp if selected else None,
            )
        else:
            assert model_binding is not None and shared_embedding is not None and sources is not None
            knowledge, sources = await _knowledge_for_execution(
                db, project, query, execution=execution, model_binding=model_binding, selection=shared_embedding,
                sources=sources, stamp=scope.stamp, cipher=cipher, factory=factory, ai_timeout=ai_timeout,
                allow_private=allow_private, purpose=kind,
            )
    if kind == "relations.generate":
        world = await story_service.list_items(db, story_service.WORLD, project_id)
        messages = build_relations_generate_messages(
            info,
            **relation_context,
            count=options["count"],
            world_text=_clip(current_world_text(scope, world), REFERENCE_WORLD_CHARS),
            instruction=instruction,
            knowledge=knowledge,
            override=override,
        )
    elif kind == "career.generate":
        world = await story_service.list_items(db, story_service.WORLD, project_id)
        options = {"count": int(params.get("count") or 3), "kind": params.get("kind")}
        messages = build_career_generate_messages(
            info,
            count=options["count"],
            kind=options["kind"],
            world_text=_clip(current_world_text(scope, world), REFERENCE_WORLD_CHARS),
            careers_text=_clip(
                await career_service.careers_detail_reference(db, project_id, scope=scope), REFERENCE_CAREERS_CHARS,
            ),
            instruction=instruction,
            knowledge=knowledge,
            override=override,
        )
    elif kind == "relations.parse":
        assert character_id is not None
        subject = await _project_character(db, project.id, character_id)
        if not subject.relations_text.strip():
            raise BadRequestError("该角色没有旧关系备注，无需解析")
        messages = build_relations_parse_messages(
            info,
            character_name=subject.name,
            relations_text=subject.relations_text,
            roster_text=_clip(
                current_character_text(scope, [c for c in characters if c.id != subject.id], roster=True),
                REFERENCE_ROSTER_CHARS,
            ),
            override=override,
        )
    else:
        world = await story_service.list_items(db, story_service.WORLD, project_id)
        common = {
            "world_text": _clip(current_world_text(scope, world), REFERENCE_WORLD_CHARS),
            "roster_text": _clip(current_character_text(scope, characters, roster=True), REFERENCE_ROSTER_CHARS),
            "organizations_text": _clip(await _orgs_text(db, project_id, scope), REFERENCE_ORGS_CHARS),
            "instruction": instruction,
            "knowledge": knowledge,
            "override": override,
        }
        if kind == "character.generate":
            messages = build_character_generate_messages(
                info,
                role_type=str(params.get("role_type") or ""),
                careers_text=_clip(
                    await career_service.careers_reference(db, project_id, scope=scope), REFERENCE_ROSTER_CHARS,
                ),
                **common,
            )
        else:
            messages = build_organization_generate_messages(info, **common)
    await scope.verify(db)
    if execution is None:
        provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
        maker = visibility_sessionmaker(db)
        provider = meter_provider(
            guard_provider(provider, scope.stamp, maker), cfg, maker, actor_id=user_id, kind=kind,
            project_id=project.id, project_owner_id=project.user_id,
        )
        checks = None
        model, thinking, max_tokens = cfg.model, cfg.thinking_level, ai_config_service.max_output_tokens(cfg)
    else:
        assert model_binding is not None and shared_embedding is not None and sources is not None
        checks = collaboration_ai_guard.for_task(
            execution, model_binding, scope.stamp, cipher=cipher, allow_private=allow_private,
            extra_bindings=(shared_embedding,), entity_sources=sources,
        )
        bound = await checks.prepare_provider(visibility_sessionmaker(db), factory, request_timeout=ai_timeout)
        provider = bound.provider
        model, thinking = bound.model, bound.thinking_level
        max_tokens = bound.max_output_k * ai_config_service.TOKENS_PER_K if bound.max_output_k else None
    # 保持来源 ORM 对象存活到提示词组装完成，避免 identity map 丢失弱引用后用新值替换快照。
    _ = retained, visible_types
    return EntityJob(
        kind=kind,
        project_id=project_id,
        provider=provider,
        model=model,
        messages=messages,
        thinking=thinking,  # type: ignore[arg-type]
        max_tokens=max_tokens,
        character_id=character_id,
        options=options,
        send_guard=checks,
    )


# ---------------- 结果 ----------------


async def build_result(db: AsyncSession, job: EntityJob, obj: Any, *, lock_sources: bool = False) -> dict[str, Any]:
    """解析后的 JSON → 草稿 + 建议（只读查询，不写库）。"""
    warnings: list[str] = []
    if job.kind == "career.generate":
        drafts, warnings = career_service.normalize_career_drafts(obj, force_kind=job.options.get("kind"))
        if not drafts:
            raise JSONExtractError("未解析到任何职业")
        if lock_sources:
            careers = list(await db.scalars(select(Career).where(Career.project_id == job.project_id)
                                            .order_by(Career.sort, Career.id).with_for_update()))
        else:
            careers = await career_service.list_project_careers(db, job.project_id)
        taken = {c.name for c in careers}
        for d in drafts:
            # 与已有职业重名的需改名后才能保存（同批内的重名已在规范化时去掉）
            d["conflict"] = d["name"] in taken
            if d["conflict"]:
                warnings.append(f"已存在名为「{d['name']}」的职业，保存前请修改名称")
        return {"drafts": drafts, "warnings": warnings}
    if lock_sources:
        characters = list(await db.scalars(select(Character).where(Character.project_id == job.project_id)
                                           .order_by(Character.sort, Character.id).with_for_update()))
    else:
        characters = await story_service.list_items(db, story_service.CHARACTERS, job.project_id)
    if job.kind == "relations.generate":
        return await generated_relation_suggestions(db, job, obj, characters, lock_sources=lock_sources)
    if job.kind == "character.generate":
        draft, raw = normalize_character_draft(obj)
        taken = {c.name for c in characters} | {a for c in characters for a in (c.aliases or [])}
        if draft["name"] in taken:
            warnings.append(f"已存在名为「{draft['name']}」的角色或别名，保存前建议修改名字")
        careers, career_warnings = await career_suggestions(
            db, job.project_id, normalize_career_assignments(obj), lock_sources=lock_sources,
        )
        return {
            "draft": draft,
            "relation_suggestions": await relation_suggestions(
                db, job.project_id, raw, characters, lock_sources=lock_sources,
            ),
            "career_suggestions": careers,
            "warnings": warnings + career_warnings,
        }
    if job.kind == "organization.generate":
        draft, members = normalize_org_draft(obj)
        orgs_query = select(Organization.name, Organization.id).where(Organization.project_id == job.project_id)
        if lock_sources:
            orgs_query = orgs_query.with_for_update()
        orgs = dict((await db.execute(orgs_query)).all())
        if draft["name"] in orgs:
            warnings.append(f"已存在名为「{draft['name']}」的组织，保存前请修改名称")
        draft["parent_id"] = orgs.get(draft["parent_name"]) if draft["parent_name"] else None
        if draft["parent_name"] and draft["parent_id"] is None:
            warnings.append(f"上级组织「{draft['parent_name']}」不存在，已忽略")
        suggestions = []
        seen: set[int] = set()
        for m in members:
            cid, reason = resolve_name(m["name"], characters)
            if cid is not None and cid in seen:
                continue
            if cid is not None:
                seen.add(cid)
            suggestions.append(
                {
                    "character_name": m["name"],
                    "character_id": cid,
                    "position": m["position"],
                    "resolved": cid is not None,
                    "reason": reason,
                }
            )
        return {"draft": draft, "member_suggestions": suggestions, "warnings": warnings}
    # relations.parse
    subject = next((c for c in characters if c.id == job.character_id), None)
    if subject is None:
        raise BadRequestError("角色已被删除，解析结果未生成")
    raw = normalize_relation_items(obj)
    if not raw:
        raise JSONExtractError("未解析到任何关系")
    return {
        "character_id": subject.id,
        "character_name": subject.name,
        "suggestions": await relation_suggestions(
            db, job.project_id, raw, characters, subject_id=subject.id, lock_sources=lock_sources,
        ),
        "warnings": warnings,
    }


async def run_entity_job(job: EntityJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    pieces: list[str] = []
    pending: list[str] = []
    pending_chars = 0
    first = True
    last_emit = time.monotonic()
    try:
        if job.send_guard is None:
            raise BadRequestError("任务身份或模型绑定已失效，请重新发起")
        stream_iter = job.provider.generate_stream(
            job.messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking
        )
        async with aclosing(stream_iter) as stream:
            async for piece in stream:
                pieces.append(piece)
                pending.append(piece)
                pending_chars += len(piece)
                now = time.monotonic()
                if first or pending_chars >= 256 or now - last_emit >= 0.1:
                    yield "delta", {"text": "".join(pending)}
                    pending.clear()
                    pending_chars, first, last_emit = 0, False, now
        if pending:
            yield "delta", {"text": "".join(pending)}
            pending.clear()
        raw = "".join(pieces)
        if not raw.strip():
            raise AIProviderError("模型未返回任何内容")
        obj = extract_json(raw)
        job.result_object = obj
        async with sessionmaker() as db:
            await job.send_guard.require_task_write(db)
            result = await build_result(db, job, obj, lock_sources=True)
        yield "done", result
    except JSONExtractError as exc:
        yield "error", {"detail": f"AI 输出解析失败：{exc}，请重新生成"}
    except AIProviderError as exc:
        if pending:
            yield "delta", {"text": "".join(pending)}
        yield "error", {"detail": str(exc)}
    except BadRequestError as exc:
        yield "error", {"detail": exc.detail}
    except Exception as exc:  # noqa: BLE001
        logger.error("设定生成失败 project=%s kind=%s error_type=%s", job.project_id, job.kind, type(exc).__name__)
        yield "error", {"detail": "生成失败：服务器内部错误"}
