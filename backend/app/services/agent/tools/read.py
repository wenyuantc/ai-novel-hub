"""只读项目工具；列表与正文均可分页，检索沿用统一 retrieval 服务。"""

from dataclasses import asdict
from math import ceil

from pydantic import Field
from sqlalchemy import or_, select

from app.core.errors import AppError, BadRequestError, ConflictError, NotFoundError
from app.models import Character, Organization, Outline, Project, StoryMemory, Task, WritingStyle
from app.models.entities import OPEN_TASK_STATUSES
from app.schemas.chapter import ChapterBrief
from app.schemas.foreshadow import ForeshadowStatus
from app.schemas.progression import Boundary, PositiveDbInt
from app.schemas.setting_fields import EntityRef, EntryType
from app.schemas.story import CharacterOut, WorldCategory, WorldSettingOut
from app.schemas.style import StyleBrief
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
from app.services.ai_dispatch import VisibilityPolicyError, visibility_sessionmaker

from .base import EmptyArgs, IdArgs, Tool, ToolCtx, collaborative, serialized
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
        **collaborative(ctx),
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
        ctx.db, ctx.user_id, ctx.project_id, allowed_ids=allowed_ids, **args.model_dump(), **collaborative(ctx),
    )
    return {**serialized(result), "page": args.page, "pages": max(1, ceil(result.total / args.page_size))}


async def entry_fields(ctx: ToolCtx, args: EntryFieldsArgs) -> dict:
    detail = await setting_field_service.read_entry_fields(
        ctx.db, ctx.user_id, ctx.project_id, args.entry.kind, args.entry.id,
        chapter_id=args.chapter_id, chapter_no=args.chapter_no, boundary=args.boundary, **collaborative(ctx),
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


async def project_for_model(ctx: ToolCtx, project: Project) -> dict:
    """项目人工投影可供管理员代管，模型只能获得实际 actor 可见的风格。"""
    out = await project_service.project_out(ctx.db, project, actor_user_id=ctx.user_id)
    style = None
    if type(ctx.user_id) is int and ctx.user_id > 0 and out.default_style_id is not None:
        # 最后一次查询将绑定、归属和内容放在同一快照，不能复用管理员投影或 ORM 缓存的旧风格。
        with ctx.db.no_autoflush:
            style = (await ctx.db.execute(select(
                WritingStyle.id, WritingStyle.name, WritingStyle.description, WritingStyle.user_id,
            ).join(Project, Project.default_style_id == WritingStyle.id).where(
                Project.id == ctx.project_id, WritingStyle.id == out.default_style_id,
                or_(WritingStyle.user_id.is_(None), WritingStyle.user_id == ctx.user_id),
            ))).one_or_none()
    visible = StyleBrief(
        id=style.id, name=style.name, description=style.description, is_preset=style.user_id is None,
    ) if style is not None else None
    # 笔名仅供人工署名展示；不能因 ProjectOut 增加字段而将私人库身份发给模型。
    payload = serialized(out.model_copy(update={
        "default_style_id": style.id if style is not None else None,
        "default_style": visible,
    }))
    payload.pop("pen_name", None)
    return payload


async def overview(ctx: ToolCtx, args: EmptyArgs) -> dict:
    await ctx.db.refresh(ctx.project)
    return await project_for_model(ctx, ctx.project)


async def characters(ctx: ToolCtx, args: CharacterListArgs) -> dict:
    rows = await story_service.list_owned(
        ctx.db, story_service.CHARACTERS, ctx.user_id, ctx.project_id, **collaborative(ctx, "manage"),
    )
    rows = [row for row in rows if allowed(ctx, "character", row.id)]
    if args.query:
        query = args.query.casefold()
        rows = [row for row in rows if query in (row.name + " " + " ".join(row.aliases)).casefold()]
    return paged([entry_payload(ctx, "character", CharacterOut.model_validate(row)) for row in rows], args)


async def story_character(ctx: ToolCtx, character_id: int):
    """协作轮次读取人工资料入口（content.read）；作者沿用原严格作者入口。"""
    read = story_service.get_collaborative if ctx.grant is not None else story_service.get_owned
    return await read(ctx.db, story_service.CHARACTERS, ctx.user_id, ctx.project_id, character_id)


async def project_chapter(ctx: ToolCtx, chapter_id: int):
    if ctx.grant is not None:
        return await chapter_service.get_manual_chapter(ctx.db, ctx.user_id, ctx.project_id, chapter_id, "content.read")
    return await chapter_service.get_project_chapter(ctx.db, ctx.user_id, ctx.project_id, chapter_id)


async def character(ctx: ToolCtx, args: IdArgs) -> dict:
    row = await story_character(ctx, args.id)
    careers = await career_service.list_assignments(
        ctx.db, ctx.user_id, ctx.project_id, args.id, **collaborative(ctx),
    )
    memberships = await organization_service.character_memberships(
        ctx.db, ctx.user_id, ctx.project_id, args.id, **collaborative(ctx),
    )
    return {
        **entry_payload(ctx, "character", CharacterOut.model_validate(row)),
        "careers": [assignment_payload(ctx, item) for item in careers if allowed(ctx, "career", item.career_id)],
        "memberships": [
            serialized(item) for item in memberships if allowed(ctx, "organization", item.organization_id)
        ],
    }


async def organizations(ctx: ToolCtx, args: PageArgs) -> dict:
    rows = await organization_service.list_orgs(ctx.db, ctx.user_id, ctx.project_id, **collaborative(ctx))
    rows = [row for row in rows if allowed(ctx, "organization", row.id)]
    return paged([entry_payload(ctx, "organization", row) for row in rows], args)


async def organization(ctx: ToolCtx, args: IdArgs) -> dict:
    item = await organization_service.get_org(ctx.db, ctx.user_id, ctx.project_id, args.id, **collaborative(ctx))
    result = entry_payload(ctx, "organization", item)
    members = await organization_service.list_members(
        ctx.db, ctx.user_id, ctx.project_id, args.id, **collaborative(ctx),
    )
    result["members"] = [serialized(member) for member in members if allowed(ctx, "character", member.character_id)]
    return result


async def relationships(ctx: ToolCtx, args: RelationshipListArgs) -> dict:
    rows = await relationship_service.list_relationships(
        ctx.db, ctx.user_id, ctx.project_id, character_id=args.character_id, **collaborative(ctx),
    )
    return paged(
        [row for row in rows if allowed(ctx, "character", row.a_id) and allowed(ctx, "character", row.b_id)],
        args,
    )


async def careers(ctx: ToolCtx, args: PageArgs) -> dict:
    rows = await career_service.list_careers(ctx.db, ctx.user_id, ctx.project_id, **collaborative(ctx))
    return paged([entry_payload(ctx, "career", row) for row in rows if allowed(ctx, "career", row.id)], args)


async def world(ctx: ToolCtx, args: WorldListArgs) -> dict:
    rows = await story_service.list_owned(
        ctx.db, story_service.WORLD, ctx.user_id, ctx.project_id, category=args.category,
        **collaborative(ctx, "manage"),
    )
    rows = [row for row in rows if allowed(ctx, "world", row.id)]
    return paged([entry_payload(ctx, "world", WorldSettingOut.model_validate(row)) for row in rows], args)


async def outline_tree(ctx: ToolCtx, args: PageArgs) -> dict:
    rows = serialized(await outline_service.outline_tree(ctx.db, ctx.user_id, ctx.project_id, **collaborative(ctx)))
    for row in rows:
        row["summary"] = row["summary"][:300]
        row["plans"] = [
            {key: plan[key] for key in ("id", "seq", "title", "chapter_id", "chapter_no", "has_content")}
            for plan in row["plans"]
        ]
    return paged(rows, args)


async def outline(ctx: ToolCtx, args: IdArgs) -> dict:
    rows = await outline_service.outline_tree(ctx.db, ctx.user_id, ctx.project_id, **collaborative(ctx))
    item = next((row for row in rows if row.id == args.id), None)
    if item is None:
        raise NotFoundError("不存在")
    data = serialized(item)
    for plan in data["plans"]:
        plan["character_ids"] = [ident for ident in plan["character_ids"] if allowed(ctx, "character", ident)]
    return data


async def chapter_briefs(ctx: ToolCtx, rows: list) -> list[dict]:
    """协作轮次与人工章节接口同一投影：隐藏作者私人风格 ID 与失效的大纲 / 细纲关联；作者轮次不变。"""
    if ctx.grant is None:
        return [serialized(ChapterBrief.model_validate(row)) for row in rows]
    views = await chapter_service.manual_chapter_views(ctx.db, ctx.user_id, ctx.project_id, rows, full=False)
    return [serialized(view) for view in views]


async def chapters(ctx: ToolCtx, args: PageArgs) -> dict:
    states = {
        state.chapter_id: serialized(state)
        for state in await analysis_service.project_states(
            ctx.db, ctx.user_id, ctx.project_id, **collaborative(ctx),
        )
    }
    list_rows = chapter_service.list_manual_chapters if ctx.grant is not None else chapter_service.list_chapters
    rows = await list_rows(ctx.db, ctx.user_id, ctx.project_id)
    briefs = await chapter_briefs(ctx, rows)
    return paged([{**brief, "analysis": states.get(brief["id"])} for brief in briefs], args)


async def chapter(ctx: ToolCtx, args: ChapterReadArgs) -> dict:
    from app.services.rich_document_ai import render_visible_sections, visible_sections
    from app.services.scene_service import check_revision, list_active_scenes, project_scenes

    row = await project_chapter(ctx, args.id)
    pages = max(1, ceil(len(row.content) / 4000))
    if args.page > pages:
        raise BadRequestError(f"页码超出范围，共 {pages} 页")
    start = (args.page - 1) * 4000
    end = min(start + 4000, len(row.content))
    revision = row.content_revision
    scenes = await list_active_scenes(ctx.db, row.id)
    projected, ranges = project_scenes(scenes)
    if not scenes or projected != row.content:
        raise ConflictError("章节场景正文投影不一致", extra={"code": "scene_invariant_error"})
    scene_lengths = {scene.id: len(scene.content) for scene in scenes}
    intervals = {}
    for item in ranges:
        if item.end < start or item.start > end:
            continue
        lower = max(0, start - item.start)
        upper = min(scene_lengths[item.scene_id], end - item.start)
        if lower > upper:
            continue
        include_upper = item.start + upper < end or (args.page == pages and item.start + upper == len(row.content))
        if lower < upper or include_upper:
            intervals[item.scene_id] = (lower, upper, include_upper)
    result = {
        **(await chapter_briefs(ctx, [row]))[0],
        "content": row.content[start:end],
        "page": args.page,
        "pages": pages,
    }
    # 正文分页与码点偏移保持旧合同；作者区块作为独立、受预算控制的非正文背景。
    visible = await visible_sections(ctx.db, ctx.project_id, scene_ids=list(intervals), anchor_ranges=intervals,
                                     max_rows=8, text_limit=256)
    author_context = render_visible_sections(visible, 1_500)
    if author_context:
        result["author_sections"] = author_context
    await check_revision(ctx.db, row, revision)
    if ctx.scope is not None:
        await ctx.scope.verify(ctx.db)
    return result


async def analysis(ctx: ToolCtx, args: AnalysisArgs) -> dict:
    await project_chapter(ctx, args.chapter_id)
    latest = (await analysis_service.latest_analyses(ctx.db, [args.chapter_id])).get(args.chapter_id)
    if latest is None:
        return {"analysis": None}
    detail = await analysis_service.get_analysis(ctx.db, ctx.user_id, latest.id, **collaborative(ctx))
    return {"analysis": analysis_payload(ctx, detail)}


async def foreshadows(ctx: ToolCtx, args: ForeshadowListArgs) -> dict:
    rows = serialized(await foreshadow_service.list_foreshadows(
        ctx.db, ctx.user_id, ctx.project_id, status=args.status, **collaborative(ctx),
    ))
    for row in rows:
        visible = [
            (ident, name) for ident, name in zip(row["character_ids"], row["character_names"], strict=True)
            if allowed(ctx, "character", ident)
        ]
        row["character_ids"] = [ident for ident, _ in visible]
        row["character_names"] = [name for _, name in visible]
    return paged(rows, args)


async def _collaborator_embedder(ctx: ToolCtx) -> tuple[retrieval_service.Embedder | None, str | None]:
    """协作轮次的检索向量（E6 裁决 1 / E4-i 裁决 12）：只用受理时冻结的作者共享向量选择。

    - 未冻结（作者向量配置本地不可用）、冻结为关键词（作者未授权 / 未配置）、或缺少执行身份（确认路径）：
      只用关键词，零作者向量调用；协作者本人的向量配置永不参与。
    - 冻结为向量：每次查询嵌入经 ``for_task`` 守卫在真实发送租约内重验原执行、原邀请轮次、作者模型授权与
      两份模型绑定；构造 provider 的普通故障经锁内复核未变 → 关键词继续，任一变化 → 失败关闭（裁决 17）。
    """
    from app.services import collaboration_ai_guard, kb_service
    from app.services.agent.access import frozen_shared_embedding

    execution = ctx.execution() if ctx.execution is not None else None
    binding = ctx.text_binding
    if execution is None or binding is None or ctx.scope is None:
        return None, None
    if (execution.grant_identity != ctx.grant or binding.grant_identity != ctx.grant
            or binding.actor_id != ctx.user_id or execution.actor_id != ctx.user_id
            or binding.project_id != ctx.project_id or execution.project_id != ctx.project_id):
        raise collaboration_ai_guard.CollaborationSendBlocked()
    selection = frozen_shared_embedding(execution, binding)
    if selection is None:
        return None, kb_service.KEYWORD_ONLY_OWNER_UNAVAILABLE
    if selection.grant_identity != ctx.grant or selection.actor_id != ctx.user_id:
        raise collaboration_ai_guard.CollaborationSendBlocked()
    if selection.mode != "vector":
        return None, (kb_service.KEYWORD_ONLY_COLLABORATOR if selection.reason == "owner_embedding_not_allowed"
                      else kb_service.KEYWORD_ONLY_OWNER_UNCONFIGURED)
    deps = ctx.task_context
    allow_private = deps.settings.allow_private_ai_endpoints
    maker = visibility_sessionmaker(ctx.db)
    guard = collaboration_ai_guard.for_task(
        execution, selection, ctx.scope.stamp, cipher=deps.cipher, allow_private=allow_private,
        extra_bindings=(binding,),
    )
    # 短事务先按原执行锁内重验并读取冻结配置的 provider/model，不能把冻结选择本身当作授权证明。
    async with maker() as verify_db:
        await guard.require_task_write(verify_db)
        try:
            snapshot = await retrieval_service.load_bound_shared_embedding_config(
                verify_db, selection, cipher=deps.cipher, allow_private=allow_private, for_update=True,
            )
        except VisibilityPolicyError:
            raise
        except AppError:
            # 冻结选择在锁内已不成立（授权 / 配置变化或本地不可用）属策略类变化：终止轮次，
            # 不能作为普通工具错误回给模型后继续（E6 裁决 11 / 20②）。
            raise collaboration_ai_guard.CollaborationSendBlocked() from None
        await verify_db.rollback()
    bound = await collaboration_ai_guard.prepare_shared_embedding(
        guard, maker, deps.factory, request_timeout=deps.settings.ai_request_timeout,
    )
    if bound is None:
        return None, None
    if (bound.provider_name, bound.model) != (snapshot.provider, snapshot.model):
        raise collaboration_ai_guard.CollaborationSendBlocked()
    # 裁决 1：只读作者旧键 provider:model:dim（作者与授权编辑者的索引任务都写这个空间）。
    embedder = retrieval_service.Embedder(bound.provider, bound.provider_name, bound.model)
    return retrieval_service.guard_embedder(embedder, ctx.scope.stamp, maker), None


async def search(ctx: ToolCtx, args: SearchArgs, source: str) -> dict:
    deps = ctx.task_context
    if ctx.grant is not None:
        embedder, warning = await _collaborator_embedder(ctx)
    else:
        embedder, warning = await retrieval_service.resolve_embedder(
            ctx.db,
            ctx.user_id,
            cipher=deps.cipher,
            factory=deps.factory,
            ai_timeout=deps.settings.ai_request_timeout,
            allow_private=deps.settings.allow_private_ai_endpoints,
            project_id=ctx.project_id,
            execution=ctx.execution() if ctx.execution is not None else None,
            usage_kind="agent.turn.embedding",
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
    if ctx.grant is not None:
        from app.services import kb_service

        # 提供方错误原文可能含作者的模型名 / 端点：协作者只看到固定文案。
        result.warnings = kb_service.public_search_warnings(result.warnings, ctx.grant)
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
    Tool("read_chapter", "分页读取章节正式正文（每页4000字）及已启用的作者区块参考", ChapterReadArgs, "read", chapter),
    Tool("get_chapter_analysis", "读取章节最新分析", AnalysisArgs, "read", analysis),
    Tool("list_foreshadows", "分页读取伏笔与回收状态", ForeshadowListArgs, "read", foreshadows),
    Tool("search_memories", "检索剧情记忆", SearchArgs, "read", memories),
    Tool("search_knowledge", "检索项目知识库", SearchArgs, "read", knowledge),
    Tool("list_writing_styles", "分页读取可用写作风格", PageArgs, "read", styles),
    Tool("list_active_tasks", "分页读取项目进行中和待审核的任务", PageArgs, "read", active_tasks),
]
