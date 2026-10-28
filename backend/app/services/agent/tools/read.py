"""只读项目工具；列表与正文均可分页，检索沿用统一 retrieval 服务。"""

from dataclasses import asdict
from math import ceil

from pydantic import Field
from sqlalchemy import select

from app.core.errors import BadRequestError, NotFoundError
from app.models import Character, Organization, Outline, Task
from app.models.entities import OPEN_TASK_STATUSES
from app.schemas.chapter import ChapterBrief
from app.schemas.foreshadow import ForeshadowStatus
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
    story_service,
    style_service,
)

from .base import EmptyArgs, IdArgs, Tool, ToolCtx, serialized


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
    if args.query:
        query = args.query.casefold()
        rows = [row for row in rows if query in (row.name + " " + " ".join(row.aliases)).casefold()]
    return paged([CharacterOut.model_validate(row) for row in rows], args)


async def character(ctx: ToolCtx, args: IdArgs) -> dict:
    row = await story_service.get_owned(ctx.db, story_service.CHARACTERS, ctx.user_id, ctx.project_id, args.id)
    return {
        **serialized(CharacterOut.model_validate(row)),
        "careers": serialized(await career_service.list_assignments(ctx.db, ctx.user_id, ctx.project_id, args.id)),
        "memberships": serialized(
            await organization_service.character_memberships(
                ctx.db,
                ctx.user_id,
                ctx.project_id,
                args.id,
            )
        ),
    }


async def organizations(ctx: ToolCtx, args: PageArgs) -> dict:
    return paged(await organization_service.list_orgs(ctx.db, ctx.user_id, ctx.project_id), args)


async def organization(ctx: ToolCtx, args: IdArgs) -> dict:
    result = serialized(await organization_service.get_org(ctx.db, ctx.user_id, ctx.project_id, args.id))
    result["members"] = serialized(
        await organization_service.list_members(ctx.db, ctx.user_id, ctx.project_id, args.id)
    )
    return result


async def relationships(ctx: ToolCtx, args: RelationshipListArgs) -> dict:
    return paged(
        await relationship_service.list_relationships(
            ctx.db, ctx.user_id, ctx.project_id, character_id=args.character_id
        ),
        args,
    )


async def careers(ctx: ToolCtx, args: PageArgs) -> dict:
    return paged(await career_service.list_careers(ctx.db, ctx.user_id, ctx.project_id), args)


async def world(ctx: ToolCtx, args: WorldListArgs) -> dict:
    rows = await story_service.list_owned(
        ctx.db, story_service.WORLD, ctx.user_id, ctx.project_id, category=args.category
    )
    return paged([WorldSettingOut.model_validate(row) for row in rows], args)


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
    return serialized(item)


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
    return {"analysis": serialized(await analysis_service.get_analysis(ctx.db, ctx.user_id, latest.id))}


async def foreshadows(ctx: ToolCtx, args: ForeshadowListArgs) -> dict:
    return paged(
        await foreshadow_service.list_foreshadows(ctx.db, ctx.user_id, ctx.project_id, status=args.status), args
    )


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
    return paged([task_service.to_out(row) for row in rows], args)


TOOLS = [
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
