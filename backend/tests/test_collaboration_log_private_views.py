"""生成记录和 Agent 对外快照的真实 actor 隔离。"""

import json
from copy import deepcopy

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models import (
    AgentChange,
    AgentConversation,
    Chapter,
    ChapterVersion,
    GenerationLog,
    McpPlugin,
    Project,
    Skill,
    Task,
    User,
    WritingStyle,
)
from app.services import generation_log_service, personal_projection
from app.services.agent import effects, service
from tests.helpers import Gate, create_ai_config, create_project, parse_sse, register, stream_with_gate
from tests.test_agent_runtime import call, change_action, send_task, setup_agent


async def _invite(owner, invited, base, username, role="viewer"):
    response = await owner.post(f"{base}/collaborators", json={"username": username, "role": role})
    assert response.status_code == 201, response.text
    invite = response.json()
    result = await invited.post(
        f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/accept",
        json={"expected_revision": invite["revision"]},
    )
    assert result.status_code == 200, result.text
    return result.json()


async def test_chapter_logs_require_personal_current_membership_and_redact_private_references(
    app, client, make_client, system_admin,
):
    owner = await register(client, "log-owner")
    viewer, editor, pending, stranger = (make_client() for _ in range(4))
    viewer_user = await register(viewer, "log-viewer")
    editor_user = await register(editor, "log-editor")
    await register(pending, "log-pending")
    await register(stranger, "log-stranger")
    owner_cfg = await create_ai_config(client)
    editor_cfg = await create_ai_config(editor)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "日志章节"})).json()
    other_project = await create_project(client, title="其他作品")
    own_member = await _invite(client, viewer, base, "log-viewer")
    await _invite(client, editor, base, "log-editor", "editor")
    admin_member = await _invite(client, system_admin, base, "settings_admin")
    await client.post(f"{base}/collaborators", json={"username": "log-pending", "role": "viewer"})

    async with app.state.sessionmaker() as db:
        admin_user = (await system_admin.get("/api/v1/auth/me")).json()
        owner_task = Task(user_id=owner["id"], project_id=project["id"], chapter_id=chapter["id"],
                          kind="chapter.generate", status="succeeded")
        editor_task = Task(user_id=editor_user["id"], project_id=project["id"], chapter_id=chapter["id"],
                           kind="chapter.generate", status="succeeded")
        other_project_task = Task(user_id=viewer_user["id"], project_id=other_project["id"],
                                  kind="chapter.generate", status="succeeded")
        db.add_all([owner_task, editor_task, other_project_task])
        await db.flush()
        owner_log = GenerationLog(project_id=project["id"], chapter_id=chapter["id"], actor_user_id=owner["id"],
                                  task_id=owner_task.id, ai_config_id=owner_cfg["id"],
                                  kind="generate", status="success")
        db.add(owner_log)
        await db.flush()
        viewer_log = GenerationLog(
            project_id=project["id"], chapter_id=chapter["id"], actor_user_id=viewer_user["id"],
            task_id=owner_task.id, ai_config_id=owner_cfg["id"], kind="rewrite", status="success",
            prompt_tokens=23, completion_tokens=None,
            params={"nested": {"ai_config_id": owner_cfg["id"], "task_id": other_project_task.id,
                               "generation_log_id": owner_log.id},
                    "diff": [{"field": "ai_config_id", "before": owner_cfg["id"], "after": owner_cfg["id"]}]},
            messages=[{"role": "assistant", "content": "已有正文"}],
        )
        editor_log = GenerationLog(project_id=project["id"], chapter_id=chapter["id"],
                                   actor_user_id=editor_user["id"], task_id=editor_task.id,
                                   ai_config_id=editor_cfg["id"], kind="generate", status="success",
                                   # 0048 起编辑者本人模型须有持久来源证明；无证明的旧日志对非作者隐藏配置。
                                   model_source="actor", model_config_owner_id=editor_user["id"])
        admin_log = GenerationLog(project_id=project["id"], chapter_id=chapter["id"],
                                  actor_user_id=admin_user["id"], kind="generate", status="success")
        db.add_all([viewer_log, editor_log, admin_log])
        await db.commit()
        ids = {"owner": owner_log.id, "viewer": viewer_log.id, "editor": editor_log.id, "admin": admin_log.id,
               "editor_task": editor_task.id}

    url = f"{base}/chapters/{chapter['id']}/generation-logs"
    for actor, label in ((client, "owner"), (viewer, "viewer"), (editor, "editor"), (system_admin, "admin")):
        response = await actor.get(url)
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()] == [ids[label]]
        for other in ("owner", "viewer", "editor", "admin"):
            if other != label:
                assert (await actor.get(f"{url}/{ids[other]}")).status_code == 404
    for actor in (pending, stranger):
        assert (await actor.get(url)).status_code == 404
        assert (await actor.get(f"{url}/{ids['viewer']}")).status_code == 404

    detail = (await viewer.get(f"{url}/{ids['viewer']}")).json()
    assert detail["ai_config_id"] is None and detail["task_id"] is None
    assert detail["params"]["nested"] == {"ai_config_id": None, "task_id": None, "generation_log_id": None}
    assert detail["params"]["diff"][0]["before"] is None
    assert detail["prompt_tokens"] == 23 and detail["completion_tokens"] is None
    detail = (await editor.get(f"{url}/{ids['editor']}")).json()
    assert detail["ai_config_id"] == editor_cfg["id"] and detail["task_id"] == ids["editor_task"]

    assert (await client.delete(f"{base}/collaborators/{viewer_user['id']}",
                                params={"expected_revision": own_member["revision"]})).status_code == 204
    assert (await viewer.get(url)).status_code == 404
    assert (await client.delete(f"{base}/collaborators/{admin_user['id']}",
                                params={"expected_revision": admin_member["revision"]})).status_code == 204
    assert (await system_admin.get(base)).status_code == 200
    assert (await system_admin.get(url)).status_code == 404

    async with app.state.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        row.title = "待写标题"
        statements = []

        def record(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(app.state.engine.sync_engine, "before_cursor_execute", record)
        try:
            with pytest.raises(NotFoundError):
                await generation_log_service.list_logs(db, viewer_user["id"], project["id"], chapter["id"])
        finally:
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", record)
        assert not any(statement.lstrip().upper().startswith("UPDATE PROJECTS") for statement in statements)
        await db.rollback()
        assert (await db.get(Project, project["id"])).title != "待写标题"


async def test_agent_effects_projection_preserves_internal_snapshot_and_undo(app, client, make_client):
    owner = await register(client, "effect-owner")
    collaborator = make_client()
    collab = await register(collaborator, "effect-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await _invite(client, collaborator, base, "effect-editor", "editor")
    owner_cfg = await create_ai_config(client)
    collab_cfg = await create_ai_config(collaborator)
    chapter = (await client.post(f"{base}/chapters", json={"title": "将删除空章"})).json()
    conversation = (await client.post(f"{base}/agent/conversations", json={})).json()
    url = f"{base}/agent/conversations/{conversation['id']}"

    async with app.state.sessionmaker() as db:
        own_task = Task(user_id=owner["id"], project_id=project["id"], chapter_id=chapter["id"],
                        kind="chapter.generate", status="succeeded")
        other_task = Task(user_id=collab["id"], project_id=project["id"], chapter_id=chapter["id"],
                          kind="chapter.generate", status="succeeded")
        db.add_all([own_task, other_task])
        await db.flush()
        own_log = GenerationLog(project_id=project["id"], chapter_id=chapter["id"], actor_user_id=owner["id"],
                                kind="generate", status="success")
        other_log = GenerationLog(project_id=project["id"], chapter_id=chapter["id"], actor_user_id=collab["id"],
                                  kind="generate", status="success")
        db.add_all([own_log, other_log])
        await db.flush()
        version = ChapterVersion(chapter_id=chapter["id"], seq=1, source="manual", content="历史", word_count=2,
                                 generation_log_id=other_log.id)
        db.add(version)
        await db.commit()
        before = await effects.snapshot(db, project["id"])
        await db.delete(await db.get(Chapter, chapter["id"]))
        await db.flush()
        captured = effects.diff(before, await effects.snapshot(db, project["id"]))
        change = AgentChange(
            conversation_id=conversation["id"], project_id=project["id"], tool_call_id="del-1",
            tool_name="delete_chapter", arguments={"task_id": other_task.id}, kind="small", status="executed",
            preview={"diff": [
                {"field": "ai_config_id", "before": collab_cfg["id"], "after": owner_cfg["id"]},
                {"field": f"tasks.{other_task.id}.chapter_id", "before": chapter["id"], "after": None},
                {"field": f"chapter_versions.{version.id}.generation_log_id", "before": other_log.id, "after": None},
            ]},
            result={"task": {"id": other_task.id, "output": "私人结果"},
                    "nested": {"generation_log_id": other_log.id, "ai_config_id": collab_cfg["id"]}},
            effects=captured,
        )
        db.add(change)
        await db.commit()
        change_id, version_id = change.id, version.id
        original = deepcopy(change.effects)
        own_task_id, other_task_id = own_task.id, other_task.id
        own_log_id, other_log_id = own_log.id, other_log.id

    response = await client.get(url)
    assert response.status_code == 200, response.text
    view = response.json()["changes"][0]
    assert view["arguments"]["task_id"] is None
    assert view["preview"]["diff"][0]["before"] is None
    assert view["preview"]["diff"][0]["after"] == owner_cfg["id"]
    assert len(view["preview"]["diff"]) == 2 and view["preview"]["diff"][1]["before"] is None
    assert view["result"]["task"] is None and view["result"]["nested"] == {
        "generation_log_id": None, "ai_config_id": None,
    }
    for table, visible_id, hidden_id in (("tasks", own_task_id, other_task_id),
                                          ("generation_logs", own_log_id, other_log_id)):
        ids = {effect["pk"]["id"] for effect in view["effects"] if effect["table"] == table}
        assert visible_id in ids and hidden_id not in ids
    version_effect = next(effect for effect in view["effects"] if effect["table"] == "chapter_versions")
    assert version_effect["before"]["generation_log_id"] is None
    async with app.state.sessionmaker() as db:
        row = await db.get(AgentChange, change_id)
        assert row.effects == original
        public = await service.public_changes(db, owner["id"], project["id"], [row])
        assert public[0].model_dump(mode="json") == view
        assert row.effects == original

    undone = await client.post(f"{base}/agent/changes/{change_id}/undo", json=change_action(view))
    assert undone.status_code == 200, undone.text
    assert all(effect.get("pk", {}).get("id") != other_task_id
               for effect in undone.json()["effects"] if effect["table"] == "tasks")
    async with app.state.sessionmaker() as db:
        restored = await db.get(ChapterVersion, version_id)
        assert restored is not None and restored.generation_log_id == other_log_id
        assert (await db.get(Task, other_task_id)).chapter_id == chapter["id"]
        assert (await db.get(GenerationLog, other_log_id)).chapter_id == chapter["id"]
        row = await db.get(AgentChange, change_id)
        assert row.effects == original


async def test_agent_project_effect_hides_legacy_owner_style_from_other_actors(
    app, client, make_client, system_admin,
):
    owner = await register(client, "legacy-style-owner")
    viewer, editor = make_client(), make_client()
    viewer_user = await register(viewer, "legacy-style-viewer")
    editor_user = await register(editor, "legacy-style-editor")
    admin_user = (await system_admin.get("/api/v1/auth/me")).json()
    project = await create_project(client)
    project_id = project["id"]
    base = f"/api/v1/projects/{project_id}"
    await _invite(client, viewer, base, "legacy-style-viewer")
    await _invite(client, editor, base, "legacy-style-editor", "editor")
    await _invite(client, system_admin, base, "settings_admin")
    secret = "历史迁移保留的作者私有写作风格"

    async with app.state.sessionmaker() as db:
        project_row = await db.get(Project, project_id)
        project_row.style = secret  # 0013 迁移保留旧列和原文。
        await db.commit()
        before = await effects.snapshot(db, project_id)
        project_row.title = "作品新标题"
        captured = effects.diff(before, await effects.snapshot(db, project_id))
        assert captured[0]["table"] == "projects" and captured[0]["before"]["style"] == secret
        conversations = [AgentConversation(project_id=project_id, user_id=actor["id"])
                         for actor in (owner, viewer_user, editor_user, admin_user)]
        db.add_all(conversations)
        await db.flush()
        changes = [AgentChange(
            conversation_id=conversation.id, project_id=project_id, tool_call_id=f"legacy-{index}",
            tool_name="update_project", kind="small", status="executed", arguments={},
            preview={"diff": [
                {"field": f"projects.{project_id}.style", "before": secret, "after": ""},
                {"field": f"projects.{project_id + 1}.style", "before": secret, "after": ""},
            ]},
            result={"ordinary": {"style": "普通正文同名字段"}, "empty": {
                "table": "projects", "before": {"style": ""}, "after": {"style": None},
            }, "unproven": {"table": "projects", "pk": {"id": project_id + 1},
                             "before": {"style": secret}},
                "nested": json.dumps({"effect": captured[0]}, ensure_ascii=False)},
            effects=deepcopy(captured),
        ) for index, conversation in enumerate(conversations)]
        db.add_all(changes)
        await db.commit()
        original = deepcopy(changes[0].effects)
        for actor, change in zip((owner, viewer_user, editor_user, admin_user), changes, strict=True):
            public = (await service.public_changes(db, actor["id"], project_id, [change]))[0]
            visible_effect = public.effects[0]
            nested = json.loads(public.result["nested"])["effect"]
            assert public.result["ordinary"]["style"] == "普通正文同名字段"
            assert public.result["unproven"]["before"]["style"] is None
            assert public.result["empty"]["before"]["style"] == ""
            assert public.result["empty"]["after"]["style"] is None
            if actor == owner:
                assert visible_effect["before"]["style"] == secret
                assert nested["after"]["style"] == secret
                assert len(public.preview["diff"]) == 1 and public.preview["diff"][0]["before"] == secret
            else:
                assert visible_effect["before"]["style"] is None
                assert visible_effect["after"]["style"] is None
                assert nested["before"]["style"] is None
                assert public.preview["diff"] == []
            assert change.effects == original


@pytest.mark.parametrize("change_kind", ["token", "conversation", "proposal_reused"])
async def test_decision_rechecks_actor_conversation_and_proposal_after_resume(
    app, client, make_client, monkeypatch, change_kind,
):
    owner = await register(client, f"decision-owner-{change_kind}")
    other_client = make_client()
    other = await register(other_client, f"decision-other-{change_kind}")
    project = await create_project(client)
    project_id = project["id"]
    base = f"/api/v1/projects/{project_id}/agent"
    conversation = (await client.post(f"{base}/conversations", json={})).json()
    async with app.state.sessionmaker() as db:
        change = AgentChange(conversation_id=conversation["id"], project_id=project_id,
                             tool_call_id="original", tool_name="test", kind="small", status="rejected",
                             result={"message": "旧提案私人结果"})
        db.add(change)
        await db.commit()
        change_id = change.id
    view = (await client.get(f"{base}/conversations/{conversation['id']}")).json()["changes"][0]
    identity = {"change_key": view["change_key"], "view_version": view["view_version"]}

    async def change_during_resume(_db, _engine, _user_id, _task_id, **_kwargs):
        # SQLite 的写锁会阻塞第二连接；释放已完成的读事务，模拟 PostgreSQL 可并发提交的窗口。
        await _db.rollback()
        async with app.state.sessionmaker() as writer:
            if change_kind == "token":
                user = await writer.get(User, owner["id"])
                user.token_version += 1
            elif change_kind == "conversation":
                row = await writer.get(AgentConversation, conversation["id"])
                row.user_id = other["id"]
            else:
                row = await writer.get(AgentChange, change_id)
                await writer.delete(row)
                await writer.flush()
                writer.add(AgentChange(id=change_id, conversation_id=conversation["id"], project_id=project_id,
                                       tool_call_id="replacement", tool_name="test", kind="small",
                                       status="rejected", result={"message": "新提案"}))
            await writer.commit()

    monkeypatch.setattr(service, "maybe_resume", change_during_resume)
    response = await client.post(f"{base}/changes/{change_id}/reject", json=identity)
    assert response.status_code == (401 if change_kind == "token" else 404), response.text
    assert "旧提案私人结果" not in response.text


@pytest.mark.parametrize("change_kind", ["token", "conversation", "proposal_reused", "task_reused"])
async def test_turn_decision_rechecks_every_result_after_resume(app, client, make_client, monkeypatch, change_kind):
    owner = await register(client, f"turn-owner-{change_kind}")
    other_client = make_client()
    other = await register(other_client, f"turn-other-{change_kind}")
    project = await create_project(client)
    project_id = project["id"]
    base = f"/api/v1/projects/{project_id}/agent"
    conversation = (await client.post(f"{base}/conversations", json={})).json()
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner["id"], project_id=project_id, kind="agent.turn", status="paused",
                    input={"request": {"conversation_id": conversation["id"]}})
        db.add(task)
        await db.flush()
        change = AgentChange(conversation_id=conversation["id"], project_id=project_id, turn_task_id=task.id,
                             turn_task_key=task.statistics_key,
                             tool_call_id="same-tool-call", tool_name="test", kind="small", status="proposed",
                             arguments={"text": "旧提案私人内容"})
        db.add(change)
        await db.commit()
        change_id, task_id, task_key = change.id, task.id, task.statistics_key
    view = (await client.get(f"{base}/conversations/{conversation['id']}")).json()["changes"][0]
    identity = {"task_key": task_key, "changes": [{"id": change_id, "change_key": view["change_key"],
                                                  "view_version": view["view_version"]}]}

    async def change_during_resume(_db, _engine, _user_id, _task_id, **_kwargs):
        await _db.rollback()  # SQLite 串行写入，模拟另一个 PostgreSQL 会话在续跑时提交。
        async with app.state.sessionmaker() as writer:
            if change_kind == "token":
                user = await writer.get(User, owner["id"])
                user.token_version += 1
            elif change_kind == "conversation":
                row = await writer.get(AgentConversation, conversation["id"])
                row.user_id = other["id"]
            elif change_kind == "proposal_reused":
                row = await writer.get(AgentChange, change_id)
                created_at = row.created_at
                await writer.delete(row)
                await writer.flush()
                writer.add(AgentChange(id=change_id, conversation_id=conversation["id"], project_id=project_id,
                                       turn_task_id=task_id, tool_call_id="same-tool-call", tool_name="test",
                                       created_at=created_at, kind="small", status="rejected",
                                       arguments={"text": "替代提案"}))
            else:
                row = await writer.get(Task, task_id)
                await writer.delete(row)
                await writer.flush()
                writer.add(Task(id=task_id, user_id=owner["id"], project_id=project_id, kind="agent.turn",
                                status="succeeded", input={"request": {"conversation_id": conversation["id"]}}))
            await writer.commit()

    monkeypatch.setattr(service, "maybe_resume", change_during_resume)
    response = await client.post(f"{base}/turns/{task_id}/reject-all", json=identity)
    assert response.status_code == (401 if change_kind == "token" else 404), response.text
    assert "旧提案私人内容" not in response.text


async def test_decision_does_not_adopt_reused_proposal_between_commit_and_refresh(app, client, monkeypatch):
    owner = await register(client, "decision-commit-reuse")
    project = await create_project(client)
    project_id = project["id"]
    base = f"/api/v1/projects/{project_id}/agent"
    conversation = (await client.post(f"{base}/conversations", json={})).json()
    async with app.state.sessionmaker() as db:
        task = Task(user_id=owner["id"], project_id=project_id, kind="agent.turn", status="paused")
        db.add(task)
        await db.flush()
        change = AgentChange(conversation_id=conversation["id"], project_id=project_id, turn_task_id=task.id,
                             turn_task_key=task.statistics_key,
                             tool_call_id="same-call", tool_name="test", kind="small", status="proposed",
                             arguments={"text": "原提案"})
        db.add(change)
        await db.commit()
        change_id, task_id = change.id, task.id
    view = (await client.get(f"{base}/conversations/{conversation['id']}")).json()["changes"][0]
    identity = {"change_key": view["change_key"], "view_version": view["view_version"]}

    refresh = AsyncSession.refresh
    replaced = False

    async def replace_before_refresh(db, instance, *args, **kwargs):
        nonlocal replaced
        if isinstance(instance, AgentChange) and instance.id == change_id and instance.status == "rejected" \
                and not db.in_transaction() and not replaced:
            replaced = True
            async with app.state.sessionmaker() as writer:
                row = await writer.get(AgentChange, change_id)
                created_at = row.created_at
                await writer.delete(row)
                await writer.flush()
                writer.add(AgentChange(id=change_id, conversation_id=conversation["id"], project_id=project_id,
                                       turn_task_id=task_id, tool_call_id="same-call", tool_name="test",
                                       kind="small", status="rejected", created_at=created_at,
                                       arguments={"text": "替代提案"}))
                await writer.commit()
        return await refresh(db, instance, *args, **kwargs)

    async def skip_resume(*_args, **_kwargs):
        return None

    monkeypatch.setattr(AsyncSession, "refresh", replace_before_refresh)
    monkeypatch.setattr(service, "maybe_resume", skip_resume)
    response = await client.post(f"{base}/changes/{change_id}/reject", json=identity)
    assert replaced and response.status_code == 404, response.text
    assert "替代提案" not in response.text


async def test_only_own_historical_agent_logs_are_open_to_current_viewer(app, client, make_client, fake_llm):
    owner = await register(client, "agent-log-owner")
    viewer = make_client()
    viewer_user = await register(viewer, "agent-log-viewer")
    project = await create_project(client)
    config = await create_ai_config(client)
    base = f"/api/v1/projects/{project['id']}"
    membership = await _invite(client, viewer, base, "agent-log-viewer")
    async with app.state.sessionmaker() as db:
        own = AgentConversation(user_id=viewer_user["id"], project_id=project["id"])
        other = AgentConversation(user_id=owner["id"], project_id=project["id"])
        db.add_all([own, other])
        await db.flush()
        own_id, other_id = own.id, other.id
        logs = [
            GenerationLog(
                project_id=project["id"], actor_user_id=viewer_user["id"], kind="agent.turn", status="success",
                ai_config_id=config["id"],
                params={"conversation_id": own_id, "_project_model": {"config_id": config["id"]},
                        "_collaboration_grant": {"invitation_id": "private", "revision": 9}},
                messages=[
                    {"role": "assistant", "content": "回答", "tool_calls": [{
                        "id": "call", "name": "start_task",
                        "arguments": json.dumps({"ai_config_id": config["id"]}),
                        "provider_metadata": {"thinking": "不应公开"},
                    }]},
                    {"role": "tool", "content": json.dumps({
                        "_project_model": {"config_id": config["id"]},
                        "nested": {"_collaboration_grant": {"invitation_id": "private"}},
                    })},
                ],
            ),
            GenerationLog(project_id=project["id"], actor_user_id=owner["id"], kind="agent.turn", status="success",
                          params={"conversation_id": own_id}, messages=[{"role": "user", "content": "作者私聊"}]),
            GenerationLog(project_id=project["id"], actor_user_id=owner["id"], kind="agent.turn", status="success",
                          params={"conversation_id": other_id}),
        ]
        db.add_all(logs)
        await db.commit()
        log_id = logs[0].id
    path = f"{base}/agent/conversations/{own_id}"
    response = await viewer.get(f"{path}/generation-logs")
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == [log_id]
    log = response.json()[0]
    assert log["ai_config_id"] is None
    assert "_project_model" not in log["params"] and "_collaboration_grant" not in log["params"]
    call = log["messages"][0]["tool_calls"][0]
    assert json.loads(call["arguments"])["ai_config_id"] is None
    assert "provider_metadata" not in call and "作者私聊" not in response.text
    assert json.loads(log["messages"][1]["content"]) == {"nested": {}}
    assert (await viewer.get(f"{base}/agent/conversations/{other_id}/generation-logs")).status_code == 404
    # E4i-d（裁决 4）：viewer 可只读查看本人会话详情，但不能发送（本人会话上的授权失败为 409，裁决 14）。
    detail = await viewer.get(path)
    assert detail.status_code == 200, detail.text
    assert "作者私聊" not in detail.text
    sent = await viewer.post(f"{path}/messages", json={"content": "不能触发 AI"})
    assert sent.status_code == 409 and sent.json()["code"] == "permission_denied", sent.text
    assert fake_llm.calls == []
    assert (await client.delete(f"{base}/collaborators/{viewer_user['id']}",
                                params={"expected_revision": membership["revision"]})).status_code == 204
    assert (await viewer.get(f"{path}/generation-logs")).status_code == 404
    assert (await viewer.get(path)).status_code == 404


async def test_agent_executed_sse_uses_public_projection(app, client, make_client, fake_llm, monkeypatch):
    project, base, url = await setup_agent(client, auto=True)
    owner = (await client.get("/api/v1/auth/me")).json()
    other_client = make_client()
    other = await register(other_client, "stream-other")
    await _invite(client, other_client, f"/api/v1/projects/{project['id']}", "stream-other", "editor")
    async with app.state.sessionmaker() as db:
        task = Task(user_id=other["id"], project_id=project["id"], kind="chapter.generate", status="succeeded")
        own_task = Task(user_id=owner["id"], project_id=project["id"], kind="chapter.generate", status="succeeded")
        log = GenerationLog(project_id=project["id"], actor_user_id=other["id"], kind="generate", status="success")
        own_log = GenerationLog(project_id=project["id"], actor_user_id=owner["id"], kind="generate", status="success")
        db.add_all([task, log, own_task, own_log])
        await db.commit()
        identities = [
            ("tasks", task.id, {"actor_user_id": other["id"], "statistics_key": task.statistics_key}),
            ("tasks", own_task.id, {"actor_user_id": owner["id"], "statistics_key": own_task.statistics_key}),
            ("generation_logs", log.id, {"actor_user_id": other["id"], "log_instance_key": log.log_instance_key}),
            ("generation_logs", own_log.id,
             {"actor_user_id": owner["id"], "log_instance_key": own_log.log_instance_key}),
        ]

    # 模拟批量变更的真实快照形状；验证实时 SSE 也走与详情/撤销相同的公共投影。
    original_diff = effects.diff

    def with_private_links(before, after):
        return [*original_diff(before, after), *[
            {"table": table, "pk": {"id": item_id}, "before": {}, "after": {}, "format_version": 6,
             "_private_identity": identity}
            for table, item_id, identity in identities
        ]]

    monkeypatch.setattr(effects, "diff", with_private_links)
    gate = Gate()
    fake_llm.push_tools([gate, call("create_character", {"name": "流式角色"})])
    fake_llm.push_tools([("text", {"text": "完成"})])
    turn = await send_task(client, url)
    response = await stream_with_gate(app, client, turn, gate)
    frames = parse_sse(response.text)
    executed = [payload["change"] for event, payload in frames if event == "executed"]
    assert len(executed) == 1
    for table in ("tasks", "generation_logs"):
        shown = [item for item in executed[0]["effects"] if item["table"] == table]
        assert [item["pk"]["id"] for item in shown] == [next(
            item_id for kind, item_id, identity in identities
            if kind == table and identity["actor_user_id"] == owner["id"]
        )]
        assert "_private_identity" not in shown[0]
    detail = await client.get(url)
    assert detail.status_code == 200
    assert detail.json()["changes"][0]["effects"] == executed[0]["effects"]
    async with app.state.sessionmaker() as db:
        change = await db.get(AgentChange, executed[0]["id"])
        assert {"tasks", "generation_logs"} <= {item["table"] for item in change.effects}


async def test_nested_private_resources_are_filtered_without_mutating_payload(app, client, make_client):
    owner = await register(client, "projection-owner")
    viewer = make_client()
    user = await register(viewer, "projection-viewer")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    await _invite(client, viewer, base, "projection-viewer")
    async with app.state.sessionmaker() as db:
        owner_skill = Skill(owner_user_id=owner["id"], name="作者私人技能", slug="private-owner")
        viewer_skill = Skill(owner_user_id=user["id"], name="自己的技能", slug="private-viewer")
        owner_plugin = McpPlugin(user_id=owner["id"], slug="owner-plugin", name="作者插件",
                                 transport="sse", url="https://example.test/owner")
        viewer_plugin = McpPlugin(user_id=user["id"], slug="viewer-plugin", name="自己的插件",
                                  transport="sse", url="https://example.test/viewer")
        db.add_all([owner_skill, viewer_skill, owner_plugin, viewer_plugin])
        await db.flush()
        global_skill = await db.scalar(select(Skill.id).where(Skill.owner_user_id.is_(None)).limit(1))
        preset = await db.scalar(select(WritingStyle.id).where(WritingStyle.user_id.is_(None)).limit(1))
        # 对外投影只展示独立会话可证实的已提交资源。
        await db.commit()
        payload = {
            "style": {"id": project["default_style_id"], "name": "作者私人风格"},
            "style_id": preset,
            "skill_id": owner_skill.id,
            "skill": {"id": global_skill, "name": "全局技能"},
            "skill_ids": [owner_skill.id, viewer_skill.id, global_skill],
            "mcp_plugin_ids": [owner_plugin.id, viewer_plugin.id],
            "_shared_embedding": {"model": {"config_id": 123, "config_fingerprint": "private-fingerprint"}},
            "events": [{"_shared_embedding": {"grant_identity": {"invitation_id": "private-grant"}},
                        "_external_claim_id": "private-claim", "_external_task_key": "private-task-key",
                        "_external_outcome_unknown": {"claim": "private-outcome"},
                        "_chapter_claim": {"claim_key": "private-run"},
                        "_batch_completion": {"execution_key": "private-execution"},
                        "_batch_attempt": {"attempt": "private-attempt"},
                        "_analysis_completion": {"attempt": "private-analysis-attempt"},
                        "_wizard_resume": {"source_key": "private-wizard-task"},
                        "_admin_chapter_cancel": {"claim": "private-admin-claim"},
                        "_admin_chapter_receipt": {"claim": "private-admin-receipt"},
                        "generation_claim_key": "private-run"},
                       {"field": "chapters.42.generation_claim_task_key", "before": "private-task"}],
            "text": json.dumps({"_project_model": {"config_id": 123}, "task_id": 999,
                                "_collaboration_grant": {"invitation_id": "private"},
                                "nested": {"_shared_embedding": {"config_fingerprint": "private-fingerprint"},
                                           "_external_claim_id": "private-claim", "_external_task_key": "task-key",
                                           "_external_outcome_unknown": {"claim": "private-outcome"},
                                           "_chapter_claim": {"claim_key": "private-run"},
                                           "_batch_completion": {"execution_key": "private-execution"},
                                           "_batch_attempt": {"attempt": "private-attempt"},
                                           "_analysis_completion": {"attempt": "private-analysis-attempt"},
                                           "_wizard_resume": {"source_key": "private-wizard-task"},
                                           "_admin_chapter_cancel": {"claim": "private-admin-claim"},
                                           "_admin_chapter_receipt": {"claim": "private-admin-receipt"}}}),
        }
        old = deepcopy(payload)
        projected = await personal_projection.project_payload(db, user["id"], project["id"], payload)
        assert projected["style"] is None and projected["style_id"] == preset
        assert projected["skill_id"] is None and projected["skill"]["id"] == global_skill
        assert projected["skill_ids"] == [viewer_skill.id, global_skill]
        assert projected["mcp_plugin_ids"] == [viewer_plugin.id]
        assert "_shared_embedding" not in projected and projected["events"] == [{}]
        assert json.loads(projected["text"]) == {"task_id": None, "nested": {}}
        assert payload == old


async def test_log_rechecks_membership_after_projection_started(app, client, make_client, monkeypatch):
    """响应仍未返回时撤权，不能因此前的 accepted 快照继续返回本人日志。"""
    await register(client, "projection-revoke-owner")
    viewer = make_client()
    user = await register(viewer, "projection-revoke-viewer")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "章节"})).json()
    member = await _invite(client, viewer, base, "projection-revoke-viewer")
    async with app.state.sessionmaker() as db:
        log = GenerationLog(project_id=project["id"], chapter_id=chapter["id"], actor_user_id=user["id"],
                            kind="generate", status="success", messages=[{"content": "只应本人看到"}])
        db.add(log)
        await db.commit()
        log_id = log.id

    original = personal_projection.require_personal_project
    calls = 0

    async def revoke_after_projection_auth(db, actor_id, project_id):
        nonlocal calls
        result = await original(db, actor_id, project_id)
        if actor_id == user["id"]:
            calls += 1
            if calls == 2:
                response = await client.delete(f"{base}/collaborators/{user['id']}",
                                               params={"expected_revision": member["revision"]})
                assert response.status_code == 204, response.text
        return result

    monkeypatch.setattr(personal_projection, "require_personal_project", revoke_after_projection_auth)
    response = await viewer.get(f"{base}/chapters/{chapter['id']}/generation-logs/{log_id}")
    assert response.status_code == 404
    assert "只应本人看到" not in response.text


async def test_log_rechecks_active_task_reference_after_projection_query(app, client, make_client, monkeypatch):
    """任务引用查出后降级，accepted viewer 不应继续收到活动任务 ID。"""
    await register(client, "projection-downgrade-owner")
    editor = make_client()
    user = await register(editor, "projection-downgrade-editor")
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "章节"})).json()
    member = await _invite(client, editor, base, "projection-downgrade-editor", "editor")
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user["id"], project_id=project["id"], chapter_id=chapter["id"],
                    kind="chapter.generate", status="running", input={"_collaboration_grant": {
                        "invitation_id": member["invitation_id"], "revision": member["revision"],
                    }})
        db.add(task)
        await db.flush()
        log = GenerationLog(project_id=project["id"], chapter_id=chapter["id"], actor_user_id=user["id"],
                            kind="generate", status="success", task_id=task.id, params={"task_id": task.id})
        db.add(log)
        await db.commit()
        log_id, task_id = log.id, task.id

    original = AsyncSession.execute
    changed = False

    async def downgrade_after_task_query(db, statement, *args, **kwargs):
        nonlocal changed
        result = await original(db, statement, *args, **kwargs)
        if not changed and "SELECT tasks.id, tasks.statistics_key" in str(statement):
            changed = True
            response = await client.patch(f"{base}/collaborators/{user['id']}", json={
                "expected_revision": member["revision"], "role": "viewer",
            })
            assert response.status_code == 200, response.text
        return result

    monkeypatch.setattr(AsyncSession, "execute", downgrade_after_task_query)
    response = await editor.get(f"{base}/chapters/{chapter['id']}/generation-logs/{log_id}")
    assert changed and response.status_code == 200, response.text
    assert task_id is not None and response.json()["task_id"] is None
    assert response.json()["params"]["task_id"] is None


async def test_old_effects_cannot_confuse_reused_ids_with_current_own_rows(app, client, make_client):
    owner = await register(client, "reuse-owner")
    other_client = make_client()
    other = await register(other_client, "reuse-other")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=owner["id"], project_id=project["id"])
        old_task = Task(id=1000, user_id=other["id"], project_id=project["id"],
                        kind="chapter.generate", status="succeeded")
        old_log = GenerationLog(id=1001, project_id=project["id"], actor_user_id=other["id"],
                                kind="generate", status="success")
        db.add_all([conversation, old_task, old_log])
        await db.flush()
        task_proof = {"actor_user_id": other["id"], "statistics_key": old_task.statistics_key}
        log_proof = {"actor_user_id": other["id"], "log_instance_key": old_log.log_instance_key}
        await db.delete(old_task)
        await db.delete(old_log)
        await db.flush()
        own_task = Task(id=1000, user_id=owner["id"], project_id=project["id"],
                        kind="chapter.generate", status="succeeded")
        own_log = GenerationLog(id=1001, project_id=project["id"], actor_user_id=owner["id"],
                                kind="generate", status="success")
        db.add_all([own_task, own_log])
        await db.flush()
        own_task_proof = {"actor_user_id": owner["id"], "statistics_key": own_task.statistics_key}
        own_log_proof = {"actor_user_id": owner["id"], "log_instance_key": own_log.log_instance_key}
        change = AgentChange(
            conversation_id=conversation.id, project_id=project["id"], tool_call_id="reuse", tool_name="test",
            kind="small", effects=[
                {"table": "tasks", "pk": {"id": 1000}, "_private_identity": task_proof},
                {"table": "tasks", "pk": {"id": 1000}},  # 旧快照没有归属证据
                {"table": "tasks", "pk": {"id": 1000}, "_private_identity": own_task_proof},
                {"table": "generation_logs", "pk": {"id": 1001}, "_private_identity": log_proof},
                {"table": "generation_logs", "pk": {"id": 1001}},
                {"table": "generation_logs", "pk": {"id": 1001}, "_private_identity": own_log_proof},
                {"table": "chapter_versions", "pk": {"id": 42},
                 "before": {"generation_log_id": 1001}, "after": None,
                 "_private_log_references": {"before": log_proof}},
            ],
        )
        db.add(change)
        await db.commit()
        projected = (await service.public_changes(db, owner["id"], project["id"], [change]))[0]
        rows = projected.effects
        assert len([item for item in rows if item["table"] == "tasks"]) == 1
        assert len([item for item in rows if item["table"] == "generation_logs"]) == 1
        assert next(item for item in rows if item["table"] == "chapter_versions")["before"][
            "generation_log_id"
        ] is None
        assert "_private_identity" not in str(rows) and "_private_log_references" not in str(rows)
