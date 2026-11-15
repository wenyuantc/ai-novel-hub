"""作品协作的章节、阅读、版本、导出和手工封面真实 HTTP 能力。"""

import asyncio
from io import BytesIO
from zipfile import ZipFile

import pytest
from sqlalchemy import event, func, select, update

from app.ai.providers import OpenAICompatProvider
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.models import Chapter, ChapterVersion, GenerationLog, Project, Task, WritingStyle
from app.schemas.chapter import ChapterUpdate
from app.services import ai_dispatch, chapter_service
from app.services.ai_dispatch import VisibilityChangedError, guard_provider, read_visibility_stamp
from tests.cover_fixtures import PNG
from tests.helpers import Gate, create_ai_config, create_project, register, task_params, wait_task
from tests.rbac_helpers import assign_roles, create_role
from tests.test_ai_visibility_dispatch import _collect, _server, _sse
from tests.test_marker_lifecycle import create_stale_marker, digest
from tests.test_open_api import new_key
from tests.test_rich_document_api import KEY, MARKER, add_section, save_payload


async def _content_setup(client, make_client):
    owner = await register(client, "content-owner")
    viewer_client, editor_client, stranger_client = make_client(), make_client(), make_client()
    viewer = await register(viewer_client, "content-viewer")
    editor = await register(editor_client, "content-editor")
    await register(stranger_client, "content-stranger")
    project = await create_project(client, style="作者私人风格")
    base = f"/api/v1/projects/{project['id']}"
    created = await client.post(f"{base}/chapters", json={"title": "第一章", "content": "原始正文"})
    assert created.status_code == 201, created.text
    chapter = created.json()
    chapter_url = f"{base}/chapters/{chapter['id']}"
    styled = await client.patch(chapter_url, json={"style_id": project["default_style_id"]})
    assert styled.status_code == 200, styled.text
    scenes = await client.get(f"{chapter_url}/scenes")
    assert scenes.status_code == 200, scenes.text
    scene = scenes.json()["items"][0]
    for guest, username, role in ((viewer_client, "content-viewer", "viewer"),
                                  (editor_client, "content-editor", "editor")):
        invite = await client.post(f"{base}/collaborators", json={"username": username, "role": role})
        assert invite.status_code == 201, invite.text
        path = f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept"
        accepted = await guest.post(path, json={"expected_revision": 1})
        assert accepted.status_code == 200, accepted.text
    return (
        owner, viewer, editor, project, chapter, scene, base, chapter_url,
        viewer_client, editor_client, stranger_client,
    )


async def test_chapter_reader_version_and_export_access_matrix(client, make_client, system_admin):
    (owner, _, _, project, chapter, scene, base, chapter_url, viewer, editor, stranger) = await _content_setup(
        client, make_client,
    )
    assert (await client.get(chapter_url)).json()["style_id"] == project["default_style_id"]
    assert (await system_admin.get(chapter_url)).json()["style_id"] == project["default_style_id"]
    for actor in (viewer, editor):
        detail = await actor.get(chapter_url)
        assert detail.status_code == 200 and detail.json()["content"] == "原始正文"
        assert detail.json()["style_id"] is None
        listing = await actor.get(f"{base}/chapters")
        assert listing.status_code == 200 and listing.json()[0]["style_id"] is None
        reader = await actor.get(f"{base}/reader/chapters/{chapter['id']}")
        assert reader.status_code == 200 and reader.json()["chapter"]["style_id"] is None
        assert (await actor.get(f"{base}/reader/toc")).status_code == 200
        assert (await actor.get(f"{chapter_url}/scenes/{scene['id']}")).status_code == 200
        assert (await actor.get(f"{chapter_url}/scenes/{scene['id']}/document")).status_code == 200
        versions = await actor.get(f"{chapter_url}/versions")
        assert versions.status_code == 200 and versions.json()
        assert (await actor.get(f"{chapter_url}/versions/{versions.json()[0]['id']}")).status_code == 200
        for fmt in ("txt", "md", "epub"):
            exported = await actor.get(f"{base}/export", params={"format": fmt})
            assert exported.status_code == 200, exported.text
            if fmt == "epub":
                with ZipFile(BytesIO(exported.content)) as archive:
                    assert archive.read("mimetype") == b"application/epub+zip"
                    metadata = archive.read("OEBPS/content.opf").decode("utf-8")
                    assert f"<dc:creator>{owner['username']}</dc:creator>" in metadata
            else:
                assert "原始正文" in exported.text
        override = await actor.get(f"{base}/export", params={"format": "epub", "author": "指定署名"})
        assert override.status_code == 200
        with ZipFile(BytesIO(override.content)) as archive:
            assert "<dc:creator>指定署名</dc:creator>" in archive.read("OEBPS/content.opf").decode("utf-8")
        assert (await actor.put(f"{base}/reading-progress", json={
            "chapter_id": chapter["id"], "ratio": 0.4,
        })).status_code == 404
        assert (await actor.get(f"{base}/backup")).status_code == 404
    assert (await system_admin.get(f"{base}/export")).status_code == 404
    assert (await stranger.get(chapter_url)).status_code == 404
    assert (await stranger.get(f"{chapter_url}/scenes/{scene['id']}")).status_code == 404
    assert (await stranger.get(f"{base}/export")).status_code == 404
    assert (await client.put(f"{base}/reading-progress", json={
        "chapter_id": chapter["id"], "ratio": 0.7,
    })).status_code == 204
    assert (await client.get(base)).json()["owner"]["id"] == owner["id"]


async def test_manual_writes_do_not_expand_internal_agent_or_trigger_owner_tasks(app, client, make_client, fake_llm):
    (_, _, editor_user, project, chapter, scene, base, chapter_url, viewer, editor, _) = await _content_setup(
        client, make_client,
    )
    versions = (await client.get(f"{chapter_url}/versions")).json()
    version_url = f"{chapter_url}/versions/{versions[0]['id']}"
    scene_url = f"{chapter_url}/scenes/{scene['id']}"
    async with app.state.sessionmaker() as db:
        before = await db.scalar(select(func.count(Task.id)))
        with pytest.raises(NotFoundError):
            await chapter_service.list_chapters(db, editor_user["id"], project["id"])
        with pytest.raises(NotFoundError):
            await chapter_service.update_chapter(
                db, editor_user["id"], project["id"], chapter["id"], ChapterUpdate(title="内部调用"),
            )

    assert (await viewer.patch(chapter_url, json={"title": "无权"})).status_code == 403
    assert (await viewer.post(f"{base}/chapters", json={"title": "无权"})).status_code == 403
    assert (await viewer.patch(scene_url, json={"expected_content_revision": 0, "title": "无权"})).status_code == 403
    assert (await viewer.patch(version_url, json={"starred": True})).status_code == 403
    assert (await viewer.put(f"{base}/cover", files={"file": ("cover.png", PNG)})).status_code == 403
    assert (await editor.patch(chapter_url, json={"style_id": None})).status_code == 403
    assert (await editor.patch(chapter_url, json={"style_id": project["default_style_id"]})).status_code == 403
    changed = await editor.patch(chapter_url, json={"title": "编辑后的章节"})
    assert changed.status_code == 200 and changed.json()["style_id"] is None
    modified_scene = await editor.patch(scene_url, json={"expected_content_revision": 0, "title": "编辑场景"})
    assert modified_scene.status_code == 200, modified_scene.text
    changed_content = await editor.patch(scene_url, json={
        "expected_content_revision": modified_scene.json()["content_revision"], "content": "协作编辑正文",
    })
    assert changed_content.status_code == 200, changed_content.text
    assert (await editor.patch(version_url, json={"starred": True})).status_code == 200
    new = await editor.post(f"{base}/chapters", json={"title": "协作者新章", "content": "新正文"})
    assert new.status_code == 201, new.text
    cover = await editor.put(f"{base}/cover", files={"file": ("cover.png", PNG)})
    assert cover.status_code == 200 and cover.json()["access"]["role"] == "editor"
    assert (await viewer.get(f"{base}/cover")).content == PNG
    assert (await editor.delete(f"{base}/cover")).status_code == 200
    assert (await editor.delete(base)).status_code == 404
    assert (await viewer.get(f"{base}/cover/prompt")).status_code == 403
    assert (await editor.get(f"{base}/cover/prompt")).status_code == 200
    current_scene = (await editor.get(scene_url)).json()
    for actor in (viewer, editor):
        cover_generate = await actor.post(f"{base}/cover/generate", json={"prompt": "星海"})
        if actor is viewer:
            assert cover_generate.status_code == 403, cover_generate.text
        else:
            assert cover_generate.status_code == 400, cover_generate.text
            assert "尚未配置" in cover_generate.json()["detail"]
        # 阶段 E 起章节 AI 按协作能力开放：viewer 缺 ai.use 为 403，editor 未配置模型为 400，均不建任务。
        chapter_generate = await actor.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
        if actor is viewer:
            assert chapter_generate.status_code == 403, chapter_generate.text
        else:
            assert chapter_generate.status_code == 400, chapter_generate.text
            assert "尚未配置" in chapter_generate.json()["detail"]
        chapter_rewrite = await actor.post(f"/api/v1/chapters/{chapter['id']}/rewrite", json={
            "op": "rewrite", "start": 0, "end": 1,
        })
        scene_rewrite = await actor.post(f"{scene_url}/rewrite", json={
            "op": "rewrite", "start": 0, "end": 1,
            "expected_content_revision": current_scene["content_revision"],
            "expected_planning_revision": current_scene["planning_revision"],
        })
        for response in (chapter_rewrite, scene_rewrite):
            if actor is viewer:
                assert response.status_code == 403 and response.json()["code"] == "project_readonly", response.text
            else:
                assert response.status_code == 400 and "尚未配置" in response.json()["detail"], response.text
    assert fake_llm.calls == [] and fake_llm.image_calls == []
    async with app.state.sessionmaker() as db:
        assert (await db.get(Chapter, chapter["id"])).style_id == project["default_style_id"]
        assert await db.scalar(select(func.count(Task.id))) == before


async def test_manual_chapter_lock_preserves_pending_changes_and_rejects_stale_content(app, client):
    owner = await register(client, "pending-owner")
    project = await create_project(client)
    created = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
        "title": "持久标题", "content": "持久正文",
    })
    assert created.status_code == 201, created.text
    chapter_id = created.json()["id"]

    async with app.state.sessionmaker() as db:
        chapter = await db.get(Chapter, chapter_id)
        chapter.title, chapter.content = "尚未提交标题", "尚未提交正文"
        locked = await chapter_service.get_manual_chapter(
            db, owner["id"], project["id"], chapter_id, "content.edit", for_update=True,
        )
        assert locked.title == "尚未提交标题" and locked.content == "尚未提交正文"
        assert locked.content_revision == created.json()["content_revision"]
        await db.rollback()

    async with app.state.sessionmaker() as stale, app.state.sessionmaker() as writer:
        old = await stale.get(Chapter, chapter_id)
        old.content = "基于旧版本的未提交正文"
        await writer.execute(update(Chapter).where(Chapter.id == chapter_id).values(
            content="另一会话已提交的正文", content_revision=old.content_revision + 1,
        ))
        await writer.commit()
        with pytest.raises(ConflictError) as conflict:
            await chapter_service.get_manual_chapter(
                stale, owner["id"], project["id"], chapter_id, "content.edit", for_update=True,
            )
        assert conflict.value.extra["code"] == "stale_scene_revision"
        await stale.rollback()
    async with app.state.sessionmaker() as check:
        current = await check.get(Chapter, chapter_id)
        assert current.content == "另一会话已提交的正文" and current.content_revision == 1

    async with app.state.sessionmaker() as pending, app.state.sessionmaker() as writer:
        row = await pending.get(Chapter, chapter_id)
        row.title = "保留当前会话的标题"
        await writer.execute(update(Chapter).where(Chapter.id == chapter_id).values(content_revision=2))
        await writer.commit()
        locked = await chapter_service.get_manual_chapter(
            pending, owner["id"], project["id"], chapter_id, "content.edit", for_update=True,
        )
        assert locked.title == "保留当前会话的标题" and locked.content_revision == 2
        await pending.rollback()

    async with app.state.sessionmaker() as pending, app.state.sessionmaker() as writer:
        row = await pending.get(Chapter, chapter_id)
        row.word_count = 999
        await writer.execute(update(Chapter).where(Chapter.id == chapter_id).values(content_revision=3))
        await writer.commit()
        with pytest.raises(ConflictError) as conflict:
            await chapter_service.get_manual_chapter(
                pending, owner["id"], project["id"], chapter_id, "content.edit", for_update=True,
            )
        assert conflict.value.extra["code"] == "stale_scene_revision"
        await pending.rollback()


async def test_denied_chapter_service_does_not_flush_pending_data_before_authorization(app, client, make_client):
    (_, viewer, _, _, chapter, _, _, _, _, _, _) = await _content_setup(client, make_client)
    async with app.state.sessionmaker() as db:
        row = await db.get(Chapter, chapter["id"])
        row.title = "不得在授权前写入"

        def forbid_flush(*_args):
            raise AssertionError("章节写入口在授权前自动 flush")

        event.listen(db.sync_session, "before_flush", forbid_flush)
        try:
            with pytest.raises(ForbiddenError):
                await chapter_service.update_chapter(
                    db, viewer["id"], chapter["project_id"], chapter["id"], ChapterUpdate(title="越权"), manual=True,
                )
            assert row.title == "不得在授权前写入"
        finally:
            event.remove(db.sync_session, "before_flush", forbid_flush)
            await db.rollback()


async def test_scene_document_and_structure_respect_editor_and_viewer(client, make_client):
    (_, _, _, _, _, scene, base, chapter_url, viewer, editor, stranger) = await _content_setup(client, make_client)
    scene_url = f"{chapter_url}/scenes/{scene['id']}"
    document_url = f"{scene_url}/document"
    document = await viewer.get(document_url)
    assert document.status_code == 200, document.text
    body = save_payload(document.json())
    add_section(body, enabled=True)
    assert (await viewer.put(document_url, json=body)).status_code == 403
    saved = await editor.put(document_url, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["document"]["sections"][0]["include_in_ai"] is True
    assert (await viewer.get(f"{base}/markers")).status_code == 200
    assert (await stranger.get(f"{base}/markers")).status_code == 404
    split_request = {"expected_content_revision": saved.json()["content_revision"], "offset": 2}
    assert (await viewer.post(f"{scene_url}/split", json=split_request)).status_code == 403
    split = await editor.post(f"{scene_url}/split", json=split_request)
    assert split.status_code == 200, split.text
    scene_ids = [item["id"] for item in split.json()["items"]]
    preview_body = {"expected_content_revision": split.json()["content_revision"], "scene_ids": scene_ids}
    preview = await viewer.post(f"{chapter_url}/scenes/merge-preview", json=preview_body)
    assert preview.status_code == 200, preview.text
    merge = {**preview_body, "expected_planning_revision": preview.json()["planning_revision"],
             "signature": preview.json()["signature"], "metadata_source": "left"}
    assert (await viewer.post(f"{chapter_url}/scenes/merge", json=merge)).status_code == 403
    merged = await editor.post(f"{chapter_url}/scenes/merge", json=merge)
    assert merged.status_code == 200, merged.text
    assert len(merged.json()["items"]) == 1
    assert (await viewer.get(chapter_url)).json()["content"] == "原始正文"
    conversion = f"{scene_url}/document/sections/{KEY}/convert"
    body = {"expected_content_revision": merged.json()["content_revision"]}
    assert (await viewer.post(conversion, json=body)).status_code == 403
    converted = await editor.post(conversion, json=body)
    assert converted.status_code == 200, converted.text
    assert "作者🙂" in converted.json()["document"]["content"]


async def test_marker_and_version_restore_are_manual_collaboration_actions(client, make_client):
    (_, _, _, project, _, scene, base, chapter_url, viewer, editor, _) = await _content_setup(client, make_client)
    versions = (await client.get(f"{chapter_url}/versions")).json()
    first = versions[0]["id"]
    assert (await client.patch(f"{chapter_url}/versions/{first}", json={"starred": True})).status_code == 200
    changed = await editor.patch(chapter_url, json={"content": "后续正文", "expected_content_revision": 0})
    assert changed.status_code == 200, changed.text
    preview = await viewer.get(f"{chapter_url}/versions/{first}/restore-preview", params={
        "expected_content_revision": changed.json()["content_revision"],
    })
    assert preview.status_code == 200, preview.text
    restore_body = {"expected_content_revision": changed.json()["content_revision"]}
    assert (await viewer.post(f"{chapter_url}/versions/{first}/restore", json=restore_body)).status_code == 403
    restored = await editor.post(f"{chapter_url}/versions/{first}/restore", json=restore_body)
    assert restored.status_code == 200, restored.text
    assert restored.json()["content"] == "原始正文" and restored.json()["style_id"] is None
    assert (await client.get(chapter_url)).json()["style_id"] == project["default_style_id"]

    document_url = f"{chapter_url}/scenes/{scene['id']}/document"
    await create_stale_marker(client, document_url)
    timeline = await viewer.get(f"{base}/markers")
    assert timeline.status_code == 200 and timeline.json()["items"][0]["key"] == MARKER
    revision = (await client.get(chapter_url)).json()["content_revision"]
    marker_url = f"{base}/markers/{scene['id']}/{MARKER}"
    relink = {"expected_source_revision": revision, "target_chapter_id": scene["chapter_id"],
              "target_scene_id": scene["id"], "expected_target_revision": revision,
              "start": 0, "end": 1, "selected_hash": digest("原")}
    assert (await viewer.post(f"{marker_url}/relink", json=relink)).status_code == 403
    moved = await editor.post(f"{marker_url}/relink", json=relink)
    assert moved.status_code == 200, moved.text
    params = {"expected_content_revision": moved.json()["target_revision"]}
    assert (await viewer.delete(marker_url, params=params)).status_code == 403
    deleted = await editor.delete(marker_url, params=params)
    assert deleted.status_code == 200, deleted.text
    assert (await viewer.get(f"{base}/markers")).json()["items"] == []


async def test_global_style_is_visible_and_cross_project_ids_are_rejected(app, client, make_client):
    (_, _, _, _, _, scene, base, chapter_url, viewer, editor, _) = await _content_setup(client, make_client)
    async with app.state.sessionmaker() as db:
        preset = await db.scalar(select(WritingStyle.id).where(WritingStyle.user_id.is_(None)).limit(1))
    assert preset is not None
    assert (await client.patch(chapter_url, json={"style_id": preset})).status_code == 200
    assert (await viewer.get(chapter_url)).json()["style_id"] == preset
    assert (await editor.get(f"{base}/chapters")).json()[0]["style_id"] == preset
    other = await create_project(client, title="另一本")
    other_base = f"/api/v1/projects/{other['id']}"
    other_chapter = (await client.post(f"{other_base}/chapters", json={"title": "隔离章", "content": "私有"})).json()
    other_scene = (await client.get(f"{other_base}/chapters/{other_chapter['id']}/scenes")).json()["items"][0]
    assert (await viewer.get(f"{base}/chapters/{other_chapter['id']}")).status_code == 404
    assert (await editor.patch(f"{base}/chapters/{other_chapter['id']}", json={"title": "越权"})).status_code == 404
    assert (await viewer.get(f"{chapter_url}/scenes/{other_scene['id']}")).status_code == 404
    assert (await editor.patch(f"{chapter_url}/scenes/{other_scene['id']}", json={
        "expected_content_revision": 0, "title": "越权",
    })).status_code == 404
    assert (await viewer.get(f"{other_base}/chapters/{other_chapter['id']}/scenes/{scene['id']}")).status_code == 404


async def test_version_log_references_remain_private_for_each_actor(app, client, make_client, system_admin):
    (owner, viewer_user, editor_user, project, chapter, _, _, chapter_url, viewer, editor, _) = await _content_setup(
        client, make_client,
    )
    async with app.state.sessionmaker() as db:
        logs = [GenerationLog(project_id=project["id"], chapter_id=chapter["id"], actor_user_id=actor,
                              kind="generate", status="success", messages=[])
                for actor in (owner["id"], viewer_user["id"], editor_user["id"])]
        db.add_all(logs)
        await db.flush()
        db.add_all([ChapterVersion(chapter_id=chapter["id"], seq=index + 2, source="generate", content="历史正文",
                                   generation_log_id=log.id) for index, log in enumerate(logs)])
        await db.commit()
        log_ids = [log.id for log in logs]
    for actor, expected in ((client, log_ids[0]), (viewer, log_ids[1]), (editor, log_ids[2]), (system_admin, None)):
        response = await actor.get(f"{chapter_url}/versions")
        assert response.status_code == 200, response.text
        projected = {row["generation_log_id"] for row in response.json()} - {None}
        assert projected == ({expected} if expected is not None else set())
        for row in response.json():
            detail = await actor.get(f"{chapter_url}/versions/{row['id']}")
            assert detail.status_code == 200
            assert detail.json()["generation_log_id"] == row["generation_log_id"]
    logs = await client.get(f"{chapter_url}/generation-logs")
    assert logs.status_code == 200 and [item["id"] for item in logs.json()] == [log_ids[0]]
    # 阶段 E 日志私人视图：当前成员只看到本人日志（见 test_collaboration_log_private_views），非成员管理员仍 404。
    for actor, expected in ((viewer, log_ids[1]), (editor, log_ids[2])):
        own = await actor.get(f"{chapter_url}/generation-logs")
        assert own.status_code == 200 and [item["id"] for item in own.json()] == [expected], own.text
    assert (await system_admin.get(f"{chapter_url}/generation-logs")).status_code == 404


async def test_readonly_collaborator_with_admin_edit_retains_manual_permissions(client, make_client, system_admin):
    (_, viewer_user, _, _, chapter, scene, base, chapter_url, viewer, _, _) = await _content_setup(client, make_client)
    role = await create_role(system_admin, "章节人工维护", ["projects.read", "projects.edit"])
    await assign_roles(system_admin, viewer_user["id"], [role["id"]])
    assert (await viewer.patch(chapter_url, json={"title": "人工管理员"})).status_code == 200
    assert (await viewer.patch(f"{chapter_url}/scenes/{scene['id']}", json={
        "expected_content_revision": chapter["content_revision"], "title": "人工管理场景",
    })).status_code == 200
    assert (await viewer.get(f"{base}/export?format=txt")).status_code == 200
    assert (await viewer.get(f"{base}/backup")).status_code == 404
    # 管理员人工权限不扩大 AI：作品可读而缺 ai.use 时按 design.md 返回 403，不再沿用阶段 D 的 404。
    generate = await viewer.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
    assert generate.status_code == 403, generate.text


async def test_editor_cannot_recycle_or_cancel_owners_running_task(app, client, make_client, fake_llm):
    (_, _, _, project, chapter, _, base, _, viewer, editor, _) = await _content_setup(client, make_client)
    assert (await client.patch(base, json={"auto_analyze": False})).status_code == 200
    await create_ai_config(client)
    gate = Gate()
    fake_llm.push(["已发送的文本", gate, "合法后续文本"])
    created = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={"target_words": 300})
    assert created.status_code == 202, created.text
    task_ref = created.json()
    task_id = task_ref["id"]
    try:
        await gate.wait_reached()
        assert (await editor.delete(base)).status_code == 404
        assert (await viewer.delete(base)).status_code == 404
        assert (await editor.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task_ref))).status_code == 404
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, task_id)
            record = await db.get(Project, project["id"])
            assert task.status == "running" and task.user_id == record.user_id
            assert record.deleted_at is None and record.deletion_started_at is None and record.deletion_token is None
        assert fake_llm.aborted == 0
    finally:
        gate.release()
    result = await wait_task(client, task_ref)
    assert result["status"] == "succeeded"


async def test_editor_document_visibility_write_waits_for_real_send(app, client, make_client, monkeypatch):
    (_, _, _, project, _, scene, _, chapter_url, _, editor, _) = await _content_setup(client, make_client)
    endpoint = f"{chapter_url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body, enabled=True)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    disable = save_payload(saved.json())
    disable["document"]["sections"][0]["include_in_ai"] = False
    async with app.state.sessionmaker() as db:
        stamp = await read_visibility_stamp(db, project["id"])
    monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.2)
    async with _server(payload=_sse("openai")) as (url, received, headers, response_body, requests):
        provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, app.state.sessionmaker)
        messages = [{"role": "user", "content": "作者可见区块"}]
        pending = asyncio.create_task(_collect(provider.generate_stream(messages, "test-model")))
        try:
            await asyncio.wait_for(received.wait(), 5)
            blocked = await editor.put(endpoint, json=disable)
            assert blocked.status_code == 409 and blocked.json()["code"] == "ai_visibility_busy", blocked.text
            headers.set()
            changed = await editor.put(endpoint, json=disable)
            assert changed.status_code == 200, changed.text
            response_body.set()
            assert await asyncio.wait_for(pending, 5) == ["ok"]
            with pytest.raises(VisibilityChangedError):
                await _collect(provider.generate_stream(messages, "test-model"))
            assert len(requests) == 1
        finally:
            headers.set()
            response_body.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_open_api_key_does_not_inherit_web_collaboration(client, make_client):
    (_, _, _, project, chapter, _, base, _, viewer, editor, _) = await _content_setup(client, make_client)
    assert (await client.put(f"{base}/cover", files={"file": ("cover.png", PNG)})).status_code == 200
    for actor in (viewer, editor):
        headers = await new_key(actor, all_projects=True)
        listing = await actor.get("/api/open/v1/novels", headers=headers)
        assert listing.status_code == 200 and listing.json()["items"] == []
        prefix = f"/api/open/v1/novels/{project['id']}"
        for suffix in ("", "/chapters", f"/chapters/{chapter['id']}", "/cover", "/export"):
            response = await actor.get(prefix + suffix, headers=headers)
            assert response.status_code == 404, (suffix, response.text)
    owner_key = await new_key(client, all_projects=True)
    assert (await client.get(f"/api/open/v1/novels/{project['id']}/export", headers=owner_key)).status_code == 200
