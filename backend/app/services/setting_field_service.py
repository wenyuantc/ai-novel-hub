"""共享字段配置、类型化值和历史依赖的唯一持久化边界。

公开包装函数取得人工项目写屏障并提交；apply/ensure/保护 helper 仅 flush，
便于导入、智能体和实体生命周期在同一个事务中组合。
"""

from collections import defaultdict
from collections.abc import Collection, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import cast

from pydantic import ValidationError
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import (
    Project,
    SettingCategory,
    SettingCategoryRule,
    SettingProgression,
    SettingTextField,
    SettingTimeline,
)
from app.models.setting_fields import SettingFieldDefinition, SettingFieldOption, SettingReferenceHandle
from app.schemas import setting_fields as s
from app.schemas.progression import ProgressionCreate, ProgressionMutationOut, StoryPosition
from app.services import setting_catalog_service as catalog
from app.services.audit_writer import AuditOperation
from app.services.project_service import get_manageable_project
from app.services.setting_field_values import (
    CanonicalFieldValue,
    FieldSpec,
    decode_public_value,
    encode_public_value,
    has_field_content,
    validate_field_value,
)


def stale_revision(revision: int, scope: str = "fields") -> ConflictError:
    return ConflictError(
        "设定已被其他操作修改，请刷新后保留草稿重试",
        extra={
            "code": "stale_revision",
            "scope": scope,
            "current_revision": revision,
        },
    )


async def fields_revision(db: AsyncSession, project_id: int) -> int:
    with db.no_autoflush:
        value = await db.scalar(select(Project.fields_revision).where(Project.id == project_id))
    if value is None:
        raise NotFoundError("项目不存在")
    return value


async def check_fields_revision(db: AsyncSession, project_id: int, expected: int | None) -> int:
    if expected is None:
        raise BadRequestError("修改共享字段时必须提供字段目录版本", extra={"code": "fields_revision_required"})
    current = await fields_revision(db, project_id)
    if current != expected:
        raise stale_revision(current)
    return current


async def claim_fields_revision(db: AsyncSession, project_id: int, expected: int) -> int:
    result = await db.execute(
        update(Project)
        .where(
            Project.id == project_id,
            Project.fields_revision == expected,
        )
        .values(fields_revision=Project.fields_revision + 1)
        .execution_options(synchronize_session="fetch")
    )
    if result.rowcount != 1:
        raise stale_revision(await fields_revision(db, project_id))
    return expected + 1


@asynccontextmanager
async def _editing(db: AsyncSession, user_id: int, project_id: int, *, configuration: bool = False):
    async with AuditOperation(db, user_id, "project.update", "project", project_id, project_id=project_id) as audit:
        project = await get_manageable_project(db, user_id, project_id, lock=True, permission="projects.edit")
        audit.target(project)
        audit.summary.changed_fields = ["field_definitions" if configuration else "setting_fields"]
        try:
            yield audit
        except IntegrityError as exc:
            await db.rollback()
            raise ConflictError("字段配置或条目值与现有记录冲突", extra={"code": "field_conflict"}) from exc
        except BaseException:
            await db.rollback()
            raise


async def get_definition(db: AsyncSession, project_id: int, definition_id: int) -> SettingFieldDefinition:
    definition = await db.scalar(
        select(SettingFieldDefinition).where(
            SettingFieldDefinition.project_id == project_id,
            SettingFieldDefinition.id == definition_id,
        )
    )
    if definition is None:
        raise NotFoundError("字段定义不存在")
    return definition


async def _metadata(
    db: AsyncSession,
    project_id: int,
    *,
    definition_ids: Collection[int] | None = None,
) -> tuple[list[s.DefinitionOut], dict[int, list[s.OptionOut]]]:
    if definition_ids is not None and not definition_ids:
        return [], {}
    predicates = [SettingFieldDefinition.project_id == project_id]
    if definition_ids is not None:
        predicates.append(SettingFieldDefinition.id.in_(definition_ids))
    # 标量投影绕过 identity map，但不覆盖调用方尚未保存的 ORM 对象。
    definitions = [
        s.DefinitionOut.model_validate(row)
        for row in await db.execute(
            select(SettingFieldDefinition.__table__)
            .where(
                *predicates,
            )
            .order_by(SettingFieldDefinition.sort, SettingFieldDefinition.id)
        )
    ]
    options: dict[int, list[s.OptionOut]] = defaultdict(list)
    for option in await db.execute(
        select(SettingFieldOption.__table__)
        .join(SettingFieldDefinition)
        .where(
            *predicates,
        )
        .order_by(SettingFieldOption.sort, SettingFieldOption.id)
    ):
        options[option.definition_id].append(s.OptionOut.model_validate(option))
    return definitions, options


def _definition_out(
    definition: SettingFieldDefinition | s.DefinitionOut,
    options: dict[int, list[s.OptionOut]],
) -> s.DefinitionOut:
    return s.DefinitionOut.model_validate(definition).model_copy(update={"options": options.get(definition.id, [])})


async def list_definitions(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    *,
    types: Sequence[s.EntryType] | None = None,
    include_archived: bool = True,
) -> s.DefinitionPage:
    with db.no_autoflush:
        await get_manageable_project(db, user_id, project_id)
        before = await fields_revision(db, project_id)
        definitions, options = await _metadata(db, project_id)
        items = [
            _definition_out(d, options)
            for d in definitions
            if (include_archived or not d.archived) and (types is None or set(d.applicable_types).intersection(types))
        ]
        after = await fields_revision(db, project_id)
        if before != after:
            raise stale_revision(after)
        return s.DefinitionPage(items=items, total=len(items), fields_revision=before)


async def read_definition(db: AsyncSession, user_id: int, project_id: int, definition_id: int) -> s.DefinitionMutation:
    page = await list_definitions(db, user_id, project_id)
    item = next((d for d in page.items if d.id == definition_id), None)
    if item is None:
        raise NotFoundError("字段定义不存在")
    return s.DefinitionMutation(item=item, fields_revision=page.fields_revision)


async def _unique_label(db: AsyncSession, model, predicates, label: str, exclude_id: int | None = None):
    stmt = select(model.id).where(*predicates, model.label == label)
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    if await db.scalar(stmt) is not None:
        code = "duplicate_field_definition" if model is SettingFieldDefinition else "duplicate_field_option"
        raise ConflictError("已存在同名字段或选项", extra={"code": code})


async def apply_definition(
    db: AsyncSession,
    project_id: int,
    data: s.DefinitionCreate | s.DefinitionUpdate,
    definition_id: int | None = None,
) -> SettingFieldDefinition:
    """持项目锁时保存目录；只 flush，不递增目录版本（由调用方整笔声明）。"""
    values = data.model_dump(exclude_unset=True, exclude={"expected_fields_revision"})
    if any(value is None for value in values.values()) or not values:
        raise BadRequestError("请选择有效的字段配置修改")
    definition = await get_definition(db, project_id, definition_id) if definition_id else None
    merged = (
        {
            key: getattr(definition, key)
            for key in (
                "label",
                "field_type",
                "applicable_types",
                "reference_types",
                "sort",
                "archived",
            )
        }
        if definition is not None
        else {}
    )
    merged.update(values)
    try:
        normalized = s.DefinitionCreate(expected_fields_revision=data.expected_fields_revision, **merged)
    except ValidationError as exc:
        raise BadRequestError("字段类型、名称或适用范围不合法") from exc
    values = normalized.model_dump(exclude={"expected_fields_revision"})
    await _unique_label(
        db, SettingFieldDefinition, [SettingFieldDefinition.project_id == project_id], values["label"], definition_id
    )
    if definition is None:
        definition = SettingFieldDefinition(project_id=project_id, **values)
        db.add(definition)
    else:
        await assert_definition_dependencies(db, definition, replacement=normalized)
        if definition.field_type != normalized.field_type:
            await db.execute(delete(SettingFieldOption).where(SettingFieldOption.definition_id == definition.id))
        for key, value in values.items():
            setattr(definition, key, value)
    await db.flush()
    return definition


async def save_definition(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    data: s.DefinitionCreate | s.DefinitionUpdate,
    definition_id: int | None = None,
) -> s.DefinitionMutation:
    async with _editing(db, user_id, project_id, configuration=True) as audit:
        await check_fields_revision(db, project_id, data.expected_fields_revision)
        definition = await apply_definition(db, project_id, data, definition_id)
        revision = await claim_fields_revision(db, project_id, data.expected_fields_revision)
        _, options = await _metadata(db, project_id)
        item = _definition_out(definition, options)
        await audit.commit()
    return s.DefinitionMutation(item=item, fields_revision=revision)


async def _bindings(db: AsyncSession, project_id: int, definition_id: int | None = None):
    stmt = (
        select(
            SettingTextField.id,
            SettingTextField.timeline_id,
            SettingTextField.definition_id,
            SettingTextField.label,
            SettingTextField.base_value,
            SettingTextField.sort,
            SettingTimeline.id,
            SettingTimeline.project_id,
            SettingTimeline.subject_kind,
            SettingTimeline.character_id,
            SettingTimeline.world_setting_id,
            SettingTimeline.organization_id,
            SettingTimeline.career_id,
        )
        .join(SettingTimeline)
        .where(SettingTimeline.project_id == project_id)
    )
    if definition_id is not None:
        stmt = stmt.where(SettingTextField.definition_id == definition_id)
    return [
        (FieldBindingSnapshot(*deepcopy(tuple(row[:6]))), TimelineIdentity(*row[6:])) for row in await db.execute(stmt)
    ]


async def _stored_values(db: AsyncSession, project_id: int, definition_id: int | None = None):
    """全部保留历史，包含 inactive 和失去章锚的事件。"""
    bindings = await _bindings(db, project_id, definition_id)
    values = [(field, timeline, None, field.base_value) for field, timeline in bindings]
    by_id = {field.id: (field, timeline) for field, timeline in bindings}
    if by_id:
        for event in await db.execute(
            select(
                SettingProgression.id,
                SettingProgression.custom_field_id,
                SettingProgression.value,
            ).where(SettingProgression.custom_field_id.in_(by_id))
        ):
            field, timeline = by_id[event.custom_field_id]
            values.append((field, timeline, event.id, event.value))
    return values


@dataclass(frozen=True)
class FieldBindingSnapshot:
    id: int
    timeline_id: int
    definition_id: int | None
    label: str | None
    base_value: CanonicalFieldValue
    sort: int


@dataclass(frozen=True)
class TimelineIdentity:
    id: int
    project_id: int
    subject_kind: str
    character_id: int | None
    world_setting_id: int | None
    organization_id: int | None
    career_id: int | None


async def read_field_bindings(
    db: AsyncSession,
    timeline_ids: Sequence[int],
    *,
    definition_ids: Collection[int] | None = None,
) -> list[FieldBindingSnapshot]:
    """只读值快照，不使用或覆盖 Session 中缓存/待保存的绑定实例。"""
    with db.no_autoflush:
        if definition_ids is not None and not definition_ids:
            return []
        predicates = [SettingTextField.timeline_id.in_(timeline_ids)]
        if definition_ids is not None:
            predicates.append(SettingTextField.definition_id.in_(definition_ids))
        rows = await db.execute(
            select(
                SettingTextField.id,
                SettingTextField.timeline_id,
                SettingTextField.definition_id,
                SettingTextField.label,
                SettingTextField.base_value,
                SettingTextField.sort,
            )
            .where(*predicates)
            .order_by(
                SettingTextField.timeline_id,
                SettingTextField.sort,
                SettingTextField.id,
            )
        )
        return [FieldBindingSnapshot(*deepcopy(tuple(row))) for row in rows]


def _timeline_ref(timeline: SettingTimeline | TimelineIdentity) -> s.EntityRef:
    kind = "career" if timeline.subject_kind == "career_entry" else timeline.subject_kind
    if kind not in catalog.TARGET_COLUMNS:
        raise BadRequestError("该主体不支持共享字段")
    return s.EntityRef(kind=kind, id=getattr(timeline, catalog.TARGET_COLUMNS[kind]))


async def _handle_targets(
    db: AsyncSession,
    project_id: int,
    *,
    handle_ids: Collection[int] | None = None,
) -> dict[int, s.EntityRef]:
    if handle_ids is not None and not handle_ids:
        return {}
    predicates = [SettingReferenceHandle.project_id == project_id]
    if handle_ids is not None:
        predicates.append(SettingReferenceHandle.id.in_(handle_ids))
    result = {}
    for handle in await db.execute(select(SettingReferenceHandle.__table__).where(*predicates)):
        result[handle.id] = s.EntityRef(kind=handle.kind, id=getattr(handle, catalog.TARGET_COLUMNS[handle.kind]))
    return result


async def load_field_specs(
    db: AsyncSession,
    project_id: int,
    fields: Sequence[SettingTextField | FieldBindingSnapshot],
    *,
    definition_ids: Collection[int] | None = None,
) -> dict[int, FieldSpec]:
    """批量预取类型及全部合法历史身份；停用身份仍合法。只读，无 autoflush。"""
    with db.no_autoflush:
        definitions, options = (
            await _metadata(db, project_id)
            if definition_ids is None
            else await _metadata(
                db,
                project_id,
                definition_ids=definition_ids,
            )
        )
        by_id = {definition.id: definition for definition in definitions}
        handle_predicates = [SettingReferenceHandle.project_id == project_id]
        if definition_ids is not None:
            reference_fields = [
                field
                for field in fields
                if by_id.get(field.definition_id) and by_id[field.definition_id].field_type == "reference"
            ]
            if reference_fields:
                values = [field.base_value for field in reference_fields]
                values.extend(
                    await db.scalars(
                        select(SettingProgression.value).where(
                            SettingProgression.custom_field_id.in_([field.id for field in reference_fields]),
                        )
                    )
                )
                handle_ids = {
                    value["reference_handle_id"]
                    for value in values
                    if isinstance(value, dict) and "reference_handle_id" in value
                }
                handle_predicates.append(SettingReferenceHandle.id.in_(handle_ids))
            else:
                handle_predicates = None
        handles = (
            frozenset(
                await db.scalars(
                    select(SettingReferenceHandle.id).where(
                        *handle_predicates,
                    )
                )
            )
            if handle_predicates is not None
            else frozenset()
        )
        result = {}
        for field in fields:
            definition = by_id.get(field.definition_id) if field.definition_id else None
            if field.definition_id is not None and definition is None:
                raise ConflictError("字段缺少同作品的定义", extra={"code": "invalid_field_graph"})
            result[field.id] = FieldSpec(
                cast(s.FieldType, definition.field_type) if definition else "text",
                field.definition_id,
                frozenset(option.id for option in options.get(field.definition_id, [])),
                handles,
            )
        return result


async def assert_definition_change_allowed(
    db: AsyncSession, definition: SettingFieldDefinition, data: s.DefinitionCreate
):
    bindings = await _bindings(db, definition.project_id, definition.id)
    if bindings and definition.field_type != data.field_type:
        raise ConflictError("字段已被条目使用，不能修改类型", extra={"code": "field_in_use"})
    values = await _stored_values(db, definition.project_id, definition.id)
    used = [
        (field, timeline, eid, value)
        for field, timeline, eid, value in values
        if has_field_content(value, has_events=eid is not None)
    ]
    if set(data.applicable_types) != set(definition.applicable_types):
        entries = await catalog.get_entries(
            db, definition.project_id, [_timeline_ref(timeline) for _, timeline, _, _ in used]
        )
        if any(entry.type not in data.applicable_types for entry in entries.values()):
            raise ConflictError("缩小适用范围会排除已有字段或历史", extra={"code": "incompatible_field_scope"})
    if definition.field_type == "reference" and set(data.reference_types) != set(definition.reference_types):
        targets = await _handle_targets(db, definition.project_id)
        refs = [
            targets[value["reference_handle_id"]]
            for _, _, _, value in values
            if isinstance(value, dict) and "reference_handle_id" in value
        ]
        entries = await catalog.get_entries(db, definition.project_id, refs)
        if any(entry.type not in data.reference_types for entry in entries.values()):
            raise ConflictError("缩小引用范围会排除已保存的目标", extra={"code": "incompatible_field_scope"})


async def assert_definition_dependencies(
    db: AsyncSession,
    definition: SettingFieldDefinition,
    *,
    replacement: s.DefinitionCreate | None = None,
) -> None:
    """定义 UPDATE/DELETE 的统一依赖闸门。

    replacement=None 表示删除。规则即使没有绑定/事件，也同时保护类型与适用范围。
    """
    with db.no_autoflush:
        rules = (await db.execute(
            select(SettingCategory.id, SettingCategory.name, SettingCategory.applicable_types)
            .join(SettingCategoryRule, SettingCategoryRule.category_id == SettingCategory.id)
            .where(SettingCategory.project_id == definition.project_id,
                   SettingCategoryRule.definition_id == definition.id)
        )).all()
    if rules:
        sources = [{"category_id": row.id, "category_name": row.name} for row in rules[:20]]
        if replacement is None or replacement.field_type != "dropdown":
            raise ConflictError(
                "字段仍被分类规则使用，请保留其下拉身份", extra={"code": "field_in_use", "sources": sources}
            )
        if any(not set(row.applicable_types).intersection(replacement.applicable_types) for row in rules):
            raise ConflictError("缩小字段适用范围会使分类规则失效",
                                extra={"code": "incompatible_field_scope", "sources": sources})
    if replacement is not None:
        await assert_definition_change_allowed(db, definition, replacement)
        return
    values = await _stored_values(db, definition.project_id, definition.id)
    if any(has_field_content(value, has_events=event_id is not None) for _, _, event_id, value in values):
        raise ConflictError("字段仍有条目值或历史，请停用以保留资料", extra={"code": "field_in_use"})


async def assert_option_unreferenced(db: AsyncSession, project_id: int, definition_id: int, option_id: int) -> None:
    """选项删除同时保护字段全历史与分类规则，不依赖延迟FK报错。"""
    with db.no_autoflush:
        category = (await db.execute(
            select(SettingCategory.id, SettingCategory.name)
            .join(SettingCategoryRule, SettingCategoryRule.category_id == SettingCategory.id)
            .where(SettingCategory.project_id == project_id, SettingCategoryRule.definition_id == definition_id,
                   SettingCategoryRule.option_id == option_id).limit(1)
        )).one_or_none()
    if category is not None:
        raise ConflictError(f"选项仍被分类「{category.name}」使用，请停用以保留规则", extra={
            "code": "option_in_use", "category_id": category.id,
        })
    if any(
        isinstance(value, dict) and value.get("option_id") == option_id
        for _, _, _, value in await _stored_values(db, project_id, definition_id)
    ):
        raise ConflictError("选项仍被字段值或历史使用，请停用以保留资料", extra={"code": "option_in_use"})


async def delete_definition(
    db: AsyncSession, user_id: int, project_id: int, definition_id: int, expected: int
) -> s.FieldRevision:
    async with _editing(db, user_id, project_id, configuration=True) as audit:
        await check_fields_revision(db, project_id, expected)
        definition = await get_definition(db, project_id, definition_id)
        await assert_definition_dependencies(db, definition)
        await db.execute(delete(SettingTextField).where(SettingTextField.definition_id == definition_id))
        await db.delete(definition)
        await db.flush()
        revision = await claim_fields_revision(db, project_id, expected)
        await audit.commit()
    return s.FieldRevision(fields_revision=revision)


async def save_option(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    definition_id: int,
    data: s.OptionCreate | s.OptionUpdate,
    option_id: int | None = None,
) -> s.OptionMutation:
    async with _editing(db, user_id, project_id, configuration=True) as audit:
        await check_fields_revision(db, project_id, data.expected_fields_revision)
        definition = await get_definition(db, project_id, definition_id)
        if definition.field_type != "dropdown":
            raise BadRequestError("只有下拉字段可以设置选项")
        option = (
            await db.scalar(
                select(SettingFieldOption).where(
                    SettingFieldOption.definition_id == definition_id,
                    SettingFieldOption.id == option_id,
                )
            )
            if option_id
            else None
        )
        if option_id and option is None:
            raise NotFoundError("字段选项不存在")
        values = data.model_dump(exclude_unset=True, exclude={"expected_fields_revision"})
        if not values or any(value is None for value in values.values()):
            raise BadRequestError("请选择有效的选项修改")
        if "label" in values:
            values["label"] = values["label"].strip()
            if not values["label"]:
                raise BadRequestError("选项名称不能为空")
            await _unique_label(
                db, SettingFieldOption, [SettingFieldOption.definition_id == definition_id], values["label"], option_id
            )
        if option is None:
            option = SettingFieldOption(definition_id=definition_id, **values)
            db.add(option)
        else:
            for key, value in values.items():
                setattr(option, key, value)
        await db.flush()
        item = s.OptionOut.model_validate(option)
        revision = await claim_fields_revision(db, project_id, data.expected_fields_revision)
        await audit.commit()
    return s.OptionMutation(item=item, fields_revision=revision)


async def delete_option(
    db: AsyncSession, user_id: int, project_id: int, definition_id: int, option_id: int, expected: int
) -> s.FieldRevision:
    async with _editing(db, user_id, project_id, configuration=True) as audit:
        await check_fields_revision(db, project_id, expected)
        await get_definition(db, project_id, definition_id)
        option = await db.scalar(
            select(SettingFieldOption).where(
                SettingFieldOption.id == option_id,
                SettingFieldOption.definition_id == definition_id,
            )
        )
        if option is None:
            raise NotFoundError("字段选项不存在")
        await assert_option_unreferenced(db, project_id, definition_id, option_id)
        await db.delete(option)
        await db.flush()
        revision = await claim_fields_revision(db, project_id, expected)
        await audit.commit()
    return s.FieldRevision(fields_revision=revision)


async def public_field_value(db: AsyncSession, project_id: int, field: SettingTextField, value: object):
    with db.no_autoflush:
        spec = (await load_field_specs(db, project_id, [field]))[field.id]
        try:
            return decode_public_value(spec, value, reference_targets=await _handle_targets(db, project_id))
        except ValueError as exc:
            raise ConflictError("字段值的历史身份不完整", extra={"code": "invalid_field_graph"}) from exc


async def encode_field_value(
    db: AsyncSession,
    timeline: SettingTimeline,
    field: SettingTextField,
    value: object,
    *,
    operation: str = "replace",
    prior_value: object = None,
    preserve_existing: bool = False,
) -> CanonicalFieldValue:
    """公共值只编码一次；原 canonical 值由 preserve_existing 单独走历史校验。"""
    definitions, option_lists = await _metadata(db, timeline.project_id)
    definition = next((item for item in definitions if item.id == field.definition_id), None)
    if field.definition_id is not None and definition is None:
        raise NotFoundError("字段定义不存在")
    handles = {}
    if definition and definition.field_type == "reference" and value is not None and not preserve_existing:
        try:
            ref = s.EntityRef.model_validate(value)
        except ValidationError as exc:
            raise BadRequestError("引用目标格式不合法") from exc
        target = await catalog.get_entry(db, timeline.project_id, ref.kind, ref.id)
        existing = await db.scalar(
            select(SettingReferenceHandle).where(
                SettingReferenceHandle.project_id == timeline.project_id,
                SettingReferenceHandle.kind == ref.kind,
                getattr(SettingReferenceHandle, catalog.TARGET_COLUMNS[ref.kind]) == ref.id,
            )
        )
        # 已保存的身份可原样保留；新选择才受停用和当前引用范围约束。
        same = bool(existing and prior_value == {"reference_handle_id": existing.id})
        if not same and target.type not in definition.reference_types:
            raise BadRequestError("该字段不允许引用此类型的条目")
        if existing is None:
            existing = SettingReferenceHandle(
                project_id=timeline.project_id,
                kind=ref.kind,
                **{
                    catalog.TARGET_COLUMNS[ref.kind]: ref.id,
                },
            )
            db.add(existing)
            await db.flush()
        handles[ref.kind, ref.id] = existing.id
    spec = (await load_field_specs(db, timeline.project_id, [field]))[field.id]
    try:
        canonical = (
            validate_field_value(spec, value, operation=operation)
            if preserve_existing
            else encode_public_value(
                spec,
                value,
                reference_handles=handles,
                operation=operation,
            )
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    changed = not preserve_existing and canonical != prior_value
    if definition and not preserve_existing:
        entry = await catalog.get_entry(
            db, timeline.project_id, _timeline_ref(timeline).kind, _timeline_ref(timeline).id
        )
        if entry.type not in definition.applicable_types:
            raise BadRequestError("字段不适用于该条目类型")
        if definition.archived and changed and has_field_content(canonical):
            raise ConflictError("字段已停用，不能设置新值", extra={"code": "field_archived"})
        if definition.field_type == "dropdown" and changed and has_field_content(canonical):
            option = next(item for item in option_lists[definition.id] if item.id == canonical["option_id"])
            if option.archived:
                raise ConflictError("选项已停用，不能设置新值", extra={"code": "option_archived"})
    return canonical


async def _ensure_binding(
    db: AsyncSession,
    timeline: SettingTimeline,
    definition: SettingFieldDefinition | s.DefinitionOut,
) -> SettingTextField:
    existing = await db.scalar(
        select(SettingTextField).where(
            SettingTextField.timeline_id == timeline.id,
            SettingTextField.definition_id == definition.id,
        )
    )
    if existing is not None:
        return existing
    if definition.archived:
        raise ConflictError("字段已停用，不能创建新的条目绑定", extra={"code": "field_archived"})
    ref = _timeline_ref(timeline)
    if (await catalog.get_entry(db, timeline.project_id, ref.kind, ref.id)).type not in definition.applicable_types:
        raise BadRequestError("字段不适用于该条目类型")
    field = SettingTextField(
        timeline_id=timeline.id,
        definition_id=definition.id,
        label=None,
        base_value="" if definition.field_type in ("text", "line") else None,
        sort=0,
    )
    db.add(field)
    await db.flush()
    return field


async def _write_target(
    db: AsyncSession,
    project_id: int,
    kind: s.EntryKind,
    entry_id: int,
    definition_id: int,
    expected_fields: int,
    expected_timeline: int,
) -> tuple[SettingTimeline, s.DefinitionOut]:
    await check_fields_revision(db, project_id, expected_fields)
    await catalog.get_entry(db, project_id, kind, entry_id)
    definitions, _ = await _metadata(db, project_id)
    definition = next((item for item in definitions if item.id == definition_id), None)
    if definition is None:
        raise NotFoundError("字段定义不存在")
    timeline = await catalog.find_entry_timeline(db, project_id, kind, entry_id)
    current = timeline.revision if timeline else 0
    if current != expected_timeline:
        raise stale_revision(current, "timeline")
    return timeline or await catalog.ensure_entry_timeline(db, project_id, kind, entry_id), definition


async def apply_field_baseline(
    db: AsyncSession, timeline: SettingTimeline, field: SettingTextField, value: object, expected_revision: int
):
    from app.services import setting_progression_service as progressions

    canonical = await encode_field_value(db, timeline, field, value, prior_value=field.base_value)
    await progressions._claim_revision(db, timeline, expected_revision)
    field.base_value = canonical
    await db.flush()
    await progressions.materialize_latest(db, timeline)


async def write_baseline(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    kind: s.EntryKind,
    entry_id: int,
    definition_id: int,
    data: s.FieldValueWrite,
) -> s.EntryFieldsOut:
    async with _editing(db, user_id, project_id) as audit:
        timeline, definition = await _write_target(
            db,
            project_id,
            kind,
            entry_id,
            definition_id,
            data.expected_fields_revision,
            data.expected_timeline_revision,
        )
        if definition.field_type != data.field_type:
            raise BadRequestError("字段类型与配置不匹配")
        field = await _ensure_binding(db, timeline, definition)
        await apply_field_baseline(db, timeline, field, data.value, timeline.revision)
        result = (await resolve_entry_fields(db, project_id, [s.EntityRef(kind=kind, id=entry_id)]))[kind, entry_id]
        await audit.commit()
    return result


async def create_field_progression(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    kind: s.EntryKind,
    entry_id: int,
    definition_id: int,
    data: s.FieldProgressionCreate,
) -> ProgressionMutationOut:
    from app.services import setting_progression_service as progressions

    async with _editing(db, user_id, project_id) as audit:
        timeline, definition = await _write_target(
            db,
            project_id,
            kind,
            entry_id,
            definition_id,
            data.expected_fields_revision,
            data.expected_timeline_revision,
        )
        if definition.field_type != data.field_type:
            raise BadRequestError("字段类型与配置不匹配")
        field = await _ensure_binding(db, timeline, definition)
        command = ProgressionCreate(
            expected_revision=timeline.revision,
            expected_fields_revision=data.expected_fields_revision,
            custom_field_id=field.id,
            **data.model_dump(
                exclude={
                    "expected_fields_revision",
                    "expected_timeline_revision",
                    "field_type",
                }
            ),
        )
        event = await progressions.apply_progression(db, timeline, command)
        item = await progressions._event_out(db, project_id, event)
        result = ProgressionMutationOut(
            item=item, timeline_revision=timeline.revision, fields_revision=data.expected_fields_revision
        )
        await audit.commit()
    return result


async def bind_legacy(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    kind: s.EntryKind,
    entry_id: int,
    field_id: int,
    data: s.LegacyBinding,
) -> s.EntryFieldsOut:
    from app.services import setting_progression_service as progressions

    async with _editing(db, user_id, project_id) as audit:
        timeline, definition = await _write_target(
            db,
            project_id,
            kind,
            entry_id,
            data.definition_id,
            data.expected_fields_revision,
            data.expected_timeline_revision,
        )
        field = await db.scalar(
            select(SettingTextField).where(
                SettingTextField.timeline_id == timeline.id,
                SettingTextField.id == field_id,
            )
        )
        if field is None:
            raise NotFoundError("原有字段不存在")
        if field.definition_id is not None or definition.field_type != "text" or definition.archived:
            raise BadRequestError("只能将原有文本字段关联到启用的多行文本定义")
        entry = await catalog.get_entry(db, project_id, kind, entry_id)
        if entry.type not in definition.applicable_types:
            raise BadRequestError("字段不适用于该条目类型")
        if (
            await db.scalar(
                select(SettingTextField.id).where(
                    SettingTextField.timeline_id == timeline.id,
                    SettingTextField.definition_id == definition.id,
                )
            )
            is not None
        ):
            raise ConflictError("该条目已绑定此字段，不能合并原有资料", extra={"code": "field_binding_exists"})
        await progressions._claim_revision(db, timeline, timeline.revision)
        # 关联只改变身份归属；旧行/旧事件的时间戳也保留。
        await db.execute(
            update(SettingTextField)
            .where(SettingTextField.id == field.id)
            .values(
                definition_id=definition.id,
                label=None,
                updated_at=field.updated_at,
            )
            .execution_options(synchronize_session="fetch")
        )
        await db.flush()
        result = (await resolve_entry_fields(db, project_id, [entry.reference]))[kind, entry_id]
        await audit.commit()
    return result


async def resolve_entry_fields(
    db: AsyncSession,
    project_id: int,
    entries: Sequence[s.EntityRef],
    position: StoryPosition | None = None,
    *,
    definition_ids: Collection[int] | None = None,
) -> dict[tuple[str, int], s.EntryFieldsOut]:
    """共享的批量时点投影；目录/时间线前后版本一致，所有引用都已公开解码。"""
    from app.services import setting_progression_service as progressions

    if not entries:
        return {}
    with db.no_autoflush:
        before = await fields_revision(db, project_id)
        identities = await catalog.get_entries(db, project_id, entries)
        predicates = [
            (SettingTimeline.subject_kind == ("career_entry" if kind == "career" else kind))
            & getattr(SettingTimeline, column).in_([ref.id for ref in entries if ref.kind == kind])
            for kind, column in catalog.TARGET_COLUMNS.items()
        ]
        if definition_ids is None:
            timelines = list(
                await db.scalars(
                    select(SettingTimeline).where(
                        SettingTimeline.project_id == project_id,
                        or_(*predicates),
                    )
                )
            )
        else:
            timelines = [
                progressions.FieldTimelineSnapshot(*row)
                for row in await db.execute(
                    select(
                        SettingTimeline.id,
                        SettingTimeline.project_id,
                        SettingTimeline.subject_kind,
                        SettingTimeline.character_id,
                        SettingTimeline.world_setting_id,
                        SettingTimeline.organization_id,
                        SettingTimeline.career_id,
                        SettingTimeline.revision,
                        SettingTimeline.baseline_status,
                    ).where(SettingTimeline.project_id == project_id, or_(*predicates))
                )
            ]
        revisions = {timeline.id: timeline.revision for timeline in timelines}
        by_entry = {(_timeline_ref(t).kind, _timeline_ref(t).id): t for t in timelines}
        definitions, options = (
            await _metadata(db, project_id)
            if definition_ids is None
            else await _metadata(
                db,
                project_id,
                definition_ids=definition_ids,
            )
        )
        fields = (
            await read_field_bindings(db, list(revisions))
            if definition_ids is None
            else await read_field_bindings(
                db,
                list(revisions),
                definition_ids=definition_ids,
            )
        )
        by_timeline: dict[int, list[FieldBindingSnapshot]] = defaultdict(list)
        for field in fields:
            by_timeline[field.timeline_id].append(field)
        if definition_ids is None:
            snapshots = await progressions.resolve_timelines(db, project_id, timelines, position)
            values_by_timeline = {timeline_id: snapshot.field_values for timeline_id, snapshot in snapshots.items()}
            specs = await load_field_specs(db, project_id, fields)
            targets = await _handle_targets(db, project_id)
        else:
            values_by_timeline = await progressions._resolve_field_timelines(
                db,
                project_id,
                timelines,
                position,
                definition_ids=definition_ids,
            )
            specs = await load_field_specs(db, project_id, fields, definition_ids=definition_ids)
            handle_ids = {
                value["reference_handle_id"]
                for value in (
                    *[field.base_value for field in fields],
                    *[value for values in values_by_timeline.values() for value in values.values()],
                )
                if isinstance(value, dict) and "reference_handle_id" in value
            }
            targets = await _handle_targets(db, project_id, handle_ids=handle_ids) if handle_ids else {}
        target_entries = await catalog.get_entries(db, project_id, list(targets.values()))
        result = {}
        for key, entry in identities.items():
            timeline = by_entry.get(key)
            actual = by_timeline.get(timeline.id, []) if timeline else []
            bound = {field.definition_id: field for field in actual if field.definition_id is not None}
            shared, legacy = [], []
            for definition in definitions:
                field = bound.get(definition.id)
                if entry.type not in definition.applicable_types and field is None:
                    continue
                empty = "" if definition.field_type in ("text", "line") else None
                base_value = field.base_value if field else empty
                value = values_by_timeline[timeline.id][field.id] if field else empty
                spec = specs[field.id] if field else FieldSpec(cast(s.FieldType, definition.field_type), definition.id)
                base_public = decode_public_value(spec, base_value, reference_targets=targets)
                public = decode_public_value(spec, value, reference_targets=targets)
                reference = (
                    target_entries.get((public["kind"], public["id"]))
                    if definition.field_type == "reference" and public
                    else None
                )
                base_reference = (
                    target_entries.get((base_public["kind"], base_public["id"]))
                    if definition.field_type == "reference" and base_public
                    else None
                )
                shared.append(
                    s.FieldSlot(
                        field_id=field.id if field else None,
                        definition_id=definition.id,
                        label=definition.label,
                        field_type=definition.field_type,
                        source="shared",
                        base_value=base_public,
                        value=public,
                        reference_types=definition.reference_types,
                        options=options.get(definition.id, []),
                        archived=definition.archived,
                        sort=definition.sort,
                        reference=reference,
                        base_reference=base_reference,
                    )
                )
            for field in actual:
                if field.definition_id is None:
                    legacy.append(
                        s.FieldSlot(
                            field_id=field.id,
                            definition_id=None,
                            label=field.label,
                            field_type="text",
                            source="legacy",
                            base_value=deepcopy(field.base_value),
                            value=values_by_timeline[timeline.id][field.id],
                            sort=field.sort,
                        )
                    )
            result[key] = s.EntryFieldsOut(
                entry=entry,
                timeline_id=timeline.id if timeline else None,
                timeline_revision=revisions.get(timeline.id, 0) if timeline else 0,
                fields_revision=before,
                position=position,
                fields=shared,
                legacy_fields=legacy,
            )
        after_revisions = dict(
            (
                await db.execute(
                    select(SettingTimeline.id, SettingTimeline.revision).where(
                        SettingTimeline.project_id == project_id,
                        or_(*predicates),
                    )
                )
            ).all()
        )
        if revisions != after_revisions:
            raise stale_revision(max(after_revisions.values(), default=0), "timeline")
        after = await fields_revision(db, project_id)
        if before != after:
            raise stale_revision(after)
        return result


async def read_entry_fields(
    db: AsyncSession,
    user_id: int,
    project_id: int,
    kind: s.EntryKind,
    entry_id: int,
    *,
    chapter_id: int | None = None,
    chapter_no: int | None = None,
    boundary="start",
) -> s.EntryFieldsOut:
    from app.services.setting_progression_service import _chapter_for_anchor

    with db.no_autoflush:
        await get_manageable_project(db, user_id, project_id)
        if chapter_id is not None and chapter_no is not None:
            raise BadRequestError("章节 ID 与计划章节号只能选择一个")
        if chapter_id is not None:
            chapter_no = (await _chapter_for_anchor(db, project_id, chapter_id)).chapter_no
        position = (
            StoryPosition(chapter_id=chapter_id, chapter_no=chapter_no, boundary=boundary) if chapter_no else None
        )
        return (await resolve_entry_fields(db, project_id, [s.EntityRef(kind=kind, id=entry_id)], position))[
            kind, entry_id
        ]


async def assert_targets_unreferenced(db: AsyncSession, project_id: int, targets: Sequence[s.EntityRef]) -> None:
    """单删/覆盖/撤销共用保护，检查所有保留基础值及事件，不遗漏停用和孤儿。"""
    with db.no_autoflush:
        handles = await _handle_targets(db, project_id)
        requested = {(target.kind, target.id) for target in targets}
        ids = {hid for hid, ref in handles.items() if (ref.kind, ref.id) in requested}
        if not ids:
            return
        uses = [
            (field, timeline, eid)
            for field, timeline, eid, value in await _stored_values(db, project_id)
            if isinstance(value, dict) and value.get("reference_handle_id") in ids
        ]
        if uses:
            definitions, _ = await _metadata(db, project_id)
            labels = {definition.id: definition.label for definition in definitions}
            entries = await catalog.get_entries(db, project_id, [_timeline_ref(timeline) for _, timeline, _ in uses])
            sources = []
            for field, timeline, eid in uses[:20]:
                ref = _timeline_ref(timeline)
                entry = entries[ref.kind, ref.id]
                sources.append(
                    {
                        "field_id": field.id,
                        "timeline_id": timeline.id,
                        "event_id": eid,
                        "entry": entry.model_dump(),
                        "field_label": labels.get(field.definition_id, field.label),
                    }
                )
            first = sources[0]
            raise ConflictError(
                f"该条目仍被引用来源「{first['entry']['name']}」的字段「{first['field_label']}」或其历史使用",
                extra={
                    "code": "setting_target_in_use",
                    "sources": sources,
                },
            )


async def cleanup_unreferenced_handles(db: AsyncSession, project_id: int, targets: Sequence[s.EntityRef]) -> None:
    """调用方持项目锁，先证明全部无依赖再清理句柄；不提交。"""
    await assert_targets_unreferenced(db, project_id, targets)
    wanted = {(target.kind, target.id) for target in targets}
    ids = [
        handle_id
        for handle_id, target in (await _handle_targets(db, project_id)).items()
        if (target.kind, target.id) in wanted
    ]
    if ids:
        await db.execute(
            delete(SettingReferenceHandle).where(
                SettingReferenceHandle.project_id == project_id,
                SettingReferenceHandle.id.in_(ids),
            )
        )
        await db.flush()


async def assert_entry_type_change_allowed(
    db: AsyncSession, project_id: int, kind: s.EntryKind, entry_id: int, new_type: s.EntryType
):
    with db.no_autoflush:
        definitions, _ = await _metadata(db, project_id)
        by_id = {d.id: d for d in definitions}
        handles = await _handle_targets(db, project_id)
        incoming = {hid for hid, ref in handles.items() if (ref.kind, ref.id) == (kind, entry_id)}
        for field, timeline, eid, value in await _stored_values(db, project_id):
            if field.definition_id is None:
                continue
            definition = by_id[field.definition_id]
            ref = _timeline_ref(timeline)
            if (
                (ref.kind, ref.id) == (kind, entry_id)
                and has_field_content(value, has_events=eid is not None)
                and new_type not in definition.applicable_types
            ):
                raise ConflictError("条目的字段或历史不适用于新的类型", extra={"code": "incompatible_field_scope"})
            if (
                isinstance(value, dict)
                and value.get("reference_handle_id") in incoming
                and new_type not in definition.reference_types
            ):
                raise ConflictError("条目仍被不允许新类型的字段引用", extra={"code": "incompatible_field_scope"})


async def validate_project_field_graph(db: AsyncSession, project_id: int) -> None:
    """导入/效果恢复复用的整图校验，不提交、不补造缺失身份。"""
    with db.no_autoflush:
        values = await _stored_values(db, project_id)
        bindings = {field.id: field for field, _, _, _ in values}
        definitions, options = await _metadata(db, project_id)
        for definition in definitions:
            try:
                s.DefinitionCreate(
                    expected_fields_revision=0, **definition.model_dump(exclude={"id", "project_id", "options"})
                )
            except ValueError as exc:
                raise ConflictError("字段定义的类型或适用范围不合法", extra={"code": "invalid_field_graph"}) from exc
            if definition.field_type != "dropdown" and options.get(definition.id):
                raise ConflictError("非下拉字段不能保存选项", extra={"code": "invalid_field_graph"})
        definitions = {definition.id: definition for definition in definitions}
        specs = await load_field_specs(db, project_id, list(bindings.values()))
        targets = await _handle_targets(db, project_id)
        target_entries = await catalog.get_entries(db, project_id, list(targets.values()))
        shared_timelines = {
            timeline.id: timeline for field, timeline, _, _ in values if field.definition_id is not None
        }
        owners = await catalog.get_entries(
            db, project_id, [_timeline_ref(timeline) for timeline in shared_timelines.values()]
        )
        events = await db.execute(
            select(
                SettingProgression.id,
                SettingProgression.timeline_id,
                SettingProgression.custom_field_id,
                SettingProgression.operation,
            )
            .join(SettingTimeline, SettingTimeline.id == SettingProgression.timeline_id)
            .where(
                SettingProgression.custom_field_id.is_not(None),
                or_(SettingTimeline.project_id == project_id, SettingProgression.custom_field_id.in_(bindings)),
            )
        )
        event_operations = {}
        for event in events:
            binding = bindings.get(event.custom_field_id)
            if binding is None or binding.timeline_id != event.timeline_id:
                raise ConflictError("保留事件的字段不属于其时间线", extra={"code": "invalid_field_graph"})
            event_operations[event.id] = event.operation
        for field, timeline, event_id, value in values:
            operation = event_operations.get(event_id, "replace")
            try:
                validate_field_value(specs[field.id], value, operation=operation)
            except ValueError as exc:
                raise ConflictError("字段值或历史的类型身份不合法", extra={"code": "invalid_field_graph"}) from exc
            if field.definition_id is None:
                continue
            definition = definitions[field.definition_id]
            ref = _timeline_ref(timeline)
            if (
                has_field_content(value, has_events=event_id is not None)
                and owners[ref.kind, ref.id].type not in definition.applicable_types
            ):
                raise ConflictError("已保存字段不适用于所属条目", extra={"code": "incompatible_field_scope"})
            if definition.field_type == "reference" and value is not None:
                target = targets[value["reference_handle_id"]]
                if target_entries[target.kind, target.id].type not in definition.reference_types:
                    raise ConflictError("已保存引用超出字段允许范围", extra={"code": "incompatible_field_scope"})
