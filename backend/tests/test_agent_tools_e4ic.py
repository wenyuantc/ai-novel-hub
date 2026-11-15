"""E4i-c：Agent 工具改走协作者分支、共享向量检索冻结、派生任务归属与撤销后单条记忆向量。

协作者 HTTP 入口仍关闭（E4i-d 开放）。这里：
- 工具层直接构造 ToolCtx（与 runner / 确认路径相同的参数），验证原轮次 accepted editor 能读写，
  而管理员兼 viewer / 已撤权 / 旧轮次、降级 viewer、撤权、重邀后的旧轮次一律以授权失效终止；
- 检索走真实轮次（中央受理 → 引擎领取 → runner 工具），断言作者向量调用的有无；
- 派生任务以测试内模拟的未来协作者确认入口（同 test_agent_audit_integration）走真实确认链。
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from app.ai.providers import AIProviderError
from app.models import (
    AgentChange,
    AgentConversation,
    AgentMessage,
    AIConfig,
    Chapter,
    Character,
    Organization,
    Project,
    RelationshipType,
    RetrievalChunk,
    StoryMemory,
    Task,
)
from app.services import kb_service, retrieval_service
from app.services.agent import effects, service, transactions
from app.services.agent.tools import get_tool
from app.services.agent.tools.base import ToolCtx
from tests.helpers import Gate, create_ai_config, create_project, fake_vector, register, wait_task
from tests.test_agent_access_e4ia import _revoke, _seed_turn, _set_role
from tests.test_agent_handler_e4ib import _actor, _invite, _send
from tests.test_agent_mcp_skills import change_action
from tests.test_agent_runtime import call

OWNER_KEY = "sk-owner-e4ic-000001"
EDITOR_KEY = "sk-editor-e4ic-00001"
OWNER_EMBED_KEY = "sk-owner-embed-e4ic1"
KB_TEXT = "# 天剑宗\n\n天剑宗师承三代，山门立于云海之上。\n\n## 戒律\n\n弟子不得私下山门。"


# ---------------- 公共夹具 ----------------


async def _project(app, owner_client, *, owner_name="e4ic-owner"):
    owner = await register(owner_client, owner_name)
    await create_ai_config(owner_client, api_key=OWNER_KEY)
    project = await create_project(owner_client)
    return owner, project


async def _call(app, actor_id, project_id, grant, name, args, *, method="execute", locked=False):
    """与 runner / 确认路径同形的 ToolCtx：根事务 + 审计协调器，成功后由协调器提交。"""
    tool = get_tool(name)
    async with (
        transactions.session(app.state.sessionmaker) as db,
        transactions.audit_scope(db, app.state.sessionmaker) as coordinator,
    ):
        project = await db.get(Project, project_id)
        ctx = ToolCtx(db, project, actor_id, app.state.task_engine.ctx, visibility_locked=locked,
                      audit_coordinator=coordinator, grant=grant)
        try:
            if method == "run":
                result = await tool.run(ctx, args)
                await db.rollback()
                return result
            result = await getattr(tool, method)(ctx, args)
        except BaseException:
            await db.rollback()
            raise
        await coordinator.commit(db, app.state.sessionmaker)
        return result


async def _seed(app, owner_client, owner_id, project_id):
    """作者经同一组工具（作者分支，grant=None）建好资料；章节走人工 HTTP。"""
    async def make(name, args):
        result = await _call(app, owner_id, project_id, None, name, args)
        assert "error" not in result, (name, result)
        return result

    character = await make("create_character", {"name": "林远"})
    other = await make("create_character", {"name": "苏晴"})
    world = await make("create_world_setting", {"title": "云海", "content": "终年不散的云海"})
    org = await make("create_organization", {"name": "天剑宗"})
    career = await make("create_career", {"name": "剑修", "stages": [{"name": "练气"}, {"name": "筑基"}]})
    foreshadow = await make("create_foreshadow", {"title": "断剑之谜"})
    outline = await make("create_outline", {"title": "第一卷"})
    response = await owner_client.post(f"/api/v1/projects/{project_id}/chapters", json={
        "title": "启程", "content": "林远推开门，走进雨夜。",
    })
    assert response.status_code == 201, response.text
    async with app.state.sessionmaker() as db:
        type_id = await db.scalar(select(RelationshipType.id).where(
            RelationshipType.project_id.is_(None),
        ).order_by(RelationshipType.id).limit(1))
    return SimpleNamespace(
        relation_type=type_id, character=character["id"], other=other["id"], world=world["id"], org=org["id"],
        career=career["id"],
        stage=career["stages"][0]["id"], foreshadow=foreshadow["id"], outline=outline["id"],
        chapter=response.json()["id"],
    )


async def _editor_world(app, owner_client, editor_client, *, allow_owner_ai=False, editor_config=True,
                        editor_name="e4ic-editor"):
    owner, project = await _project(app, owner_client)
    editor = await register(editor_client, editor_name)
    if editor_config:
        await create_ai_config(editor_client, api_key=EDITOR_KEY)
    member = await _invite(owner_client, editor_client, project["id"], editor_name, allow_owner_ai=allow_owner_ai)
    return SimpleNamespace(owner=owner, editor=editor, project=project, member=member,
                           grant=(member["invitation_id"], member["revision"]))


async def _snapshot(app, project_id):
    async with app.state.sessionmaker() as db:
        return (
            await db.scalar(select(func.count(Character.id)).where(Character.project_id == project_id)),
            await db.scalar(select(func.count(Organization.id)).where(Organization.project_id == project_id)),
            [(row.title, row.content) for row in await db.scalars(
                select(Chapter).where(Chapter.project_id == project_id).order_by(Chapter.id))],
        )


# ---------------- 读工具：协作者分支 ----------------


READS = [
    ("get_project_overview", lambda s: {}),
    ("list_characters", lambda s: {}),
    ("get_character", lambda s: {"id": s.character}),
    ("list_world_settings", lambda s: {}),
    ("list_organizations", lambda s: {}),
    ("get_organization", lambda s: {"id": s.org}),
    ("list_relationships", lambda s: {}),
    ("list_careers", lambda s: {}),
    ("get_outline_tree", lambda s: {}),
    ("get_outline", lambda s: {"id": s.outline}),
    ("list_chapters", lambda s: {}),
    ("read_chapter", lambda s: {"id": s.chapter}),
    ("get_chapter_analysis", lambda s: {"chapter_id": s.chapter}),
    ("list_foreshadows", lambda s: {}),
    ("list_setting_field_definitions", lambda s: {}),
    ("list_setting_entries", lambda s: {}),
    ("get_setting_entry_fields", lambda s: {"entry": {"kind": "character", "id": s.character}}),
    ("list_setting_timelines", lambda s: {}),
]
# 原先只认作者的读取：编辑者走作者分支时得到“不存在”。
STRICT_READS = {"list_characters", "get_character", "list_world_settings", "get_outline_tree", "get_outline",
                "read_chapter", "get_chapter_analysis", "list_chapters"}


async def test_editor_read_tools_use_collaborator_paths(app, client, make_client):
    editor_client = make_client()
    world = await _editor_world(app, client, editor_client)
    seeded = await _seed(app, client, world.owner["id"], world.project["id"])
    for name, args_for in READS:
        result = await _call(app, world.editor["id"], world.project["id"], world.grant, name, args_for(seeded),
                             method="run")
        assert "error" not in result, (name, result)
        if name in STRICT_READS:
            # 作者分支（grant=None）的默认 helper 仍严格作者：协作者分支不是改默认行为得来的。
            strict = await _call(app, world.editor["id"], world.project["id"], None, name, args_for(seeded),
                                 method="run")
            assert strict == {"error": "不存在"}, (name, strict)
    detail = await _call(app, world.editor["id"], world.project["id"], world.grant, "get_character",
                         {"id": seeded.character}, method="run")
    assert detail["name"] == "林远"
    chapter = await _call(app, world.editor["id"], world.project["id"], world.grant, "read_chapter",
                          {"id": seeded.chapter}, method="run")
    assert chapter["content"] == "林远推开门，走进雨夜。"


async def test_editor_chapter_reads_hide_owner_private_style_id(app, client, make_client):
    """复核补测：协作轮次的章节列表 / 正文与人工章节接口同一投影，不向编辑者（及其模型）给出作者私人风格 ID。"""
    editor_client = make_client()
    world = await _editor_world(app, client, editor_client)
    seeded = await _seed(app, client, world.owner["id"], world.project["id"])
    style = await client.post("/api/v1/styles", json={"name": "作者私有风格"})
    assert style.status_code == 201, style.text
    style_id = style.json()["id"]
    response = await client.patch(f"/api/v1/projects/{world.project['id']}/chapters/{seeded.chapter}",
                                  json={"style_id": style_id})
    assert response.status_code == 200, response.text
    listed = await _call(app, world.editor["id"], world.project["id"], world.grant, "list_chapters", {},
                         method="run")
    read = await _call(app, world.editor["id"], world.project["id"], world.grant, "read_chapter",
                       {"id": seeded.chapter}, method="run")
    assert [item["style_id"] for item in listed["items"]] == [None]
    assert read["style_id"] is None and read["content"] == "林远推开门，走进雨夜。"
    assert str(style_id) not in json.dumps({"style": [listed["items"][0]["style_id"], read["style_id"]]})
    # 作者轮次行为不变：本人风格照常可见。
    own = await _call(app, world.owner["id"], world.project["id"], None, "read_chapter", {"id": seeded.chapter},
                      method="run")
    own_list = await _call(app, world.owner["id"], world.project["id"], None, "list_chapters", {}, method="run")
    assert own["style_id"] == style_id and own_list["items"][0]["style_id"] == style_id


# ---------------- 写工具：原轮次编辑者成功 ----------------


WRITES = [
    ("create_character", lambda s: {"name": "新角色"}),
    ("update_character", lambda s: {"id": s.character, "personality": "沉稳"}),
    ("delete_world_setting", lambda s: {"id": s.world}),
    ("create_outline", lambda s: {"title": "第二卷"}),
    ("create_organization", lambda s: {"name": "新组织"}),
    ("update_organization", lambda s: {"id": s.org, "purpose": "守护云海"}),
    ("create_career", lambda s: {"name": "符修", "stages": [{"name": "入门"}]}),
    ("create_relationship", lambda s: {"a_id": s.character, "b_id": s.other, "type_id": s.relation_type}),
    ("create_foreshadow", lambda s: {"title": "云海异象"}),
    ("update_foreshadow", lambda s: {"id": s.foreshadow, "content": "断剑来自上一代宗主"}),
    ("set_org_member", lambda s: {"organization_id": s.org, "character_id": s.character}),
    ("assign_career", lambda s: {"character_id": s.character, "career_id": s.career, "stage_id": s.stage}),
    ("update_chapter_plans", lambda s: {"outline_id": s.outline, "plans": []}),
    ("update_chapter_meta", lambda s: {"id": s.chapter, "title": "雨夜启程"}),
    ("replace_chapter_text", lambda s: {"chapter_id": s.chapter, "original": "雨夜", "replacement": "风雪夜"}),
    ("update_project", lambda s: {"synopsis": "协作者写入"}),
]


@pytest.mark.parametrize("locked", [False, True])
async def test_editor_write_tools_succeed_with_original_grant(app, client, make_client, locked):
    editor_client = make_client()
    world = await _editor_world(app, client, editor_client)
    seeded = await _seed(app, client, world.owner["id"], world.project["id"])
    for name, args_for in WRITES:
        preview = await _call(app, world.editor["id"], world.project["id"], world.grant, name, args_for(seeded),
                              method="preview", locked=locked)
        assert "error" not in preview, (name, preview)
        result = await _call(app, world.editor["id"], world.project["id"], world.grant, name, args_for(seeded),
                             locked=locked)
        assert "error" not in result, (name, result)
    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, seeded.chapter)
        assert (chapter.title, chapter.content) == ("雨夜启程", "林远推开门，走进风雪夜。")
        assert await db.scalar(select(Character.id).where(Character.name == "新角色")) is not None
        assert (await db.get(Project, world.project["id"])).synopsis == "协作者写入"


# ---------------- 非原轮次编辑者：失败关闭，管理员 projects.edit 不并入 ----------------


DENIED_WRITES = [
    ("create_character", lambda s: {"name": "越权角色"}),
    ("create_organization", lambda s: {"name": "越权组织"}),
    ("update_chapter_meta", lambda s: {"id": s.chapter, "title": "越权标题"}),
    ("replace_chapter_text", lambda s: {"chapter_id": s.chapter, "original": "雨夜", "replacement": "越权"}),
]


async def _denied_source(app, client, system_admin, make_client, source):
    """返回（actor_id，项目，种子，用于工具的 grant）。管理员来源的账号仍保有人工 projects.edit。"""
    owner, project = await _project(app, client)
    seeded = await _seed(app, client, owner["id"], project["id"])
    pid = project["id"]
    if source.startswith("admin_"):
        member_client, username = system_admin, "settings_admin"
    else:
        member_client, username = make_client(), "e4ic-member"
        await register(member_client, username)
    actor_id = (await member_client.get("/api/v1/auth/me")).json()["id"]
    role = "viewer" if source == "admin_viewer" else "editor"
    member = await _invite(client, member_client, pid, username, role=role)
    grant = (member["invitation_id"], member["revision"])
    if source in ("admin_revoked", "revoked"):
        await _revoke(client, pid, actor_id, member["revision"])
    elif source in ("admin_stale_grant",):
        changed = await _set_role(client, pid, actor_id, member["revision"], "viewer")
        await _set_role(client, pid, actor_id, changed["revision"], "editor")
    elif source == "downgraded_viewer":
        await _set_role(client, pid, actor_id, member["revision"], "viewer")
    elif source == "old_round":
        await _revoke(client, pid, actor_id, member["revision"])
        await _invite(client, member_client, pid, username)
    return actor_id, project, seeded, grant


@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("source", [
    "admin_viewer", "admin_revoked", "admin_stale_grant", "downgraded_viewer", "revoked", "old_round",
])
async def test_non_original_editor_cannot_write_through_collaborator_paths(
    app, client, system_admin, make_client, source, locked,
):
    actor_id, project, seeded, grant = await _denied_source(app, client, system_admin, make_client, source)
    before = await _snapshot(app, project["id"])
    for name, args_for in DENIED_WRITES:
        for method in ("preview", "execute"):
            # 授权失效终止轮次（VisibilityPolicyError），不能被工具层转成普通错误后继续。
            with pytest.raises(transactions.AgentAuthorizationChangedError):
                await _call(app, actor_id, project["id"], grant, name, args_for(seeded), method=method, locked=locked)
    assert await _snapshot(app, project["id"]) == before


# ---------------- 检索：受理冻结 + for_task 逐次重验 ----------------


async def _index_owner_kb(app, owner_client, project_id, owner_id):
    """作者上传资料（无向量时为关键词），再配置作者向量并把分块写成作者旧键空间（裁决 1）的向量。"""
    upload = await owner_client.post(f"/api/v1/projects/{project_id}/kb/documents", files={
        "file": ("资料.md", KB_TEXT.encode(), "text/markdown"),
    })
    assert upload.status_code == 202, upload.text
    assert (await wait_task(owner_client, upload.json()["task"]))["status"] == "succeeded"
    await create_ai_config(owner_client, name="作者向量", model="owner-embed", model_type="embedding",
                           api_key=OWNER_EMBED_KEY)
    async with app.state.sessionmaker() as db:
        config = await retrieval_service.default_embedding_config(db, owner_id)
        for row in await db.scalars(select(RetrievalChunk).where(
            RetrievalChunk.project_id == project_id, RetrievalChunk.source == "kb",
        )):
            vector = retrieval_service.normalize_vector(fake_vector(row.text))
            row.embedding = retrieval_service.encode_vector(vector)
            row.embedding_key = retrieval_service.make_embedding_key(
                retrieval_service.config_model_key(config), len(vector),
            )
            row.version += 1
        await db.commit()
    retrieval_service.invalidate(project_id)


async def _retrieval_world(app, owner_client, editor_client, *, allow_owner_ai):
    world = await _editor_world(app, owner_client, editor_client, allow_owner_ai=allow_owner_ai)
    # 协作者本人也有向量配置：任何情况下都不应被检索使用。
    await create_ai_config(editor_client, name="本人向量", model="editor-embed", model_type="embedding")
    await _index_owner_kb(app, owner_client, world.project["id"], world.owner["id"])
    async with app.state.sessionmaker() as db:
        # 与真实入口（E4i-d）一致：协作者会话记录创建时的邀请轮次。
        conversation = AgentConversation(user_id=world.editor["id"], project_id=world.project["id"],
                                         collaboration_invitation_id=world.grant[0])
        db.add(conversation)
        await db.commit()
        world.conversation_id = conversation.id
    return world


async def _terminal(app, task_id):
    async with app.state.sessionmaker() as db:
        for _ in range(1000):
            row = await db.get(Task, task_id, populate_existing=True)
            if row.status not in ("queued", "running"):
                return row
            await db.rollback()
            await asyncio.sleep(0.01)
    raise AssertionError("任务未结束")


async def _search_turn(app, fake_llm, world, query="天剑宗师承"):
    fake_llm.push_tools([call("search_knowledge", {"query": query})])
    before = len(fake_llm.embed_calls)
    task = await _send(app, world.editor["id"], world.project["id"], world.conversation_id)
    row = await _terminal(app, task["id"])
    async with app.state.sessionmaker() as db:
        content = await db.scalar(select(AgentMessage.content).where(
            AgentMessage.turn_task_id == task["id"], AgentMessage.role == "tool",
        ))
    data = json.loads(content)["data"] if content is not None else None
    return row, data, content, fake_llm.embed_calls[before:]


async def test_granted_editor_search_uses_frozen_author_vectors(app, client, make_client, fake_llm):
    editor_client = make_client()
    world = await _retrieval_world(app, client, editor_client, allow_owner_ai=True)
    row, data, _, embeds = await _search_turn(app, fake_llm, world)
    assert row.status == "succeeded", row.error
    selection = row.input["_shared_embedding"]
    assert selection["mode"] == "vector" and selection["model"]["config_owner_id"] == world.owner["id"]
    assert selection["grant_identity"] == {"invitation_id": world.grant[0], "revision": world.grant[1]}
    # 查询向量用作者向量模型（费用来源=作者）；协作者本人的向量配置不参与。
    assert [(item["model"], item["texts"]) for item in embeds] == [("owner-embed", ["天剑宗师承"])]
    assert data["mode"] == "hybrid" and data["hits"], data


async def test_unauthorized_editor_search_is_keyword_only(app, client, make_client, fake_llm):
    editor_client = make_client()
    world = await _retrieval_world(app, client, editor_client, allow_owner_ai=False)
    row, data, _, embeds = await _search_turn(app, fake_llm, world)
    assert row.status == "succeeded", row.error
    assert row.input["_shared_embedding"]["mode"] == "keyword"
    assert row.input["_shared_embedding"]["reason"] == "owner_embedding_not_allowed"
    # 零作者向量调用，也不改用协作者本人向量模型。
    assert embeds == []
    assert data["mode"] == "keyword" and data["hits"], data
    assert kb_service.KEYWORD_ONLY_COLLABORATOR in data["warnings"]


async def test_unusable_author_embedding_config_is_keyword_only(app, client, make_client, fake_llm):
    editor_client = make_client()
    world = await _retrieval_world(app, client, editor_client, allow_owner_ai=True)
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(
            AIConfig.user_id == world.owner["id"], AIConfig.model_type == "embedding",
        ).values(api_key_enc="损坏的密文"))
        await db.commit()
    row, data, _, embeds = await _search_turn(app, fake_llm, world)
    assert row.status == "succeeded", row.error
    assert "_shared_embedding" not in row.input
    assert embeds == [] and data["mode"] == "keyword"
    assert data["warnings"] == [kb_service.KEYWORD_ONLY_OWNER_UNAVAILABLE]


async def test_ordinary_embed_failure_degrades_to_keyword_with_public_warning(app, client, make_client, fake_llm):
    editor_client = make_client()
    world = await _retrieval_world(app, client, editor_client, allow_owner_ai=True)
    fake_llm.embed_error = AIProviderError("模型调用失败：owner-embed 端点 internal.owner.example 拒绝")
    row, data, content, embeds = await _search_turn(app, fake_llm, world)
    assert row.status == "succeeded", row.error
    assert [item["model"] for item in embeds] == ["owner-embed"]
    assert data["mode"] == "keyword" and data["hits"], data
    # 提供方错误原文（作者模型名 / 端点）不进入协作者的工具结果。
    assert data["warnings"] == ["查询向量生成失败，本次只用关键词检索"]
    assert "internal.owner.example" not in content


@pytest.mark.parametrize("change", ["model_off", "revoke"])
async def test_authorization_change_before_query_embed_fails_closed(app, client, make_client, fake_llm, change):
    editor_client = make_client()
    world = await _retrieval_world(app, client, editor_client, allow_owner_ai=True)
    gate = Gate()
    # 受控发送门禁（模拟响应头）之前停住：撤权在冻结选择、构造 provider 之后、真实发送之前提交。
    fake_llm.embed_before_gates.append(gate)
    fake_llm.push_tools([call("search_knowledge", {"query": "天剑宗师承"})])
    before = len(fake_llm.embed_calls)
    task = await _send(app, world.editor["id"], world.project["id"], world.conversation_id)
    try:
        await gate.wait_reached(10)
        base = f"/api/v1/projects/{world.project['id']}"
        if change == "revoke":
            await _revoke(client, world.project["id"], world.editor["id"], world.member["revision"])
        else:
            response = await client.patch(f"{base}/collaborators/{world.editor['id']}", json={
                "expected_revision": world.member["revision"], "allow_owner_ai": False,
            })
            assert response.status_code == 200, response.text
    finally:
        gate.release()
    row = await _terminal(app, task["id"])
    assert row.status == "failed", (row.status, row.error)
    # 授权 / 策略类错误不降级为关键词：本次查询零作者向量发送，检索结果也不写入会话。
    assert fake_llm.embed_calls[before:] == []
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(AgentMessage.id).where(
            AgentMessage.turn_task_id == task["id"], AgentMessage.role == "tool",
        )) is None


async def test_frozen_vector_selection_failure_inside_lock_fails_closed(
    app, client, make_client, fake_llm, monkeypatch,
):
    """复核补测：锁内读取冻结配置失败（授权 / 配置类）终止轮次，不作为普通工具错误继续，零作者向量调用。"""
    from app.core.errors import ForbiddenError

    editor_client = make_client()
    world = await _retrieval_world(app, client, editor_client, allow_owner_ai=True)

    async def changed(*args, **kwargs):
        raise ForbiddenError("模型授权已变化", extra={"code": "permission_denied"})

    monkeypatch.setattr(retrieval_service, "load_bound_shared_embedding_config", changed)
    row, data, content, embeds = await _search_turn(app, fake_llm, world)
    assert row.status == "failed", (row.status, row.error)
    assert embeds == [] and content is None and data is None


async def test_author_search_unchanged(app, client, fake_llm):
    """作者轮次不冻结共享选择，仍按本人默认向量检索。"""
    from tests.test_agent_runtime import send_task, setup_agent

    project, _, url = await setup_agent(client)
    owner_id = (await client.get("/api/v1/auth/me")).json()["id"]
    await _index_owner_kb(app, client, project["id"], owner_id)
    fake_llm.push_tools([call("search_knowledge", {"query": "天剑宗师承"})])
    before = len(fake_llm.embed_calls)
    turn = await send_task(client, url)
    done = await wait_task(client, turn)
    assert done["status"] == "succeeded", done
    row = await _terminal(app, turn["id"])
    assert "_shared_embedding" not in row.input
    assert [item["model"] for item in fake_llm.embed_calls[before:]] == ["owner-embed"]


# ---------------- start_task：预览不暴露作者配置 ID，派生任务归编辑者与原轮次 ----------------


def _simulate_collaborator_entry(monkeypatch, editor_id, project_id):
    """E4i-d 起使用真实协作者确认入口；只关闭续跑，使本测试只观察确认事务本身。"""
    async def no_resume(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "maybe_resume", no_resume)


async def test_editor_start_task_preview_hides_owner_config_and_derived_task_is_editors(
    app, client, make_client, fake_llm, monkeypatch,
):
    editor_client = make_client()
    # 编辑者本人无文本配置、作者授权开启：对话与派生任务都回退作者模型（费用来源=作者）。
    world = await _editor_world(app, client, editor_client, allow_owner_ai=True, editor_config=False)
    response = await client.post(f"/api/v1/projects/{world.project['id']}/chapters", json={
        "title": "启程", "content": "林远推开门，走进雨夜。",
    })
    assert response.status_code == 201, response.text
    chapter_id = response.json()["id"]
    async with app.state.sessionmaker() as db:
        # 与真实入口（E4i-d）一致：协作者会话记录创建时的邀请轮次。
        conversation = AgentConversation(user_id=world.editor["id"], project_id=world.project["id"],
                                         collaboration_invitation_id=world.grant[0])
        db.add(conversation)
        await db.commit()
        cid = conversation.id
        owner_config_id = await db.scalar(select(AIConfig.id).where(AIConfig.user_id == world.owner["id"]))
    fake_llm.push_tools([call("start_task", {"kind": "chapter.generate", "params": {"chapter_id": chapter_id}})])
    turn = await _send(app, world.editor["id"], world.project["id"], cid)
    row = await _terminal(app, turn["id"])
    assert row.status == "paused", row.error
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        change = await db.scalar(select(AgentChange).where(AgentChange.turn_task_id == turn["id"]))
    assert change.tool_name == "start_task" and change.status == "proposed"
    # 预览进入模型上下文与本人面板：作者回退时不给作者的配置 ID，只提示费用来源。
    assert change.preview["ai_config_id"] is None
    assert "将使用作者授权的模型配置，外部费用由作者账户承担" in change.preview["warnings"]
    assert all(item["field"] != "ai_config_id" or item["after"] is None for item in change.preview["diff"])

    _simulate_collaborator_entry(monkeypatch, world.editor["id"], world.project["id"])
    base = f"/api/v1/projects/{world.project['id']}/agent"
    detail = await editor_client.get(f"{base}/conversations/{cid}")
    assert detail.status_code == 200, detail.text
    view = next(item for item in detail.json()["changes"] if item["id"] == change.id)
    confirmed = await editor_client.post(f"{base}/changes/{change.id}/confirm", json=change_action(view))
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "executed", confirmed.json()
    async with app.state.sessionmaker() as db:
        derived = await db.get(Task, confirmed.json()["result"]["task_id"])
        assert derived.kind == "chapter.generate" and derived.status == "queued"
        # 派生任务归编辑者、绑定本轮同一邀请轮次，模型按 R4 回退作者配置（费用来源=作者）。
        assert derived.user_id == world.editor["id"]
        assert derived.input["_collaboration_grant"] == {
            "invitation_id": world.grant[0], "revision": world.grant[1],
        }
        model = derived.input["_project_model"]
        assert (model["source"], model["config_owner_id"], model["actor_id"]) == (
            "owner", world.owner["id"], world.editor["id"],
        )
        assert model["config_id"] == owner_config_id


# ---------------- 裁决 17：撤销恢复记忆后只补建该条记忆的作者向量 ----------------


async def _memory_undo_world(app, owner_client, editor_client, *, allow_owner_ai):
    world = await _editor_world(app, owner_client, editor_client, allow_owner_ai=allow_owner_ai)
    pid = world.project["id"]
    created = []
    for content in ("林远在云海边拾到断剑", "苏晴拜入天剑宗"):
        response = await owner_client.post(f"/api/v1/projects/{pid}/memories", json={
            "chapter_no": 1, "content": content,
        })
        assert response.status_code == 201, response.text
        created.append(response.json()["id"])
    # 作者此后才配置向量：两条记忆都还缺向量。
    await create_ai_config(owner_client, name="作者向量", model="owner-embed", model_type="embedding",
                           api_key=OWNER_EMBED_KEY)
    return world, created


async def _undo_restoring_memory(app, monkeypatch, world, memory_id):
    actor_id, pid = world.editor["id"], world.project["id"]
    _, _, _, (change_id,) = await _seed_turn(app, actor_id, pid, world.grant, tool_name="update_chapter_plans")
    async with app.state.sessionmaker() as db:
        memory = await db.get(StoryMemory, memory_id)
        row = await db.get(AgentChange, change_id)
        row.status = "executed"
        row.effects = [{"table": "story_memories", "pk": {"id": memory_id},
                        "before": {"id": memory_id, "content": memory.content}, "after": None}]
        await db.commit()

    # 只替换效果还原本身（其正确性由 effects 测试覆盖）；锁、能力判定、提交与派生均为真实路径。
    async def no_preflight(*_args, **_kwargs):
        return None

    async def restored(*_args, **_kwargs):
        return True

    monkeypatch.setattr(effects, "preflight_effect_versions", no_preflight)
    monkeypatch.setattr(effects, "undo", restored)
    async with app.state.sessionmaker() as db:
        db.info["authenticated_actor"] = await _actor(app, actor_id)
        row = await db.get(AgentChange, change_id)
        await service._undo_changes(db, actor_id, pid, [row], app.state.task_engine)


async def test_editor_undo_restoring_memory_enqueues_only_that_memory(app, client, make_client, monkeypatch):
    editor_client = make_client()
    world, (memory_id, _other) = await _memory_undo_world(app, client, editor_client, allow_owner_ai=True)
    async with app.state.sessionmaker() as db:
        owner_tasks = await db.scalar(select(func.count(Task.id)).where(
            Task.user_id == world.owner["id"], Task.kind == "kb.index",
        ))
    await _undo_restoring_memory(app, monkeypatch, world, memory_id)
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(Task).where(
            Task.user_id == world.editor["id"], Task.kind == "kb.index",
        )))
        assert len(rows) == 1
        task = rows[0]
        assert task.project_id == world.project["id"] and task.input["request"]["mode"] == "memory"
        # 只绑定本次恢复的那一条记忆，不补全书；归编辑者并绑定原邀请轮次与作者向量选择。
        assert task.input["_manual_memory_ref_ids"] == [memory_id]
        assert task.input["_collaboration_grant"] == {"invitation_id": world.grant[0], "revision": world.grant[1]}
        assert task.input["_shared_embedding"]["mode"] == "vector"
        # 不派生作者名下的全书记忆向量任务。
        assert await db.scalar(select(func.count(Task.id)).where(
            Task.user_id == world.owner["id"], Task.kind == "kb.index",
        )) == owner_tasks


async def test_unauthorized_editor_undo_creates_no_memory_vector_task(app, client, make_client, monkeypatch):
    editor_client = make_client()
    world, (memory_id, _other) = await _memory_undo_world(app, client, editor_client, allow_owner_ai=False)
    await _undo_restoring_memory(app, monkeypatch, world, memory_id)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.kind == "kb.index", Task.user_id.in_(
            (world.owner["id"], world.editor["id"]),
        ), Task.input["request"]["mode"].as_string() == "memory")) == 0


async def test_undo_memory_ids_only_include_restored_rows():
    rows = [
        {"table": "story_memories", "pk": {"id": 3}, "before": {"id": 3}, "after": None},
        {"table": "story_memories", "pk": {"id": 4}, "before": None, "after": {"id": 4}},
        {"table": "story_memories", "pk": {"id": 3}, "before": {"id": 3}, "after": {"id": 3}},
        {"table": "characters", "pk": {"id": 5}, "before": {"id": 5}, "after": None},
    ]
    assert service.restored_memory_ids(rows) == [3]

