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

from .base import ToolCtx

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
    elif model is CareerStage:
        stmt = stmt.join(Career, Career.id == CareerStage.career_id).where(Career.project_id == ctx.project_id)
    elif model is OrganizationMember:
        stmt = stmt.join(Organization, Organization.id == OrganizationMember.organization_id).where(
            Organization.project_id == ctx.project_id
        )
    elif model is CharacterCareer:
        stmt = stmt.join(Character, Character.id == CharacterCareer.character_id).where(
            Character.project_id == ctx.project_id
        )
    elif model is SettingTextField:
        stmt = stmt.join(SettingTimeline, SettingTimeline.id == SettingTextField.timeline_id).where(
            SettingTimeline.project_id == ctx.project_id
        )
    else:
        stmt = stmt.where(model.project_id == ctx.project_id)
    if await ctx.db.scalar(stmt) is None:
        raise NotFoundError("不存在")


async def validate_references(ctx: ToolCtx, values: Any, *, id_model: type | None = None) -> None:
    if not isinstance(values, dict):
        return
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
