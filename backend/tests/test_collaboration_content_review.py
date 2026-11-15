"""协作内容边界的独立交叉复核。"""

import asyncio
from io import BytesIO
from zipfile import ZipFile

import pytest
from sqlalchemy import text

from app.ai.providers import OpenAICompatProvider
from app.models import Chapter, ChapterPlan, Task
from app.services import ai_dispatch, chapter_service
from app.services.ai_dispatch import guard_provider, read_visibility_stamp
from tests.helpers import Gate, create_ai_config, create_project, register, wait_task
from tests.rbac_helpers import assign_roles, create_role
from tests.test_ai_visibility_dispatch import _collect, _server, _sse
from tests.test_collaboration_content_access import _content_setup


async def test_editor_cannot_delete_chapter_while_owner_generation_is_running(app, client, make_client, fake_llm):
    (_, _, _, _, chapter, _, base, chapter_url, _, editor, _) = await _content_setup(client, make_client)
    assert (await client.patch(base, json={"auto_analyze": False})).status_code == 200
    await create_ai_config(client)
    gate = Gate()
    fake_llm.push(["已发送", gate, "后续正文"])
    response = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={"target_words": 300})
    assert response.status_code == 202, response.text
    task_ref = response.json()
    task_id = task_ref["id"]
    try:
        await gate.wait_reached()
        denied = await editor.delete(chapter_url)
        assert denied.status_code == 409, denied.text
        async with app.state.sessionmaker() as db:
            task = await db.get(Task, task_id)
            assert task.status == "running" and task.chapter_id == chapter["id"]
            assert await db.get(Chapter, chapter["id"]) is not None
    finally:
        gate.release()
    assert (await wait_task(client, task_ref))["status"] == "succeeded"


@pytest.mark.parametrize("kind,status,scope", [
    ("chapter.analyze", "queued", "chapter"),
    ("chapter.generate", "paused", "chapter"),
    ("chapter.batch", "running", "batch"),
    ("chapter.batch", "paused", "batch"),
    ("chapter.batch", "queued", "invalid_batch"),
    ("agent.turn", "running", "unknown"),
])
async def test_editor_cannot_delete_chapter_with_other_active_or_uncertain_task(
    app, client, make_client, kind, status, scope,
):
    (owner, _, _, project, chapter, _, _, chapter_url, _, editor, _) = await _content_setup(client, make_client)
    await app.state.task_engine.stop()
    task_input = {"chapter_id": chapter["id"]} if scope == "chapter" else (
        {"chapter_ids": [chapter["id"]]} if scope == "batch" else {"chapter_ids": "unverifiable"}
        if scope == "invalid_batch" else {}
    )
    async with app.state.sessionmaker() as db:
        task = Task(
            user_id=owner["id"], project_id=project["id"], chapter_id=chapter["id"] if scope == "chapter" else None,
            kind=kind, status=status, input=task_input,
        )
        db.add(task)
        await db.commit()
        task_id = task.id
    denied = await editor.delete(chapter_url)
    assert denied.status_code == 409, denied.text
    assert denied.json()["code"] == "chapter_in_use"
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        assert task.status == status
        assert await db.get(Chapter, chapter["id"]) is not None


@pytest.mark.parametrize("role", ["owner", "admin", "admin_collaborator"])
async def test_existing_owner_and_admin_delete_behavior_is_preserved(app, client, make_client, system_admin, role):
    (owner, _, editor_user, project, chapter, _, _, chapter_url, _, editor, _) = await _content_setup(
        client, make_client,
    )
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        db.add(Task(user_id=owner["id"], project_id=project["id"], chapter_id=chapter["id"],
                    kind="chapter.analyze", status="queued", input={"chapter_id": chapter["id"]}))
        await db.commit()
    if role == "admin_collaborator":
        delegated = await create_role(system_admin, "章节管理者", ["projects.read", "projects.edit"])
        await assign_roles(system_admin, editor_user["id"], [delegated["id"]])
    actor = {"owner": client, "admin": system_admin, "admin_collaborator": editor}[role]
    deleted = await actor.delete(chapter_url)
    assert deleted.status_code == 204, deleted.text


async def test_editor_may_delete_chapter_unrelated_to_proven_single_chapter_task(app, client, make_client):
    (owner, _, _, project, _, _, base, chapter_url, _, editor, _) = await _content_setup(client, make_client)
    other = await client.post(f"{base}/chapters", json={"title": "其他章", "content": "其他内容"})
    assert other.status_code == 201, other.text
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        db.add(Task(user_id=owner["id"], project_id=project["id"], chapter_id=other.json()["id"],
                    kind="chapter.analyze", status="queued", input={"chapter_id": other.json()["id"]}))
        await db.commit()
    deleted = await editor.delete(chapter_url)
    assert deleted.status_code == 204, deleted.text


async def test_editor_chapter_deletion_waits_for_sync_ai_send(app, client, make_client, monkeypatch):
    (_, _, _, project, _, _, _, chapter_url, _, editor, _) = await _content_setup(client, make_client)
    async with app.state.sessionmaker() as db:
        stamp = await read_visibility_stamp(db, project["id"])
    monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.2)
    async with _server(payload=_sse("openai")) as (url, received, headers, response_body, requests):
        provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, app.state.sessionmaker)
        send = asyncio.create_task(_collect(provider.generate_stream([{"role": "user", "content": "正文"}], "model")))
        try:
            await asyncio.wait_for(received.wait(), 5)
            blocked = await editor.delete(chapter_url)
            assert blocked.status_code == 409 and blocked.json()["code"] == "ai_visibility_busy", blocked.text
            headers.set()
            response_body.set()
            assert await asyncio.wait_for(send, 5) == ["ok"]
            assert (await editor.delete(chapter_url)).status_code == 204
            assert len(requests) == 1
        finally:
            headers.set()
            response_body.set()
            if not send.done():
                send.cancel()
            await asyncio.gather(send, return_exceptions=True)


@pytest.mark.parametrize("busy", [False, True])
async def test_nested_sqlite_visibility_timeout_restores_original(app, client, monkeypatch, busy):
    user = await register(client, "visibility-timeout-owner")
    project = await create_project(client)
    if busy:
        async def conflict(*args, **kwargs):
            raise ai_dispatch.VisibilityBusyError()

        monkeypatch.setattr(ai_dispatch, "lock_visibility_write", conflict)
    async with app.state.sessionmaker() as db:
        await db.execute(text("PRAGMA busy_timeout = 4321"))
        if busy:
            with pytest.raises(ai_dispatch.VisibilityBusyError):
                await chapter_service.lock_manual_project_visibility(db, user["id"], project["id"])
        else:
            await chapter_service.lock_manual_project_visibility(db, user["id"], project["id"])
        assert await db.scalar(text("PRAGMA busy_timeout")) == 4321
        await db.rollback()


@pytest.mark.parametrize("was_admin", [False, True])
async def test_delete_rechecks_changed_admin_source_before_mutation(app, client, make_client, system_admin, monkeypatch,
                                                             was_admin):
    (_, _, editor_user, _, chapter, _, _, chapter_url, _, editor, _) = await _content_setup(client, make_client)
    role = await create_role(system_admin, "变动中的章节管理者", ["projects.read", "projects.edit"])
    if was_admin:
        await assign_roles(system_admin, editor_user["id"], [role["id"]])
    reached, release = asyncio.Event(), asyncio.Event()
    original = chapter_service.get_manual_chapter

    async def before_project_lock(db, user_id, project_id, chapter_id, capability, **kwargs):
        assert kwargs["visibility_write"] is not was_admin
        reached.set()
        await release.wait()
        return await original(db, user_id, project_id, chapter_id, capability, **kwargs)

    monkeypatch.setattr(chapter_service, "get_manual_chapter", before_project_lock)
    deletion = asyncio.create_task(editor.delete(chapter_url))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        await assign_roles(system_admin, editor_user["id"], [] if was_admin else [role["id"]])
        release.set()
        response = await asyncio.wait_for(deletion, 5)
        assert response.status_code == 409 and response.json()["code"] == "collaboration_busy", response.text
        async with app.state.sessionmaker() as db:
            assert await db.get(Chapter, chapter["id"]) is not None
    finally:
        release.set()
        if not deletion.done():
            deletion.cancel()
        await asyncio.gather(deletion, return_exceptions=True)


@pytest.mark.parametrize("path", ["chapter", "list", "reader", "toc", "epub", "update"])
async def test_corrupt_cross_project_chapter_references_do_not_leak_or_modify_other_book(
    app, client, make_client, path,
):
    (_, _, _, project, chapter, _, base, chapter_url, viewer, editor, _) = await _content_setup(client, make_client)
    other = await create_project(client, title="另一部作品")
    other_base = f"/api/v1/projects/{other['id']}"
    secret = "不应泄露的他书卷名"
    outline = await client.post(f"{other_base}/outlines", json={
        "title": "他书大纲", "chapter_no": 1, "volume_no": 2, "volume_title": secret,
    })
    assert outline.status_code == 201, outline.text
    other_chapter = await client.post(f"{other_base}/chapters", json={
        "outline_id": outline.json()["id"], "title": "他书章节", "content": "另一书正文",
    })
    assert other_chapter.status_code == 201, other_chapter.text
    async with app.state.sessionmaker() as db:
        plan = ChapterPlan(project_id=other["id"], outline_id=outline.json()["id"], seq=1,
                           title="他书细纲", chapter_id=other_chapter.json()["id"])
        db.add(plan)
        await db.flush()
        (await db.get(Chapter, other_chapter.json()["id"])).plan_id = plan.id
        corrupted = await db.get(Chapter, chapter["id"])
        corrupted.plan_id, corrupted.outline_id = plan.id, outline.json()["id"]
        await db.commit()
        plan_id = plan.id
    if path == "update":
        response = await editor.patch(chapter_url, json={"outline_id": None})
        assert response.status_code == 409, response.text
        async with app.state.sessionmaker() as db:
            assert (await db.get(ChapterPlan, plan_id)).chapter_id == other_chapter.json()["id"]
            row = await db.get(Chapter, chapter["id"])
            assert row.plan_id == plan_id and row.outline_id == outline.json()["id"]
    elif path == "epub":
        response = await viewer.get(f"{base}/export", params={"format": "epub"})
        assert response.status_code == 200, response.text
        with ZipFile(BytesIO(response.content)) as archive:
            texts = [archive.read(name).decode() for name in archive.namelist()
                     if name.endswith((".xhtml", ".ncx", ".opf"))]
        assert all(secret not in text for text in texts)
    elif path == "toc":
        response = await viewer.get(f"{base}/reader/toc")
        assert response.status_code == 200
        assert response.json()[0]["volume_title"] is None and response.json()[0]["volume_no"] is None
    else:
        urls = {"chapter": chapter_url, "list": f"{base}/chapters", "reader": f"{base}/reader/chapters/{chapter['id']}"}
        response = await viewer.get(urls[path])
        assert response.status_code == 200, response.text
        row = (response.json()[0] if path == "list"
               else response.json()["chapter"] if path == "reader" else response.json())
        assert row["plan_id"] is None and row["outline_id"] is None
