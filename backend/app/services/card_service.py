"""角色 / 组织卡片导入导出（R8，格式 ainovel.cards v1）。

- 卡片按名称关联，不含任何内部 ID；导入时不信任卡片里的任何 ID。
- 导出：勾选的角色和组织 + 两端都在导出集合内的关系 + 成员角色在集合内的成员关系；
  上级组织也在导出集合内时才写 parent_name。
- 导入：同名冲突逐项选择 skip / rename（默认 skip）；rename 追加「（导入）」「（导入2）」……直到不冲突。
  建立「卡片名 → 目标 id」映射：新建项映射到新 id，被跳过的同名项映射到目标项目已有的同名对象；
  关系与成员关系的两端先在映射里找，再按名称找目标项目已有对象。
  关系类型按 (role_a, role_b) 匹配预置 / 本项目自定义类型（反向匹配时互换两端），匹配不上新建自定义类型；
  关系一律经 relationship_service.apply_change(source="import") 写入；同一对角色已有关系时跳过并记入 warnings。
  整个导入在一个事务内完成。
"""

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.models import Character, Organization, OrganizationMember, Relationship, RelationshipType
from app.models.entities import LIFE_STATUSES, MEMBER_STATUSES, RELATION_STATUSES
from app.schemas.card import (
    CARD_FORMAT,
    CARD_VERSION,
    MAX_CARD_BYTES,
    CardExportRequest,
    CardFile,
    CardImportRequest,
    CardImportResult,
    CardPreview,
    ImportedItem,
    PreviewItem,
    PreviewRelationship,
)
from app.schemas.story import MAX_ALIAS_LEN, MAX_ALIASES
from app.services import relationship_service, story_service
from app.services.cast_service import is_protagonist_role
from app.services.organization_service import MAX_DEPTH
from app.services.project_service import get_owned_project

RENAME_SUFFIX = "（导入）"


# ---------------- 导出 ----------------


async def export_cards(db: AsyncSession, user_id: int, project_id: int, req: CardExportRequest) -> dict[str, Any]:
    await get_owned_project(db, user_id, project_id)
    char_ids = list(dict.fromkeys(req.character_ids))
    org_ids = list(dict.fromkeys(req.organization_ids))
    chars = list(
        await db.scalars(
            select(Character)
            .where(Character.project_id == project_id, Character.id.in_(char_ids))
            .order_by(Character.sort, Character.id)
        )
    )
    if len(chars) != len(char_ids):
        raise BadRequestError("部分角色不存在或不属于该项目")
    orgs = list(
        await db.scalars(
            select(Organization)
            .where(Organization.project_id == project_id, Organization.id.in_(org_ids))
            .order_by(Organization.sort, Organization.id)
        )
    )
    if len(orgs) != len(org_ids):
        raise BadRequestError("部分组织不存在或不属于该项目")

    char_names = {c.id: c.name for c in chars}
    org_names = {o.id: o.name for o in orgs}
    members: dict[int, list[dict[str, Any]]] = {}
    if orgs and chars:
        rows = await db.scalars(
            select(OrganizationMember)
            .where(OrganizationMember.organization_id.in_(org_names), OrganizationMember.character_id.in_(char_names))
            .order_by(OrganizationMember.id)
        )
        for m in rows:
            members.setdefault(m.organization_id, []).append(
                {
                    "character_name": char_names[m.character_id],
                    "position": m.position,
                    "status": m.status,
                    "note": m.note,
                }
            )
    rels: list[dict[str, Any]] = []
    if len(chars) >= 2:
        rows = await db.execute(
            select(Relationship, RelationshipType)
            .join(RelationshipType, RelationshipType.id == Relationship.type_id)
            .where(Relationship.a_id.in_(char_names), Relationship.b_id.in_(char_names))
            .order_by(Relationship.id)
        )
        for r, t in rows.all():
            rels.append(
                {
                    "a_name": char_names[r.a_id],
                    "b_name": char_names[r.b_id],
                    "role_a": t.role_a,
                    "role_b": t.role_b,
                    "category": t.category,
                    "intimacy": r.intimacy,
                    "status": r.status,
                    "description": r.description,
                }
            )
    return {
        "format": CARD_FORMAT,
        "version": CARD_VERSION,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "characters": [
            {
                "name": c.name,
                "aliases": list(c.aliases or []),
                "role_type": c.role_type,
                "age": c.age,
                "gender": c.gender,
                "appearance": c.appearance,
                "personality": c.personality,
                "background": c.background,
                "profile": c.profile,
                "current_state": c.current_state,
                "life_status": c.life_status,
            }
            for c in chars
        ],
        "organizations": [
            {
                "name": o.name,
                "org_type": o.org_type,
                "purpose": o.purpose,
                "location": o.location,
                "power_level": o.power_level,
                "description": o.description,
                "parent_name": org_names.get(o.parent_id) if o.parent_id is not None else None,
                "members": members.get(o.id, []),
            }
            for o in orgs
        ],
        "relationships": rels,
    }


# ---------------- 解析 / 校验 ----------------


def parse_card(data: Any) -> CardFile:
    """校验大小、format、version 与结构；错误一律 400 中文提示。"""
    try:
        # 紧凑序列化，与前端 JSON.stringify 口径一致（导出文件带缩进，缩进不计入上限）
        size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise BadRequestError("卡片文件格式不正确") from exc
    if size > MAX_CARD_BYTES:
        raise BadRequestError("卡片文件过大，上限为 2MB")
    if not isinstance(data, dict):
        raise BadRequestError("卡片文件格式不正确：应为 JSON 对象")
    if data.get("format") != CARD_FORMAT:
        raise BadRequestError("不是有效的卡片文件（format 应为 ainovel.cards）")
    if data.get("version") != CARD_VERSION:
        raise BadRequestError(f"不支持的卡片版本：{data.get('version')}（当前支持 {CARD_VERSION}）")
    try:
        card = CardFile.model_validate(data)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(x) for x in err.get("loc", ()))
        raise BadRequestError(f"卡片文件格式不正确：{loc}: {err.get('msg', '')}") from exc
    return card


def _dedupe_names(items: list[Any], label: str, warnings: list[str]) -> list[Any]:
    seen: set[str] = set()
    out = []
    for it in items:
        name = it.name.strip()
        if not name:
            continue
        if name in seen:
            warnings.append(f"卡片中有重复的{label}「{name}」，只导入第一个")
            continue
        seen.add(name)
        it.name = name
        out.append(it)
    return out


def rename_for(name: str, taken: set[str], max_len: int = 100) -> str:
    """追加「（导入）」「（导入2）」……直到不与 taken 冲突；超长时截断原名。"""
    n = 1
    while True:
        suffix = RENAME_SUFFIX if n == 1 else f"（导入{n}）"
        candidate = name[: max_len - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        n += 1


async def _existing(db: AsyncSession, project_id: int) -> tuple[dict[str, int], dict[str, int]]:
    """目标项目已有的「名称 → id」（角色同名时取排序最前的一个）。"""
    chars: dict[str, int] = {}
    for cid, name in (
        await db.execute(
            select(Character.id, Character.name)
            .where(Character.project_id == project_id)
            .order_by(Character.sort, Character.id)
        )
    ).all():
        chars.setdefault(name, cid)
    orgs = dict(
        (
            await db.execute(select(Organization.name, Organization.id).where(Organization.project_id == project_id))
        ).all()
    )
    return chars, orgs


def _preview_items(names: list[str], existing: dict[str, int]) -> list[PreviewItem]:
    taken = set(existing) | set(names)
    items = []
    for name in names:
        if name in existing:
            new_name = rename_for(name, taken)
            taken.add(new_name)
            items.append(PreviewItem(name=name, status="conflict", rename_to=new_name))
        else:
            items.append(PreviewItem(name=name, status="new"))
    return items


async def preview_cards(db: AsyncSession, user_id: int, project_id: int, data: Any) -> CardPreview:
    await get_owned_project(db, user_id, project_id)
    card = parse_card(data)
    warnings: list[str] = []
    chars = _dedupe_names(card.characters, "角色", warnings)
    orgs = _dedupe_names(card.organizations, "组织", warnings)
    existing_chars, existing_orgs = await _existing(db, project_id)
    known_chars = set(existing_chars) | {c.name for c in chars}
    known_orgs = set(existing_orgs) | {o.name for o in orgs}
    rels = []
    for r in card.relationships:
        a_name, b_name = r.a_name.strip(), r.b_name.strip()
        ok = a_name in known_chars and b_name in known_chars and a_name != b_name and bool(r.role_a.strip())
        rels.append(
            PreviewRelationship(
                a_name=r.a_name, b_name=r.b_name, role_a=r.role_a, role_b=r.role_b, status="ok" if ok else "unresolved"
            )
        )
    for o in orgs:
        for m in o.members:
            if m.character_name.strip() not in known_chars:
                warnings.append(f"组织「{o.name}」的成员「{m.character_name}」无法关联，将被忽略")
        if o.parent_name and o.parent_name.strip() not in known_orgs:
            warnings.append(f"组织「{o.name}」的上级组织「{o.parent_name}」无法关联，将作为顶级组织导入")
    return CardPreview(
        characters=_preview_items([c.name for c in chars], existing_chars),
        organizations=_preview_items([o.name for o in orgs], existing_orgs),
        relationships=rels,
        warnings=warnings,
    )


# ---------------- 导入 ----------------


def _aliases(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        v = str(v).strip()[:MAX_ALIAS_LEN]
        if v and v not in out:
            out.append(v)
    return out[:MAX_ALIASES]


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


async def import_cards(db: AsyncSession, user_id: int, project_id: int, req: CardImportRequest) -> CardImportResult:
    await get_owned_project(db, user_id, project_id)
    card = parse_card(req.data)
    warnings: list[str] = []
    chars = _dedupe_names(card.characters, "角色", warnings)
    orgs = _dedupe_names(card.organizations, "组织", warnings)
    existing_chars, existing_orgs = await _existing(db, project_id)

    # ---- 角色 ----
    char_map: dict[str, int] = {}
    char_results: list[ImportedItem] = []
    taken_chars = set(existing_chars) | {c.name for c in chars}
    for c in chars:
        name = c.name
        action = "created"
        if name in existing_chars:
            if req.resolutions.characters.get(name, "skip") == "skip":
                char_map[name] = existing_chars[name]
                char_results.append(
                    ImportedItem(name=name, imported_name=None, id=existing_chars[name], action="skipped")
                )
                continue
            name = rename_for(name, taken_chars)
            taken_chars.add(name)
            action = "renamed"
        values = {
            "name": name,
            "aliases": _aliases(c.aliases),
            "role_type": c.role_type.strip()[:50],
            "age": c.age[:50],
            "gender": c.gender[:20],
            "appearance": c.appearance,
            "personality": c.personality,
            "background": c.background,
            "profile": c.profile,
            "current_state": c.current_state,
            "life_status": c.life_status if c.life_status in LIFE_STATUSES else "alive",
            "always_in_context": is_protagonist_role(c.role_type),
        }
        created = await story_service.new_character(db, project_id, values)
        char_map[c.name] = created.id
        char_results.append(ImportedItem(name=c.name, imported_name=name, id=created.id, action=action))

    def char_id(name: str) -> int | None:
        name = name.strip()
        return char_map.get(name, existing_chars.get(name))

    # ---- 组织 ----
    org_map: dict[str, int] = {}
    org_results: list[ImportedItem] = []
    new_orgs: dict[str, Organization] = {}
    taken_orgs = set(existing_orgs) | {o.name for o in orgs}
    next_sort = await story_service.next_sort(db, Organization, project_id)
    for o in orgs:
        name = o.name
        action = "created"
        if name in existing_orgs:
            if req.resolutions.organizations.get(name, "skip") == "skip":
                org_map[name] = existing_orgs[name]
                org_results.append(
                    ImportedItem(name=name, imported_name=None, id=existing_orgs[name], action="skipped")
                )
                continue
            name = rename_for(name, taken_orgs)
            taken_orgs.add(name)
            action = "renamed"
        org = Organization(
            project_id=project_id,
            name=name,
            org_type=o.org_type,
            purpose=o.purpose,
            location=o.location,
            power_level=o.power_level,
            description=o.description,
            sort=next_sort,
        )
        next_sort += 1
        db.add(org)
        await db.flush()
        org_map[o.name] = org.id
        new_orgs[o.name] = org
        org_results.append(ImportedItem(name=o.name, imported_name=name, id=org.id, action=action))

    # 上级组织：只设置新建的组织；用内存中的上级链检测循环
    parents = dict(
        (
            await db.execute(
                select(Organization.id, Organization.parent_id).where(Organization.project_id == project_id)
            )
        ).all()
    )
    for o in orgs:
        org = new_orgs.get(o.name)
        if org is None or not o.parent_name:
            continue
        pname = o.parent_name.strip()
        parent_id = org_map.get(pname, existing_orgs.get(pname))
        if parent_id is None:
            warnings.append(f"组织「{o.name}」的上级组织「{pname}」无法关联，已作为顶级组织导入")
            continue
        cur: int | None = parent_id
        cyclic = False
        for _ in range(MAX_DEPTH + 1):
            if cur is None:
                break
            if cur == org.id:
                cyclic = True
                break
            cur = parents.get(cur)
        if cyclic or cur is not None:
            warnings.append(f"组织「{o.name}」的上级组织「{pname}」会形成循环引用，已忽略")
            continue
        org.parent_id = parent_id
        parents[org.id] = parent_id
    await db.flush()

    # ---- 成员关系 ----
    members_created = 0
    for o in orgs:
        oid = org_map.get(o.name)
        if oid is None:
            continue
        for m in o.members:
            cid = char_id(m.character_name)
            if cid is None:
                warnings.append(f"组织「{o.name}」的成员「{m.character_name}」无法关联，已忽略")
                continue
            dup = await db.scalar(
                select(OrganizationMember.id).where(
                    OrganizationMember.organization_id == oid, OrganizationMember.character_id == cid
                )
            )
            if dup is not None:
                warnings.append(f"「{m.character_name}」已是组织「{o.name}」的成员，已跳过")
                continue
            db.add(
                OrganizationMember(
                    organization_id=oid,
                    character_id=cid,
                    position=m.position[:50],
                    status=m.status if m.status in MEMBER_STATUSES else "active",
                    note=m.note,
                )
            )
            members_created += 1
            await db.flush()

    # ---- 人物关系 ----
    rels_created = 0
    for r in card.relationships:
        a, b = char_id(r.a_name), char_id(r.b_name)
        label = f"「{r.a_name}」与「{r.b_name}」"
        if a is None or b is None:
            warnings.append(f"关系 {label} 无法关联到角色，已忽略")
            continue
        if a == b:
            warnings.append(f"关系 {label} 指向同一角色，已忽略")
            continue
        if not r.role_a.strip():
            warnings.append(f"关系 {label} 缺少称呼，已忽略")
            continue
        if await relationship_service.find_pair(db, a, b) is not None:
            warnings.append(f"{label} 之间已有关系，已跳过")
            continue
        await relationship_service.create_relation(
            db,
            project_id,
            a_id=a,
            b_id=b,
            role_a=r.role_a,
            role_b=r.role_b,
            category=r.category,
            intimacy=_clamp(r.intimacy, -100, 100),
            status=r.status if r.status in RELATION_STATUSES else "normal",
            description=r.description,
            source="import",
        )
        rels_created += 1

    await db.commit()
    return CardImportResult(
        characters=char_results,
        organizations=org_results,
        relationships_created=rels_created,
        members_created=members_created,
        warnings=warnings,
    )
