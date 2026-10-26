"""AI 单体生成角色 / 组织 / 职业、解析旧关系备注
（后台任务 character.generate / organization.generate / career.generate / relations.parse）。

任务只产出草稿，不写任何设定数据，也不写 generation_logs（与向导一致）。保存由前端预览确认后调用
`characters/with-relations`、`organizations/with-members`、`careers/batch`、`relationships/batch` 完成。
职业草稿与已有职业（或同批中更早的草稿）同名时标记 conflict=true，需改名后才能保存。
生成角色时若项目已有职业，提示词附上职业阶梯，草稿带 career_suggestions（按职业名 / 阶段名精确匹配）。

关系建议从「主体」（新角色 / 被解析的角色）视角给出：
- self_role = 主体的身份，target_role = 对方的身份；
- 按 (self_role, target_role) 匹配已有类型：正向匹配 subject_is_a = True（主体是 role_a 一方），
  反向匹配 subject_is_a = False；匹配不上 type.type_id = None（保存时新建自定义类型），subject_is_a = True。
- 名字按「名字 → 别名」精确匹配已有角色；匹配不上 / 有歧义 / 指向自己 / 两人已有关系 → resolved = False，附 reason。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.json_extract import JSONExtractError, extract_json
from app.ai.prompts import (
    build_career_generate_messages,
    build_character_generate_messages,
    build_organization_generate_messages,
    build_relations_parse_messages,
)
from app.ai.providers import AIProviderError, ChatMessage, LLMProvider, ProviderFactory, ThinkingLevel
from app.core.errors import BadRequestError
from app.core.security import SecretCipher
from app.models import Character, Organization, OrganizationMember
from app.models.entities import RELATION_CATEGORIES
from app.schemas.story import MAX_ALIAS_LEN, MAX_ALIASES
from app.services import ai_config_service, career_service, relationship_service, story_service
from app.services.cast_service import is_protagonist_role
from app.services.context_builder import MEMBER_STATUS_LABELS
from app.services.generation_service import Event
from app.services.project_service import get_owned_project, project_info

logger = logging.getLogger(__name__)

EntityKind = Literal["character.generate", "organization.generate", "career.generate", "relations.parse"]
REFERENCE_WORLD_CHARS = 4000
REFERENCE_ROSTER_CHARS = 3000
REFERENCE_ORGS_CHARS = 6000

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


@dataclass
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
    # career.generate 的请求参数（count / kind）
    options: dict[str, Any] = field(default_factory=dict)


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
) -> list[dict[str, Any]]:
    out = []
    for r in raw:
        target_id, reason = resolve_name(r["target"], characters)
        if target_id is not None and target_id == subject_id:
            target_id, reason = None, "不能与自己建立关系"
        existing_id = None
        if target_id is not None and subject_id is not None:
            existing = await relationship_service.find_pair(db, subject_id, target_id)
            if existing is not None:
                existing_id, reason = existing.id, "两人之间已有关系"
        t, flipped = await relationship_service.find_type(db, project_id, r["self_role"], r["target_role"])
        if t is not None:
            type_ref = {"type_id": t.id, "role_a": t.role_a, "role_b": t.role_b, "category": t.category}
        else:
            type_ref = {
                "type_id": None,
                "role_a": r["self_role"],
                "role_b": r["target_role"],
                "category": r["category"],
            }
        out.append(
            {
                "target_name": r["target"],
                "target_id": target_id,
                "resolved": target_id is not None and existing_id is None,
                "reason": reason,
                "existing_relationship_id": existing_id,
                "self_role": r["self_role"],
                "target_role": r["target_role"],
                "subject_is_a": not flipped,
                "type": type_ref,
                "intimacy": r["intimacy"],
                "status": "normal",
                "description": r["description"],
            }
        )
    return out


async def career_suggestions(
    db: AsyncSession, project_id: int, raw: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """职业建议：按职业名精确匹配；阶段名对不上时取第 1 阶并给警告；超出主 1 副 3 的标记 resolved=False。"""
    careers = await career_service.list_project_careers(db, project_id)
    if not careers:
        return [], []
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


async def validate_entity_task(
    db: AsyncSession,
    *,
    user_id: int,
    kind: EntityKind,
    project_id: int,
    character_id: int | None,
    ai_config_id: int | None,
    cipher: SecretCipher,
    factory: ProviderFactory,
    ai_timeout: float,
    allow_private: bool,
) -> tuple[int, str]:
    """创建任务时同步校验归属、配置与端点；返回 (ai_config_id, 任务标题)。不修改任何数据。"""
    await get_owned_project(db, user_id, project_id)
    title = {
        "character.generate": "AI 生成角色",
        "organization.generate": "AI 生成组织",
        "career.generate": "AI 生成职业",
    }.get(kind, "")
    if kind == "relations.parse":
        if character_id is None:
            raise BadRequestError("缺少角色")
        character = await story_service.get_owned(db, story_service.CHARACTERS, user_id, project_id, character_id)
        if not character.relations_text.strip():
            raise BadRequestError("该角色没有旧关系备注，无需解析")
        title = f"解析旧关系 · {character.name}"
    cfg = await ai_config_service.resolve_config(db, user_id, ai_config_id)
    await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    return cfg.id, title[:200]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "……"


async def _orgs_text(db: AsyncSession, project_id: int) -> str:
    """已有组织的完整资料（类型 / 势力 / 上级 / 所在地 / 宗旨 / 描述 / 成员），供 AI 生成角色与组织时参考。"""
    orgs = list(
        await db.scalars(
            select(Organization)
            .where(Organization.project_id == project_id)
            .order_by(Organization.sort, Organization.id)
        )
    )
    if not orgs:
        return ""
    names = {o.id: o.name for o in orgs}
    members: dict[int, list[str]] = {}
    rows = await db.execute(
        select(OrganizationMember, Character.name)
        .join(Character, Character.id == OrganizationMember.character_id)
        .where(OrganizationMember.organization_id.in_(names))
        .order_by(OrganizationMember.id)
    )
    for m, char_name in rows:
        extra = [x for x in (m.position.strip(), MEMBER_STATUS_LABELS.get(m.status, "")) if x]
        members.setdefault(m.organization_id, []).append(char_name + (f"（{'，'.join(extra)}）" if extra else ""))
    blocks: list[str] = []
    for o in orgs:
        head = [o.name + (f"（{o.org_type}）" if o.org_type else "")]
        if o.power_level is not None:
            head.append(f"势力 {o.power_level}")
        if o.parent_id in names:
            head.append(f"上级：{names[o.parent_id]}")
        if o.location.strip():
            head.append(f"所在地：{o.location.strip()}")
        lines = ["- " + "｜".join(head)]
        for label, text in (("宗旨", o.purpose), ("描述", o.description)):
            if text.strip():
                lines.append(f"  {label}：{text.strip()}")
        if members.get(o.id):
            lines.append(f"  成员：{'、'.join(members[o.id])}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


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
) -> EntityJob:
    project = await get_owned_project(db, user_id, project_id)
    cfg = await ai_config_service.resolve_config(db, user_id, params.get("ai_config_id"))
    provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
    info = project_info(project)
    characters = await story_service.list_items(db, story_service.CHARACTERS, project_id)
    instruction = str(params.get("instruction") or "")
    character_id: int | None = None
    options: dict[str, Any] = {}
    if kind == "career.generate":
        world = await story_service.list_items(db, story_service.WORLD, project_id)
        options = {"count": int(params.get("count") or 3), "kind": params.get("kind")}
        messages = build_career_generate_messages(
            info,
            count=options["count"],
            kind=options["kind"],
            world_text=_clip(story_service.format_world(world), REFERENCE_WORLD_CHARS),
            careers_text=_clip(await career_service.careers_reference(db, project_id), REFERENCE_ROSTER_CHARS),
            instruction=instruction,
        )
    elif kind == "relations.parse":
        character_id = int(params["character_id"])
        subject = await story_service.get_owned(db, story_service.CHARACTERS, user_id, project_id, character_id)
        if not subject.relations_text.strip():
            raise BadRequestError("该角色没有旧关系备注，无需解析")
        messages = build_relations_parse_messages(
            info,
            character_name=subject.name,
            relations_text=subject.relations_text,
            roster_text=_clip(
                story_service.format_roster([c for c in characters if c.id != subject.id]), REFERENCE_ROSTER_CHARS
            ),
        )
    else:
        world = await story_service.list_items(db, story_service.WORLD, project_id)
        common = {
            "world_text": _clip(story_service.format_world(world), REFERENCE_WORLD_CHARS),
            "roster_text": _clip(story_service.format_roster(characters), REFERENCE_ROSTER_CHARS),
            "organizations_text": _clip(await _orgs_text(db, project_id), REFERENCE_ORGS_CHARS),
            "instruction": instruction,
        }
        if kind == "character.generate":
            messages = build_character_generate_messages(
                info,
                role_type=str(params.get("role_type") or ""),
                careers_text=_clip(await career_service.careers_reference(db, project_id), REFERENCE_ROSTER_CHARS),
                **common,
            )
        else:
            messages = build_organization_generate_messages(info, **common)
    return EntityJob(
        kind=kind,
        project_id=project_id,
        provider=provider,
        model=cfg.model,
        messages=messages,
        thinking=cfg.thinking_level,  # type: ignore[arg-type]
        max_tokens=ai_config_service.max_output_tokens(cfg),
        character_id=character_id,
        options=options,
    )


# ---------------- 结果 ----------------


async def build_result(db: AsyncSession, job: EntityJob, obj: Any) -> dict[str, Any]:
    """解析后的 JSON → 草稿 + 建议（只读查询，不写库）。"""
    warnings: list[str] = []
    if job.kind == "career.generate":
        drafts, warnings = career_service.normalize_career_drafts(obj, force_kind=job.options.get("kind"))
        if not drafts:
            raise JSONExtractError("未解析到任何职业")
        taken = {c.name for c in await career_service.list_project_careers(db, job.project_id)}
        for d in drafts:
            # 与已有职业重名的需改名后才能保存（同批内的重名已在规范化时去掉）
            d["conflict"] = d["name"] in taken
            if d["conflict"]:
                warnings.append(f"已存在名为「{d['name']}」的职业，保存前请修改名称")
        return {"drafts": drafts, "warnings": warnings}
    characters = await story_service.list_items(db, story_service.CHARACTERS, job.project_id)
    if job.kind == "character.generate":
        draft, raw = normalize_character_draft(obj)
        taken = {c.name for c in characters} | {a for c in characters for a in (c.aliases or [])}
        if draft["name"] in taken:
            warnings.append(f"已存在名为「{draft['name']}」的角色或别名，保存前建议修改名字")
        careers, career_warnings = await career_suggestions(db, job.project_id, normalize_career_assignments(obj))
        return {
            "draft": draft,
            "relation_suggestions": await relation_suggestions(db, job.project_id, raw, characters),
            "career_suggestions": careers,
            "warnings": warnings + career_warnings,
        }
    if job.kind == "organization.generate":
        draft, members = normalize_org_draft(obj)
        orgs = dict(
            (
                await db.execute(
                    select(Organization.name, Organization.id).where(Organization.project_id == job.project_id)
                )
            ).all()
        )
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
        "suggestions": await relation_suggestions(db, job.project_id, raw, characters, subject_id=subject.id),
        "warnings": warnings,
    }


async def run_entity_job(job: EntityJob, sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Event]:
    pieces: list[str] = []
    try:
        stream_iter = job.provider.generate_stream(
            job.messages, job.model, max_tokens=job.max_tokens, thinking=job.thinking
        )
        async with aclosing(stream_iter) as stream:
            async for piece in stream:
                pieces.append(piece)
                yield "delta", {"text": piece}
        raw = "".join(pieces)
        if not raw.strip():
            raise AIProviderError("模型未返回任何内容")
        obj = extract_json(raw)
        async with sessionmaker() as db:
            result = await build_result(db, job, obj)
        yield "done", result
    except JSONExtractError as exc:
        yield "error", {"detail": f"AI 输出解析失败：{exc}，请重新生成"}
    except AIProviderError as exc:
        yield "error", {"detail": str(exc)}
    except BadRequestError as exc:
        yield "error", {"detail": exc.detail}
    except Exception:  # noqa: BLE001
        logger.exception("设定生成失败 project=%s kind=%s", job.project_id, job.kind)
        yield "error", {"detail": "生成失败：服务器内部错误"}
