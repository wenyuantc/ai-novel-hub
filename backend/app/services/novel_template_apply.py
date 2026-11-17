"""将小说模板的已验证快照应用到新作品；事务由调用者持有。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any, cast

import anyio
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.models import (
    Career,
    CareerStage,
    ChapterPlan,
    Character,
    Organization,
    Outline,
    Project,
    RelationshipType,
    WritingStyle,
)
from app.models.progressions import SettingTextField, SettingTimeline
from app.models.setting_categories import SettingCategory, SettingCategoryRule
from app.models.setting_fields import SettingAiLink, SettingFieldOption
from app.schemas.career import CareerCreate, StageIn
from app.schemas.novel_template_snapshot import NovelTemplateSnapshot
from app.schemas.organization import MemberCreate, OrganizationCreate
from app.schemas.setting_field_value import EntityKind, EntityRef, FieldType
from app.schemas.setting_fields import DefinitionCreate
from app.services import (
    career_service,
    organization_service,
    relationship_service,
    setting_ai_visibility,
    setting_catalog_service,
    setting_category_service,
    setting_field_service,
    setting_image_service,
    story_service,
)
from app.services.setting_field_values import FieldSpec, validate_field_value
from app.services.setting_image_codec import StandardImage

_CHARACTER_FIELDS = (
    "name", "aliases", "role_type", "age", "gender", "appearance", "personality", "background", "profile",
    "relations_text", "current_state", "life_status", "always_in_context", "sort",
)
_WORLD_FIELDS = ("title", "aliases", "category", "content", "sort")
_ORGANIZATION_FIELDS = ("name", "org_type", "purpose", "location", "power_level", "description", "sort")
_CAREER_FIELDS = ("name", "kind", "description", "requirements", "abilities", "rules", "sort")
_STYLE_FIELDS = (
    "name", "description", "pov", "rhythm", "dialogue", "description_density", "tone", "diction", "taboos",
    "sample", "extra", "sort",
)
_CONFIG_FIELDS = (
    "genre", "premise", "synopsis", "theme", "tags", "selling_points", "audience", "target_words", "auto_analyze",
)
_VALIDATION_LIMITER = anyio.CapacityLimiter(2)


def _invalid(detail: str = "小说模板内容不完整或损坏") -> BadRequestError:
    return BadRequestError(detail, extra={"code": "invalid_novel_template"})


async def _apply_config(db: AsyncSession, project: Project, config: Any) -> None:
    if config is None:
        return
    for name in _CONFIG_FIELDS:
        setattr(project, name, getattr(config, name))
    if config.style is None:
        project.default_style_id = None
        return
    values = {name: getattr(config.style, name) for name in _STYLE_FIELDS}
    # 预设或本人风格只有每个冻结字段均一致才可复用，不能单凭 preset_key。
    conditions = [getattr(WritingStyle, name) == value for name, value in values.items()]
    existing = await db.scalar(select(WritingStyle).where(
        or_(WritingStyle.user_id == project.user_id, WritingStyle.user_id.is_(None)), *conditions,
    ).order_by(WritingStyle.id).limit(1).with_for_update())
    if existing is None:
        existing = WritingStyle(user_id=project.user_id, preset_key=None, **values)
        db.add(existing)
        await db.flush()
    project.default_style_id = existing.id


async def _apply_definitions(db: AsyncSession, project_id: int, snapshot: NovelTemplateSnapshot):
    definitions, options, option_sets = {}, {}, {}
    for item in snapshot.field_definitions:
        definition = await setting_field_service.apply_definition(
            db, project_id, DefinitionCreate(
                expected_fields_revision=0, **item.model_dump(exclude={"ref", "options"}),
            ),
        )
        definitions[item.ref] = definition
        option_sets[item.ref] = set()
        for option in item.options:
            row = SettingFieldOption(definition_id=definition.id, **option.model_dump(exclude={"ref"}))
            db.add(row)
            options[option.ref] = row
    await db.flush()
    for item in snapshot.field_definitions:
        option_sets[item.ref].update(options[option.ref].id for option in item.options)
    return definitions, options, option_sets


async def _apply_categories(db: AsyncSession, project_id: int, snapshot: NovelTemplateSnapshot,
                            definitions: dict, options: dict) -> None:
    # 历史快照可包含停用定义/选项；先按 DTO 精确建图，再复用项目图校验。
    for item in snapshot.categories:
        category = SettingCategory(project_id=project_id, name=item.name, applicable_types=item.applicable_types,
                                   match_mode=item.match_mode, sort=item.sort)
        db.add(category)
        await db.flush()
        for rule in item.rules:
            db.add(SettingCategoryRule(
                category_id=category.id, kind=rule.kind, sort=rule.sort, tag=rule.tag,
                definition_id=definitions[rule.definition_ref].id if rule.definition_ref is not None else None,
                option_id=options[rule.option_ref].id if rule.option_ref is not None else None,
            ))
    await db.flush()


async def _apply_relationship_types(db: AsyncSession, project_id: int, snapshot: NovelTemplateSnapshot):
    globals_by_values: dict[tuple, list[RelationshipType]] = defaultdict(list)
    for item in await db.scalars(select(RelationshipType).where(RelationshipType.project_id.is_(None))):
        globals_by_values[item.category, item.role_a, item.role_b, item.sort].append(item)
    planned: dict[str, RelationshipType | None] = {}
    new_pairs: set[tuple[str, str]] = set()
    for item in snapshot.relationship_types:
        matches = globals_by_values[item.category, item.role_a, item.role_b, item.sort]
        match = matches[0] if item.origin == "global" and len(matches) == 1 else None
        if match is None:
            pair = item.role_a, item.role_b
            if pair in new_pairs:
                raise BadRequestError("模板关系类型存在同名冲突，无法创建作品",
                                      extra={"code": "template_relationship_type_conflict"})
            new_pairs.add(pair)
        planned[item.ref] = match
    for item in snapshot.relationship_types:
        if planned[item.ref] is None:
            row = RelationshipType(project_id=project_id, category=item.category, role_a=item.role_a,
                                   role_b=item.role_b, sort=item.sort)
            db.add(row)
            planned[item.ref] = row
    await db.flush()
    return {ref: item.id for ref, item in planned.items() if item is not None}


async def _apply_entries(db: AsyncSession, project_id: int, snapshot: NovelTemplateSnapshot):
    entries: dict[str, tuple[EntityKind, Any, Any]] = {}
    stages: dict[str, CareerStage] = {}
    for item in snapshot.characters:
        values = {name: getattr(item, name) for name in _CHARACTER_FIELDS}
        values.update(setting_ai_visibility.normalize_settings("character", item.ai_settings).model_dump())
        values["setting_tags"] = item.setting_tags
        entries[item.ref] = ("character", await story_service.new_character(db, project_id, values), item)
    for item in snapshot.world_settings:
        values = {name: getattr(item, name) for name in _WORLD_FIELDS}
        values.update(setting_ai_visibility.normalize_settings("world", item.ai_settings).model_dump())
        values["setting_tags"] = item.setting_tags
        entries[item.ref] = ("world", await story_service.new_world(db, project_id, values), item)
    for item in snapshot.organizations:
        values = {name: getattr(item, name) for name in _ORGANIZATION_FIELDS}
        entity = await organization_service.new_org(db, project_id, OrganizationCreate(parent_id=None, **values))
        settings = setting_ai_visibility.normalize_settings("organization", item.ai_settings)
        for key, value in settings.model_dump().items():
            setattr(entity, key, value)
        entity.setting_tags = item.setting_tags
        entries[item.ref] = ("organization", entity, item)
    for item in snapshot.careers:
        values = {name: getattr(item, name) for name in _CAREER_FIELDS}
        career = await career_service.new_career(db, project_id, CareerCreate(
            **values, stages=[StageIn(name=stage.name, description=stage.description) for stage in item.stages],
        ))
        for key, value in setting_ai_visibility.normalize_settings("career", item.ai_settings).model_dump().items():
            setattr(career, key, value)
        career.setting_tags = item.setting_tags
        entries[item.ref] = ("career", career, item)
        created_stages = list(await db.scalars(select(CareerStage).where(
            CareerStage.career_id == career.id,
        ).order_by(CareerStage.position, CareerStage.id)))
        if len(created_stages) != len(item.stages):
            raise _invalid()
        stages.update({source.ref: target for source, target in zip(item.stages, created_stages, strict=True)})
    for item in snapshot.organizations:
        if item.parent_ref is not None:
            entries[item.ref][1].parent_id = entries[item.parent_ref][1].id
    await db.flush()
    return entries, stages


async def _apply_fields_and_links(db: AsyncSession, project_id: int, snapshot: NovelTemplateSnapshot,
                                  entries: dict, definitions: dict, options: dict, option_sets: dict) -> None:
    references = [EntityRef(kind=kind, id=entity.id) for kind, entity, _ in entries.values()]
    projected = await setting_catalog_service.get_entries(db, project_id, references)
    by_ref = {ref: projected[kind, entity.id] for ref, (kind, entity, _) in entries.items()}
    required = {link for _, _, item in entries.values() for link in item.ai_link_refs}
    required.update(ref for ref, (_, _, item) in entries.items() if item.ai_link_refs)
    for _, _, item in entries.values():
        for field in item.fields:
            if isinstance(field.value, dict) and "entry_ref" in field.value:
                required.add(field.value["entry_ref"])
    handles = await setting_catalog_service.ensure_reference_handles(
        db, project_id, [by_ref[ref] for ref in sorted(required)],
    )
    handle_ids = frozenset(handle.id for handle in handles.values())
    timelines = list(await db.scalars(select(SettingTimeline).where(SettingTimeline.project_id == project_id)))
    timeline_map = {}
    for timeline in timelines:
        kind = "career" if timeline.subject_kind == "career_entry" else timeline.subject_kind
        if kind in setting_catalog_service.TARGET_COLUMNS:
            timeline_map[kind, getattr(timeline, setting_catalog_service.TARGET_COLUMNS[kind])] = timeline
    for ref, (kind, entity, item) in entries.items():
        timeline = timeline_map.get((kind, entity.id))
        if timeline is None or timeline.baseline_status != "confirmed":
            raise _invalid()
        for index, legacy in enumerate(item.custom_text_fields):
            db.add(SettingTextField(timeline_id=timeline.id, label=legacy.name, definition_id=None,
                                    base_value=legacy.value, ai_hidden=legacy.ai_hidden, sort=index))
        for field in item.fields:
            definition = definitions[field.definition_ref]
            value = field.value
            if value is not None and definition.field_type == "dropdown":
                value = {"option_id": options[value["option_ref"]].id}
            elif value is not None and definition.field_type == "reference":
                target = by_ref[value["entry_ref"]].reference
                value = {"reference_handle_id": handles[target.kind, target.id].id}
            spec = FieldSpec(cast(FieldType, definition.field_type), definition.id,
                             frozenset(option_sets[field.definition_ref]),
                             handle_ids)
            try:
                canonical = validate_field_value(spec, value)
            except ValueError as exc:
                raise _invalid() from exc
            db.add(SettingTextField(timeline_id=timeline.id, definition_id=definition.id, label=None,
                                    base_value=canonical))
        if item.ai_link_refs:
            source = by_ref[ref].reference
            for target_ref in item.ai_link_refs:
                target = by_ref[target_ref].reference
                db.add(SettingAiLink(project_id=project_id, source_handle_id=handles[source.kind, source.id].id,
                                     target_handle_id=handles[target.kind, target.id].id))
    await db.flush()


async def _apply_relationships(db: AsyncSession, project_id: int, snapshot: NovelTemplateSnapshot,
                               entries: dict, stages: dict, type_ids: dict) -> None:
    for item in snapshot.relationships:
        await relationship_service.create_relation(
            db, project_id, a_id=entries[item.a_ref][1].id, b_id=entries[item.b_ref][1].id,
            type_id=type_ids[item.type_ref], intimacy=item.intimacy, status=item.status,
            description=item.description, source="import",
        )
    for item in snapshot.memberships:
        org = cast(Organization, entries[item.organization_ref][1])
        character = cast(Character, entries[item.character_ref][1])
        await organization_service.new_member(db, org, MemberCreate(
            character_id=character.id, position=item.position, status=item.status, note=item.note,
        ))
    for item in snapshot.assignments:
        character = cast(Character, entries[item.character_ref][1])
        career = cast(Career, entries[item.career_ref][1])
        await career_service.assign_new(db, character, career, stage_id=stages[item.stage_ref].id,
                                        role=item.role, note=item.note, source="import")


async def _apply_outlines(db: AsyncSession, project_id: int, snapshot: NovelTemplateSnapshot,
                          entries: dict) -> None:
    volumes: dict[int, int] = {}
    for number, item in enumerate(snapshot.outlines, start=1):
        volume_no = volumes.setdefault(item.volume_no, len(volumes) + 1)
        outline = Outline(project_id=project_id, volume_no=volume_no, volume_title=item.volume_title,
                          chapter_no=number, title=item.title, summary=item.summary, sort=number)
        db.add(outline)
        await db.flush()
        for seq, plan in enumerate(item.plans, start=1):
            db.add(ChapterPlan(project_id=project_id, outline_id=outline.id, seq=seq, title=plan.title,
                               summary=plan.summary, key_events=plan.key_events,
                               character_ids=[entries[ref][1].id for ref in plan.character_refs],
                               emotion=plan.emotion, goal=plan.goal, est_words=plan.est_words, chapter_id=None))
    await db.flush()


async def apply_template(db: AsyncSession, project: Project, snapshot: NovelTemplateSnapshot,
                         images: Mapping[str, StandardImage]) -> None:
    """只写当前快照及新基线，任何失败由调用方整笔回滚；本函数永不提交。"""
    from app.services.novel_template_snapshot import validate_snapshot

    # 校验图与标准图为纯 CPU 操作，在写入模板内容前离开事件循环执行。
    snapshot = await anyio.to_thread.run_sync(validate_snapshot, snapshot, dict(images), limiter=_VALIDATION_LIMITER)
    project_id = project.id
    if project_id is None:
        raise ValueError("模板应用前必须先 flush 新作品")
    await _apply_config(db, project, snapshot.config)
    definitions, options, option_sets = await _apply_definitions(db, project_id, snapshot)
    await _apply_categories(db, project_id, snapshot, definitions, options)
    type_ids = await _apply_relationship_types(db, project_id, snapshot)
    entries, stages = await _apply_entries(db, project_id, snapshot)
    await _apply_fields_and_links(db, project_id, snapshot, entries, definitions, options, option_sets)
    await _apply_relationships(db, project_id, snapshot, entries, stages, type_ids)
    await _apply_outlines(db, project_id, snapshot, entries)
    for ref, image in images.items():
        kind, entity, _ = entries[ref]
        await setting_image_service.set_book_image_without_commit(db, project_id, kind, entity.id, image)
    await db.flush()
    await setting_field_service.validate_project_field_graph(db, project_id)
    await setting_category_service.validate_project_category_graph(db, project_id)
    await setting_ai_visibility.validate_project_links(db, project_id)
