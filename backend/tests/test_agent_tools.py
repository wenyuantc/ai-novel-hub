"""工具真实服务集成：预览回滚、项目边界、正文校验和任务白名单。"""

from copy import deepcopy

import pytest
from sqlalchemy import delete, func, select

from app.models import (
    AIConfig,
    Career,
    Chapter,
    ChapterAnalysis,
    ChapterVersion,
    Character,
    CharacterCareer,
    Organization,
    OrganizationMember,
    Outline,
    Project,
    Relationship,
    Task,
)
from app.services import analysis_service, chapter_service, retrieval_service
from app.services.agent import effects
from app.services.agent.tools import ToolCtx, get_tool, list_tools
from tests.helpers import create_ai_config, create_project, register


@pytest.fixture
async def ctx(app, client):
    user = await register(client)
    project = await create_project(client, style="")
    await create_ai_config(client)
    # 只验证提交边界，后台调度由 agent-turn 集成测试覆盖。
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        yield ToolCtx(db, row, user["id"], app.state.task_engine.ctx)


async def apply(ctx, name, args):
    tool = get_tool(name)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    original = deepcopy(args)
    preview = await tool.preview(ctx, args)
    assert "error" not in preview, preview
    assert args == original
    assert await effects.snapshot(ctx.db, ctx.project_id) == before
    result = await tool.execute(ctx, args)
    assert "error" not in result, result
    await ctx.db.commit()
    return result, preview


async def new_character(ctx, name="林远"):
    return (await apply(ctx, "create_character", {"name": name}))[0]


async def new_chapter(ctx, content="山风穿过竹林。林远握紧长剑。"):
    from app.schemas.chapter import ChapterCreate

    return await chapter_service.create_chapter(
        ctx.db,
        ctx.user_id,
        ctx.project_id,
        ChapterCreate(title="启程", content=content),
    )


def test_registry_has_only_explicit_tools():
    names = {tool.name for tool in list_tools()}
    assert len(names) == 48
    assert get_tool("delete_project") is None
    assert get_tool("__import__") is None
    for tool in list_tools():
        assert tool.parameters["type"] == "object"
        assert tool.parameters.get("additionalProperties") is False
    schema = get_tool("start_task").parameters
    assert "chapter_id" in schema["$defs"]["AgentTaskParams"]["properties"]


@pytest.mark.parametrize(
    "noun,create,update,field",
    [
        ("character", {"name": "林远", "personality": "急躁"}, {"personality": "沉稳"}, "personality"),
        ("world_setting", {"title": "天剑宗", "content": "古老宗门"}, {"content": "千年剑宗"}, "content"),
        ("outline", {"title": "启程", "summary": "出发"}, {"summary": "离开宗门"}, "summary"),
        ("organization", {"name": "天剑宗"}, {"purpose": "守护天下"}, "purpose"),
        ("career", {"name": "剑修", "stages": [{"name": "入门"}]}, {"description": "以剑修行"}, "description"),
        ("foreshadow", {"title": "玉佩"}, {"content": "刻有族徽"}, "content"),
    ],
)
async def test_crud_preview_and_execution(ctx, noun, create, update, field):
    created, preview = await apply(ctx, f"create_{noun}", create)
    assert created["id"] > 0 and preview["diff"]
    updated, preview = await apply(ctx, f"update_{noun}", {"id": created["id"], **update})
    assert updated[field] == update[field]
    assert any(d["field"].endswith("." + field) and d["after"] == update[field] for d in preview["diff"])
    _, preview = await apply(ctx, f"delete_{noun}", {"id": created["id"]})
    assert any(d["before"] is not None and d["after"] is None for d in preview["diff"])


async def test_relationship_lifecycle_and_custom_type_preview(ctx):
    a, b = await new_character(ctx), await new_character(ctx, "陈风")
    created, preview = await apply(
        ctx,
        "create_relationship",
        {
            "a_id": a["id"],
            "b_id": b["id"],
            "role_a": "传剑者",
            "role_b": "承剑者",
        },
    )
    assert any(d["field"].startswith("relationship_types.") for d in preview["diff"])
    changed, _ = await apply(ctx, "update_relationship", {"id": created["id"], "intimacy": 50})
    assert changed["intimacy"] == 50
    await apply(ctx, "delete_relationship", {"id": created["id"]})
    assert await ctx.db.get(Relationship, created["id"]) is None


async def test_project_update_allowlist(ctx):
    result, _ = await apply(ctx, "update_project", {"theme": "成长", "tags": ["冒险", "冒险"], "target_words": 90000})
    assert result["theme"] == "成长" and result["tags"] == ["冒险"]
    for args in ({"title": "不允许改书名"}, {"status": "completed"}, {"style": "绕过风格选择"}):
        assert "error" in await get_tool("update_project").preview(ctx, args)


async def test_member_and_career_assignments(ctx):
    char = await new_character(ctx)
    org, _ = await apply(ctx, "create_organization", {"name": "天剑宗"})
    keys = {"organization_id": org["id"], "character_id": char["id"]}
    member, _ = await apply(ctx, "set_org_member", {**keys, "position": "弟子", "note": "内门"})
    changed, _ = await apply(ctx, "set_org_member", {**keys, "position": "长老"})
    assert changed["id"] == member["id"] and changed["note"] == "内门"
    await apply(ctx, "remove_org_member", keys)
    assert await ctx.db.get(OrganizationMember, member["id"]) is None
    career, _ = await apply(ctx, "create_career", {"name": "剑修", "stages": [{"name": "入门"}, {"name": "大师"}]})
    assigned, _ = await apply(ctx, "assign_career", {"character_id": char["id"], "career_id": career["id"]})
    changed, _ = await apply(
        ctx,
        "assign_career",
        {
            "character_id": char["id"],
            "career_id": career["id"],
            "stage_id": career["stages"][1]["id"],
        },
    )
    assert changed["id"] == assigned["id"] and changed["stage_position"] == 2
    await apply(ctx, "remove_career_assignment", {"character_id": char["id"], "assignment_id": assigned["id"]})
    assert await ctx.db.get(CharacterCareer, assigned["id"]) is None


async def test_plans_and_chapter_meta(ctx):
    outline, _ = await apply(ctx, "create_outline", {"title": "启程"})
    result, _ = await apply(
        ctx,
        "update_chapter_plans",
        {
            "outline_id": outline["id"],
            "plans": [{"title": "出门", "summary": "去市集"}],
        },
    )
    assert result["items"][0]["title"] == "出门"
    ch = await new_chapter(ctx)
    result, preview = await apply(ctx, "update_chapter_meta", {"id": ch.id, "title": "初遇"})
    assert result["title"] == "初遇" and preview["diff"]


async def test_read_tools_and_chapter_pagination(ctx):
    char = await new_character(ctx)
    org, _ = await apply(ctx, "create_organization", {"name": "天剑宗"})
    outline, _ = await apply(ctx, "create_outline", {"title": "启程"})
    content = "甲" * 4000 + "乙" * 4000 + "丙"
    ch = await new_chapter(ctx, content)
    args_by_name = {
        "get_character": {"id": char["id"]},
        "get_organization": {"id": org["id"]},
        "get_outline": {"id": outline["id"]},
        "read_chapter": {"id": ch.id},
        "get_chapter_analysis": {"chapter_id": ch.id},
        "search_memories": {"query": "竹林"},
        "search_knowledge": {"query": "竹林"},
    }
    for tool in list_tools():
        if tool.kind == "read":
            result = await tool.run(ctx, args_by_name.get(tool.name, {}))
            assert "error" not in result, (tool.name, result)
    tool = get_tool("read_chapter")
    pages = [await tool.run(ctx, {"id": ch.id, "page": i}) for i in (1, 2, 3)]
    assert "".join(page["content"] for page in pages) == content
    assert all(page["pages"] == 3 for page in pages)
    assert "error" in await tool.run(ctx, {"id": ch.id, "page": 4})


async def test_search_excludes_foreign_and_disabled(ctx):
    other = Project(user_id=ctx.user_id, title="另一项目")
    ctx.db.add(other)
    await ctx.db.flush()
    for source in ("memory", "kb"):
        await retrieval_service.upsert_many(
            ctx.db,
            ctx.project_id,
            source,
            [
                retrieval_service.ChunkInput(1, "竹林深处有古剑"),
                retrieval_service.ChunkInput(2, "竹林不能召回", enabled=False),
            ],
        )
        await retrieval_service.upsert_many(ctx.db, other.id, source, [retrieval_service.ChunkInput(3, "竹林其他项目")])
    await ctx.db.commit()
    for name in ("search_memories", "search_knowledge"):
        result = await get_tool(name).run(ctx, {"query": "竹林", "top_k": 10})
        assert [hit["ref_id"] for hit in result["hits"]] == [1]
        assert "error" in await get_tool(name).run(ctx, {"query": "竹林", "top_k": 11})


@pytest.mark.parametrize("content,original", [("重复。重复。", "找不到"), ("重复。重复。", "重复"), ("哈哈哈", "哈哈")])
async def test_replacement_requires_unique_original(ctx, content, original):
    ch = await new_chapter(ctx, content)
    args = {"chapter_id": ch.id, "original": original, "replacement": "新文本"}
    for method in (get_tool("replace_chapter_text").preview, get_tool("replace_chapter_text").execute):
        result = await method(ctx, args)
        assert result == {"error": "原文未找到或不唯一，请扩大或缩小引用范围"}
    await ctx.db.refresh(ch)
    assert ch.content == content


async def test_replacement_records_agent_version_and_stales_analysis(ctx):
    ch = await new_chapter(ctx)
    cid = ch.id
    analysis = ChapterAnalysis(
        project_id=ctx.project_id, chapter_id=cid, chapter_no=1, content_hash=analysis_service.content_hash(ch.content)
    )
    ctx.db.add(analysis)
    await ctx.db.commit()
    tool = get_tool("replace_chapter_text")
    args = {"chapter_id": cid, "original": "握紧", "replacement": "放下"}
    before = await tool.preview(ctx, args)
    ch.content += "远处传来钟声。"
    await ctx.db.commit()
    refreshed = await tool.preview(ctx, args)
    assert before["content_hash"] != refreshed["content_hash"]
    result = await tool.execute(ctx, args)
    assert "error" not in result, result
    await ctx.db.commit()
    version = await ctx.db.get(ChapterVersion, result["after_version_id"])
    previous = await ctx.db.get(ChapterVersion, result["before_version_id"])
    assert version.source == "agent" and "放下" in version.content and "握紧" in previous.content
    assert (await analysis_service.chapter_state(ctx.db, ctx.user_id, cid)).status == "stale"
    ch.status = "generating"
    await ctx.db.commit()
    result = await tool.preview(ctx, {**args, "original": "放下"})
    assert "正在生成" in result["error"]


async def test_foreign_project_references_at_every_depth(ctx):
    foreign = Project(user_id=ctx.user_id, title="不能访问的项目")
    ctx.db.add(foreign)
    await ctx.db.flush()
    character = Character(project_id=foreign.id, name="别人的角色")
    chapter = Chapter(project_id=foreign.id, chapter_no=1, title="秘密", content="秘密正文")
    org = Organization(project_id=foreign.id, name="秘密组织")
    outline = Outline(project_id=foreign.id, title="秘密大纲", chapter_no=1)
    ctx.db.add_all([character, chapter, org, outline])
    await ctx.db.commit()
    cases = [
        ("get_character", {"id": character.id}),
        ("read_chapter", {"id": chapter.id}),
        ("update_character", {"id": character.id, "name": "越权"}),
        ("create_organization", {"name": "越权", "parent_id": org.id}),
        ("create_foreshadow", {"title": "越权", "character_ids": [character.id]}),
        ("update_chapter_plans", {"outline_id": outline.id, "plans": []}),
        ("replace_chapter_text", {"chapter_id": chapter.id, "original": "秘密", "replacement": "泄露"}),
        ("start_task", {"kind": "chapter.generate", "params": {"chapter_id": chapter.id}}),
        ("start_task", {"kind": "character.generate", "params": {"project_id": foreign.id}}),
        ("start_task", {"kind": "character.generate", "params": {"extra": {"character_ids": [character.id]}}}),
    ]
    for name, args in cases:
        tool = get_tool(name)
        method = tool.run if tool.kind == "read" else tool.preview
        assert await method(ctx, args) == {"error": "不存在"}, name


async def test_preview_stable_across_unrelated_generated_ids_and_batch_creates(ctx):
    tool = get_tool("create_character")
    args = {"name": "新角色"}
    before = await tool.preview(ctx, args)
    other = Project(user_id=ctx.user_id, title="另一项目")
    ctx.db.add(other)
    await ctx.db.flush()
    ctx.db.add(Character(project_id=other.id, name="占用序列"))
    await ctx.db.commit()
    assert await tool.preview(ctx, args) == before
    await new_character(ctx, "同批前一个角色")
    assert await tool.preview(ctx, args) == before


async def test_preview_failure_rolls_back_and_preserves_pending_outer_write(ctx):
    char = await new_character(ctx)
    row = await ctx.db.get(Character, char["id"])
    row.personality = "调用者待提交的编辑"
    bad = await get_tool("create_career").preview(ctx, {"name": "会失败", "stages": []})
    assert "error" in bad
    assert await ctx.db.scalar(select(func.count(Career.id))) == 0
    await ctx.db.commit()
    await ctx.db.refresh(row)
    assert row.personality == "调用者待提交的编辑"


async def test_execute_does_not_commit_outer_transaction(ctx, app):
    result = await get_tool("create_character").execute(ctx, {"name": "应随外层回滚"})
    assert "error" not in result
    await ctx.db.rollback()
    async with app.state.sessionmaker() as other:
        assert await other.scalar(select(func.count(Character.id))) == 0


@pytest.mark.parametrize(
    "kind",
    [
        "chapter.generate",
        "chapter.polish",
        "chapter.revise",
        "chapter.batch",
        "chapter.analyze",
        "outline.expand",
        "outline.continue",
        "character.generate",
        "organization.generate",
        "career.generate",
    ],
)
async def test_task_preview_uses_real_handler_without_claiming(ctx, fake_llm, kind):
    ch = await new_chapter(ctx)
    cid = ch.id
    outline, _ = await apply(ctx, "create_outline", {"title": "启程"})
    analysis = ChapterAnalysis(
        project_id=ctx.project_id, chapter_id=cid, chapter_no=1, content_hash=analysis_service.content_hash(ch.content)
    )
    ctx.db.add(analysis)
    await ctx.db.commit()
    params = {}
    if kind.startswith("chapter.") and kind != "chapter.batch":
        params["chapter_id"] = cid
    if kind == "chapter.revise":
        params.update(analysis_id=analysis.id, instruction="更紧凑")
    if kind == "chapter.batch":
        params.update(start_chapter_id=cid, end_chapter_id=cid, include_written=True)
    if kind == "outline.expand":
        params["outline_id"] = outline["id"]
    preview = await get_tool("start_task").preview(ctx, {"kind": kind, "params": params})
    assert "error" not in preview, preview
    assert "AI 调用" in preview["warnings"][0]
    await ctx.db.refresh(ch)
    assert ch.status == "draft"
    assert await ctx.db.scalar(select(func.count(Task.id))) == 0
    assert fake_llm.calls == []


async def test_start_task_whitelist_validation_and_transaction(ctx):
    tool = get_tool("start_task")
    assert "error" in await tool.preview(ctx, {"kind": "book.import", "params": {}})
    assert "error" in await tool.preview(ctx, {"kind": "chapter.generate", "params": {}})
    result = await tool.execute(ctx, {"kind": "character.generate", "params": {"instruction": "冷静的剑修"}})
    assert "error" not in result, result
    task = await ctx.db.get(Task, result["task_id"])
    assert task.project_id == ctx.project_id and task.status == "queued"
    await ctx.db.commit()
    active = await get_tool("list_active_tasks").run(ctx, {})
    assert [row["id"] for row in active["items"]] == [task.id]


async def test_task_request_always_binds_server_project(ctx):
    # 引用校验之外的第二道防线：即使参数里混入其他项目，最终请求仍绑定服务端项目。
    from app.services.agent.tools.tasks import StartArgs, request_params

    args = StartArgs.model_validate({"kind": "character.generate", "params": {"project_id": ctx.project_id + 999}})
    assert request_params(ctx, args)["project_id"] == ctx.project_id


async def test_changed_target_and_cascades_refresh_preview(ctx):
    char = await new_character(ctx)
    org, _ = await apply(ctx, "create_organization", {"name": "天剑宗"})
    await apply(ctx, "set_org_member", {"organization_id": org["id"], "character_id": char["id"]})
    tool = get_tool("update_character")
    args = {"id": char["id"], "personality": "冷静"}
    before = await tool.preview(ctx, args)
    row = await ctx.db.get(Character, char["id"])
    row.background = "其他编辑器补充的背景"
    await ctx.db.commit()
    refreshed = await tool.preview(ctx, args)
    assert before["diff"] == refreshed["diff"]
    assert before["guards"] != refreshed["guards"]
    assert refreshed["target"] == "林远"
    preview = await get_tool("delete_character").preview(ctx, {"id": char["id"]})
    assert any(item["field"].startswith("organization_members.") for item in preview["diff"])
    assert await ctx.db.scalar(select(func.count(OrganizationMember.id))) == 1


async def test_preview_does_not_change_project_timestamp(ctx):
    stamp = ctx.project.updated_at
    await get_tool("create_character").preview(ctx, {"name": "林远"})
    await ctx.db.commit()
    await ctx.db.refresh(ctx.project)
    assert ctx.project.updated_at == stamp
    assert await ctx.db.scalar(select(func.count(Character.id))) == 0


async def test_preview_reuses_business_validation_without_partial_writes(ctx):
    root, _ = await apply(ctx, "create_organization", {"name": "总会"})
    child, _ = await apply(ctx, "create_organization", {"name": "分会", "parent_id": root["id"]})
    result = await get_tool("update_organization").preview(ctx, {"id": root["id"], "parent_id": child["id"]})
    assert "下级" in result["error"]
    assert (await ctx.db.get(Organization, root["id"])).parent_id is None
    result = await get_tool("create_character").preview(ctx, {"name": "x" * 101})
    assert result["error"] == "工具参数校验失败" and result["errors"]


async def test_task_preview_real_handler_rejects_missing_config(ctx):
    await ctx.db.execute(delete(AIConfig).where(AIConfig.user_id == ctx.user_id))
    await ctx.db.commit()
    result = await get_tool("start_task").preview(ctx, {"kind": "character.generate", "params": {}})
    assert "尚未配置" in result["error"]
    assert await ctx.db.scalar(select(func.count(Task.id))) == 0


async def test_cross_user_nested_config_and_ids_are_not_found(ctx, make_client):
    other_client = make_client()
    await register(other_client, "bob")
    project = await create_project(other_client, style="")
    config = await create_ai_config(other_client)
    character = (
        await other_client.post(
            f"/api/v1/projects/{project['id']}/characters",
            json={"name": "私密角色"},
        )
    ).json()
    assert await get_tool("get_character").run(ctx, {"id": character["id"]}) == {"error": "不存在"}
    assert await get_tool("start_task").preview(
        ctx,
        {
            "kind": "character.generate",
            "params": {"ai_config_id": config["id"]},
        },
    ) == {"error": "不存在"}


async def test_task_execute_also_stays_in_outer_transaction(ctx, app):
    result = await get_tool("start_task").execute(ctx, {"kind": "character.generate", "params": {}})
    assert "task_id" in result
    await ctx.db.rollback()
    async with app.state.sessionmaker() as other:
        assert await other.scalar(select(func.count(Task.id))) == 0


def test_preview_diff_ignores_created_at_of_inserted_rows():
    from app.services.agent.tools.base import preview_diff

    row = {"id": 9, "project_id": 1, "name": "新人", "created_at": "2026-01-01T00:00:00"}
    diff = preview_diff([{"table": "characters", "pk": {"id": 9}, "before": None, "after": row}])
    assert [item["field"].rsplit(".", 1)[-1] for item in diff] == ["name"]
