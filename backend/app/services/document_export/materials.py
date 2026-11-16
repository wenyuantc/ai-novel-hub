"""按明确选择读取本书的设定和笔记，并投影为可阅读文档内容。

调用方必须先校验作品的 export 能力；本模块只读取当前作品，不处理认证、写入或文件渲染。
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import NotFoundError
from app.models import (
    Career,
    CareerStage,
    Character,
    CharacterCareer,
    Organization,
    OrganizationMember,
    Project,
    ProjectNote,
    Relationship,
    RelationshipType,
    SettingTimeline,
)
from app.schemas.setting_fields import EntityRef, EntryFieldsOut
from app.services import setting_catalog_service, setting_field_service
from app.services.setting_field_formatting import format_entry_fields

_BATCH_SIZE = 200


@dataclass(frozen=True)
class MaterialSection:
    """供文档渲染使用的纯值；笔记正文的字段标签为空。"""

    title: str
    fields: list[tuple[str, str]]


_KIND_ORDER = ("character", "world", "organization", "career")
_LABELS = {"character": "人物", "world": "世界观", "organization": "组织", "career": "职业"}
_WORLD_CATEGORIES = {
    "place": "地点", "faction": "势力", "item": "物品", "rule": "规则", "history": "历史", "other": "其他",
}
_LIFE_STATUSES = {"alive": "存活", "dead": "死亡", "missing": "失踪", "other": "其他"}
_MEMBER_STATUSES = {"active": "在职", "left": "离开", "expelled": "除名", "dead": "死亡"}
_RELATION_STATUSES = {"normal": "正常", "broken": "破裂", "past": "过去", "complicated": "复杂"}
_CAREER_KINDS = {"main": "主修", "sub": "副修"}

# 明确可读档案白名单，不从 ORM 导出 AI 配置、数据库身份或内部元数据。
_ENTITY_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "character": (
        ("role_type", "定位"), ("profile", "简介"), ("relations_text", "旧关系备注"),
        ("aliases", "别名"), ("setting_tags", "标签"), ("age", "年龄"), ("gender", "性别"),
        ("appearance", "外貌"), ("personality", "性格"), ("background", "背景"),
        ("current_state", "当前处境"), ("life_status", "生存状态"),
    ),
    "world": (("category", "分类"), ("aliases", "别名"), ("setting_tags", "标签"), ("content", "内容")),
    "organization": (
        ("org_type", "类型"), ("purpose", "宗旨"), ("location", "驻地"), ("power_level", "势力"),
        ("description", "简介"), ("setting_tags", "标签"),
    ),
    "career": (
        ("kind", "类型"), ("description", "简介"), ("requirements", "获得条件"), ("abilities", "特殊能力"),
        ("rules", "体系规则"), ("setting_tags", "标签"),
    ),
}


def _batches[T](values: Sequence[T]) -> Iterator[Sequence[T]]:
    for start in range(0, len(values), _BATCH_SIZE):
        yield values[start:start + _BATCH_SIZE]


def _readable(kind: str, name: str, value: Any) -> str:
    if value is None:
        return ""
    if name in {"aliases", "setting_tags"}:
        return "、".join(value)
    if kind == "world" and name == "category":
        return _WORLD_CATEGORIES.get(value, value)
    if kind == "character" and name == "life_status":
        return _LIFE_STATUSES.get(value, value)
    if kind == "career" and name == "kind":
        return _CAREER_KINDS.get(value, value)
    return str(value)


async def _version_stamp(db: AsyncSession, project_id: int, refs: Sequence[EntityRef]) -> tuple:
    """读取字段目录及所有选中条目的时间线代次；包含时间线新增和删除。"""
    project = (await db.execute(select(Project.fields_revision, Project.ai_visibility_revision)
                                .where(Project.id == project_id))).one_or_none()
    if project is None:
        raise NotFoundError("项目不存在")
    timelines: list[tuple[str, int, int, int]] = []
    for batch in _batches(refs):
        for kind, column_name in setting_catalog_service.TARGET_COLUMNS.items():
            ids = [ref.id for ref in batch if ref.kind == kind]
            if not ids:
                continue
            subject_kind = "career_entry" if kind == "career" else kind
            column = getattr(SettingTimeline, column_name)
            rows = await db.execute(select(column, SettingTimeline.id, SettingTimeline.revision).where(
                SettingTimeline.project_id == project_id,
                SettingTimeline.subject_kind == subject_kind,
                column.in_(ids),
            ))
            timelines.extend((kind, entry_id, timeline_id, revision) for entry_id, timeline_id, revision in rows)
    return project.fields_revision, project.ai_visibility_revision, tuple(sorted(timelines))


async def _entities(db: AsyncSession, project_id: int, refs: Sequence[EntityRef]) -> dict[tuple[str, int], dict]:
    selected = {(ref.kind, ref.id) for ref in refs}
    found: dict[tuple[str, int], dict] = {}
    for kind in _KIND_ORDER:
        model = setting_catalog_service.ENTRY_MODELS[kind]
        ids = sorted(ref.id for ref in refs if ref.kind == kind)
        name = model.title if kind == "world" else model.name
        columns = [model.id, model.sort, name.label("entry_name")]
        columns.extend(getattr(model, field).label(field) for field, _ in _ENTITY_FIELDS[kind])
        if kind == "organization":
            columns.append(model.parent_id)
        for batch in _batches(ids):
            result = await db.execute(select(*columns).where(model.project_id == project_id, model.id.in_(batch)))
            for row in result.mappings():
                found[kind, row["id"]] = dict(row)
    if set(found) != selected:
        raise NotFoundError("设定条目不存在")
    return found


def _add_field(section: MaterialSection, label: str, value: str) -> None:
    if value:
        section.fields.append((label, value))


def _add_custom_fields(section: MaterialSection, projection: EntryFieldsOut) -> None:
    for field in [*projection.fields, *projection.legacy_fields]:
        if field.field_type in {"text", "line"}:
            # 通用格式器用于简短 AI 参考，会 strip 字段；可阅读文档须保留作者的原始文本。
            value = field.value if isinstance(field.value, str) else ""
        else:
            single = projection.model_copy(update={"fields": [field], "legacy_fields": []})
            rendered = format_entry_fields(single)
            value = rendered.removeprefix(f"{field.label}：") if rendered else ""
        _add_field(section, f"自定义字段 · {field.label}", value)


async def _organization_parents(
    db: AsyncSession, project_id: int, entities: dict[tuple[str, int], dict],
    sections: dict[tuple[str, int], MaterialSection],
) -> None:
    parent_ids = sorted({row["parent_id"] for (kind, _), row in entities.items()
                         if kind == "organization" and row["parent_id"] is not None})
    parents = {}
    for batch in _batches(parent_ids):
        parents.update(dict((await db.execute(select(Organization.id, Organization.name).where(
            Organization.project_id == project_id, Organization.id.in_(batch),
        ))).all()))
    for key, row in entities.items():
        if key[0] == "organization" and row["parent_id"] in parents:
            _add_field(sections[key], "上级组织", parents[row["parent_id"]])


async def _relationships(
    db: AsyncSession, project_id: int, sections: dict[tuple[str, int], MaterialSection],
    order: dict[tuple[str, int], int],
) -> None:
    ids = sorted(entry_id for kind, entry_id in sections if kind == "character")
    left, right = aliased(Character), aliased(Character)
    records = {}
    for batch in _batches(ids):
        stmt = (select(Relationship.id, Relationship.a_id, Relationship.b_id, Relationship.intimacy,
                       Relationship.status, Relationship.description, left.name.label("a_name"),
                       right.name.label("b_name"), RelationshipType.role_a, RelationshipType.role_b)
                .join(left, and_(left.id == Relationship.a_id, left.project_id == project_id))
                .join(right, and_(right.id == Relationship.b_id, right.project_id == project_id))
                .join(RelationshipType, RelationshipType.id == Relationship.type_id)
                .where(Relationship.project_id == project_id, Relationship.is_present.is_(True),
                       or_(RelationshipType.project_id.is_(None), RelationshipType.project_id == project_id),
                       or_(Relationship.a_id.in_(batch), Relationship.b_id.in_(batch))))
        for row in (await db.execute(stmt)).mappings():
            records[row["id"]] = row
    for row in sorted(records.values(), key=lambda item: item["id"]):
        targets = [("character", entry_id) for entry_id in (row["a_id"], row["b_id"])
                   if ("character", entry_id) in sections]
        target = min(targets, key=order.__getitem__)
        status = _RELATION_STATUSES.get(row["status"], row["status"])
        description = f"；说明：{row['description']}" if row["description"] else ""
        value = (f"{row['a_name']}是{row['b_name']}的{row['role_a']}；"
                 f"{row['b_name']}是{row['a_name']}的{row['role_b']}；"
                 f"状态：{status}；亲密度：{row['intimacy']}{description}")
        _add_field(sections[target], "人物关系", value)


async def _memberships(
    db: AsyncSession, project_id: int, sections: dict[tuple[str, int], MaterialSection],
    order: dict[tuple[str, int], int],
) -> None:
    records = {}
    for kind, column in (
        ("character", OrganizationMember.character_id), ("organization", OrganizationMember.organization_id)
    ):
        ids = sorted(entry_id for entry_kind, entry_id in sections if entry_kind == kind)
        for batch in _batches(ids):
            stmt = (select(OrganizationMember.id, OrganizationMember.character_id, OrganizationMember.organization_id,
                           OrganizationMember.position, OrganizationMember.status, OrganizationMember.note,
                           Character.name.label("character_name"), Organization.name.label("organization_name"))
                    .join(Character, and_(Character.id == OrganizationMember.character_id,
                                          Character.project_id == project_id))
                    .join(Organization, and_(Organization.id == OrganizationMember.organization_id,
                                             Organization.project_id == project_id))
                    .where(column.in_(batch)))
            for row in (await db.execute(stmt)).mappings():
                records[row["id"]] = row
    for row in sorted(records.values(), key=lambda item: item["id"]):
        candidates = [("character", row["character_id"]), ("organization", row["organization_id"])]
        target = min((key for key in candidates if key in sections), key=order.__getitem__)
        status = _MEMBER_STATUSES.get(row["status"], row["status"])
        position = f"；职务：{row['position']}" if row["position"] else ""
        note = f"；备注：{row['note']}" if row["note"] else ""
        _add_field(sections[target], "组织成员", f"{row['character_name']}：{row['organization_name']}"
                   f"{position}；状态：{status}{note}")


async def _career_details(
    db: AsyncSession, project_id: int, sections: dict[tuple[str, int], MaterialSection],
    order: dict[tuple[str, int], int],
) -> None:
    career_ids = sorted(entry_id for kind, entry_id in sections if kind == "career")
    for batch in _batches(career_ids):
        rows = await db.execute(select(CareerStage.career_id, CareerStage.position, CareerStage.name,
                                       CareerStage.description).where(CareerStage.career_id.in_(batch))
                                .order_by(CareerStage.career_id, CareerStage.position, CareerStage.id))
        for row in rows:
            description = f"：{row.description}" if row.description else ""
            _add_field(sections["career", row.career_id], "职业阶段", f"{row.position}. {row.name}{description}")

    records = {}
    for kind, column in (("character", CharacterCareer.character_id), ("career", CharacterCareer.career_id)):
        ids = sorted(entry_id for entry_kind, entry_id in sections if entry_kind == kind)
        for batch in _batches(ids):
            stmt = (select(CharacterCareer.id, CharacterCareer.character_id, CharacterCareer.career_id,
                           CharacterCareer.role, CharacterCareer.note, Character.name.label("character_name"),
                           Career.name.label("career_name"), CareerStage.name.label("stage_name"))
                    .join(Character, and_(Character.id == CharacterCareer.character_id,
                                          Character.project_id == project_id))
                    .join(Career, and_(Career.id == CharacterCareer.career_id, Career.project_id == project_id))
                    .join(CareerStage, and_(CareerStage.id == CharacterCareer.stage_id,
                                            CareerStage.career_id == Career.id))
                    .where(column.in_(batch)))
            for row in (await db.execute(stmt)).mappings():
                records[row["id"]] = row
    for row in sorted(records.values(), key=lambda item: item["id"]):
        candidates = [("character", row["character_id"]), ("career", row["career_id"])]
        target = min((key for key in candidates if key in sections), key=order.__getitem__)
        role = _CAREER_KINDS.get(row["role"], row["role"])
        note = f"；备注：{row['note']}" if row["note"] else ""
        _add_field(sections[target], "职业挂接", f"{row['character_name']}：{row['career_name']}（{role}）"
                   f"；阶段：{row['stage_name']}{note}")


async def _notes(db: AsyncSession, project_id: int, note_ids: Sequence[int]) -> list[MaterialSection]:
    rows = []
    for batch in _batches(note_ids):
        result = await db.execute(select(ProjectNote.id, ProjectNote.title, ProjectNote.content,
                                         ProjectNote.is_pinned, ProjectNote.updated_at).where(
                                             ProjectNote.project_id == project_id, ProjectNote.id.in_(batch)))
        rows.extend(result.all())
    if {row.id for row in rows} != set(note_ids):
        raise NotFoundError("笔记不存在")
    rows.sort(key=lambda row: (row.is_pinned, row.updated_at, row.id), reverse=True)
    return [MaterialSection(row.title, [("", row.content)]) for row in rows]


async def load_materials(
    db: AsyncSession, project_id: int, settings: Sequence[EntityRef], note_ids: Sequence[int],
) -> tuple[list[MaterialSection], list[MaterialSection]]:
    """读取已授权作品的已选资料，返回独立纯值，不提交、不触发 autoflush。"""
    with db.no_autoflush:
        if not settings:
            return [], await _notes(db, project_id, note_ids)
        before = await _version_stamp(db, project_id, settings)
        entities = await _entities(db, project_id, settings)
        ordered = sorted(entities, key=lambda key: (_KIND_ORDER.index(key[0]), entities[key]["sort"], key[1]))
        sections = {}
        for key in ordered:
            kind, _ = key
            row = entities[key]
            fields = [(label, value) for name, label in _ENTITY_FIELDS[kind]
                      if (value := _readable(kind, name, row[name]))]
            sections[key] = MaterialSection(f"{_LABELS[kind]} · {row['entry_name']}", fields)

        for batch in _batches(settings):
            projections = await setting_field_service.resolve_entry_fields(db, project_id, batch)
            for key, projection in projections.items():
                _add_custom_fields(sections[key], projection)
        order = {key: index for index, key in enumerate(ordered)}
        await _organization_parents(db, project_id, entities, sections)
        await _relationships(db, project_id, sections, order)
        await _memberships(db, project_id, sections, order)
        await _career_details(db, project_id, sections, order)
        if await _version_stamp(db, project_id, settings) != before:
            raise setting_field_service.stale_revision(await setting_field_service.fields_revision(db, project_id))
        notes = await _notes(db, project_id, note_ids)
        return [sections[key] for key in ordered], notes
