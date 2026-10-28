"""设定、大纲、细纲和章节元信息的工具，复用原有服务校验与写入。"""

from pydantic import BaseModel, Field, create_model
from sqlalchemy import select

from app.models import (
    Career,
    Chapter,
    CharacterCareer,
    Foreshadow,
    Organization,
    Relationship,
)
from app.schemas.career import AssignmentCreate, CareerCreate, CareerUpdate
from app.schemas.chapter import ChapterUpdate
from app.schemas.foreshadow import ForeshadowCreate, ForeshadowUpdate
from app.schemas.organization import MemberCreate, MemberUpdate, OrganizationCreate, OrganizationUpdate
from app.schemas.outline import PlansSync
from app.schemas.project import ProjectUpdate
from app.schemas.relationship import RelationshipCreate, RelationshipUpdate
from app.schemas.story import (
    CharacterCreate,
    CharacterUpdate,
    OutlineCreate,
    OutlineUpdate,
    WorldSettingCreate,
    WorldSettingUpdate,
)
from app.services import (
    career_service,
    chapter_service,
    foreshadow_service,
    organization_service,
    plan_service,
    project_service,
    relationship_service,
    story_service,
)

from .base import EmptyArgs, IdArgs, Tool, ToolCtx, arguments, serialized


def crud_tools(
    noun: str,
    label: str,
    model: type,
    create_schema: type[BaseModel],
    update_schema: type[BaseModel],
    create,
    update,
    delete,
) -> list[Tool]:
    def operation(action, schema, with_id):
        async def invoke(ctx: ToolCtx, args: BaseModel):
            params = [ctx.db, ctx.user_id, ctx.project_id]
            if with_id:
                params.append(args.id)
            if schema:
                params.append(schema.model_validate(args.model_dump(exclude_unset=True, exclude={"id"})))
            return await action(*params)

        return invoke

    return [
        Tool(
            f"create_{noun}",
            f"新建{label}",
            arguments(f"Create{model.__name__}", create_schema),
            "small",
            operation(create, create_schema, False),
            id_model=model,
        ),
        Tool(
            f"update_{noun}",
            f"修改{label}",
            arguments(f"Update{model.__name__}", update_schema, with_id=True),
            "small",
            operation(update, update_schema, True),
            id_model=model,
        ),
        Tool(f"delete_{noun}", f"删除{label}", IdArgs, "delete", operation(delete, None, True), id_model=model),
    ]


# story_service 的 kind 位于第二个位置，单独闭包保持公共入口的参数契约。
def story_action(action, kind):
    async def invoke(db, user_id, project_id, *args):
        return await action(db, kind, user_id, project_id, *args)

    return invoke


PROJECT_FIELDS = {
    "synopsis",
    "theme",
    "tags",
    "selling_points",
    "audience",
    "genre",
    "target_words",
    "default_style_id",
}
ProjectArgs = create_model(
    "AgentProjectUpdate",
    __base__=EmptyArgs,
    **{
        name: (ProjectUpdate.model_fields[name].annotation, ProjectUpdate.model_fields[name])
        for name in sorted(PROJECT_FIELDS)
    },
)


async def update_project(ctx: ToolCtx, args: BaseModel):
    data = ProjectUpdate.model_validate(args.model_dump(exclude_unset=True))
    project, _ = await project_service.update_project(ctx.db, ctx.user_id, ctx.project_id, data)
    return serialized(await project_service.project_out(ctx.db, project))


class MemberArgs(MemberCreate):
    organization_id: int = Field(gt=0)


class RemoveMemberArgs(EmptyArgs):
    organization_id: int = Field(gt=0)
    character_id: int = Field(gt=0)


async def set_member(ctx: ToolCtx, args: MemberArgs):
    current = await organization_service.find_membership(ctx.db, args.organization_id, args.character_id)
    data = args.model_dump(exclude_unset=True, exclude={"organization_id", "character_id"})
    if current is not None:
        return await organization_service.update_member(
            ctx.db,
            ctx.user_id,
            ctx.project_id,
            args.organization_id,
            current.id,
            MemberUpdate.model_validate(data),
        )
    return await organization_service.add_member(
        ctx.db,
        ctx.user_id,
        ctx.project_id,
        args.organization_id,
        MemberCreate.model_validate({"character_id": args.character_id, **data}),
    )


async def remove_member(ctx: ToolCtx, args: RemoveMemberArgs):
    current = await organization_service.find_membership(ctx.db, args.organization_id, args.character_id)
    if current is not None:
        await organization_service.remove_member(ctx.db, ctx.user_id, ctx.project_id, args.organization_id, current.id)
    return {"ok": True}


class AssignmentArgs(AssignmentCreate):
    character_id: int = Field(gt=0)


class RemoveAssignmentArgs(EmptyArgs):
    character_id: int = Field(gt=0)
    assignment_id: int = Field(gt=0)


async def assign_career(ctx: ToolCtx, args: AssignmentArgs):
    character = await story_service.get_owned(
        ctx.db, story_service.CHARACTERS, ctx.user_id, ctx.project_id, args.character_id
    )
    career = await career_service.get_owned_career(ctx.db, ctx.user_id, ctx.project_id, args.career_id)
    await career_service.apply_assignment(
        ctx.db,
        character,
        career,
        stage_id=args.stage_id,
        note=args.note if "note" in args.model_fields_set else None,
        role=args.role,
        source="manual",
        chapter_no=args.chapter_no,
        history_note=args.history_note,
    )
    row = await ctx.db.scalar(
        select(CharacterCareer).where(
            CharacterCareer.character_id == args.character_id,
            CharacterCareer.career_id == args.career_id,
        )
    )
    return (await career_service.assignment_outs(ctx.db, [row]))[0]


async def remove_assignment(ctx: ToolCtx, args: RemoveAssignmentArgs):
    await career_service.remove_assignment(ctx.db, ctx.user_id, ctx.project_id, args.character_id, args.assignment_id)
    return {"ok": True}


class PlanArgs(PlansSync):
    outline_id: int = Field(gt=0)


async def update_plans(ctx: ToolCtx, args: PlanArgs):
    return await plan_service.sync_plans(
        ctx.db, ctx.user_id, ctx.project_id, args.outline_id, PlansSync(plans=args.plans)
    )


class ChapterMetaArgs(IdArgs):
    title: str = Field(min_length=1, max_length=200)


async def update_chapter(ctx: ToolCtx, args: ChapterMetaArgs):
    row = await chapter_service.update_chapter(
        ctx.db, ctx.user_id, ctx.project_id, args.id, ChapterUpdate(title=args.title)
    )
    return {"id": row.id, "title": row.title}


TOOLS = [Tool("update_project", "修改作品信息", ProjectArgs, "small", update_project)]
for noun, label, kind, create_schema, update_schema in (
    ("character", "角色", story_service.CHARACTERS, CharacterCreate, CharacterUpdate),
    ("world_setting", "世界观条目", story_service.WORLD, WorldSettingCreate, WorldSettingUpdate),
    ("outline", "大纲", story_service.OUTLINES, OutlineCreate, OutlineUpdate),
):
    TOOLS.extend(
        crud_tools(
            noun,
            label,
            kind.model,
            create_schema,
            update_schema,
            story_action(story_service.create_owned, kind),
            story_action(story_service.update_owned, kind),
            story_action(story_service.delete_owned, kind),
        )
    )
for noun, label, model, create_schema, update_schema, create, update, delete in (
    (
        "organization",
        "组织",
        Organization,
        OrganizationCreate,
        OrganizationUpdate,
        organization_service.create_org,
        organization_service.update_org,
        organization_service.delete_org,
    ),
    (
        "relationship",
        "人物关系",
        Relationship,
        RelationshipCreate,
        RelationshipUpdate,
        relationship_service.create_relationship,
        relationship_service.update_relationship,
        relationship_service.delete_relationship,
    ),
    (
        "career",
        "职业",
        Career,
        CareerCreate,
        CareerUpdate,
        career_service.create_career,
        career_service.update_career,
        career_service.delete_career,
    ),
    (
        "foreshadow",
        "伏笔",
        Foreshadow,
        ForeshadowCreate,
        ForeshadowUpdate,
        foreshadow_service.create_foreshadow,
        foreshadow_service.update_foreshadow,
        foreshadow_service.delete_foreshadow,
    ),
):
    TOOLS.extend(crud_tools(noun, label, model, create_schema, update_schema, create, update, delete))
TOOLS.extend(
    [
        Tool("set_org_member", "设置组织成员及职位", arguments("AgentMember", MemberArgs), "small", set_member),
        Tool("remove_org_member", "移除组织成员", RemoveMemberArgs, "delete", remove_member),
        Tool(
            "assign_career", "设置角色职业及阶段", arguments("AgentAssignment", AssignmentArgs), "small", assign_career
        ),
        Tool("remove_career_assignment", "移除角色职业", RemoveAssignmentArgs, "delete", remove_assignment),
        Tool("update_chapter_plans", "整体更新大纲的细纲", arguments("AgentPlans", PlanArgs), "small", update_plans),
        Tool("update_chapter_meta", "修改章节标题", ChapterMetaArgs, "small", update_chapter, id_model=Chapter),
    ]
)
