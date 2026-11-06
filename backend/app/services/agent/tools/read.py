"""只读项目工具；列表与正文均可分页，检索沿用统一 retrieval 服务。"""

from dataclasses import asdict
from math import ceil

from pydantic import Field
from sqlalchemy import select

from app.core.errors import BadRequestError, NotFoundError
from app.models import Character, Organization, Outline, StoryMemory, Task
from app.models.entities import OPEN_TASK_STATUSES
from app.schemas.chapter import ChapterBrief
from app.schemas.foreshadow import ForeshadowStatus
from app.schemas.progression import Boundary, PositiveDbInt
from app.schemas.setting_fields import EntityRef, EntryType
from app.schemas.story import CharacterOut, WorldCategory, WorldSettingOut
from app.services import (
    analysis_service,
    career_service,
    chapter_service,
    foreshadow_service,
    organization_service,
    outline_service,
    project_service,
    relationship_service,
    retrieval_service,
    setting_catalog_service,
    setting_field_service,
    story_service,
    style_service,
)
from app.services.ai_dispatch import visibility_sessionmaker

from .base import EmptyArgs, IdArgs, Tool, ToolCtx, serialized
from .visibility import allowed, analysis_payload, assignment_payload, entry_payload


class PageArgs(EmptyArgs):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=50)


class CharacterListArgs(PageArgs):
    query: str | None = Field(default=None, max_length=200)


class RelationshipListArgs(PageArgs):
    character_id: int | None = None


class WorldListArgs(PageArgs):
    category: WorldCategory | None = None


class ForeshadowListArgs(PageArgs):
    status: ForeshadowStatus | None = None


class ChapterReadArgs(IdArgs):
    page: int = Field(default=1, ge=1)


class AnalysisArgs(EmptyArgs):
    chapter_id: int = Field(gt=0)


class SearchArgs(EmptyArgs):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=10)


class FieldDefinitionListArgs(PageArgs):
    types: list[EntryType] | None = Field(default=None, min_length=1, max_length=9)
    include_archived: bool = True


class SettingEntryListArgs(PageArgs):
    types: list[EntryType] | None = Field(default=None, min_length=1, max_length=9)
    q: str | None = Field(default=None, max_length=200)


class EntryFieldsArgs(PageArgs):
    entry: EntityRef
    chapter_id: PositiveDbInt | None = None
    chapter_no: PositiveDbInt | None = None
    boundary: Boundary = "start"


async def field_definitions(ctx: ToolCtx, args: FieldDefinitionListArgs) -> dict:
    result = await setting_field_service.list_definitions(
        ctx.db, ctx.user_id, ctx.project_id, types=args.types, include_archived=args.include_archived,
    )
    items = result.items if ctx.scope is None else [
        item.model_dump(mode="json", exclude={"ai_hidden"}) for item in result.items if not item.ai_hidden
    ]
    return {**paged(items, args), "fields_revision": result.fields_revision}


async def setting_entries(ctx: ToolCtx, args: SettingEntryListArgs) -> dict:
    allowed_ids = (
        {kind: ctx.scope.ids(kind) for kind in setting_catalog_service.ENTRY_MODELS}
        if ctx.scope is not None else None
    )
    result = await setting_catalog_service.list_entries(
        ctx.db, ctx.user_id, ctx.project_id, allowed_ids=allowed_ids, **args.model_dump(),
    )
    return {**serialized(result), "page": args.page, "pages": max(1, ceil(result.total / args.page_size))}


async def entry_fields(ctx: ToolCtx, args: EntryFieldsArgs) -> dict:
    detail = await setting_field_service.read_entry_fields(
        ctx.db, ctx.user_id, ctx.project_id, args.entry.kind, args.entry.id,
        chapter_id=args.chapter_id, chapter_no=args.chapter_no, boundary=args.boundary,
    )
    if ctx.scope is not None:
        detail = ctx.scope.entry_fields(detail)
    result = serialized(detail)
    # 单条目的字段也有界；引用投影只有身份、当前名称，绝不展开目标正文。
    combined = result["fields"] + result["legacy_fields"]
    if ctx.scope is not None:
        result.pop("ai_visibility_revision", None)
        for item in combined:
            item.pop("ai_hidden", None)
    page = paged(combined, args)
    result["fields"] = [item for item in page["items"] if item["source"] == "shared"]
    result["legacy_fields"] = [item for item in page["items"] if item["source"] == "legacy"]
    return {**result, **{key: page[key] for key in ("page", "pages", "total")}}


def paged(items: list, args: PageArgs) -> dict:
    pages = max(1, ceil(len(items) / args.page_size))
    start = (args.page - 1) * args.page_size
    return {
        "items": serialized(items[start : start + args.page_size]),
        "page": args.page,
        "pages": pages,
        "total": len(items),
    }


async def overview(ctx: ToolCtx, args: EmptyArgs) -> dict:
    await ctx.db.refresh(ctx.project)
    return serialized(await project_service.project_out(ctx.db, ctx.project))


async def characters(ctx: ToolCtx, args: CharacterListArgs) -> dict:
    rows = await story_service.list_owned(ctx.db, story_service.CHARACTERS, ctx.user_id, ctx.project_id)
    rows = [row for row in rows if allowed(ctx, "character", row.id)]
    if args.query:
        query = args.query.casefold()
        rows = [row for row in rows if query in (row.name + " " + " ".join(row.aliases)).casefold()]
    return paged([entry_payload(ctx, "character", CharacterOut.model_validate(row)) for row in rows], args)


async def character(ctx: ToolCtx, args: IdArgs) -> dict:
    row = await story_service.get_owned(ctx.db, story_service.CHARACTERS, ctx.user_id, ctx.project_id, args.id)
    careers = await career_service.list_assignments(ctx.db, ctx.user_id, ctx.project_id, args.id)
    memberships = await organization_service.character_memberships(ctx.db, ctx.user_id, ctx.project_id, args.id)
    return {
        **entry_payload(ctx, "character", CharacterOut.model_validate(row)),
        "careers": [assignment_payload(ctx, item) for item in careers if allowed(ctx, "career", item.career_id)],
        "memberships": [
            serialized(item) for item in memberships if allowed(ctx, "organization", item.organization_id)
        ],
    }


async def organizations(ctx: ToolCtx, args: PageArgs) -> dict:
    rows = await organization_service.list_orgs(ctx.db, ctx.user_id, ctx.project_id)
    rows = [row for row in rows if allowed(ctx, "organization", row.id)]
    return paged([entry_payload(ctx, "organization", row) for row in rows], args)


async def organization(ctx: ToolCtx, args: IdArgs) -> dict:
    item = await organization_service.get_org(ctx.db, ctx.user_id, ctx.project_id, args.id)
    result = entry_payload(ctx, "organization", item)
    members = await organization_service.list_members(ctx.db, ctx.user_id, ctx.project_id, args.id)
    result["members"] = [serialized(member) for member in members if allowed(ctx, "character", member.character_id)]
    return result


async def relationships(ctx: ToolCtx, args: RelationshipListArgs) -> dict:
    rows = await relationship_service.list_relationships(
        ctx.db, ctx.user_id, ctx.project_id, character_id=args.character_id,
    )
    return paged(
        [row for row in rows if allowed(ctx, "character", row.a_id) and allowed(ctx, "character", row.b_id)],
        args,
    )


async def careers(ctx: ToolCtx, args: PageArgs) -> dict:
    rows = await career_service.list_careers(ctx.db, ctx.user_id, ctx.project_id)
    return paged([entry_payload(ctx, "career", row) for row in rows if allowed(ctx, "career", row.id)], args)


async def world(ctx: ToolCtx, args: WorldListArgs) -> dict:
    rows = await story_service.list_owned(
        ctx.db, story_service.WORLD, ctx.user_id, ctx.project_id, category=args.category
    )
    rows = [row for row in rows if allowed(ctx, "world", row.id)]
    return paged([entry_payload(ctx, "world", WorldSettingOut.model_validate(row)) for row in rows], args)


async def outline_tree(ctx: ToolCtx, args: PageArgs) -> dict:
    rows = serialized(await outline_service.outline_tree(ctx.db, ctx.user_id, ctx.project_id))
    for row in rows:
        row["summary"] = row["summary"][:300]
        row["plans"] = [
            {key: plan[key] for key in ("id", "seq", "title", "chapter_id", "chapter_no", "has_content")}
            for plan in row["plans"]
        ]
    return paged(rows, args)


async def outline(ctx: ToolCtx, args: IdArgs) -> dict:
    rows = await outline_service.outline_tree(ctx.db, ctx.user_id, ctx.project_id)
    item = next((row for row in rows if row.id == args.id), None)
    if item is None:
        raise NotFoundError("不存在")
    data = serialized(item)
    for plan in data["plans"]:
        plan["character_ids"] = [ident for ident in plan["character_ids"] if allowed(ctx, "character", ident)]
    return data


async def chapters(ctx: ToolCtx, args: PageArgs) -> dict:
    states = {
        state.chapter_id: serialized(state)
        for state in await analysis_service.project_states(ctx.db, ctx.user_id, ctx.project_id)
    }
    rows = await chapter_service.list_chapters(ctx.db, ctx.user_id, ctx.project_id)
    return paged(
        [{**serialized(ChapterBrief.model_validate(row)), "analysis": states.get(row.id)} for row in rows], args
    )


async def chapter(ctx: ToolCtx, args: ChapterReadArgs) -> dict:
    row = await chapter_service.get_project_chapter(ctx.db, ctx.user_id, ctx.project_id, args.id)
    pages = max(1, ceil(len(row.content) / 4000))
    if args.page > pages:
        raise BadRequestError(f"页码超出范围，共 {pages} 页")
    start = (args.page - 1) * 4000
    return {
        **serialized(ChapterBrief.model_validate(row)),
        "content": row.content[start : start + 4000],
        "page": args.page,
        "pages": pages,
    }


async def analysis(ctx: ToolCtx, args: AnalysisArgs) -> dict:
    await chapter_service.get_project_chapter(ctx.db, ctx.user_id, ctx.project_id, args.chapter_id)
    latest = (await analysis_service.latest_analyses(ctx.db, [args.chapter_id])).get(args.chapter_id)
    if latest is None:
        return {"analysis": None}
    return {"analysis": analysis_payload(ctx, await analysis_service.get_analysis(ctx.db, ctx.user_id, latest.id))}


async def foreshadows(ctx: ToolCtx, args: ForeshadowListArgs) -> dict:
    rows = serialized(await foreshadow_service.list_foreshadows(
        ctx.db, ctx.user_id, ctx.project_id, status=args.status,
    ))
    for row in rows:
        visible = [
            (ident, name) for ident, name in zip(row["character_ids"], row["character_names"], strict=True)
            if allowed(ctx, "character", ident)
        ]
        row["character_ids"] = [ident for ident, _ in visible]
        row["character_names"] = [name for _, name in visible]
    return paged(rows, args)


async def search(ctx: ToolCtx, args: SearchArgs, source: str) -> dict:
    deps = ctx.task_context
    embedder, warning = await retrieval_service.resolve_embedder(
        ctx.db,
        ctx.user_id,
        cipher=deps.cipher,
        factory=deps.factory,
        ai_timeout=deps.settings.ai_request_timeout,
        allow_private=deps.settings.allow_private_ai_endpoints,
    )
    if ctx.scope is not None:
        embedder = retrieval_service.guard_embedder(embedder, ctx.scope.stamp, visibility_sessionmaker(ctx.db))
    result = await retrieval_service.search(
        ctx.db,
        ctx.project_id,
        source,
        args.query,
        top_k=args.top_k,
        min_score=0.1,
        embedder=embedder,
    )
    if warning:
        result.warnings.append(warning)
    if source == "memory" and ctx.scope is not None and result.hits:
        # 内部全文索引含结构化角色名，模型工具只能拿剧情记忆的原正文。
        refs = {hit.ref_id for hit in result.hits}
        originals = dict((await ctx.db.execute(select(StoryMemory.id, StoryMemory.content).where(
            StoryMemory.project_id == ctx.project_id, StoryMemory.id.in_(refs),
        ))).all())
        result.hits = [hit for hit in result.hits if hit.ref_id in originals]
        for hit in result.hits:
            hit.text = originals[hit.ref_id]
    return asdict(result)


async def memories(ctx: ToolCtx, args: SearchArgs) -> dict:
    return await search(ctx, args, "memory")


async def knowledge(ctx: ToolCtx, args: SearchArgs) -> dict:
    return await search(ctx, args, "kb")


async def styles(ctx: ToolCtx, args: PageArgs) -> dict:
    return paged(await style_service.list_styles(ctx.db, ctx.user_id), args)


async def active_tasks(ctx: ToolCtx, args: PageArgs) -> dict:
    from app.services import task_service

    rows = list(
        await ctx.db.scalars(
            select(Task)
            .where(
                Task.project_id == ctx.project_id,
                Task.user_id == ctx.user_id,
                Task.status.in_(OPEN_TASK_STATUSES),
            )
            .order_by(Task.id.desc())
        )
    )
    if ctx.scope is None:
        return paged([task_service.to_out(row) for row in rows], args)
    # 任务结果/标题/断点可以携带生成的档案快照或目标名；此工具只报告安全状态。
    return paged([
        {"id": row.id, "kind": row.kind, "status": row.status, "progress": row.progress, "chapter_id": row.chapter_id}
        for row in rows
    ], args)


TOOLS = [
    Tool("list_setting_field_definitions", "分页读取共享字段类型与选项目录", FieldDefinitionListArgs,
         "read", field_definitions),
    Tool("list_setting_entries", "分页查找角色、世界观、组织或职业引用目标", SettingEntryListArgs,
         "read", setting_entries),
    Tool("get_setting_entry_fields", "分页读取条目当前或指定章节的有效字段", EntryFieldsArgs, "read", entry_fields),
    Tool("get_project_overview", "读取作品信息、字数统计和默认风格", EmptyArgs, "read", overview),
    Tool("list_characters", "分页读取角色，可按名字或别名查找", CharacterListArgs, "read", characters),
    Tool("get_character", "读取角色详情", IdArgs, "read", character, id_model=Character),
    Tool("list_organizations", "分页读取组织", PageArgs, "read", organizations),
    Tool("get_organization", "读取组织与成员", IdArgs, "read", organization, id_model=Organization),
    Tool("list_relationships", "分页读取人物关系", RelationshipListArgs, "read", relationships),
    Tool("list_careers", "分页读取职业和阶段", PageArgs, "read", careers),
    Tool("list_world_settings", "分页读取世界观", WorldListArgs, "read", world),
    Tool("get_outline_tree", "分页读取卷、大纲与细纲概要", PageArgs, "read", outline_tree),
    Tool("get_outline", "读取大纲及其细纲", IdArgs, "read", outline, id_model=Outline),
    Tool("list_chapters", "分页读取章节及分析状态", PageArgs, "read", chapters),
    Tool("read_chapter", "分页读取章节正文，每页4000字", ChapterReadArgs, "read", chapter),
    Tool("get_chapter_analysis", "读取章节最新分析", AnalysisArgs, "read", analysis),
    Tool("list_foreshadows", "分页读取伏笔与回收状态", ForeshadowListArgs, "read", foreshadows),
    Tool("search_memories", "检索剧情记忆", SearchArgs, "read", memories),
    Tool("search_knowledge", "检索项目知识库", SearchArgs, "read", knowledge),
    Tool("list_writing_styles", "分页读取可用写作风格", PageArgs, "read", styles),
    Tool("list_active_tasks", "分页读取项目进行中和待审核的任务", PageArgs, "read", active_tasks),
]
