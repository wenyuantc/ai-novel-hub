"""Agent 工具的模型结果不能继承管理员人工代管的私人风格可见性。"""

import json

import pytest
from sqlalchemy import select, update

from app.core.security import ACCESS_TOKEN_COOKIE, decode_access_token
from app.models import (
    AgentChange,
    AgentConversation,
    AgentMessage,
    AIConfig,
    Project,
    ProjectCollaborator,
    User,
    WritingStyle,
)
from app.services import project_access, project_service
from app.services.agent import context, transactions
from app.services.agent.tools import get_tool
from app.services.agent.tools.base import ToolCtx
from app.services.ai_dispatch import VisibilityPolicyError
from tests.helpers import create_ai_config, create_project, register
from tests.test_agent_context_execution_identity import _capture_execution, _provider
from tests.test_agent_context_execution_identity import _setup as _setup_guarded_execution
from tests.test_agent_runtime import send_task, setup_agent
from tests.test_collaboration_style_privacy import _style


async def _world(app, owner, make_client):
    author = await register(owner, "agent-style-owner")
    editor_client, manager_client = make_client(), make_client()
    editor = await register(editor_client, "agent-style-editor")
    manager = await register(manager_client, "agent-style-manager")
    owner_style = await _style(owner, "作者风格秘密名称", "作者风格秘密描述")
    editor_style = await _style(editor_client, "编辑者自己的风格", "编辑者自己的描述")
    project = await create_project(owner, style="", default_style_id=owner_style["id"])
    async with app.state.sessionmaker() as db:
        (await db.get(User, manager["id"])).is_admin = True
        preset = await db.scalar(select(WritingStyle).where(WritingStyle.user_id.is_(None)).limit(1))
        preset_id = preset.id
        await db.commit()
    for username, client in (("agent-style-editor", editor_client), ("agent-style-manager", manager_client)):
        invitation = await owner.post(
            f"/api/v1/projects/{project['id']}/collaborators", json={"username": username, "role": "editor"},
        )
        assert invitation.status_code == 201, invitation.text
        data = invitation.json()
        accepted = await client.post(
            f"/api/v1/collaboration/invitations/project/{data['invitation_id']}/accept",
            json={"expected_revision": data["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
    return author, editor, manager, editor_client, manager_client, project, owner_style, editor_style, preset_id


async def _grant(db, project_id, actor_id):
    """协作执行体绑定的原邀请轮次；Agent 写入锁后复核要求与之一致。"""
    member = await db.scalar(select(ProjectCollaborator).where(
        ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == actor_id,
    ))
    return member.invitation_id, member.revision


async def _overview(app, project_id, actor_id):
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, project_id)
        ctx = ToolCtx(db, project, actor_id, app.state.task_engine.ctx)
        return await get_tool("get_project_overview").run(ctx, {})


async def test_project_tools_and_history_do_not_send_pen_name_to_model(app, client, fake_llm):
    """人工 API 保留署名；实时工具输出及旧回执重放不能带用户私库身份。"""
    owner = await register(client, "agent-pen-owner")
    response = await client.post("/api/v1/pen-names", json={"name": "仅人工可见的笔名"})
    assert response.status_code == 201, response.text
    pen = response.json()
    project = await create_project(client, pen_name_id=pen["id"])
    assert project["pen_name"] == {"id": pen["id"], "name": "仅人工可见的笔名"}
    config = await create_ai_config(client)

    async with (
        transactions.session(app.state.sessionmaker) as db,
        transactions.audit_scope(db, app.state.sessionmaker) as coordinator,
    ):
        row = await project_access.require_project(
            db, owner["id"], project["id"], "ai.use", for_update=True, visibility_write=True,
        )
        ctx = ToolCtx(db, row, owner["id"], app.state.task_engine.ctx,
                      visibility_locked=True, audit_coordinator=coordinator)
        overview = await get_tool("get_project_overview").run(ctx, {})
        changed = await get_tool("update_project").execute(ctx, {"synopsis": "人工摘要仍可写"})
        assert overview["title"] == project["title"] and "pen_name" not in overview
        assert changed["synopsis"] == "人工摘要仍可写" and "pen_name" not in changed
        await coordinator.commit(db, app.state.sessionmaker)

    async with app.state.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        old = (await project_service.project_out(db, row, actor_user_id=owner["id"])).model_dump(mode="json")
        assert old["pen_name"] == {"id": pen["id"], "name": "仅人工可见的笔名"}
        conversation = AgentConversation(user_id=owner["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        messages_to_add = [AgentMessage(conversation_id=conversation.id, role="user", content="继续创作")]
        for index, name in enumerate(("get_project_overview", "update_project")):
            call_id = f"legacy-{index}"
            messages_to_add.extend([
                AgentMessage(conversation_id=conversation.id, role="assistant", content="", tool_calls=[{
                    "id": call_id, "name": name, "arguments": "{}",
                }]),
                AgentMessage(conversation_id=conversation.id, role="tool", tool_call_id=call_id,
                             tool_name=name, content=context.tool_result(old)),
            ])
        db.add_all(messages_to_add)
        await db.flush()
        messages, _ = await context.build_messages(
            db, conversation, row, await db.get(AIConfig, config["id"]),
            fake_llm.factory("openai_compat", "test", None, 30),
        )
        replayed = [message for message in messages if message.get("tool_call_id", "").startswith("legacy-")]
        assert len(replayed) == 2
        for message in replayed:
            data = json.loads(message["content"])["data"]
            assert data["title"] == project["title"] and "pen_name" not in data
        assert "仅人工可见的笔名" not in json.dumps(messages, ensure_ascii=False)


def _assert_hidden(payload):
    assert payload["default_style_id"] is None and payload["default_style"] is None
    text = json.dumps(payload, ensure_ascii=False)
    assert "作者风格秘密名称" not in text and "作者风格秘密描述" not in text


def test_legacy_external_execution_marker_filtered_without_losing_safe_error():
    value = {
        "error": "外部调用结果未知",
        "nested": json.dumps({"_external_execution": "execution-private", "safe": "安全信息"}),
    }
    change = AgentChange(kind="external", status="failed", result=value)
    message = AgentMessage(role="tool", tool_name="external", tool_call_id="c", content=json.dumps({"data": value}))
    for projected in (context.change_result_for_model(change), context.message_dict(message)):
        rendered = json.dumps(projected, ensure_ascii=False)
        assert "外部调用结果未知" in rendered and "安全信息" in rendered
        assert "_external_execution" not in rendered and "execution-private" not in rendered
    assert change.result == value


def test_history_drops_internal_preview_guards_without_losing_ordinary_data():
    hidden = {"table": "projects", "pk": {"id": 1}, "hash": "internal-style-hash"}
    value = {
        "error": "需要确认",
        "preview": {"title": "修改", "diff": [], "guards": [hidden]},
        "nested": json.dumps({"diff": [], "guards": [hidden], "warnings": []}),
        "mcp_preview": {"diff": [], "warnings": [], "guards": [{
            "plugin_id": 42, "schema_hash": "plugin-schema-private", "enabled": True,
        }]},
        "external": {"preview": {"diff": [], "warnings": [], "guards": [{"name": "普通卫兵"}]}},
    }
    change = AgentChange(kind="external", status="failed", result=value)
    message = AgentMessage(role="tool", tool_name="external", tool_call_id="c", content=json.dumps({"data": value}))
    for projected in (context.change_result_for_model(change), context.message_dict(message)):
        rendered = json.dumps(projected, ensure_ascii=False)
        assert "需要确认" in rendered and "普通卫兵" in rendered
        assert "internal-style-hash" not in rendered and "plugin-schema-private" not in rendered
    assert change.result == value and value["preview"]["guards"] == [hidden]
    truncated = AgentMessage(role="tool", tool_name="external", tool_call_id="c",
                             content='{"diff":[],"guards":[{"hash":"internal-style-hash"')
    assert "internal-style-hash" not in context.message_dict(truncated)["content"]


async def test_overview_uses_actual_actor_not_admin_projection(app, client, make_client):
    author, editor, manager, _, _, project, own_style, editor_style, preset_id = await _world(app, client, make_client)
    owner_view = await _overview(app, project["id"], author["id"])
    assert owner_view["default_style_id"] == own_style["id"]
    assert owner_view["default_style"]["description"] == "作者风格秘密描述"
    _assert_hidden(await _overview(app, project["id"], editor["id"]))
    _assert_hidden(await _overview(app, project["id"], manager["id"]))

    # 异常绑定其他人的风格也按真实持有人判断，不按作品作者或管理员身份推测。
    async with app.state.sessionmaker() as db:
        (await db.get(Project, project["id"])).default_style_id = editor_style["id"]
        await db.commit()
    assert (await _overview(app, project["id"], editor["id"]))["default_style_id"] == editor_style["id"]
    assert (await _overview(app, project["id"], author["id"]))["default_style_id"] is None
    assert (await _overview(app, project["id"], manager["id"]))["default_style_id"] is None

    async with app.state.sessionmaker() as db:
        (await db.get(Project, project["id"])).default_style_id = preset_id
        await db.commit()
    for actor in (author, editor, manager):
        result = await _overview(app, project["id"], actor["id"])
        assert result["default_style_id"] == preset_id and result["default_style"]["is_preset"] is True


@pytest.mark.parametrize("actor_name", ["editor", "manager"])
async def test_update_preview_and_execution_hide_private_style_and_preserve_binding(
    app, client, make_client, actor_name,
):
    author, editor, manager, _, _, project, style, _, _ = await _world(app, client, make_client)
    actor = editor if actor_name == "editor" else manager
    tool = get_tool("update_project")
    async with (
        transactions.session(app.state.sessionmaker) as db,
        transactions.audit_scope(db, app.state.sessionmaker) as coordinator,
    ):
        # 此处只模拟未来协作执行体已持有许可；生产 Agent 入口仍然严格作者专用。
        row = await project_access.require_project(
            db, actor["id"], project["id"], "ai.use", for_update=True, visibility_write=True,
        )
        ctx = ToolCtx(db, row, actor["id"], app.state.task_engine.ctx,
                      visibility_locked=True, audit_coordinator=coordinator,
                      grant=await _grant(db, project["id"], actor["id"]))
        preview = await tool.preview(ctx, {"synopsis": "新的剧情"})
        assert "error" not in preview
        assert "作者风格秘密" not in json.dumps(preview, ensure_ascii=False)
        assert all(not item["field"].endswith(".default_style_id") for item in preview["diff"])
        assert (await db.get(Project, project["id"])).default_style_id == style["id"]
        result = await tool.execute(ctx, {"synopsis": "新的剧情"})
        assert "error" not in result
        _assert_hidden(result)
        await coordinator.commit(db, app.state.sessionmaker)
    async with app.state.sessionmaker() as db:
        stored = await db.get(Project, project["id"])
        assert stored.default_style_id == style["id"] and stored.synopsis == "新的剧情"
        assert stored.user_id == author["id"]


@pytest.mark.parametrize("actor_name", ["editor", "manager"])
@pytest.mark.parametrize("requested", ["null", "preset"])
async def test_collaborator_cannot_replace_private_style_binding(app, client, make_client, actor_name, requested):
    _, editor, manager, _, _, project, style, _, preset_id = await _world(app, client, make_client)
    actor = editor if actor_name == "editor" else manager
    async with (
        transactions.session(app.state.sessionmaker) as db,
        transactions.audit_scope(db, app.state.sessionmaker) as coordinator,
    ):
        row = await project_access.require_project(
            db, actor["id"], project["id"], "ai.use", for_update=True, visibility_write=True,
        )
        ctx = ToolCtx(db, row, actor["id"], app.state.task_engine.ctx,
                      visibility_locked=True, audit_coordinator=coordinator,
                      grant=await _grant(db, project["id"], actor["id"]))
        params = {"default_style_id": None if requested == "null" else preset_id}
        assert "error" in await get_tool("update_project").preview(ctx, params)
        assert "error" in await get_tool("update_project").execute(ctx, params)
        await coordinator.commit(db, app.state.sessionmaker)
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project["id"])).default_style_id == style["id"]


@pytest.mark.parametrize("requested", ["null", "preset", "own"])
async def test_owner_can_change_default_style_with_visible_result(app, client, make_client, requested):
    owner, _, _, _, _, project, own_style, _, preset_id = await _world(app, client, make_client)
    style_id = None if requested == "null" else preset_id if requested == "preset" else own_style["id"]
    async with (
        transactions.session(app.state.sessionmaker) as db,
        transactions.audit_scope(db, app.state.sessionmaker) as coordinator,
    ):
        row = await project_access.require_project(
            db, owner["id"], project["id"], "ai.use", for_update=True, visibility_write=True,
        )
        ctx = ToolCtx(db, row, owner["id"], app.state.task_engine.ctx,
                      visibility_locked=True, audit_coordinator=coordinator)
        preview = await get_tool("update_project").preview(ctx, {"default_style_id": style_id})
        assert "error" not in preview
        result = await get_tool("update_project").execute(ctx, {"default_style_id": style_id})
        assert "error" not in result and result["default_style_id"] == style_id
        assert (result["default_style"] is None) == (style_id is None)
        if style_id is not None:
            assert result["default_style"]["is_preset"] == (requested == "preset")
        await coordinator.commit(db, app.state.sessionmaker)
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project["id"])).default_style_id == style_id


async def test_model_messages_contain_sanitized_overview(app, client, make_client, fake_llm):
    _, _, manager, _, manager_client, project, _, _, _ = await _world(app, client, make_client)
    # 让同一真实管理员账号持有自己的配置；这里只测试消息组装，不开放协作者 Agent。
    config = await create_ai_config(manager_client)
    async with app.state.sessionmaker() as db:
        manager_config = await db.get(AIConfig, config["id"])
        project_row = await db.get(Project, project["id"])
        conversation = AgentConversation(user_id=manager["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        result = await get_tool("get_project_overview").run(
            ToolCtx(db, project_row, manager["id"], app.state.task_engine.ctx), {},
        )
        _assert_hidden(result)
        db.add_all([
            AgentMessage(conversation_id=conversation.id, role="user", content="检查作品"),
            AgentMessage(conversation_id=conversation.id, role="assistant", content="", tool_calls=[{
                "id": "overview", "name": "get_project_overview", "arguments": "{}",
            }]),
            AgentMessage(conversation_id=conversation.id, role="tool", tool_call_id="overview",
                         tool_name="get_project_overview", content=context.tool_result(result)),
        ])
        await db.flush()
        provider = fake_llm.factory("openai_compat", "test", None, 30)
        messages, _ = await context.build_messages(db, conversation, project_row, manager_config, provider)
        text = json.dumps(messages, ensure_ascii=False)
        assert "作者风格秘密名称" not in text and "作者风格秘密描述" not in text
        tool_message = next(item for item in messages if item.get("tool_call_id") == "overview")
        _assert_hidden(json.loads(tool_message["content"])["data"])


async def test_legacy_admin_overview_never_replays_foreign_private_style(app, client, make_client, fake_llm):
    _, _, manager, _, manager_client, project, _, _, _ = await _world(app, client, make_client)
    config = await create_ai_config(manager_client)
    async with app.state.sessionmaker() as db:
        project_row = await db.get(Project, project["id"])
        conversation = AgentConversation(user_id=manager["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        # 模拟旧版本用管理员人工投影写入的历史原始记录；不能修改事实以制造通过。
        old = (await project_service.project_out(db, project_row, actor_user_id=manager["id"])).model_dump(mode="json")
        assert old["default_style"]["name"] == "作者风格秘密名称"
        db.add_all([
            AgentMessage(conversation_id=conversation.id, role="user", content="继续创作"),
            AgentMessage(conversation_id=conversation.id, role="assistant", content="", tool_calls=[{
                "id": "old-overview", "name": "get_project_overview", "arguments": "{}",
            }]),
            AgentMessage(conversation_id=conversation.id, role="tool", tool_call_id="old-overview",
                         tool_name="get_project_overview", content=context.tool_result(old)),
        ])
        await db.flush()
        messages, _ = await context.build_messages(
            db, conversation, project_row, await db.get(AIConfig, config["id"]),
            fake_llm.factory("openai_compat", "test", None, 30),
        )
        text = json.dumps(messages, ensure_ascii=False)
        assert "作者风格秘密名称" not in text and "作者风格秘密描述" not in text
        tool_message = next(item for item in messages if item.get("tool_call_id") == "old-overview")
        _assert_hidden(json.loads(tool_message["content"])["data"])


async def test_legacy_project_result_discards_untrusted_extra_fields(app, client, make_client, fake_llm):
    _, _, manager, _, manager_client, project, _, _, _ = await _world(app, client, make_client)
    config = await create_ai_config(manager_client)
    async with app.state.sessionmaker() as db:
        project_row = await db.get(Project, project["id"])
        raw = (await project_service.project_out(db, project_row, actor_user_id=manager["id"])).model_dump(mode="json")
        raw["unknown_nested"] = {"content": "作者风格秘密描述"}
        content = json.dumps({"untrusted": True, "data": raw, "unknown": "作者风格秘密名称"}, ensure_ascii=False)
        conversation = AgentConversation(user_id=manager["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        db.add_all([
            AgentMessage(conversation_id=conversation.id, role="user", content="继续"),
            AgentMessage(conversation_id=conversation.id, role="assistant", content="", tool_calls=[{
                "id": "injected", "name": "get_project_overview", "arguments": "{}",
            }]),
        ])
        old = AgentMessage(conversation_id=conversation.id, role="tool", tool_call_id="injected",
                           tool_name="get_project_overview", content=content)
        db.add(old)
        await db.flush()
        messages, _ = await context.build_messages(
            db, conversation, project_row, await db.get(AIConfig, config["id"]),
            fake_llm.factory("openai_compat", "test", None, 30),
        )
        text = json.dumps(messages, ensure_ascii=False)
        assert "作者风格秘密名称" not in text and "作者风格秘密描述" not in text
        assert old.content == content


@pytest.mark.parametrize("actor_name", ["editor", "manager"])
async def test_malformed_legacy_project_result_is_not_sent(app, client, make_client, fake_llm, actor_name):
    _, editor, manager, editor_client, manager_client, project, style, _, _ = await _world(app, client, make_client)
    actor, actor_client = (editor, editor_client) if actor_name == "editor" else (manager, manager_client)
    config = await create_ai_config(actor_client)
    malformed = json.dumps({
        "untrusted": True,
        "data": f'{{"default_style_id":{style["id"]},"default_style":{{"name":"作者风格秘密名称"',
        "warning": "内容过长",
    }, ensure_ascii=False)
    async with app.state.sessionmaker() as db:
        project_row = await db.get(Project, project["id"])
        conversation = AgentConversation(user_id=actor["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        db.add_all([
            AgentMessage(conversation_id=conversation.id, role="user", content="继续"),
            AgentMessage(conversation_id=conversation.id, role="assistant", content="", tool_calls=[{
                "id": "bad", "name": "get_project_overview", "arguments": "{}",
            }]),
        ])
        old = AgentMessage(conversation_id=conversation.id, role="tool", tool_call_id="bad",
                           tool_name="get_project_overview", content=malformed)
        db.add(old)
        await db.flush()
        messages, _ = await context.build_messages(
            db, conversation, project_row, await db.get(AIConfig, config["id"]),
            fake_llm.factory("openai_compat", "test", None, 30),
        )
        assert "作者风格秘密名称" not in json.dumps(messages, ensure_ascii=False)
        assert old.content == malformed


async def test_legacy_style_id_reused_by_actor_does_not_validate_old_foreign_details(
    app, client, make_client, fake_llm,
):
    _, _, manager, _, manager_client, project, style, _, _ = await _world(app, client, make_client)
    config = await create_ai_config(manager_client)
    async with app.state.sessionmaker() as db:
        project_row = await db.get(Project, project["id"])
        legacy = (await project_service.project_out(
            db, project_row, actor_user_id=manager["id"],
        )).model_dump(mode="json")
        assert legacy["default_style"]["name"] == "作者风格秘密名称"
        content = context.tool_result(legacy)
        # 用同 ID 替换风格，模拟 ID 复用；不得用新持有关系反向证明旧内容归属。
        await db.execute(update(WritingStyle).where(WritingStyle.id == style["id"]).values(
            user_id=manager["id"], name="新持有人风格", description="新持有人描述",
        ))
        conversation = AgentConversation(user_id=manager["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        db.add_all([
            AgentMessage(conversation_id=conversation.id, role="user", content="继续"),
            AgentMessage(conversation_id=conversation.id, role="assistant", content="", tool_calls=[{
                "id": "reused", "name": "get_project_overview", "arguments": "{}",
            }]),
        ])
        old = AgentMessage(conversation_id=conversation.id, role="tool", tool_call_id="reused",
                           tool_name="get_project_overview", content=content)
        db.add(old)
        await db.flush()
        messages, _ = await context.build_messages(
            db, conversation, project_row, await db.get(AIConfig, config["id"]),
            fake_llm.factory("openai_compat", "test", None, 30),
        )
        assert "作者风格秘密名称" not in json.dumps(messages, ensure_ascii=False)
        assert old.content == content


@pytest.mark.parametrize("actor_name", ["editor", "manager"])
async def test_history_preview_diff_hides_foreign_style_id_and_legacy_style_text(
    app, client, make_client, fake_llm, actor_name,
):
    _, editor, manager, editor_client, manager_client, project, style, _, _ = await _world(app, client, make_client)
    actor, actor_client = (editor, editor_client) if actor_name == "editor" else (manager, manager_client)
    config = await create_ai_config(actor_client)
    payload = {
        "diff": [
            {"field": f"projects.{project['id']}.default_style_id", "before": style["id"], "after": None},
            {"field": f"projects.{project['id']}.style", "before": "作者风格秘密描述", "after": ""},
        ],
        "safe": "正常内容",
    }
    content = context.tool_result(payload)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=actor["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        db.add_all([
            AgentMessage(conversation_id=conversation.id, role="user", content="继续"),
            AgentMessage(conversation_id=conversation.id, role="assistant", content="", tool_calls=[{
                "id": "diff", "name": "read", "arguments": "{}",
            }]),
        ])
        old = AgentMessage(conversation_id=conversation.id, role="tool", tool_call_id="diff",
                           tool_name="read", content=content)
        db.add(old)
        await db.flush()
        messages, _ = await context.build_messages(
            db, conversation, await db.get(Project, project["id"]), await db.get(AIConfig, config["id"]),
            fake_llm.factory("openai_compat", "test", None, 30),
        )
        item = next(message for message in messages if message.get("tool_call_id") == "diff")
        data = json.loads(item["content"])["data"]
        assert data["safe"] == "正常内容"
        assert data["diff"][0]["before"] is None and data["diff"][0]["after"] is None
        assert "作者风格秘密描述" not in json.dumps(messages, ensure_ascii=False)
        assert old.content == content


@pytest.mark.parametrize("actor_name", ["owner", "manager"])
async def test_legacy_assistant_tool_arguments_do_not_replay_foreign_style_id(
    app, client, make_client, fake_llm, actor_name,
):
    owner, _, manager, _, manager_client, project, style, _, _ = await _world(app, client, make_client)
    actor, actor_client = (owner, client) if actor_name == "owner" else (manager, manager_client)
    config = await create_ai_config(actor_client)
    arguments = json.dumps({"default_style_id": style["id"]}, ensure_ascii=False)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=actor["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        assistant = AgentMessage(conversation_id=conversation.id, role="assistant", content="", tool_calls=[{
            "id": "old-write", "name": "update_project", "arguments": arguments,
            "provider_metadata": {"signature": "keep-default_style-metadata"},
        }])
        db.add_all([
            AgentMessage(conversation_id=conversation.id, role="user", content="继续"), assistant,
            AgentMessage(conversation_id=conversation.id, role="tool", tool_call_id="old-write",
                         tool_name="update_project", content=context.tool_result({"error": "操作未完成"})),
        ])
        await db.flush()
        messages, _ = await context.build_messages(
            db, conversation, await db.get(Project, project["id"]), await db.get(AIConfig, config["id"]),
            fake_llm.factory("openai_compat", "test", None, 30),
        )
        output = next(message for message in messages if message.get("tool_calls"))
        expected_id = style["id"] if actor_name == "owner" else None
        assert json.loads(output["tool_calls"][0]["arguments"])["default_style_id"] == expected_id
        assert output["tool_calls"][0]["provider_metadata"]["signature"] == "keep-default_style-metadata"
        assert assistant.tool_calls[0]["arguments"] == arguments


async def test_legacy_unnamed_tool_note_cannot_replay_private_style(app, client, make_client, fake_llm):
    _, _, manager, _, manager_client, project, style, _, _ = await _world(app, client, make_client)
    config = await create_ai_config(manager_client)
    content = json.dumps({"default_style_id": style["id"], "default_style": {
        "id": style["id"], "name": "作者风格秘密名称", "description": "作者风格秘密描述", "is_preset": False,
    }}, ensure_ascii=False)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=manager["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        old = AgentMessage(conversation_id=conversation.id, role="tool", content=content)
        db.add_all([AgentMessage(conversation_id=conversation.id, role="user", content="继续"), old])
        await db.flush()
        messages, _ = await context.build_messages(
            db, conversation, await db.get(Project, project["id"]), await db.get(AIConfig, config["id"]),
            fake_llm.factory("openai_compat", "test", None, 30),
        )
        text = json.dumps(messages, ensure_ascii=False)
        assert "作者风格秘密名称" not in text and "作者风格秘密描述" not in text
        assert old.content == content


@pytest.mark.parametrize("actor_name", ["owner", "editor", "manager"])
@pytest.mark.parametrize("style_source", ["own", "preset"])
async def test_proven_own_or_global_legacy_style_remains_visible(
    app, client, make_client, fake_llm, actor_name, style_source,
):
    owner, editor, manager, editor_client, manager_client, project, owner_style, editor_style, preset_id = await _world(
        app, client, make_client,
    )
    actor, actor_client = {
        "owner": (owner, client), "editor": (editor, editor_client), "manager": (manager, manager_client),
    }[actor_name]
    own_style_id = owner_style["id"] if actor_name == "owner" else editor_style["id"]
    if actor_name == "manager":
        own_style_id = (await _style(manager_client, "管理员本人风格", "管理员本人描述"))["id"]
    style_id = own_style_id if style_source == "own" else preset_id
    config = await create_ai_config(actor_client)
    async with app.state.sessionmaker() as db:
        project_row = await db.get(Project, project["id"])
        project_row.default_style_id = style_id
        await db.flush()
        expected = (await project_service.project_out(
            db, project_row, actor_user_id=actor["id"],
        )).model_dump(mode="json")
        assert expected["default_style_id"] == style_id
        conversation = AgentConversation(user_id=actor["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        db.add_all([
            AgentMessage(conversation_id=conversation.id, role="user", content="继续"),
            AgentMessage(conversation_id=conversation.id, role="assistant", content="", tool_calls=[{
                "id": "good", "name": "get_project_overview", "arguments": "{}",
            }]),
        ])
        content = context.tool_result(expected)
        old = AgentMessage(conversation_id=conversation.id, role="tool", tool_call_id="good",
                           tool_name="get_project_overview", content=content)
        db.add(old)
        await db.flush()
        messages, _ = await context.build_messages(
            db, conversation, project_row, await db.get(AIConfig, config["id"]),
            fake_llm.factory("openai_compat", "test", None, 30),
        )
        item = next(message for message in messages if message.get("tool_call_id") == "good")
        data = json.loads(item["content"])["data"]
        assert (data["default_style_id"], data["default_style"]) == (style_id, expected["default_style"])
        assert old.content == content


@pytest.mark.parametrize("actor_name", ["owner", "editor", "manager"])
async def test_unproven_legacy_summary_allowed_only_for_owner(app, client, make_client, fake_llm, actor_name):
    owner, editor, manager, editor_client, manager_client, project, _, _, _ = await _world(app, client, make_client)
    actor, actor_client = {
        "owner": (owner, client), "editor": (editor, editor_client), "manager": (manager, manager_client),
    }[actor_name]
    config = await create_ai_config(actor_client)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=actor["id"], project_id=project["id"],
                                         summary="旧摘要包含风格秘密", summarized_until=None)
        db.add(conversation)
        await db.flush()
        db.add(AgentMessage(conversation_id=conversation.id, role="user", content="继续"))
        await db.flush()
        args = (db, conversation, await db.get(Project, project["id"]), await db.get(AIConfig, config["id"]),
                fake_llm.factory("openai_compat", "test", None, 30))
        if actor_name == "owner":
            messages, _ = await context.build_messages(*args)
            assert "旧摘要包含风格秘密" in json.dumps(messages, ensure_ascii=False)
        else:
            with pytest.raises(VisibilityPolicyError) as error:
                await context.build_messages(*args)
            assert error.value.extra["code"] == "agent_history_unverifiable"
        assert conversation.summary == "旧摘要包含风格秘密"


async def test_send_guard_revalidates_style_after_messages_were_built(app, client, fake_llm):
    execution, config_id = await _setup_guarded_execution(app, client)
    style = await _style(client, "作者原风格", "原描述")
    async with app.state.sessionmaker() as db:
        (await db.get(Project, execution.project_id)).default_style_id = style["id"]
        await db.commit()
    async with app.state.sessionmaker() as db:
        conversation = await db.get(AgentConversation, execution.conversation_id)
        project = await db.get(Project, execution.project_id)
        config = await db.get(AIConfig, config_id)
        # 与生产 handler 相同经受控工厂登记，guard 才会进入风格复验而非提前拒绝来源。
        provider = await _provider(app, config, fake_llm.factory)
        guarded = await context.guard_agent_provider(db, conversation, project, config, provider, execution=execution)
        await db.commit()
    async with app.state.sessionmaker() as db:
        (await db.get(WritingStyle, style["id"])).description = "更改后的描述"
        await db.commit()
    with pytest.raises(VisibilityPolicyError):
        _ = [item async for item in guarded.chat_tools_stream([{"role": "user", "content": "旧风格"}], "model", [])]
    assert fake_llm.tool_calls == []


async def test_summary_never_sends_unproven_project_tool_with_missing_recorded_name(app, client, fake_llm):
    project, _, url = await setup_agent(client)
    # 先停引擎，由测试按真实领取流程冻结执行身份，避免后台抢先运行本轮。
    await app.state.task_engine.stop()
    actor = decode_access_token(client.cookies[ACCESS_TOKEN_COOKIE], app.state.settings.jwt_secret)
    assert actor is not None
    async with app.state.sessionmaker() as db:
        conversation = await db.scalar(select(AgentConversation).where(AgentConversation.project_id == project["id"]))
        config = await db.scalar(select(AIConfig))
        config.context_window_k = 1
        config_id = config.id
        db.add_all([
            AgentMessage(conversation_id=conversation.id, role="assistant", content="", tool_calls=[{
                "id": "legacy-unknown", "name": "get_project_overview", "arguments": "{}",
            }]),
            AgentMessage(conversation_id=conversation.id, role="tool", tool_call_id="legacy-unknown",
                         tool_name=None, content=context.tool_result({"unexpected": "第三方私密风格描述"})),
        ])
        for index in range(3):
            db.add(AgentMessage(conversation_id=conversation.id, role="user", content=f"早期 {index}" + "背景" * 700))
        await db.commit()
    task_ref = await send_task(client, url, "本轮请求")
    execution = await _capture_execution(app, task_ref, actor)
    fake_llm.push("安全摘要")
    async with app.state.sessionmaker() as db:
        config = await db.get(AIConfig, config_id)
        await context.build_messages(
            db, await db.get(AgentConversation, execution.conversation_id),
            await db.get(Project, execution.project_id), config,
            await _provider(app, config, fake_llm.factory), task_id=execution.task_id, execution=execution,
        )
    assert len(fake_llm.calls) == 1
    sent = json.dumps(fake_llm.calls[0]["messages"], ensure_ascii=False)
    assert "legacy-unknown" in sent and "第三方私密风格描述" not in sent


async def test_overview_rechecks_style_owner_and_values_at_final_projection(app, client, make_client, monkeypatch):
    _, _, manager, _, _, project, old_style, _, _ = await _world(app, client, make_client)
    original = project_service.project_out

    async def replace_style_identity(db, row, **kwargs):
        out = await original(db, row, **kwargs)
        # 同 ID 的风格归属与内容在中途改变；不得把旧归属下的名称/描述交给新持有人。
        await db.execute(update(WritingStyle).where(WritingStyle.id == old_style["id"]).values(
            user_id=manager["id"], name="管理员新风格", description="管理员新描述",
        ))
        return out

    monkeypatch.setattr(project_service, "project_out", replace_style_identity)
    async with app.state.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        result = await get_tool("get_project_overview").run(
            ToolCtx(db, row, manager["id"], app.state.task_engine.ctx), {},
        )
        assert result["default_style_id"] == old_style["id"]
        assert result["default_style"]["name"] == "管理员新风格"
        assert "作者风格秘密" not in json.dumps(result, ensure_ascii=False)
        await db.rollback()
