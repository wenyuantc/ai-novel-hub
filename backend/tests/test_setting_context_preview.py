"""章节设定预览使用章初状态，并沿用人工内容读取权限。"""

from sqlalchemy import event, select

from app.api.v1.chapters import get_setting_context
from app.models import (
    Chapter,
    Character,
    Organization,
    Project,
    SettingProgression,
    SettingTimeline,
    User,
    WorldSetting,
)
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role


async def _seed_project(app, client):
    await register(client, "author")
    project = await create_project(client)
    project_id = project["id"]
    async with app.state.sessionmaker() as db:
        chapters = [Chapter(project_id=project_id, chapter_no=n, title=f"第{n}章") for n in (5, 20, 21)]
        world = WorldSetting(
            project_id=project_id, title="山门", category="place", content="山门被毁", ai_mode="always",
        )
        character = Character(
            project_id=project_id, name="林远", role_type="主角", current_state="金丹", ai_mode="always",
        )
        db.add_all([*chapters, world, character])
        await db.flush()
        world_timeline = SettingTimeline(
            project_id=project_id,
            subject_kind="world",
            world_setting_id=world.id,
            baseline={"content": "山门完好"},
            baseline_status="legacy_unverified",
            legacy_compat_allowed=True,
        )
        character_timeline = SettingTimeline(
            project_id=project_id,
            subject_kind="character",
            character_id=character.id,
            baseline={
                "profile": "",
                "appearance": "",
                "personality": "",
                "background": "",
                "current_state": "练气",
                "life_status": "alive",
                "age": "",
                "gender": "",
            },
            baseline_status="confirmed",
        )
        db.add_all([world_timeline, character_timeline])
        await db.flush()
        db.add_all([
            SettingProgression(
                timeline_id=world_timeline.id,
                field_key="content",
                operation="replace",
                value="山门被毁",
                anchor_kind="chapter",
                chapter_id=chapters[1].id,
                boundary="end",
                source="analysis",
                protection="automatic",
            ),
            SettingProgression(
                timeline_id=character_timeline.id,
                field_key="current_state",
                operation="replace",
                value="金丹",
                anchor_kind="chapter",
                chapter_id=chapters[1].id,
                boundary="end",
                source="analysis",
                protection="automatic",
            ),
        ])
        await db.commit()
        return project_id, {chapter.chapter_no: chapter.id for chapter in chapters}, world_timeline.id


async def test_setting_context_preview_uses_chapter_start_without_ai_or_database_writes(app, client, fake_llm):
    project_id, chapters, _ = await _seed_project(app, client)
    statements = []

    def collect_write(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
            statements.append(statement)

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", collect_write)
    try:
        payloads = {}
        for no in (5, 20, 21):
            response = await client.get(f"/api/v1/projects/{project_id}/chapters/{chapters[no]}/setting-context")
            assert response.status_code == 200, response.text
            payloads[no] = response.json()
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", collect_write)

    for no in (5, 20):
        assert "山门完好" in payloads[no]["text"]
        assert "练气" in payloads[no]["text"]
        assert "山门被毁" not in payloads[no]["text"]
        assert "金丹" not in payloads[no]["text"]
        assert payloads[no]["position"] == {"chapter_id": chapters[no], "chapter_no": no, "boundary": "start"}
        assert payloads[no]["legacy_unverified_count"] == 1
        assert payloads[no]["warnings"]
    assert "山门被毁" in payloads[21]["text"]
    assert "金丹" in payloads[21]["text"]
    assert not fake_llm.calls
    assert not fake_llm.embed_calls
    assert statements == []


async def test_setting_context_preview_allows_readonly_manager_but_isolates_other_users(
    app, client, make_client, system_admin
):
    project_id, chapters, _ = await _seed_project(app, client)
    viewer = make_client()
    viewer_user = await register(viewer, "readonly")
    reading = await create_role(system_admin, "设定只读", ["projects.read"])
    await assign_roles(system_admin, viewer_user["id"], [reading["id"]])
    outsider = make_client()
    await register(outsider, "outsider")
    other_project = await create_project(outsider)
    other_chapter = await outsider.post(
        f"/api/v1/projects/{other_project['id']}/chapters", json={"title": "另一本"}
    )
    assert other_chapter.status_code == 201, other_chapter.text

    url = f"/api/v1/projects/{project_id}/chapters/{chapters[5]}/setting-context"
    response = await viewer.get(url)
    assert response.status_code == 200, response.text
    assert "练气" in response.json()["text"]
    update = await viewer.patch(f"/api/v1/projects/{project_id}/chapters/{chapters[5]}", json={"title": "越权"})
    assert update.status_code == 403
    assert (await viewer.post(f"/api/v1/chapters/{chapters[5]}/generate")).status_code == 404
    assert (await outsider.get(url)).status_code == 404
    assert (await make_client().get(url)).status_code == 401
    assert (
        await viewer.get(f"/api/v1/projects/{project_id}/chapters/{other_chapter.json()['id']}/setting-context")
    ).status_code == 404


async def test_setting_context_preview_warning_clears_after_baseline_calibration(app, client):
    project_id, chapters, world_timeline_id = await _seed_project(app, client)
    url = f"/api/v1/projects/{project_id}/chapters/{chapters[5]}/setting-context"
    response = await client.get(url)
    assert response.status_code == 200, response.text
    assert response.json()["legacy_unverified_count"] == 1
    async with app.state.sessionmaker() as db:
        timeline = await db.get(SettingTimeline, world_timeline_id)
        timeline.baseline_status = "confirmed"
        await db.commit()
    response = await client.get(url)
    assert response.status_code == 200, response.text
    assert response.json()["legacy_unverified_count"] == 0
    assert response.json()["warnings"] == []


async def test_setting_context_preview_counts_unverified_subjects_without_loading_all_timelines(app, client):
    project_id, chapters, _ = await _seed_project(app, client)
    other_project = await create_project(client)
    async with app.state.sessionmaker() as db:
        character_timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project_id, SettingTimeline.subject_kind == "character"
            )
        )
        character_timeline.baseline_status = "legacy_unverified"
        organization = Organization(project_id=project_id, name="问道宗")
        other_world = WorldSetting(project_id=other_project["id"], title="其他作品", content="其他作品的设定")
        confirmed_world = WorldSetting(project_id=project_id, title="已校准", content="已校准的设定")
        db.add_all([organization, other_world, confirmed_world])
        await db.flush()
        db.add_all([
            SettingTimeline(
                project_id=project_id,
                subject_kind="membership",
                character_id=character_timeline.character_id,
                organization_id=organization.id,
                baseline={"exists": True, "position": "弟子", "status": "active", "note": ""},
                baseline_status="legacy_unverified",
            ),
            SettingTimeline(
                project_id=other_project["id"], subject_kind="world", world_setting_id=other_world.id,
                baseline={"content": "其他作品的设定"}, baseline_status="legacy_unverified",
            ),
            SettingTimeline(
                project_id=project_id, subject_kind="world", world_setting_id=confirmed_world.id,
                baseline={"content": "已校准的设定"}, baseline_status="confirmed",
            ),
        ])
        await db.commit()

    statements = []

    def collect_select(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", collect_select)
    try:
        response = await client.get(f"/api/v1/projects/{project_id}/chapters/{chapters[5]}/setting-context")
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", collect_select)

    assert response.status_code == 200, response.text
    assert response.json()["legacy_unverified_count"] == 3
    assert "3" in response.json()["warnings"][0]
    assert any("count(" in statement and "from setting_timelines" in statement for statement in statements)


async def test_setting_context_preview_does_not_flush_or_lose_unrelated_session_changes(app, client):
    project_id, chapters, _ = await _seed_project(app, client)
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, project_id)
        chapter = await db.get(Chapter, chapters[5])
        user = await db.get(User, project.user_id)
        original_project_title, original_chapter_title = project.title, chapter.title
        project.title = "尚未提交的作品名"
        chapter.title = "尚未提交的章节名"
        statements = []

        def collect_write(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
                statements.append(statement)

        event.listen(app.state.engine.sync_engine, "before_cursor_execute", collect_write)
        try:
            result = await get_setting_context(project_id, chapters[5], user, db)
            with db.no_autoflush:
                stored_project_title = await db.scalar(select(Project.title).where(Project.id == project_id))
                stored_chapter_title = await db.scalar(select(Chapter.title).where(Chapter.id == chapters[5]))
        finally:
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", collect_write)

        assert result.position.chapter_no == 5
        assert "练气" in result.text
        assert statements == []
        assert project.title == "尚未提交的作品名" and project in db.dirty
        assert chapter.title == "尚未提交的章节名" and chapter in db.dirty
        assert stored_project_title == original_project_title
        assert stored_chapter_title == original_chapter_title
