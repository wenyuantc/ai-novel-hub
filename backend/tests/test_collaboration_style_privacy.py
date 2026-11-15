"""作品提示词的默认风格仅可按真实触发者读取，协作 AI 入口仍保持关闭。"""

import pytest
from sqlalchemy import select

from app.models import AgentConversation, AgentMessage, AIConfig, Chapter, Project, User, WritingStyle
from app.schemas.chapter import WizardRequest
from app.services import (
    analysis_service,
    context_builder,
    entity_ai_service,
    outline_ai_service,
    project_service,
    style_service,
    wizard_service,
)
from app.services.agent import context as agent_context
from tests.cover_fixtures import PNG
from tests.helpers import analysis_json, create_ai_config, create_project, register, wait_task


async def _style(client, name, description, tone=""):
    response = await client.post(
        "/api/v1/styles", json={"name": name, "description": description, "tone": tone},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_project_info_requires_real_actor_and_only_reads_visible_default(app, client, make_client):
    owner = await register(client)
    editor_client = make_client()
    editor = await register(editor_client, "style_editor")
    admin_client = make_client()
    admin = await register(admin_client, "style_admin")
    owner_style = await _style(client, "作者私有风格", "作者私密描述")
    editor_style = await _style(editor_client, "编辑者私有风格", "编辑者私密描述")
    project = await create_project(client, style="", default_style_id=owner_style["id"])

    async with app.state.sessionmaker() as db:
        (await db.get(User, admin["id"])).is_admin = True
        await db.commit()
        row = await db.get(Project, project["id"])
        preset = await db.scalar(select(WritingStyle).where(WritingStyle.user_id.is_(None)).limit(1))
        assert preset is not None

        assert (await project_service.project_info(db, row, actor_user_id=owner["id"])).style == "作者私密描述"
        for actor in (editor["id"], admin["id"]):
            assert (await project_service.project_info(db, row, actor_user_id=actor)).style == ""
        with pytest.raises(TypeError):
            await project_service.project_info(db, row)
        with pytest.raises(ValueError):
            await style_service.default_description(db, row, actor_user_id=None)

        # 全局预设对全部真实 actor 可见，即使管理员没有作者的私人风格权限。
        row.default_style_id = preset.id
        for actor in (owner["id"], editor["id"], admin["id"]):
            info = await project_service.project_info(db, row, actor_user_id=actor)
            assert info.style == preset.description.strip()

        # 异常旧绑定也不能因为作品 owner 不同而覆盖对真实 actor 的可见性判断。
        row.default_style_id = editor_style["id"]
        assert (await project_service.project_info(db, row, actor_user_id=editor["id"])).style == "编辑者私密描述"
        assert (await project_service.project_info(db, row, actor_user_id=owner["id"])).style == ""


async def test_chapter_context_uses_real_actor_for_project_default(app, client, make_client):
    owner = await register(client)
    other = make_client()
    editor = await register(other, "context_editor")
    style = await _style(client, "作者风格名", "独有风格描述")
    project = await create_project(client, style="", default_style_id=style["id"])
    chapter = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "开始"})
    assert chapter.status_code == 201

    async with app.state.sessionmaker() as db:
        project_row = await db.get(Project, project["id"])
        chapter_row = await db.get(Chapter, chapter.json()["id"])
        owner_text = await context_builder.build_chapter_context(
            db, project_row, chapter_row, 4000, actor_user_id=owner["id"],
        )
        editor_text = await context_builder.build_chapter_context(
            db, project_row, chapter_row, 4000, actor_user_id=editor["id"],
        )
        assert "独有风格描述" in owner_text
        assert "独有风格描述" not in editor_text
        assert "作者风格名" not in editor_text
        with pytest.raises(TypeError):
            await context_builder.build_chapter_context(db, project_row, chapter_row, 4000)


async def test_cover_prompt_filters_invisible_style_and_reports_safe_warning(app, client, make_client):
    await register(client)
    other = make_client()
    await register(other, "cover_other")
    own_style = await _style(client, "本人风格", "本人描述", "本人基调")
    foreign = await _style(other, "他人秘密名称", "他人秘密描述", "他人秘密基调")
    project = await create_project(client, style="", default_style_id=own_style["id"])
    path = f"/api/v1/projects/{project['id']}/cover/prompt"

    owner_response = await client.get(path)
    assert owner_response.status_code == 200
    assert "本人风格" in owner_response.json()["prompt"]
    assert (await other.get(path)).status_code == 404

    # 模拟已有损坏或历史绑定：公开入口仍不能泄露另一用户的风格。
    async with app.state.sessionmaker() as db:
        (await db.get(Project, project["id"])).default_style_id = foreign["id"]
        await db.commit()
    response = await client.get(path)
    assert response.status_code == 200
    body = response.json()
    for secret in ("他人秘密名称", "他人秘密描述", "他人秘密基调"):
        assert secret not in str(body)
    assert body["warning"] == "当前账号无法继承作品默认风格，已按无风格生成提示词。"
    assert owner_response.json()["warning"] is None


async def test_project_views_filter_foreign_style_without_overwriting_binding(app, client, make_client):
    await register(client, "view_owner")
    editor_client, admin_client, overlap_client, stranger_client = (make_client() for _ in range(4))
    await register(editor_client, "view_editor")
    admin = await register(admin_client, "view_admin")
    overlap = await register(overlap_client, "view_overlap")
    await register(stranger_client, "view_stranger")
    owner_style = await _style(client, "作者本人风格", "作者本人描述")
    foreign_style = await _style(stranger_client, "第三人秘密风格", "第三人秘密描述")
    editor_style = await _style(editor_client, "编辑者风格", "编辑者描述")
    project = await create_project(client, style="", default_style_id=owner_style["id"])
    assert project["default_style_id"] == owner_style["id"]
    base = f"/api/v1/projects/{project['id']}"

    async with app.state.sessionmaker() as db:
        (await db.get(User, admin["id"])).is_admin = True
        (await db.get(User, overlap["id"])).is_admin = True
        preset_id = await db.scalar(select(WritingStyle.id).where(WritingStyle.user_id.is_(None)).limit(1))
        await db.commit()

    for username, guest in (("view_editor", editor_client), ("view_overlap", overlap_client)):
        invite = await client.post(f"{base}/collaborators", json={"username": username, "role": "editor"})
        assert invite.status_code == 201, invite.text
        row = invite.json()
        response = await guest.post(
            f"/api/v1/collaboration/invitations/project/{row['invitation_id']}/accept",
            json={"expected_revision": row["revision"]},
        )
        assert response.status_code == 200

    async def get_view(actor):
        response = await actor.get(base)
        assert response.status_code == 200, response.text
        return response.json()

    async def get_shelf(actor):
        response = await actor.get("/api/v1/projects")
        assert response.status_code == 200, response.text
        return next(item for item in response.json() if item["id"] == project["id"])

    # 原作者和管理员代管可见作者的风格；普通协作者不能看到作者的私人风格。
    for actor in (client, admin_client, overlap_client):
        assert (await get_view(actor))["default_style_id"] == owner_style["id"]
    assert (await get_view(editor_client))["default_style_id"] is None
    assert (await get_shelf(editor_client))["default_style"] is None
    assert (await get_shelf(overlap_client))["default_style_id"] == owner_style["id"]

    # 损坏的第三人绑定对作者、普通协作者、管理员及重叠身份均匿名隐藏。
    async with app.state.sessionmaker() as db:
        (await db.get(Project, project["id"])).default_style_id = foreign_style["id"]
        await db.commit()
    for actor in (client, editor_client, admin_client, overlap_client):
        view = await get_view(actor)
        assert view["default_style_id"] is None and view["default_style"] is None
        assert "第三人秘密" not in str(view)
    for actor in (client, editor_client, overlap_client):
        shelf = await get_shelf(actor)
        assert shelf["default_style_id"] is None and shelf["default_style"] is None
    for actor, title in ((client, "作者修改"), (editor_client, "编辑者修改"),
                         (admin_client, "管理员修改"), (overlap_client, "重叠身份修改")):
        response = await actor.patch(base, json={"title": title})
        assert response.status_code == 200, response.text
        assert response.json()["default_style_id"] is None and response.json()["default_style"] is None
        cover = await actor.put(f"{base}/cover", files={"file": ("cover.png", PNG)})
        assert cover.status_code == 200, cover.text
        assert cover.json()["default_style_id"] is None and cover.json()["default_style"] is None
    async with app.state.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        assert row.default_style_id == foreign_style["id"]
        row.default_style_id = editor_style["id"]
        await db.commit()

    # 即使是异常绑定，风格本人仍可见；其他人不能据此访问该风格。
    assert (await get_view(editor_client))["default_style_id"] == editor_style["id"]
    for actor in (client, admin_client, overlap_client):
        assert (await get_view(actor))["default_style_id"] is None
    async with app.state.sessionmaker() as db:
        (await db.get(Project, project["id"])).default_style_id = preset_id
        await db.commit()
    for actor in (client, editor_client, admin_client, overlap_client):
        assert (await get_view(actor))["default_style_id"] == preset_id


async def test_project_view_hides_changed_style_binding_during_projection(app, client, make_client, monkeypatch):
    owner = await register(client)
    other = make_client()
    await register(other, "style_racing_other")
    own_style = await _style(client, "本人风格", "本人描述")
    foreign = await _style(other, "第三人风格", "第三人描述")
    project = await create_project(client, style="", default_style_id=own_style["id"])
    original = project_service.project_access.resolve_access

    async def changed_after_style_read(db, actor_id, project_id, **kwargs):
        row, access, member = await original(db, actor_id, project_id, **kwargs)
        # 模拟访问重验时刷新了项目默认风格；先前读取的风格行仍是旧绑定。
        row.default_style_id = foreign["id"]
        return row, access, member

    monkeypatch.setattr(project_service.project_access, "resolve_access", changed_after_style_read)
    async with app.state.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        result = await project_service.project_out(db, row, actor_user_id=owner["id"])
        assert result.default_style_id is None and result.default_style is None
        await db.rollback()


async def test_model_requests_omit_foreign_default_style_and_preserve_owner_style(app, client, make_client, fake_llm):
    await register(client)
    other = make_client()
    await register(other, "prompt_other")
    own_style = await _style(client, "作者可见风格", "作者可见描述")
    foreign = await _style(other, "他人私有风格名", "他人私有风格描述", "他人私有风格基调")
    await create_ai_config(client)
    project = await create_project(client, style="", default_style_id=own_style["id"])
    base = f"/api/v1/projects/{project['id']}"
    chapter = await client.post(f"{base}/chapters", json={"title": "第一章", "content": "主角走进城门。"})
    assert chapter.status_code == 201
    chapter_id = chapter.json()["id"]

    async def run(path, request, response):
        before = len(fake_llm.calls)
        fake_llm.push(response)
        created = await client.post(path, json=request)
        assert created.status_code == 202, created.text
        task = await wait_task(client, created.json(), within=15)
        assert task["status"] == "succeeded", task
        assert len(fake_llm.calls) == before + 1
        return str(fake_llm.calls[-1]["messages"])

    own_prompt = await run(f"{base}/wizard/world", {}, '[{"title":"山门","content":"青山"}]')
    assert "作者可见描述" in own_prompt
    async with app.state.sessionmaker() as db:
        (await db.get(Project, project["id"])).default_style_id = foreign["id"]
        await db.commit()

    outline = await client.post(f"{base}/outlines", json={"chapter_no": 1, "title": "第一章", "summary": "出发"})
    assert outline.status_code == 201, outline.text
    operations = (
        (f"{base}/wizard/world", {}, '[{"title":"新门派","content":"云海"}]'),
        (f"{base}/characters/generate", {}, '{"character":{"name":"新角色"},"relationships":[]}'),
        (f"/api/v1/chapters/{chapter_id}/generate", {"auto_continue": False}, "城门外出现一座山。"),
        (f"/api/v1/chapters/{chapter_id}/analyze", {}, analysis_json()),
        (f"{base}/outlines/continue", {"count": 1}, '{"outlines":[{"title":"续章","summary":"启程"}]}'),
    )
    for path, request, response in operations:
        prompt = await run(path, request, response)
        for secret in ("他人私有风格名", "他人私有风格描述", "他人私有风格基调"):
            assert secret not in prompt


async def test_job_prompt_builders_use_real_actor(app, client, make_client, fake_llm, monkeypatch):
    await register(client)
    other = make_client()
    editor = await register(other, "builder_editor")
    editor_config = await create_ai_config(other)
    owner_style = await _style(client, "作者隐私名称", "作者隐私描述")
    project = await create_project(client, style="", default_style_id=owner_style["id"])
    base = f"/api/v1/projects/{project['id']}"
    chapter = await client.post(f"{base}/chapters", json={"title": "开始", "content": "主角走进城门。"})
    assert chapter.status_code == 201

    # 只绕过测试中的旧作者入口以模拟未来协作 Job，不改变生产授权、任务入口或外部发送。
    async def existing_project(db, _user_id, project_id):
        return await db.get(Project, project_id)

    async def existing_chapter(db, _user_id, chapter_id):
        return await db.get(Chapter, chapter_id)

    async def existing_execution_project(db, _user_id, project_id, _execution):
        return await db.get(Project, project_id)

    for module in (analysis_service,):
        monkeypatch.setattr(module, "get_owned_project", existing_project)
    # 实体/大纲/向导（E4-g3a/b/c）经共用的 get_execution_project 读取作品。
    for module in (entity_ai_service, outline_ai_service, wizard_service):
        monkeypatch.setattr(module, "get_execution_project", existing_execution_project)
    monkeypatch.setattr(analysis_service, "get_owned_chapter", existing_chapter)

    async with app.state.sessionmaker() as db:
        common = {
            "user_id": editor["id"], "project_id": project["id"], "cipher": app.state.cipher,
            "factory": app.state.provider_factory, "ai_timeout": 30, "allow_private": False,
        }
        jobs = [
            await wizard_service.build_wizard_job(db, kind="world", req=WizardRequest(), ai_config_id=None, **common),
            await entity_ai_service.build_entity_job(db, kind="character.generate", params={}, **common),
            await outline_ai_service.build_outline_job(db, kind="outline.continue", params={"count": 1}, **common),
            await analysis_service.build_analysis_job(
                db, user_id=editor["id"], chapter_id=chapter.json()["id"], ai_config_id=None,
                cipher=app.state.cipher, factory=app.state.provider_factory, ai_timeout=30, allow_private=False,
            ),
        ]
        for job in jobs:
            assert "作者隐私描述" not in str(job.messages)
            assert "作者隐私名称" not in str(job.messages)

        conversation = AgentConversation(user_id=editor["id"], project_id=project["id"])
        db.add(conversation)
        await db.flush()
        db.add(AgentMessage(conversation_id=conversation.id, role="user", content="帮我继续"))
        await db.flush()
        config = await db.get(AIConfig, editor_config["id"])
        provider = fake_llm.factory("openai_compat", "x", None, 30)
        messages, _ = await agent_context.build_messages(
            db, conversation, await db.get(Project, project["id"]), config, provider,
        )
        assert "作者隐私描述" not in str(messages)
        assert "作者隐私名称" not in str(messages)
