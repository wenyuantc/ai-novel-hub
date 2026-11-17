"""从已授权的一致读事务构建小说模板当前档案快照，不提交。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import anyio
from pydantic import ValidationError
from sqlalchemy import LargeBinary, Text, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, BadRequestError, ConflictError
from app.models import (
    Career,
    CareerStage,
    ChapterPlan,
    Character,
    CharacterCareer,
    Organization,
    OrganizationMember,
    Outline,
    Project,
    Relationship,
    RelationshipType,
    SettingAiLink,
    SettingCategory,
    SettingCategoryRule,
    SettingFieldDefinition,
    SettingFieldOption,
    SettingTextField,
    SettingTimeline,
    WorldSetting,
    WritingStyle,
)
from app.models.setting_images import SettingImageResource
from app.schemas.novel_template_snapshot import (
    MAX_ENTRIES,
    MAX_IMAGE_BYTES,
    MAX_JSON_BYTES,
    MAX_OUTLINES,
    MAX_PLANS,
    NovelTemplateSnapshot,
    TemplateDependency,
    TemplateIncludedDefinitions,
    TemplateSelection,
)
from app.schemas.setting_ai_visibility import MAX_AI_LINKS, StoredAiSettings
from app.schemas.setting_field_value import EntityRef
from app.services import setting_ai_visibility as visibility
from app.services import setting_catalog_service as catalog
from app.services import setting_field_service as fields
from app.services.project_transfer import thumbnails
from app.services.setting_field_values import has_field_content
from app.services.setting_image_codec import MAX_STANDARD_BYTES, StandardImage, validate_standard_image

_MODELS = {"character": Character, "world": WorldSetting, "organization": Organization, "career": Career}
_GROUPS = {"character": "characters", "world": "world_settings", "organization": "organizations", "career": "careers"}
_ATTRS = {
    "character": (
        "name",
        "aliases",
        "role_type",
        "age",
        "gender",
        "appearance",
        "personality",
        "background",
        "profile",
        "relations_text",
        "current_state",
        "life_status",
        "always_in_context",
        "sort",
    ),
    "world": ("title", "aliases", "category", "content", "sort"),
    "organization": ("name", "org_type", "purpose", "location", "power_level", "description", "sort"),
    "career": ("name", "kind", "description", "requirements", "abilities", "rules", "sort"),
}
_TEXT_ATTRS = {
    "character": (
        "name",
        "role_type",
        "age",
        "gender",
        "appearance",
        "personality",
        "background",
        "profile",
        "relations_text",
        "current_state",
        "life_status",
    ),
    "world": ("title", "category", "content"),
    "organization": ("name", "org_type", "purpose", "location", "description"),
    "career": ("name", "kind", "description", "requirements", "abilities", "rules"),
}
_CONFIG = (
    "genre",
    "premise",
    "synopsis",
    "theme",
    "tags",
    "selling_points",
    "audience",
    "target_words",
    "auto_analyze",
)
_STYLE = (
    "name",
    "description",
    "pov",
    "rhythm",
    "dialogue",
    "description_density",
    "tone",
    "diction",
    "taboos",
    "sample",
    "extra",
    "sort",
)
_SNAPSHOT_LIMITER = anyio.CapacityLimiter(2)


@dataclass(frozen=True)
class CaptureResult:
    snapshot: NovelTemplateSnapshot | None
    images: dict[str, StandardImage]
    source_stamp: str
    missing_dependencies: list[TemplateDependency]
    included_definitions: TemplateIncludedDefinitions
    included_relations: int
    omitted_relations: int
    counts: dict[str, int]
    estimated_bytes: int
    json_bytes: int
    image_bytes: int
    warnings: list[str]


def _too_large() -> BadRequestError:
    return BadRequestError("模板内容过大，请减少所选设定、大纲或图片", extra={"code": "template_too_large"})


def _invalid() -> BadRequestError:
    return BadRequestError("小说模板快照无效", extra={"code": "invalid_novel_template"})


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _without_missing_dependencies(data: dict[str, Any]) -> dict[str, Any]:
    """仅为校验暂去缺失边；不改变预览内容，也不能作为待保存快照。"""
    partial = deepcopy(data)
    for group in ("characters", "world_settings", "organizations", "careers"):
        for entry in partial[group]:
            entry["ai_link_refs"] = [ref for ref in entry["ai_link_refs"] if not ref.startswith("missing.")]
            entry["fields"] = [
                field
                for field in entry["fields"]
                if not (isinstance(field["value"], dict) and field["value"].get("entry_ref", "").startswith("missing."))
            ]
            if group == "organizations" and (entry["parent_ref"] or "").startswith("missing."):
                entry["parent_ref"] = None
    partial["assignments"] = [item for item in partial["assignments"] if not item["career_ref"].startswith("missing.")]
    for outline in partial["outlines"]:
        for plan in outline["plans"]:
            plan["character_refs"] = [ref for ref in plan["character_refs"] if not ref.startswith("missing.")]
    return partial


def _chunks(items: list[Any], size: int = 400):
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def _byte_length(db: AsyncSession, column):
    """文本实际 UTF-8 字节数；只计源字符串作为 JSON 字节的安全下界。"""
    text = cast(column, Text)
    if db.get_bind().dialect.name == "postgresql":
        return func.octet_length(text)
    return func.length(cast(text, LargeBinary))


async def _selected_text_size(
    db: AsyncSession, project_id: int, model, ids: list[int], columns: tuple[str, ...]
) -> int:
    total = 0
    for batch in _chunks(ids):
        row = (
            await db.execute(
                select(*(func.coalesce(func.sum(_byte_length(db, getattr(model, name))), 0) for name in columns)).where(
                    model.project_id == project_id, model.id.in_(batch)
                )
            )
        ).one()
        total += sum(row)
        if total > MAX_JSON_BYTES:
            raise _too_large()
    return total


def validate_snapshot(
    snapshot: NovelTemplateSnapshot | dict, images: dict[str, StandardImage]
) -> NovelTemplateSnapshot:
    """使用前重新校验白名单、全图和独立图像，不能信任数据库中的 JSON。"""
    try:
        raw = snapshot.model_dump(mode="json") if isinstance(snapshot, NovelTemplateSnapshot) else snapshot
        payload = _json_bytes(raw)
        if len(payload) > MAX_JSON_BYTES:
            raise _too_large()
        value = NovelTemplateSnapshot.model_validate(raw)
        expected = {
            entry.image_ref
            for entries in (value.characters, value.world_settings, value.organizations, value.careers)
            for entry in entries
            if entry.image_ref is not None
        }
        if set(images) != expected:
            raise _invalid()
        total = 0
        for image in images.values():
            if not isinstance(image, StandardImage):
                raise _invalid()
            total += len(image.data)
            if total > MAX_IMAGE_BYTES:
                raise _too_large()
            try:
                verified = validate_standard_image(image.data, image.mime_type)
            except AppError as exc:
                raise _invalid() from exc
            if (verified.width, verified.height, verified.sha256) != (image.width, image.height, image.sha256):
                raise _invalid()
        return value
    except AppError:
        raise
    except (ValidationError, ValueError, TypeError, OverflowError) as exc:
        raise _invalid() from exc


async def _load_selected(db: AsyncSession, project_id: int, kind: str, ids: list[int]) -> list[Any]:
    model = _MODELS[kind]
    rows = []
    for batch in _chunks(ids):
        rows.extend((await db.scalars(select(model).where(model.project_id == project_id, model.id.in_(batch)))).all())
    if len(rows) != len(ids):
        raise BadRequestError("部分所选设定不存在或不属于该作品")
    return sorted(rows, key=lambda row: (row.sort, row.id))


async def _load_images(
    db: AsyncSession,
    project_id: int,
    entries: dict[str, list[Any]],
    local: dict[tuple[str, int], str],
) -> tuple[dict[str, StandardImage], dict[str, str]]:
    versions = await thumbnails.versions_for_entries(
        db, project_id, {kind: [item.id for item in rows] for kind, rows in entries.items()}
    )
    version_ids = list(dict.fromkeys(versions.values()))
    sizes = {}
    actual_sizes = {}
    binary_length = (
        func.octet_length(SettingImageResource.data)
        if db.get_bind().dialect.name == "postgresql"
        else func.length(SettingImageResource.data)
    )
    for batch in _chunks(version_ids):
        rows = await db.execute(
            select(SettingImageResource.id, SettingImageResource.byte_size, binary_length).where(
                SettingImageResource.id.in_(batch)
            )
        )
        for version, declared, actual in rows:
            sizes[version], actual_sizes[version] = declared, actual
    if len(sizes) != len(version_ids):
        raise ConflictError("源图片已变化，请重新预览", extra={"code": "template_source_changed"})
    if (
        sum(sizes[version] for version in versions.values()) > MAX_IMAGE_BYTES
        or sum(actual_sizes[version] for version in versions.values()) > MAX_IMAGE_BYTES
    ):
        raise _too_large()
    if any(size != actual_sizes[version] or size > MAX_STANDARD_BYTES for version, size in sizes.items()):
        raise _invalid()
    images: dict[str, StandardImage] = {}
    stamps: dict[str, str] = {}
    pairs = list(versions.items())
    actual_total = 0
    for batch in _chunks(pairs, 4):
        rows = await db.execute(
            select(
                SettingImageResource.id,
                SettingImageResource.mime_type,
                SettingImageResource.data,
                SettingImageResource.width,
                SettingImageResource.height,
                SettingImageResource.sha256,
            ).where(
                binary_length <= MAX_STANDARD_BYTES,
                or_(
                    *(
                        and_(
                            SettingImageResource.id == version,
                            SettingImageResource.byte_size == sizes[version],
                            binary_length == actual_sizes[version],
                        )
                        for _, version in batch
                    )
                ),
            )
        )
        by_version = {row.id: row for row in rows}
        for key, version in batch:
            row = by_version.get(version)
            if row is None:
                raise ConflictError("源图片已变化，请重新预览", extra={"code": "template_source_changed"})
            actual_total += len(row.data)
            if actual_total > MAX_IMAGE_BYTES:
                raise _too_large()
            if len(row.data) != sizes[version]:
                raise _invalid()
            try:
                image = await anyio.to_thread.run_sync(
                    validate_standard_image,
                    row.data,
                    row.mime_type,
                    limiter=_SNAPSHOT_LIMITER,
                )
            except AppError as exc:
                raise _invalid() from exc
            if (image.width, image.height, image.sha256) != (row.width, row.height, row.sha256):
                raise _invalid()
            ref = local[key]
            images[ref] = image
            stamps[ref] = f"{version}:{image.sha256}"
    return images, stamps


async def capture(db: AsyncSession, project_id: int, selection: TemplateSelection) -> CaptureResult:
    """从 caller 已创建的一致读快照提取内容；不授权、不提交、不取得系列锁。"""
    try:
        return await _capture(db, project_id, selection)
    except AppError:
        raise
    except (ValidationError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise _invalid() from exc


async def _capture(db: AsyncSession, project_id: int, selection: TemplateSelection) -> CaptureResult:
    selection = TemplateSelection.model_validate(selection)
    if (
        sum(
            len(items)
            for items in (
                selection.character_ids,
                selection.world_ids,
                selection.organization_ids,
                selection.career_ids,
            )
        )
        > MAX_ENTRIES
        or len(selection.outline_ids) > MAX_OUTLINES
    ):
        raise _too_large()
    with db.no_autoflush:
        project = await db.scalar(select(Project).where(Project.id == project_id))
        if project is None:
            raise BadRequestError("来源作品不存在")
        source_ids = {
            "character": selection.character_ids,
            "world": selection.world_ids,
            "organization": selection.organization_ids,
            "career": selection.career_ids,
        }
        text_size = 0
        for kind, ids in source_ids.items():
            text_size += await _selected_text_size(db, project_id, _MODELS[kind], ids, _TEXT_ATTRS[kind])
            if text_size > MAX_JSON_BYTES:
                raise _too_large()
        groups = {
            "character": await _load_selected(db, project_id, "character", selection.character_ids),
            "world": await _load_selected(db, project_id, "world", selection.world_ids),
            "organization": await _load_selected(db, project_id, "organization", selection.organization_ids),
            "career": await _load_selected(db, project_id, "career", selection.career_ids),
        }
        local = {
            (kind, item.id): f"{kind}.{index}" for kind, rows in groups.items() for index, item in enumerate(rows, 1)
        }
        kind_labels = {"character": "角色", "world": "世界观", "organization": "组织", "career": "职业"}
        display = {
            (kind, item.id): f"{kind_labels[kind]}「{item.title if kind == 'world' else item.name}」"
            for kind, rows in groups.items()
            for item in rows
        }
        refs = [EntityRef(kind=kind, id=item.id) for kind, rows in groups.items() for item in rows]
        selected_definitions: set[int] = set()
        for kind, ids in source_ids.items():
            for batch in _chunks(ids):
                selected_definitions.update(
                    await db.scalars(
                        select(SettingTextField.definition_id)
                        .join(SettingTimeline, SettingTimeline.id == SettingTextField.timeline_id)
                        .where(
                            SettingTimeline.project_id == project_id,
                            SettingTimeline.subject_kind == ("career_entry" if kind == "career" else kind),
                            getattr(SettingTimeline, catalog.TARGET_COLUMNS[kind]).in_(batch),
                            SettingTextField.definition_id.is_not(None),
                        )
                        .distinct()
                        .limit(501)
                    )
                )
                if len(selected_definitions) > 500:
                    raise _too_large()
        scopes = {}
        for batch in _chunks(list(selected_definitions)):
            rows = await db.execute(
                select(SettingFieldDefinition.id, SettingFieldDefinition.applicable_types).where(
                    SettingFieldDefinition.project_id == project_id, SettingFieldDefinition.id.in_(batch)
                )
            )
            scopes.update(dict(rows.all()))
        if set(scopes) != selected_definitions:
            raise _invalid()
        resolved = {}
        # 每条时间线最多 1000 个共享/旧字段；窗口子查询两次引用字段身份，避免超过 PG 参数上限。
        for batch in _chunks(refs, 10):
            projected = await fields.resolve_entry_fields(
                db,
                project_id,
                batch,
                definition_ids=selected_definitions,
                include_legacy=True,
                current_limit_bytes=MAX_JSON_BYTES,
            )
            for key, result in projected.items():
                # 空槽不进入快照；每批立即舍弃，避免 2000×500 个目录空槽常驻内存。
                bound = []
                for slot in result.fields:
                    if slot.field_id is None:
                        continue
                    if result.entry.type not in scopes[slot.definition_id]:
                        if has_field_content(slot.value):
                            raise _invalid()
                        continue
                    bound.append(slot)
                # 每个实际字段的 JSON 必然包含键名及分隔符；这是保守的体积下界。
                text_size += 20 * (len(bound) + len(result.legacy_fields))
                if text_size > MAX_JSON_BYTES:
                    raise _too_large()
                for slot in [*bound, *result.legacy_fields]:
                    if isinstance(slot.value, str):
                        text_size += len(slot.value.encode("utf-8"))
                        if text_size > MAX_JSON_BYTES:
                            raise _too_large()
                resolved[key] = result.model_copy(update={"fields": bound})
        links = await visibility.load_link_targets(db, project_id, local.keys())
        for source, targets in links.items():
            if len(targets) > MAX_AI_LINKS or len(set(targets)) != len(targets) or source in targets:
                raise _invalid()
        # 被损坏的目标不能被 load_link_targets 的内连接悄悄忽略。
        for batch in _chunks(list(local), 200):
            sources = await catalog.find_reference_handles(db, project_id, batch)
            inverse = {handle.id: key for key, handle in sources.items()}
            if inverse:
                counts = await db.execute(
                    select(SettingAiLink.source_handle_id, func.count())
                    .where(SettingAiLink.project_id == project_id, SettingAiLink.source_handle_id.in_(inverse))
                    .group_by(SettingAiLink.source_handle_id)
                )
                for source_id, count in counts:
                    if count != len(links.get(inverse[source_id], ())):
                        raise _invalid()
        missing: dict[tuple[str, int], list[str]] = {}

        def require(kind: str, ident: int, reason: str) -> str:
            key = (kind, ident)
            if key not in local:
                missing.setdefault(key, []).append(reason)
                return f"missing.{kind}.{ident}"
            return local[key]

        definition_ids = {
            slot.definition_id for value in resolved.values() for slot in value.fields if slot.field_id is not None
        }
        stmt = select(SettingFieldDefinition).where(SettingFieldDefinition.project_id == project_id)
        if not selection.include_config:
            stmt = stmt.where(SettingFieldDefinition.id.in_(definition_ids))
        definitions = list(
            await db.scalars(stmt.order_by(SettingFieldDefinition.sort, SettingFieldDefinition.id).limit(501))
        )
        if len(definitions) > 500:
            raise _too_large()
        by_definition = {item.id: item for item in definitions}
        used_fields = {
            key: [
                slot
                for slot in value.fields
                if slot.field_id is not None
                and slot.definition_id in by_definition
                and (
                    value.entry.type in by_definition[slot.definition_id].applicable_types
                    or has_field_content(slot.value)
                )
            ]
            for key, value in resolved.items()
        }
        for key, slots in used_fields.items():
            for slot in slots:
                definition = by_definition[slot.definition_id]
                if resolved[key].entry.type not in definition.applicable_types:
                    raise _invalid()
                if slot.field_type == "reference" and slot.value is not None:
                    if slot.reference is None or slot.reference.type not in definition.reference_types:
                        raise _invalid()
        if not selection.include_config:
            wanted = {slot.definition_id for slots in used_fields.values() for slot in slots}
            definitions = [item for item in definitions if item.id in wanted]
        definition_refs = {item.id: f"definition.{index}" for index, item in enumerate(definitions, 1)}
        used_option_ids = {
            slot.value["option_id"]
            for slots in used_fields.values()
            for slot in slots
            if slot.field_type == "dropdown" and slot.value is not None
        }
        text_size += sum(len(item.label.encode("utf-8")) for item in definitions)
        option_batches = list(_chunks(list(definition_refs) if selection.include_config else sorted(used_option_ids)))
        option_counts: dict[int, int] = {}
        for batch in option_batches:
            column = SettingFieldOption.definition_id if selection.include_config else SettingFieldOption.id
            budgets = await db.execute(
                select(
                    SettingFieldOption.definition_id,
                    func.count(),
                    func.coalesce(func.sum(_byte_length(db, SettingFieldOption.label)), 0),
                )
                .where(column.in_(batch))
                .group_by(SettingFieldOption.definition_id)
            )
            for definition_id, count, size in budgets:
                option_counts[definition_id] = option_counts.get(definition_id, 0) + count
                if option_counts[definition_id] > 500:
                    raise _invalid()
                # 每个实际选项含有 ref、名称、sort 和 archived；60 字节是严格下界。
                text_size += count * 60 + size
                if text_size > MAX_JSON_BYTES:
                    raise _too_large()
        options = []
        for batch in option_batches:
            column = SettingFieldOption.definition_id if selection.include_config else SettingFieldOption.id
            rows = await db.stream(
                select(
                    SettingFieldOption.id,
                    SettingFieldOption.definition_id,
                    SettingFieldOption.label,
                    SettingFieldOption.sort,
                    SettingFieldOption.archived,
                )
                .where(column.in_(batch))
                .order_by(SettingFieldOption.definition_id, SettingFieldOption.sort, SettingFieldOption.id)
                .execution_options(yield_per=100)
            )
            async for row in rows:
                options.append(row)
        options.sort(key=lambda item: (item.definition_id, item.sort, item.id))
        by_options: dict[int, list[Any]] = {}
        option_refs = {}
        for option in options:
            bucket = by_options.setdefault(option.definition_id, [])
            bucket.append(option)
            option_refs[option.id] = f"{definition_refs[option.definition_id]}.option.{len(bucket)}"
        data: dict[str, Any] = {
            "format": "ainovel.novel-template",
            "version": 1,
            "config": None,
            "field_definitions": [],
            "categories": [],
            "characters": [],
            "world_settings": [],
            "organizations": [],
            "careers": [],
            "relationship_types": [],
            "relationships": [],
            "memberships": [],
            "assignments": [],
            "outlines": [],
        }
        for definition in definitions:
            data["field_definitions"].append(
                {
                    "ref": definition_refs[definition.id],
                    **{
                        key: getattr(definition, key)
                        for key in (
                            "label",
                            "field_type",
                            "applicable_types",
                            "reference_types",
                            "sort",
                            "archived",
                            "ai_hidden",
                        )
                    },
                    "options": [
                        {
                            "ref": option_refs[option.id],
                            "label": option.label,
                            "sort": option.sort,
                            "archived": option.archived,
                        }
                        for option in by_options.get(definition.id, [])
                    ],
                }
            )
        if selection.include_config:
            config = {name: getattr(project, name) for name in _CONFIG}
            config["style"] = None
            if project.default_style_id is not None:
                style_length = await db.scalar(
                    select(
                        func.coalesce(_byte_length(db, WritingStyle.sample), 0)
                        + func.coalesce(_byte_length(db, WritingStyle.extra), 0)
                    ).where(
                        WritingStyle.id == project.default_style_id,
                        or_(WritingStyle.user_id.is_(None), WritingStyle.user_id == project.user_id),
                    )
                )
                if style_length is None:
                    raise _invalid()
                text_size += style_length
                if text_size > MAX_JSON_BYTES:
                    raise _too_large()
                style = await db.scalar(
                    select(WritingStyle).where(
                        WritingStyle.id == project.default_style_id,
                        or_(WritingStyle.user_id.is_(None), WritingStyle.user_id == project.user_id),
                    )
                )
                if style is None:
                    raise _invalid()
                config["style"] = {name: getattr(style, name) for name in _STYLE}
            data["config"] = config
            category_count, category_size = (
                await db.execute(
                    select(func.count(), func.coalesce(func.sum(_byte_length(db, SettingCategory.name)), 0)).where(
                        SettingCategory.project_id == project_id
                    )
                )
            ).one()
            rule_count, rule_size = (
                await db.execute(
                    select(
                        func.count(),
                        func.coalesce(func.sum(func.coalesce(_byte_length(db, SettingCategoryRule.tag), 0)), 0),
                    )
                    .select_from(SettingCategoryRule)
                    .join(SettingCategory, SettingCategory.id == SettingCategoryRule.category_id)
                    .where(SettingCategory.project_id == project_id)
                )
            ).one()
            # 分类及规则的 JSON 键、局部 ref 至少分别占 50 和 60 字节。
            text_size += category_count * 50 + category_size + rule_count * 60 + rule_size
            if text_size > MAX_JSON_BYTES:
                raise _too_large()
            category_rows = []
            category_stream = await db.stream(
                select(
                    SettingCategory.id,
                    SettingCategory.name,
                    SettingCategory.applicable_types,
                    SettingCategory.match_mode,
                    SettingCategory.sort,
                )
                .where(SettingCategory.project_id == project_id)
                .order_by(SettingCategory.sort, SettingCategory.id)
                .execution_options(yield_per=100)
            )
            async for row in category_stream:
                category_rows.append(row)
            by_category: dict[int, list[Any]] = {}
            for batch in _chunks([row.id for row in category_rows]):
                rule_stream = await db.stream(
                    select(
                        SettingCategoryRule.category_id,
                        SettingCategoryRule.kind,
                        SettingCategoryRule.sort,
                        SettingCategoryRule.tag,
                        SettingCategoryRule.definition_id,
                        SettingCategoryRule.option_id,
                    )
                    .where(SettingCategoryRule.category_id.in_(batch))
                    .order_by(SettingCategoryRule.category_id, SettingCategoryRule.sort, SettingCategoryRule.id)
                    .execution_options(yield_per=100)
                )
                async for rule in rule_stream:
                    bucket = by_category.setdefault(rule.category_id, [])
                    bucket.append(rule)
                    if len(bucket) > 20:
                        raise _invalid()
            for index, category in enumerate(category_rows, 1):
                data["categories"].append(
                    {
                        "ref": f"category.{index}",
                        "name": category.name,
                        "applicable_types": category.applicable_types,
                        "match_mode": category.match_mode,
                        "sort": category.sort,
                        "rules": [
                            {
                                "kind": rule.kind,
                                "sort": rule.sort,
                                "tag": rule.tag,
                                "definition_ref": definition_refs.get(rule.definition_id),
                                "option_ref": option_refs.get(rule.option_id),
                            }
                            for rule in by_category.get(category.id, [])
                        ],
                    }
                )
        for kind, rows in groups.items():
            for item in rows:
                key = (kind, item.id)
                output = {name: getattr(item, name) for name in _ATTRS[kind]}
                output.update(
                    {
                        "ref": local[key],
                        "setting_tags": list(item.setting_tags),
                        "ai_settings": visibility.normalize_settings(
                            kind, StoredAiSettings.model_validate(item)
                        ).model_dump(),
                        "ai_link_refs": [
                            require(*target, f"{display[key]}的 AI 关联") for target in links.get(key, ())
                        ],
                        "image_ref": None,
                        "fields": [],
                        "custom_text_fields": [],
                    }
                )
                projection = resolved[key]
                output["custom_text_fields"] = [
                    {"name": slot.label, "value": slot.value, "ai_hidden": slot.ai_hidden}
                    for slot in projection.legacy_fields
                ]
                for slot in used_fields[key]:
                    public = slot.value
                    if public is not None and slot.field_type == "dropdown":
                        public = {"option_ref": option_refs[public["option_id"]]}
                    elif public is not None and slot.field_type == "reference":
                        public = {
                            "entry_ref": require(public["kind"], public["id"], f"{display[key]}的字段「{slot.label}」")
                        }
                    output["fields"].append({"definition_ref": definition_refs[slot.definition_id], "value": public})
                if kind == "organization":
                    output["parent_ref"] = (
                        require("organization", item.parent_id, f"{display[key]}的上级组织")
                        if item.parent_id is not None
                        else None
                    )
                data[_GROUPS[kind]].append(output)

        stages_by_career: dict[int, list[Any]] = {}
        stage_refs = {}
        for batch in _chunks([item.id for item in groups["career"]]):
            budgets = await db.execute(
                select(
                    CareerStage.career_id,
                    func.count(),
                    func.coalesce(
                        func.sum(_byte_length(db, CareerStage.name) + _byte_length(db, CareerStage.description)), 0
                    ),
                )
                .where(CareerStage.career_id.in_(batch))
                .group_by(CareerStage.career_id)
            )
            for _, count, size in budgets:
                if count > 100:
                    raise _invalid()
                # 阶段对象必有 ref、名称、描述和顺序，60 字节小于其规范 JSON 下界。
                text_size += count * 60 + size
                if text_size > MAX_JSON_BYTES:
                    raise _too_large()
            rows = await db.stream(
                select(
                    CareerStage.id,
                    CareerStage.career_id,
                    CareerStage.position,
                    CareerStage.name,
                    CareerStage.description,
                )
                .where(CareerStage.career_id.in_(batch))
                .order_by(CareerStage.career_id, CareerStage.position, CareerStage.id)
                .execution_options(yield_per=100)
            )
            async for stage in rows:
                bucket = stages_by_career.setdefault(stage.career_id, [])
                bucket.append(stage)
                stage_refs[stage.id] = f"{local['career', stage.career_id]}.stage.{len(bucket)}"
        for item, output in zip(groups["career"], data["careers"], strict=True):
            output["stages"] = [
                {
                    "ref": stage_refs[stage.id],
                    "name": stage.name,
                    "description": stage.description,
                    "position": stage.position,
                }
                for stage in stages_by_career.get(item.id, [])
            ]
        assignments = []
        for batch in _chunks([item.id for item in groups["character"]], 200):
            found = (
                await db.execute(
                    select(CharacterCareer, Career.id, Career.project_id, Career.kind)
                    .join(Career, Career.id == CharacterCareer.career_id)
                    .where(CharacterCareer.character_id.in_(batch))
                    .order_by(CharacterCareer.role, Career.sort, Career.id)
                )
            ).all()
            expected = await db.scalar(
                select(func.count(CharacterCareer.id)).where(CharacterCareer.character_id.in_(batch))
            )
            if len(found) != expected:
                raise _invalid()
            assignments.extend(found)
        stages_for_assignments = {}
        for batch in _chunks(list({assignment.stage_id for assignment, _, _, _ in assignments})):
            stages_for_assignments.update(
                dict(
                    (
                        await db.execute(select(CareerStage.id, CareerStage.career_id).where(CareerStage.id.in_(batch)))
                    ).all()
                )
            )
        assignment_counts: dict[tuple[int, str], int] = {}
        for assignment, career_id, career_project_id, career_kind in assignments:
            count_key = (assignment.character_id, assignment.role)
            assignment_counts[count_key] = assignment_counts.get(count_key, 0) + 1
            if (
                career_project_id != project_id
                or stages_for_assignments.get(assignment.stage_id) != career_id
                or assignment.role != career_kind
                or assignment.role not in ("main", "sub")
                or assignment_counts[count_key] > (1 if assignment.role == "main" else 3)
            ):
                raise _invalid()
            career_ref = require("career", career_id, f"{display['character', assignment.character_id]}的当前职业")
            data["assignments"].append(
                {
                    "character_ref": local["character", assignment.character_id],
                    "career_ref": career_ref,
                    "stage_ref": stage_refs.get(assignment.stage_id, "missing.stage"),
                    "role": assignment.role,
                    "note": assignment.note,
                }
            )

        outlines = []
        text_size += await _selected_text_size(
            db, project_id, Outline, selection.outline_ids, ("volume_title", "title", "summary")
        )
        if text_size > MAX_JSON_BYTES:
            raise _too_large()
        for batch in _chunks(selection.outline_ids):
            outlines.extend(
                (await db.scalars(select(Outline).where(Outline.project_id == project_id, Outline.id.in_(batch)))).all()
            )
        if len(outlines) != len(selection.outline_ids):
            raise BadRequestError("部分所选大纲不存在或不属于该作品")
        outlines.sort(key=lambda item: (item.volume_no, item.sort, item.chapter_no, item.id))
        outline_ids = [item.id for item in outlines]
        plan_count = 0
        plan_json_size = 0
        for batch in _chunks(outline_ids):
            plan_count += (
                await db.scalar(
                    select(func.count())
                    .select_from(ChapterPlan)
                    .where(ChapterPlan.project_id == project_id, ChapterPlan.outline_id.in_(batch))
                )
            ) or 0
            if plan_count > MAX_PLANS:
                raise _too_large()
            plan_size = await db.scalar(
                select(
                    func.coalesce(
                        func.sum(
                            _byte_length(db, ChapterPlan.title)
                            + _byte_length(db, ChapterPlan.summary)
                            + _byte_length(db, ChapterPlan.emotion)
                            + _byte_length(db, ChapterPlan.goal)
                        ),
                        0,
                    )
                ).where(ChapterPlan.project_id == project_id, ChapterPlan.outline_id.in_(batch))
            )
            text_size += plan_size or 0
            if text_size > MAX_JSON_BYTES:
                raise _too_large()
            # 两个 JSON 列均来自本次选择，且会在最终快照中保留或改写为更长的局部引用。
            # SQLite 可能将一个字符编码成六个 \uXXXX 字节；用六倍余量作保守下界，
            # 在驱动解码前阻断损坏的超大单行，同时不因正常的转义存储误拒合法快照。
            plan_json_size += (
                await db.scalar(
                    select(
                        func.coalesce(
                            func.sum(
                                _byte_length(db, ChapterPlan.key_events)
                                + _byte_length(db, ChapterPlan.character_ids)
                            ),
                            0,
                        )
                    ).where(ChapterPlan.project_id == project_id, ChapterPlan.outline_id.in_(batch))
                )
            ) or 0
            if plan_json_size > 6 * (MAX_JSON_BYTES - text_size):
                raise _too_large()
        plans = []
        for batch in _chunks(outline_ids):
            rows = await db.stream_scalars(
                select(ChapterPlan)
                .where(ChapterPlan.project_id == project_id, ChapterPlan.outline_id.in_(batch))
                .order_by(ChapterPlan.outline_id, ChapterPlan.seq, ChapterPlan.id)
                .execution_options(yield_per=100)
            )
            async for plan in rows:
                text_size += sum(len(item.encode("utf-8")) for item in plan.key_events if isinstance(item, str))
                if text_size > MAX_JSON_BYTES:
                    await rows.close()
                    raise _too_large()
                plans.append(plan)
        plans_by_outline: dict[int, list[Any]] = {}
        for plan in plans:
            plans_by_outline.setdefault(plan.outline_id, []).append(plan)
        for index, outline in enumerate(outlines, 1):
            data["outlines"].append(
                {
                    "ref": f"outline.{index}",
                    "volume_no": outline.volume_no,
                    "volume_title": outline.volume_title,
                    "title": outline.title,
                    "summary": outline.summary,
                    "sort": outline.sort,
                    "plans": [
                        {
                            "title": plan.title,
                            "summary": plan.summary,
                            "key_events": list(plan.key_events),
                            "character_refs": [
                                require("character", ident, f"大纲「{outline.title}」的细纲角色")
                                for ident in plan.character_ids
                            ],
                            "emotion": plan.emotion,
                            "goal": plan.goal,
                            "est_words": plan.est_words,
                            "seq": plan.seq,
                        }
                        for plan in plans_by_outline.get(outline.id, [])
                    ],
                }
            )

        char_ids = [item.id for item in groups["character"]]
        org_ids = [item.id for item in groups["organization"]]
        # 未选择的关联只计数，不加载可能很大的正文；入选两端的关系先检查文本下界和数量。
        kept = []
        total_relations = 0
        if char_ids:
            active = (Relationship.project_id == project_id, Relationship.is_present.is_(True))
            total_relations = (
                await db.scalar(
                    select(func.count(Relationship.id)).where(
                        *active,
                        or_(Relationship.a_id.in_(char_ids), Relationship.b_id.in_(char_ids)),
                    )
                )
            ) or 0
            both = (*active, Relationship.a_id.in_(char_ids), Relationship.b_id.in_(char_ids))
            budget = (
                await db.execute(
                    select(
                        func.count(Relationship.id),
                        func.coalesce(func.sum(_byte_length(db, Relationship.description)), 0),
                    ).where(*both)
                )
            ).one()
            text_size += budget[1]
            if budget[0] * 50 + text_size > MAX_JSON_BYTES:
                raise _too_large()
            kept = (
                await db.execute(
                    select(Relationship, RelationshipType)
                    .join(RelationshipType, RelationshipType.id == Relationship.type_id)
                    .where(*both)
                    .order_by(Relationship.id)
                )
            ).all()
            if len(kept) != budget[0]:
                raise _invalid()
        for _, typ in kept:
            if typ.project_id not in (None, project_id):
                raise _invalid()
        type_rows = {typ.id: typ for _, typ in kept}
        type_refs = {ident: f"relationship_type.{index}" for index, ident in enumerate(sorted(type_rows), 1)}
        data["relationship_types"] = [
            {
                "ref": type_refs[ident],
                "origin": "global" if typ.project_id is None else "project",
                "category": typ.category,
                "role_a": typ.role_a,
                "role_b": typ.role_b,
                "sort": typ.sort,
            }
            for ident, typ in sorted(type_rows.items())
        ]
        data["relationships"] = [
            {
                "type_ref": type_refs[relationship.type_id],
                "a_ref": local["character", relationship.a_id],
                "b_ref": local["character", relationship.b_id],
                "intimacy": relationship.intimacy,
                "status": relationship.status,
                "description": relationship.description,
            }
            for relationship, _ in kept
        ]
        kept_members = []
        total_members = 0
        if org_ids or char_ids:
            member_scope = (
                select(OrganizationMember)
                .join(
                    Organization,
                    Organization.id == OrganizationMember.organization_id,
                )
                .join(Character, Character.id == OrganizationMember.character_id)
                .where(
                    Organization.project_id == project_id,
                    Character.project_id == project_id,
                )
            )
            touched = or_(
                OrganizationMember.organization_id.in_(org_ids), OrganizationMember.character_id.in_(char_ids)
            )
            total_members = (
                await db.scalar(select(func.count()).select_from(member_scope.where(touched).subquery()))
            ) or 0
            if org_ids and char_ids:
                both_members = member_scope.where(
                    OrganizationMember.organization_id.in_(org_ids), OrganizationMember.character_id.in_(char_ids)
                )
                member_rows = both_members.subquery()
                member_count, member_size = (
                    await db.execute(
                        select(
                            func.count(),
                            func.coalesce(
                                func.sum(
                                    _byte_length(db, member_rows.c.position) + _byte_length(db, member_rows.c.note)
                                ),
                                0,
                            ),
                        ).select_from(member_rows)
                    )
                ).one()
                text_size += member_size
                if member_count * 50 + text_size > MAX_JSON_BYTES:
                    raise _too_large()
                kept_members = list((await db.scalars(both_members.order_by(OrganizationMember.id))).all())
        data["memberships"] = [
            {
                "organization_ref": local["organization", member.organization_id],
                "character_ref": local["character", member.character_id],
                "position": member.position,
                "status": member.status,
                "note": member.note,
            }
            for member in kept_members
        ]
        omitted = total_relations - len(kept) + total_members - len(kept_members)

        images, image_stamps = await _load_images(db, project_id, groups, local)
        for kind, rows in groups.items():
            for item, output in zip(rows, data[_GROUPS[kind]], strict=True):
                if local[kind, item.id] in images:
                    output["image_ref"] = local[kind, item.id]
        dependencies = []
        if missing:
            external = []
            for batch in _chunks([EntityRef(kind=kind, id=ident) for kind, ident in sorted(missing)], 200):
                external.extend((await catalog.get_entries(db, project_id, batch)).items())
            for key, entry in external:
                dependencies.append(
                    TemplateDependency(
                        kind=key[0], id=key[1], name=entry.name, reason="；".join(dict.fromkeys(missing[key]))[:500]
                    )
                )
        try:
            encoded = _json_bytes(data)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _invalid() from exc
        if len(encoded) > MAX_JSON_BYTES:
            raise _too_large()
        # 展示已选内容实际携带的定义；缺实体依赖时也能完整预览已有结果。
        included_definitions = TemplateIncludedDefinitions.model_validate(
            {
                "fields": [
                    {
                        "name": definition["label"],
                        "field_type": definition["field_type"],
                        "options": [option["label"] for option in definition["options"]],
                    }
                    for definition in data["field_definitions"]
                ],
                "categories": [category["name"] for category in data["categories"]],
                "relationship_types": [
                    {key: definition[key] for key in ("origin", "category", "role_a", "role_b")}
                    for definition in data["relationship_types"]
                ],
                "careers": [
                    {"name": career["name"], "stages": [stage["name"] for stage in career["stages"]]}
                    for career in data["careers"]
                ],
            }
        )
        image_bytes = sum(len(image.data) for image in images.values())
        canonical_selection = selection.model_dump(mode="json")
        canonical_selection = {
            key: sorted(value) if isinstance(value, list) else value for key, value in canonical_selection.items()
        }
        stamp_input = _json_bytes({"snapshot": data, "images": image_stamps, "selection": canonical_selection})
        stamp = hashlib.sha256(stamp_input).hexdigest()
        validated = await anyio.to_thread.run_sync(
            validate_snapshot,
            _without_missing_dependencies(data) if dependencies else data,
            images,
            limiter=_SNAPSHOT_LIMITER,
        )
        snapshot = None if dependencies else validated
        counts = {
            name: len(data[name]) for name in ("characters", "world_settings", "organizations", "careers", "outlines")
        }
        counts.update({"plans": len(plans), "images": len(images)})
        return CaptureResult(
            snapshot=snapshot,
            images=images,
            source_stamp=stamp,
            missing_dependencies=dependencies,
            included_definitions=included_definitions,
            included_relations=len(kept) + len(kept_members),
            omitted_relations=omitted,
            counts=counts,
            estimated_bytes=len(encoded) + image_bytes,
            json_bytes=len(encoded),
            image_bytes=image_bytes,
            warnings=["快照采用当前有效档案值，包含有效的计划章节变化。"],
        )
