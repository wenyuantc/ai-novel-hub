"""设定、大纲、细纲和章节元信息的工具，复用原有服务校验与写入。"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, create_model, model_validator
from sqlalchemy import select

from app.core.errors import BadRequestError, NotFoundError
from app.models import (
    Career,
    Chapter,
    CharacterCareer,
    Foreshadow,
    Organization,
    Relationship,
    SettingTextField,
    SettingTimeline,
)
from app.schemas.career import AssignmentCreate, CareerCreate, CareerUpdate
from app.schemas.chapter import ChapterUpdate
from app.schemas.foreshadow import ForeshadowCreate, ForeshadowUpdate
from app.schemas.organization import MemberCreate, MemberUpdate, OrganizationCreate, OrganizationUpdate
from app.schemas.outline import PlansSync
from app.schemas.progression import (
    AnchorKind,
    BaselineUpdate,
    Boundary,
    NonnegativeDbInt,
    PositiveDbInt,
    ProgressionCreate,
    ProgressionOperation,
    SubjectKind,
)
from app.schemas.project import ProjectUpdate
from app.schemas.relationship import RelationshipCreate, RelationshipUpdate
from app.schemas.setting_field_value import DropdownSelection, EntityRef, FieldType
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
    setting_field_service,
    setting_progression_service,
    story_service,
)

from .base import EmptyArgs, IdArgs, Tool, ToolCtx, arguments, serialized
from .read import PageArgs, paged
from .visibility import timeline_allowed, timeline_fields_allowed


def _crud_schema(name: str, schema: type[BaseModel], *, with_id: bool = False) -> type[BaseModel]:
    if "always_in_context" in schema.model_fields:
        # 旧常驻也是作者策略。仅从模型的输入协议移除，不改人工表单的合同；
        # 操作仍通过原 schema 再次校验。
        schema = create_model(
            f"{name}AuthorFields",
            __base__=EmptyArgs,
            **{
                key: (field.annotation, field)
                for key, field in schema.model_fields.items()
                if key != "always_in_context"
            },
        )
    return arguments(name, schema, with_id=with_id)


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
            _crud_schema(f"Create{model.__name__}", create_schema),
            "small",
            operation(create, create_schema, False),
            id_model=model,
        ),
        Tool(
            f"update_{noun}",
            f"修改{label}",
            _crud_schema(f"Update{model.__name__}", update_schema, with_id=True),
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


class TimelineListArgs(PageArgs):
    subject_kind: SubjectKind | None = None
    character_id: PositiveDbInt | None = None
    related_character_id: PositiveDbInt | None = None
    world_setting_id: PositiveDbInt | None = None
    career_id: PositiveDbInt | None = None
    organization_id: PositiveDbInt | None = None


class _IdentityValue(EmptyArgs):
    a_id: PositiveDbInt
    b_id: PositiveDbInt
    type_id: PositiveDbInt


_Intimacy = Annotated[int, Field(strict=True, ge=-100, le=100)]
_Status = Literal["normal", "broken", "past", "complicated", "active", "left", "expelled", "dead"]


class _BaselineChanges(EmptyArgs):
    """五类主体允许修改的字段；主体及字段组合仍由统一设定服务校验。"""

    profile: str | None = None
    appearance: str | None = None
    personality: str | None = None
    background: str | None = None
    current_state: str | None = None
    age: str | None = None
    gender: str | None = None
    life_status: Literal["alive", "dead", "missing", "other"] | None = None
    content: str | None = None
    exists: Annotated[bool, Field(strict=True)] | None = None
    identity: _IdentityValue | None = None
    intimacy: _Intimacy | None = None
    status: _Status | None = None
    description: str | None = None
    role: Literal["main", "sub"] | None = None
    stage_id: PositiveDbInt | None = None
    position: str | None = None
    note: str | None = None


class _PresenceState(EmptyArgs):
    """建立关系、职业或成员时对应主体的完整状态。"""

    identity: _IdentityValue | None = None
    intimacy: _Intimacy | None = None
    status: _Status | None = None
    description: str | None = None
    role: Literal["main", "sub"] | None = None
    stage_id: PositiveDbInt | None = None
    position: str | None = None
    note: str | None = None


class _PresenceValue(EmptyArgs):
    exists: Annotated[bool, Field(strict=True)]
    state: _PresenceState | None = Field(default=None, description="建立时提供该主体完整状态；退出时省略。")


class _CustomFieldValue(EmptyArgs):
    """显式、单一类型；null 只表示清空选择或引用。"""

    text: str | None = Field(default=None, description="多行文本或旧文本字段；空串清空。")
    line: str | None = Field(default=None, max_length=500, description="单行字段；空串清空，不允许换行。")
    dropdown: DropdownSelection | None = Field(default=None, description="下拉选项；显式 null 清空。")
    reference: EntityRef | None = Field(default=None, description="引用目标的类型及 ID；显式 null 清空。")

    @model_validator(mode="after")
    def require_one_value(self) -> Self:
        if len(self.model_fields_set) != 1:
            raise ValueError("变化值必须且只能填写一个类型化值")
        key = next(iter(self.model_fields_set))
        if getattr(self, key) is None and key not in {"dropdown", "reference"}:
            raise ValueError("只有下拉和引用字段允许 null 清空")
        return self

    def unpack(self):
        value = getattr(self, next(iter(self.model_fields_set)))
        return value.model_dump(exclude_unset=True) if isinstance(value, BaseModel) else value


class _ProgressionValue(_CustomFieldValue):
    intimacy: _Intimacy | None = Field(default=None, description="关系亲密度使用。")
    stage_id: PositiveDbInt | None = Field(default=None, description="职业阶段引用使用。")
    identity: _IdentityValue | None = Field(default=None, description="关系身份字段使用。")
    presence: _PresenceValue | None = Field(default=None, description="关系、职业或成员的建立／退出使用。")


class BaselineArgs(EmptyArgs):
    timeline_id: PositiveDbInt
    expected_revision: PositiveDbInt
    changes: _BaselineChanges = Field(default_factory=_BaselineChanges, description="只填写要修正的基础字段。")
    confirm: bool = False

    @model_validator(mode="after")
    def require_intent(self) -> Self:
        if not self.changes.model_fields_set and not self.confirm:
            raise ValueError("请选择要修改的基础字段或确认已校准")
        return self


class ProgressionArgs(EmptyArgs):
    timeline_id: PositiveDbInt
    expected_revision: PositiveDbInt
    expected_fields_revision: NonnegativeDbInt | None = None
    field_key: str | None = Field(default=None, min_length=1, max_length=64)
    custom_field_id: PositiveDbInt | None = None
    operation: ProgressionOperation
    value: _ProgressionValue = Field(
        description="只填写一个与字段匹配的显式类型值；dropdown/reference 可用 null 清空。"
    )
    anchor_kind: AnchorKind
    chapter_id: PositiveDbInt | None = None
    chapter_no: PositiveDbInt | None = None
    boundary: Boundary = "start"
    sort_order: NonnegativeDbInt = 0
    note: str = Field(default="", max_length=200)


class FieldBaselineArgs(EmptyArgs):
    entry: EntityRef
    definition_id: PositiveDbInt
    expected_fields_revision: NonnegativeDbInt
    expected_timeline_revision: NonnegativeDbInt
    field_type: FieldType
    value: _CustomFieldValue


class FieldProgressionArgs(FieldBaselineArgs):
    operation: ProgressionOperation
    anchor_kind: AnchorKind
    chapter_id: PositiveDbInt | None = None
    chapter_no: PositiveDbInt | None = None
    boundary: Boundary = "start"
    sort_order: NonnegativeDbInt = 0
    note: str = Field(default="", max_length=200)


async def _shared_field_target(ctx: ToolCtx, args: FieldBaselineArgs):
    if next(iter(args.value.model_fields_set)) != args.field_type:
        raise BadRequestError("变化值的类型与设定字段不匹配")
    timeline, definition = await setting_field_service._write_target(
        ctx.db, ctx.project_id, args.entry.kind, args.entry.id, args.definition_id,
        args.expected_fields_revision, args.expected_timeline_revision,
    )
    if definition.field_type != args.field_type:
        raise BadRequestError("字段类型与配置不匹配")
    return timeline, await setting_field_service._ensure_binding(ctx.db, timeline, definition)


async def update_setting_field_baseline(ctx: ToolCtx, args: FieldBaselineArgs):
    timeline, field = await _shared_field_target(ctx, args)
    await setting_field_service.apply_field_baseline(ctx.db, timeline, field, args.value.unpack(), timeline.revision)
    return (await setting_field_service.resolve_entry_fields(ctx.db, ctx.project_id, [args.entry]))[
        args.entry.kind, args.entry.id
    ]


async def record_setting_field_progression(ctx: ToolCtx, args: FieldProgressionArgs):
    timeline, field = await _shared_field_target(ctx, args)
    command = ProgressionCreate.model_validate({
        **args.model_dump(exclude={"entry", "definition_id", "expected_timeline_revision", "field_type", "value"}),
        "expected_revision": timeline.revision, "custom_field_id": field.id, "value": args.value.unpack(),
    })
    # 正式服务统一处理公共值编码、CAS、合法清空和物化，不在工具层复制写入规则。
    return await setting_progression_service.create_progression(
        ctx.db, ctx.user_id, ctx.project_id, timeline.id, command,
    )


async def list_setting_timelines(ctx: ToolCtx, args: TimelineListArgs):
    filters = args.model_dump(exclude={"page", "page_size"}, exclude_none=True)
    result = await setting_progression_service.list_timelines(ctx.db, ctx.user_id, ctx.project_id, **filters)
    page = paged([item for item in result.items if timeline_allowed(ctx, item)], args)
    timeline_ids = [item["id"] for item in page["items"]]
    fields_by_timeline: dict[int, list[dict]] = {timeline_id: [] for timeline_id in timeline_ids}
    definitions = await setting_field_service.list_definitions(ctx.db, ctx.user_id, ctx.project_id)
    by_id = {item.id: item for item in definitions.items}
    if timeline_ids:
        fields = await ctx.db.execute(
            select(SettingTextField)
            .join(SettingTimeline, SettingTimeline.id == SettingTextField.timeline_id)
            .where(SettingTimeline.project_id == ctx.project_id, SettingTimeline.id.in_(timeline_ids))
            .order_by(SettingTextField.timeline_id, SettingTextField.sort, SettingTextField.id)
        )
        for (field,) in fields:
            definition = by_id.get(field.definition_id)
            if ctx.scope is not None and (field.ai_hidden or (definition and definition.ai_hidden)):
                continue
            metadata = {
                "id": field.id, "label": definition.label if definition else field.label,
                "field_type": definition.field_type if definition else "text", "definition_id": field.definition_id,
            }
            if definition:
                metadata.update(options=serialized(definition.options), reference_types=definition.reference_types,
                                archived=definition.archived)
            fields_by_timeline[field.timeline_id].append(metadata)
    for item in page["items"]:
        item["custom_fields"] = fields_by_timeline[item["id"]]
    return {**page, "fields_revision": definitions.fields_revision}


async def update_setting_baseline(ctx: ToolCtx, args: BaselineArgs):
    await timeline_fields_allowed(ctx, args.timeline_id, args.changes.model_fields_set)
    data = BaselineUpdate.model_validate(args.model_dump(exclude={"timeline_id"}, exclude_unset=True))
    return await setting_progression_service.update_baseline(
        ctx.db, ctx.user_id, ctx.project_id, args.timeline_id, data
    )


async def record_setting_progression(ctx: ToolCtx, args: ProgressionArgs):
    if args.field_key:
        await timeline_fields_allowed(ctx, args.timeline_id, (args.field_key,))
    expected_type = {
        "intimacy": "intimacy",
        "stage_id": "stage_id",
        "identity": "identity",
        "presence": "presence",
    }.get(args.field_key, "text")
    if args.custom_field_id is not None:
        field = await ctx.db.scalar(select(SettingTextField).where(
            SettingTextField.id == args.custom_field_id, SettingTextField.timeline_id == args.timeline_id,
        ))
        if field is None:
            raise NotFoundError("字段不存在")
        specs = await setting_field_service.load_field_specs(ctx.db, ctx.project_id, [field])
        expected_type = specs[field.id].field_type
    if next(iter(args.value.model_fields_set)) != expected_type:
        raise BadRequestError("变化值的类型与设定字段不匹配")
    values = args.model_dump(exclude={"timeline_id", "value"}, exclude_unset=True)
    data = ProgressionCreate.model_validate({**values, "value": args.value.unpack()})
    return await setting_progression_service.create_progression(
        ctx.db, ctx.user_id, ctx.project_id, args.timeline_id, data
    )


class RemoveMemberArgs(EmptyArgs):
    organization_id: int = Field(gt=0)
    character_id: int = Field(gt=0)
    edit_mode: Literal["progression", "delete"] | None = None
    expected_revision: PositiveDbInt | None = None
    anchor_kind: Literal["chapter", "planned"] | None = None
    chapter_id: PositiveDbInt | None = None
    chapter_no: PositiveDbInt | None = None
    boundary: Literal["start", "end"] = "start"


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
        await organization_service.remove_member(
            ctx.db,
            ctx.user_id,
            ctx.project_id,
            args.organization_id,
            current.id,
            edit_mode=args.edit_mode,
            expected_revision=args.expected_revision,
            anchor_kind=args.anchor_kind,
            chapter_id=args.chapter_id,
            chapter_no=args.chapter_no,
            boundary=args.boundary,
        )
    elif args.edit_mode == "delete":
        if (
            args.anchor_kind is not None
            or args.chapter_id is not None
            or args.chapter_no is not None
            or args.boundary != "start"
        ):
            raise BadRequestError("删除成员资料不能指定生效章节")
        if args.expected_revision is None:
            raise BadRequestError("删除成员资料必须指定设定版本")
        await organization_service.delete_membership_record(
            ctx.db, ctx.user_id, ctx.project_id, args.organization_id, args.character_id, args.expected_revision
        )
    elif (
        args.edit_mode is not None
        or args.anchor_kind is not None
        or args.chapter_id is not None
        or args.chapter_no is not None
    ):
        raise NotFoundError("当前成员不存在，请通过时间线记录变化")
    return {"ok": True}


class AssignmentArgs(AssignmentCreate):
    character_id: int = Field(gt=0)


class RemoveAssignmentArgs(EmptyArgs):
    character_id: int = Field(gt=0)
    assignment_id: int = Field(gt=0)
    edit_mode: Literal["baseline", "progression", "delete"] | None = None
    expected_revision: PositiveDbInt | None = None
    anchor_kind: Literal["chapter", "planned"] | None = None
    chapter_id: PositiveDbInt | None = None
    chapter_no: PositiveDbInt | None = None
    boundary: Literal["start", "end"] = "start"


async def assign_career(ctx: ToolCtx, args: AssignmentArgs):
    if (args.edit_mode is not None or args.chapter_id is not None or args.chapter_no is not None) and (
        args.expected_revision is None
    ):
        raise BadRequestError("明确修改意图或章节时必须指定设定版本")
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
        chapter_id=args.chapter_id,
        anchor_kind=args.anchor_kind,
        boundary=args.boundary,
        edit_mode=args.edit_mode,
        expected_revision=args.expected_revision,
        history_note=args.history_note,
    )
    row = await ctx.db.scalar(
        select(CharacterCareer).where(
            CharacterCareer.character_id == args.character_id,
            CharacterCareer.career_id == args.career_id,
        )
    )
    return (await career_service.assignment_outs(ctx.db, [row]))[0] if row is not None else {"ok": True}


async def remove_assignment(ctx: ToolCtx, args: RemoveAssignmentArgs):
    if (args.edit_mode is not None or args.chapter_id is not None or args.chapter_no is not None) and (
        args.expected_revision is None
    ):
        raise BadRequestError("明确修改意图或章节时必须指定设定版本")
    await career_service.remove_assignment(
        ctx.db,
        ctx.user_id,
        ctx.project_id,
        args.character_id,
        args.assignment_id,
        chapter_id=args.chapter_id,
        chapter_no=args.chapter_no,
        anchor_kind=args.anchor_kind,
        boundary=args.boundary,
        edit_mode=args.edit_mode,
        expected_revision=args.expected_revision,
    )
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
        Tool("update_setting_field_baseline", "修正共享字段起始值（首次写入可建绑定）", FieldBaselineArgs, "small",
             update_setting_field_baseline),
        Tool("record_setting_field_progression", "记录共享字段章节变化（首次写入可建绑定）", FieldProgressionArgs,
             "small", record_setting_field_progression),
        Tool("list_setting_timelines", "列出设定时间线及版本", TimelineListArgs, "read", list_setting_timelines),
        Tool("update_setting_baseline", "修正或确认设定基础值", BaselineArgs, "small", update_setting_baseline),
        Tool(
            "record_setting_progression",
            "在明确章节位置记录设定变化",
            ProgressionArgs,
            "small",
            record_setting_progression,
        ),
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
