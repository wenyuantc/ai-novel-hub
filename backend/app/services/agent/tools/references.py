"""工具引用的权限边界，包括模型可能塞进嵌套任务参数的 ID。"""

from typing import Any

from sqlalchemy import or_, select

from app.core.errors import BadRequestError, NotFoundError
from app.models import (
    AIConfig,
    Career,
    CareerStage,
    Chapter,
    ChapterAnalysis,
    ChapterPlan,
    Character,
    CharacterCareer,
    Foreshadow,
    Organization,
    OrganizationMember,
    Outline,
    Relationship,
    RelationshipType,
    SettingTextField,
    SettingTimeline,
    Skill,
    Task,
    WorldSetting,
    WritingStyle,
)
from app.models.setting_fields import SettingFieldDefinition, SettingFieldOption, SettingReferenceHandle
from app.services.setting_catalog_service import reference_handle_key

from .base import ToolCtx
from .visibility import ENTRY_KINDS, allowed, allowed_field, timeline_allowed

_MODELS = {
    "character_id": Character,
    "related_character_id": Character,
    "a_id": Character,
    "b_id": Character,
    "target_id": Character,
    "chapter_id": Chapter,
    "start_chapter_id": Chapter,
    "end_chapter_id": Chapter,
    "outline_id": Outline,
    "plan_id": ChapterPlan,
    "organization_id": Organization,
    "parent_id": Organization,
    "career_id": Career,
    "stage_id": CareerStage,
    "assignment_id": CharacterCareer,
    "relationship_id": Relationship,
    "type_id": RelationshipType,
    "foreshadow_id": Foreshadow,
    "world_setting_id": WorldSetting,
    "member_id": OrganizationMember,
    "analysis_id": ChapterAnalysis,
    "timeline_id": SettingTimeline,
    "custom_field_id": SettingTextField,
    "definition_id": SettingFieldDefinition,
    "option_id": SettingFieldOption,
    "reference_handle_id": SettingReferenceHandle,
    "task_id": Task,
    "continue_from_task_id": Task,
    "ai_config_id": AIConfig,
    "style_id": WritingStyle,
    "default_style_id": WritingStyle,
    # 用户级：本人或全局 Skill
    "skill_id": Skill,
}


async def owned_reference(ctx: ToolCtx, model: type, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise BadRequestError("引用 ID 必须是正整数")
    # 这些模型的主键都是 INTEGER；按 PostgreSQL INTEGER 的上限统一校验，
    # 避免越界值进入驱动绑定，也确保 SQLite 和 PostgreSQL 的工具行为一致。
    if value > 2_147_483_647:
        raise BadRequestError("引用 ID 超出有效范围")
    if model in ENTRY_KINDS and not allowed(ctx, ENTRY_KINDS[model], value):
        raise NotFoundError("不存在")
    if ctx.scope is not None and model in (SettingTimeline, SettingReferenceHandle):
        row = await ctx.db.scalar(select(model).where(model.id == value, model.project_id == ctx.project_id))
        if row is None:
            raise NotFoundError("不存在")
        if model is SettingTimeline and not timeline_allowed(ctx, row):
            raise NotFoundError("不存在")
        if model is SettingReferenceHandle and not allowed(ctx, *reference_handle_key(row)):
            raise NotFoundError("不存在")
        return
    stmt = select(model.id).where(model.id == value)
    if model is WritingStyle:
        stmt = stmt.where(or_(WritingStyle.user_id.is_(None), WritingStyle.user_id == ctx.user_id))
    elif model is AIConfig:
        stmt = stmt.where(AIConfig.user_id == ctx.user_id)
    elif model is Skill:
        stmt = stmt.where(or_(Skill.owner_user_id.is_(None), Skill.owner_user_id == ctx.user_id))
    elif model is RelationshipType:
        stmt = stmt.where(or_(RelationshipType.project_id.is_(None), RelationshipType.project_id == ctx.project_id))
    elif model is Relationship:
        stmt = stmt.where(Relationship.project_id == ctx.project_id, Relationship.is_present.is_(True))
        if ctx.scope is not None:
            chars = ctx.scope.ids("character")
            stmt = stmt.where(Relationship.a_id.in_(chars), Relationship.b_id.in_(chars))
    elif model is CareerStage:
        stmt = stmt.join(Career, Career.id == CareerStage.career_id).where(Career.project_id == ctx.project_id)
        if ctx.scope is not None:
            career_id = await ctx.db.scalar(stmt.with_only_columns(CareerStage.career_id))
            if not allowed_field(ctx, "career", career_id, "stages"):
                raise NotFoundError("不存在")
            return
    elif model is OrganizationMember:
        stmt = stmt.join(Organization, Organization.id == OrganizationMember.organization_id).where(
            Organization.project_id == ctx.project_id
        )
        if ctx.scope is not None:
            stmt = stmt.where(
                OrganizationMember.organization_id.in_(ctx.scope.ids("organization")),
                OrganizationMember.character_id.in_(ctx.scope.ids("character")),
            )
    elif model is CharacterCareer:
        stmt = stmt.join(Character, Character.id == CharacterCareer.character_id).where(
            Character.project_id == ctx.project_id
        )
        if ctx.scope is not None:
            stmt = stmt.where(
                CharacterCareer.character_id.in_(ctx.scope.ids("character")),
                CharacterCareer.career_id.in_(ctx.scope.ids("career")),
            )
    elif model is SettingTextField:
        stmt = stmt.join(SettingTimeline, SettingTimeline.id == SettingTextField.timeline_id).where(
            SettingTimeline.project_id == ctx.project_id
        )
        if ctx.scope is not None:
            binding = await ctx.db.scalar(
                stmt.with_only_columns(SettingTextField).where(SettingTextField.ai_hidden.is_(False)),
            )
            if binding is None:
                raise NotFoundError("不存在")
            await owned_reference(ctx, SettingTimeline, binding.timeline_id)
            if binding.definition_id is not None:
                await owned_reference(ctx, SettingFieldDefinition, binding.definition_id)
            return
    elif model is SettingFieldOption:
        stmt = stmt.join(SettingFieldDefinition, SettingFieldDefinition.id == SettingFieldOption.definition_id).where(
            SettingFieldDefinition.project_id == ctx.project_id
        )
        if ctx.scope is not None:
            stmt = stmt.where(SettingFieldDefinition.ai_hidden.is_(False))
    elif model is SettingFieldDefinition and ctx.scope is not None:
        stmt = stmt.where(
            SettingFieldDefinition.project_id == ctx.project_id, SettingFieldDefinition.ai_hidden.is_(False),
        )
    else:
        stmt = stmt.where(model.project_id == ctx.project_id)
    if await ctx.db.scalar(stmt) is None:
        raise NotFoundError("不存在")


async def validate_references(ctx: ToolCtx, values: Any, *, id_model: type | None = None) -> None:
    if not isinstance(values, dict):
        return
    # 公共 EntityRef 的 id 由 kind 决定；仍继续遍历所有键，不能让额外嵌套 ID 绕过边界。
    if "id" in values and isinstance(values.get("kind"), str) and values["kind"] in {
        "character", "world", "organization", "career",
    }:
        id_model = {"character": Character, "world": WorldSetting, "organization": Organization, "career": Career}[
            values["kind"]
        ]
    for key, value in values.items():
        if value is None:
            continue
        if key == "project_id":
            if type(value) is not int or value != ctx.project_id:
                raise NotFoundError("不存在")
        elif key == "user_id":
            raise BadRequestError("工具不能指定用户")
        elif key == "id" and id_model is not None:
            await owned_reference(ctx, id_model, value)
        elif key in _MODELS:
            await owned_reference(ctx, _MODELS[key], value)
        elif key == "style" and isinstance(value, int):
            await owned_reference(ctx, WritingStyle, value)
        elif key.endswith("_ids") and key != "suggestion_ids":
            model = _MODELS.get(key[:-1])
            if model is None:
                raise BadRequestError(f"不支持的引用参数：{key}")
            if not isinstance(value, list):
                raise BadRequestError("引用 ID 列表格式不正确")
            for item in value:
                await owned_reference(ctx, model, item)
        elif key.endswith("_id"):
            raise BadRequestError(f"不支持的引用参数：{key}")
        if isinstance(value, dict):
            await validate_references(ctx, value)
        elif isinstance(value, list):
            nested_model = {"stages": CareerStage, "plans": ChapterPlan}.get(key)
            for item in value:
                await validate_references(ctx, item, id_model=nested_model)
