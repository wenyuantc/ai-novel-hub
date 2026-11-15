"""人工规划和资料入口按作品协作角色授权。"""

from tests.helpers import create_project, register


async def _collaborators(owner, make_client):
    await register(owner, "planning-owner")
    viewer = make_client()
    editor = make_client()
    stranger = make_client()
    users = {
        "viewer": await register(viewer, "planning-viewer"),
        "editor": await register(editor, "planning-editor"),
        "stranger": await register(stranger, "planning-stranger"),
    }
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    for role, account in (("viewer", viewer), ("editor", editor)):
        invited = await owner.post(f"{base}/collaborators", json={
            "username": f"planning-{role}", "role": role,
        })
        assert invited.status_code == 201, invited.text
        invitation = invited.json()
        accepted = await account.post(
            f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
            json={"expected_revision": invitation["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
    return base, viewer, editor, stranger, users


async def test_note_collaboration_roles_and_revocation(client, make_client):
    base, viewer, editor, stranger, users = await _collaborators(client, make_client)
    original = await client.post(f"{base}/notes", json={"title": "线索", "content": "作者笔记"})
    assert original.status_code == 201
    note = original.json()
    path = f"{base}/notes/{note['id']}"

    assert (await viewer.get(path)).json()["content"] == "作者笔记"
    assert (await viewer.get(f"{base}/notes")).json()["total"] == 1
    assert (await viewer.patch(path, json={"expected_revision": 1, "content": "越权"})).status_code == 403
    assert (await viewer.post(f"{base}/notes", json={"title": "越权"})).status_code == 403
    assert (await stranger.get(path)).status_code == 404
    assert (await stranger.patch(path, json={"expected_revision": 1, "content": "越权"})).status_code == 404

    edited = await editor.patch(path, json={"expected_revision": 1, "content": "协作者整理"})
    assert edited.status_code == 200, edited.text
    assert (await client.get(path)).json()["content"] == "协作者整理"
    second = await create_project(client, title="另一部作品")
    cross = f"/api/v1/projects/{second['id']}/notes/{note['id']}"
    assert (await client.get(cross)).status_code == 404
    assert (await editor.patch(cross, json={"expected_revision": 2, "content": "错书"})).status_code == 404

    changed = await client.patch(f"{base}/collaborators/{users['editor']['id']}", json={
        "expected_revision": 2, "role": "viewer",
    })
    assert changed.status_code == 200
    assert (await editor.get(path)).status_code == 200
    assert (await editor.patch(path, json={"expected_revision": 2, "content": "降权后写入"})).status_code == 403
    assert (await client.delete(f"{base}/collaborators/{users['editor']['id']}", params={
        "expected_revision": 3,
    })).status_code == 204
    assert (await editor.get(path)).status_code == 404


async def test_planning_roles_read_write_and_cross_project(client, make_client):
    base, viewer, editor, stranger, _ = await _collaborators(client, make_client)
    chapter = await client.post(f"{base}/chapters", json={"title": "第一章", "chapter_no": 1})
    assert chapter.status_code == 201, chapter.text
    matrix = await viewer.get(f"{base}/planning/matrix")
    assert matrix.status_code == 200, matrix.text
    revision = matrix.json()["revision"]
    assert matrix.json()["total"] == 1
    payload = {"expected_revision": revision, "name": "伏笔"}
    assert (await viewer.post(f"{base}/planning/tags", json=payload)).status_code == 403
    assert (await stranger.get(f"{base}/planning/matrix")).status_code == 404
    assert (await stranger.post(f"{base}/planning/tags", json=payload)).status_code == 404
    saved = await editor.post(f"{base}/planning/tags", json=payload)
    assert saved.status_code == 201, saved.text
    assert (await client.get(f"{base}/planning/tags")).json()["items"][0]["name"] == "伏笔"

    other = await create_project(client, title="另一本")
    foreign_chapter = await client.post(
        f"/api/v1/projects/{other['id']}/chapters", json={"title": "另一章", "chapter_no": 1},
    )
    assert foreign_chapter.status_code == 201
    bad = await editor.patch(f"{base}/planning/units/batch", json={
        "expected_revision": saved.json()["revision"],
        "targets": [{"kind": "chapter", "id": foreign_chapter.json()["id"]}],
        "pov": {"op": "clear"},
    })
    assert bad.status_code == 404


async def test_memory_and_knowledge_manual_access_has_no_owner_index_side_effect(
    app, client, make_client, fake_llm,
):
    from sqlalchemy import func, select

    from app.models import Task

    base, viewer, editor, stranger, _ = await _collaborators(client, make_client)
    created = await client.post(f"{base}/memories", json={"chapter_no": 1, "content": "山中密道"})
    assert created.status_code == 201, created.text
    memory_id = created.json()["id"]
    async with app.state.sessionmaker() as db:
        before_tasks = await db.scalar(select(func.count()).select_from(Task))
    assert (await viewer.get(f"{base}/memories")).json()["total"] == 1
    assert (await viewer.get(f"{base}/kb/settings")).status_code == 200
    assert (await viewer.patch(f"{base}/memories/{memory_id}", json={"content": "越权"})).status_code == 403
    assert (await viewer.patch(f"{base}/kb/settings", json={"top_k": 4})).status_code == 403
    assert (await stranger.get(f"{base}/memories")).status_code == 404
    updated = await editor.patch(f"{base}/memories/{memory_id}", json={"content": "山中暗道"})
    assert updated.status_code == 200, updated.text
    changed = await editor.patch(f"{base}/kb/settings", json={"top_k": 4})
    assert changed.status_code == 200, changed.text
    # 阶段 E6（phase-e-shared-index-keyword.md）：作者未授权共享向量时，编辑者只走关键词检索/上传，
    # 重建为 403 owner_embedding_not_allowed；均不建任务、不发向量请求（见下方断言）。
    for route in ("memories/search", "kb/search"):
        assert (await viewer.post(f"{base}/{route}", json={"query": "山"})).status_code == 403
        searched = await editor.post(f"{base}/{route}", json={"query": "山"})
        assert searched.status_code == 200 and searched.json()["mode"] == "keyword", searched.text
        assert (await stranger.post(f"{base}/{route}", json={"query": "山"})).status_code == 404
    files = {"file": ("notes.txt", b"Some knowledge", "text/plain")}
    assert (await viewer.post(f"{base}/kb/documents", files=files)).status_code == 403
    uploaded = await editor.post(f"{base}/kb/documents", files=files)
    assert uploaded.status_code == 202 and uploaded.json()["task"] is None, uploaded.text
    assert uploaded.json()["document"]["status"] == "ready_keyword_only"
    assert (await viewer.post(f"{base}/kb/rebuild")).status_code == 403
    rebuild = await editor.post(f"{base}/kb/rebuild")
    assert rebuild.status_code == 403 and rebuild.json()["code"] == "owner_embedding_not_allowed", rebuild.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Task)) == before_tasks
    assert not fake_llm.embed_calls


async def test_existing_knowledge_documents_are_project_scoped(app, client, make_client, fake_llm):
    from sqlalchemy import func, select

    from app.models import KbChunk, KbDocument, Task

    base, viewer, editor, stranger, _ = await _collaborators(client, make_client)
    project_id = int(base.rsplit("/", 1)[-1])
    foreign = await create_project(client, title="别书")
    async with app.state.sessionmaker() as db:
        docs = [KbDocument(project_id=pid, filename=name, sha256=sha * 64, status="ready",
                           text="资料正文", char_count=4, chunk_count=1)
                for pid, name, sha in ((project_id, "本书.txt", "a"), (foreign["id"], "别书.txt", "b"))]
        db.add_all(docs)
        await db.flush()
        db.add_all([KbChunk(document_id=doc.id, seq=1, content="资料正文") for doc in docs])
        ids = [doc.id for doc in docs]
        await db.commit()
        task_count = await db.scalar(select(func.count()).select_from(Task))
    assert [doc["id"] for doc in (await viewer.get(f"{base}/kb/documents")).json()] == [ids[0]]
    assert (await viewer.get(f"{base}/kb/documents/{ids[0]}/chunks")).json()["total"] == 1
    assert (await viewer.get(f"{base}/kb/documents/{ids[1]}/chunks")).status_code == 404
    assert (await stranger.get(f"{base}/kb/documents")).status_code == 404
    assert (await viewer.patch(f"{base}/kb/documents/{ids[0]}", json={"inject": False})).status_code == 403
    assert (await editor.patch(f"{base}/kb/documents/{ids[1]}", json={"inject": False})).status_code == 404
    assert (await editor.patch(f"{base}/kb/documents/{ids[0]}", json={"inject": False})).status_code == 200
    assert (await editor.delete(f"{base}/kb/documents/{ids[0]}")).status_code == 204
    async with app.state.sessionmaker() as db:
        assert await db.get(KbDocument, ids[1]) is not None
        assert await db.scalar(select(func.count()).select_from(Task)) == task_count
    assert not fake_llm.calls and not fake_llm.embed_calls


async def test_mentions_only_include_the_collaborators_own_chat(app, client, make_client):
    from app.models import AgentConversation, AgentMessage

    base, viewer, editor, stranger, users = await _collaborators(client, make_client)
    character = await client.post(f"{base}/characters", json={"name": "林远"})
    assert character.status_code == 201, character.text
    owner = (await client.get("/api/v1/auth/me")).json()
    project_id = int(base.rsplit("/", 1)[-1])
    async with app.state.sessionmaker() as db:
        conversations = [AgentConversation(user_id=actor, project_id=project_id, title=label) for actor, label in (
            (owner["id"], "作者"), (users["editor"]["id"], "编辑者"), (users["viewer"]["id"], "查看者"),
        )]
        db.add_all(conversations)
        await db.flush()
        messages = [AgentMessage(conversation_id=conversation.id, role="user", content=f"林远在{label}私聊")
                    for conversation, label in zip(conversations, ("作者", "编辑者", "查看者"), strict=True)]
        db.add_all(messages)
        await db.flush()
        message_ids = [message.id for message in messages]
        await db.commit()
    url = f"{base}/mentions"
    for actor, own_id, hidden_ids in ((editor, message_ids[1], (message_ids[0], message_ids[2])),
                                      (viewer, message_ids[2], (message_ids[0], message_ids[1]))):
        hits = await actor.get(url, params={"source_type": "chat"})
        assert hits.status_code == 200, hits.text
        assert {hit["source"]["id"] for hit in hits.json()["items"]} == {own_id}
        assert (await actor.get(f"{url}/sources/chat/{own_id}")).status_code == 200
        for hidden in hidden_ids:
            assert (await actor.get(f"{url}/sources/chat/{hidden}")).status_code == 404
    assert (await stranger.get(url)).status_code == 404


async def test_outline_manual_routes_roles_and_foreign_subresources(client, make_client):
    base, viewer, editor, stranger, _ = await _collaborators(client, make_client)
    outline = await client.post(f"{base}/outlines", json={"chapter_no": 1, "title": "开端"})
    assert outline.status_code == 201, outline.text
    outline_id = outline.json()["id"]
    plans_path = f"{base}/outlines/{outline_id}/plans"
    synced = await client.put(plans_path, json={"plans": [{"title": "细纲"}]})
    assert synced.status_code == 200, synced.text
    plan_id = synced.json()[0]["id"]
    assert (await viewer.get(f"{base}/outlines/tree")).status_code == 200
    assert (await viewer.get(plans_path)).json()[0]["title"] == "细纲"
    exported = await viewer.get(f"{base}/outlines/export")
    assert exported.status_code == 200
    assert (await viewer.post(f"{base}/outlines/import/preview", json={"data": exported.json()})).status_code == 200
    assert (await viewer.put(plans_path, json={"plans": []})).status_code == 403
    assert (await viewer.post(f"{base}/outline-snapshots", json={})).status_code == 403
    assert (await stranger.get(plans_path)).status_code == 404
    edited = await editor.patch(f"{plans_path}/{plan_id}", json={"title": "协作细纲"})
    assert edited.status_code == 200, edited.text
    snapshot = await editor.post(f"{base}/outline-snapshots", json={"note": "协作快照"})
    assert snapshot.status_code == 201, snapshot.text
    assert (await viewer.get(f"{base}/outline-snapshots/{snapshot.json()['id']}")).status_code == 200
    assert (await editor.post(f"{base}/outlines/append", json={"items": [{"title": "续纲"}]})).status_code == 201
    assert (await editor.put(plans_path, json={"plans": [{"id": plan_id, "title": "改细纲"}]})).status_code == 200
    other = await create_project(client, title="另一部作品")
    foreign = await client.post(f"/api/v1/projects/{other['id']}/outlines", json={"chapter_no": 1, "title": "别书"})
    assert foreign.status_code == 201
    assert (await editor.get(f"{base}/outlines/{foreign.json()['id']}/plans")).status_code == 404
    assert (await editor.post(f"{base}/outline-snapshots/{snapshot.json()['id'] + 999}/restore")).status_code == 404


async def test_planning_picker_hides_unshared_series_identity(client, make_client):
    from tests.test_series import _entry, _join, _new_series, _use

    base, viewer, _, _, _ = await _collaborators(client, make_client)
    project = {"id": int(base.rsplit("/", 1)[-1])}
    series = await _join(client, await _new_series(client), project)
    series_url = f"/api/v1/series/{series['id']}"
    entry = await _entry(client, series_url, series["revision"], "character", name="系列角色")
    adopted = await _use(client, f"{series_url}/entries/{entry['item']['id']}", entry["revision"], project["id"])
    params = {"kind": "character", "q": "系列角色"}
    owner = await client.get(f"{base}/planning/entries", params=params)
    assert owner.json()["items"][0]["series_shared"] is True
    single = await viewer.get(f"{base}/planning/entries", params=params)
    assert single.status_code == 200, single.text
    assert single.json()["items"][0]["reference"]["id"] == adopted["ref"]["id"]
    assert single.json()["items"][0]["series_shared"] is False
    invite = await client.post(f"{series_url}/collaborators", json={"username": "planning-viewer"})
    assert invite.status_code == 201, invite.text
    invitation = invite.json()
    accepted = await viewer.post(
        f"/api/v1/collaboration/invitations/series/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert (await viewer.get(f"{base}/planning/entries", params=params)).json()["items"][0]["series_shared"] is True


async def test_analysis_reads_hide_other_actors_tasks_and_manual_writeback(app, client, make_client, fake_llm):
    from sqlalchemy import func, select

    from app.models import AnalysisChange, ChapterAnalysis, ProjectCollaborator, Task

    base, viewer, editor, stranger, users = await _collaborators(client, make_client)
    owner = (await client.get("/api/v1/auth/me")).json()
    project_id = int(base.rsplit("/", 1)[-1])
    chapter = await client.post(f"{base}/chapters", json={"title": "开篇", "chapter_no": 1})
    assert chapter.status_code == 201
    chapter_id = chapter.json()["id"]
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == project_id, ProjectCollaborator.user_id == users["editor"]["id"],
        ))
        grant = {"invitation_id": member.invitation_id, "revision": member.revision}
        tasks = [
            Task(user_id=owner["id"], project_id=project_id, chapter_id=chapter_id,
                 kind="chapter.analyze", status="queued"),
            Task(user_id=users["editor"]["id"], project_id=project_id, chapter_id=chapter_id,
                 kind="chapter.analyze", status="queued", input={"_collaboration_grant": grant}),
        ]
        db.add_all(tasks)
        await db.flush()
        analyses = [ChapterAnalysis(project_id=project_id, chapter_id=chapter_id, chapter_no=1,
                                   content_hash="0" * 64, task_id=task.id, summary="作品分析") for task in tasks]
        db.add_all(analyses)
        await db.flush()
        changes = [AnalysisChange(project_id=project_id, analysis_id=analyses[0].id, chapter_no=1,
                                  kind="foreshadow_new", status="pending", after={"title": title})
                   for title in ("新伏笔", "拒绝的伏笔")]
        db.add_all(changes)
        await db.flush()
        task_ids = [task.id for task in tasks]
        analysis_ids = [analysis.id for analysis in analyses]
        change_ids = [change.id for change in changes]
        await db.commit()
    history_path = f"/api/v1/chapters/{chapter_id}/analyses"
    for actor, expected in ((client, {task_ids[0]}), (editor, {task_ids[1]}), (viewer, set())):
        history = await actor.get(history_path)
        assert history.status_code == 200, history.text
        assert {row["task_id"] for row in history.json() if row["task_id"] is not None} == expected
        for index, analysis_id in enumerate(analysis_ids):
            detail = await actor.get(f"/api/v1/analyses/{analysis_id}")
            assert detail.status_code == 200, detail.text
            assert detail.json()["task_id"] == (task_ids[index] if task_ids[index] in expected else None)
        status = await actor.get(f"{base}/analysis/statuses")
        assert status.status_code == 200, status.text
        assert (status.json()[0]["status"] == "analyzing") is bool(expected)
        active_task_id = status.json()[0]["active_task_id"]
        assert active_task_id in expected if expected else active_task_id is None
    assert (await stranger.get(history_path)).status_code == 404
    assert (await stranger.get(f"/api/v1/analyses/{analysis_ids[0]}")).status_code == 404
    # E4-g1 起分析是协作 AI 入口：viewer 缺 ai.use → 403；非成员 → 404；editor 本人无模型且未获作者授权 → 400。
    # 三者都不建任务（端到端开放见 test_collaboration_analysis_entry_e4g.py）。
    for actor, expected in ((viewer, 403), (editor, 400), (stranger, 404)):
        response = await actor.post(f"/api/v1/chapters/{chapter_id}/analyze", json={})
        assert response.status_code == expected, response.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == len(task_ids)
    assert (await viewer.post(f"/api/v1/analysis-changes/{change_ids[0]}/confirm")).status_code == 403
    assert (await stranger.post(f"/api/v1/analysis-changes/{change_ids[0]}/reject")).status_code == 404
    confirmed = await editor.post(f"/api/v1/analysis-changes/{change_ids[0]}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert (await editor.post(f"/api/v1/analysis-changes/{change_ids[0]}/revert")).status_code == 200
    rejected = await editor.post(f"/api/v1/analysis-changes/{change_ids[1]}/reject")
    assert rejected.status_code == 200, rejected.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Task)) == len(task_ids)
    assert not fake_llm.calls and not fake_llm.embed_calls


async def test_analysis_rejects_inconsistent_project_children(app, client, make_client):
    from app.models import AnalysisChange, ChapterAnalysis

    base, viewer, editor, _, _ = await _collaborators(client, make_client)
    project_id = int(base.rsplit("/", 1)[-1])
    other = await create_project(client, title="另一部作品")
    foreign = await client.post(f"/api/v1/projects/{other['id']}/chapters", json={
        "title": "别书章节", "chapter_no": 1,
    })
    assert foreign.status_code == 201
    async with app.state.sessionmaker() as db:
        inconsistent = ChapterAnalysis(project_id=project_id, chapter_id=foreign.json()["id"], chapter_no=1,
                                       content_hash="0" * 64)
        foreign_analysis = ChapterAnalysis(project_id=other["id"], chapter_id=foreign.json()["id"], chapter_no=1,
                                          content_hash="0" * 64)
        db.add_all([inconsistent, foreign_analysis])
        await db.flush()
        inconsistent_change = AnalysisChange(project_id=project_id, analysis_id=foreign_analysis.id, chapter_no=1,
                                             kind="foreshadow_new", status="pending", after={"title": "越界"})
        db.add(inconsistent_change)
        await db.flush()
        analysis_id, change_id = inconsistent.id, inconsistent_change.id
        await db.commit()
    assert (await viewer.get(f"/api/v1/analyses/{analysis_id}")).status_code == 404
    assert (await editor.post(f"/api/v1/analysis-changes/{change_id}/confirm")).status_code == 404


async def test_admin_and_collaborator_use_combined_manual_capabilities(app, client, make_client, system_admin):
    from app.models import AnalysisChange, ChapterAnalysis, Task
    from tests.rbac_helpers import assign_roles, create_role
    from tests.test_series import _entry, _join, _new_series, _use

    base, viewer, _, stranger, users = await _collaborators(client, make_client)
    project = {"id": int(base.rsplit("/", 1)[-1])}
    series = await _join(client, await _new_series(client), project)
    series_url = f"/api/v1/series/{series['id']}"
    entry = await _entry(client, series_url, series["revision"], "character", name="管理员可见")
    await _use(client, f"{series_url}/entries/{entry['item']['id']}", entry["revision"], project["id"])
    params = {"kind": "character", "q": "管理员可见"}
    assert (await viewer.get(f"{base}/planning/entries", params=params)).json()["items"][0]["series_shared"] is False
    role = await create_role(system_admin, "人工作品管理", ["projects.read", "projects.edit"])
    await assign_roles(system_admin, users["viewer"]["id"], [role["id"]])
    assert (await viewer.get(f"{base}/planning/entries", params=params)).json()["items"][0]["series_shared"] is True
    saved = await viewer.post(f"{base}/notes", json={"title": "管理员能力与协作并集"})
    assert saved.status_code == 201, saved.text
    assert (await viewer.get(series_url)).status_code == 404
    await assign_roles(system_admin, users["stranger"]["id"], [role["id"]])
    chapter = await client.post(f"{base}/chapters", json={"title": "章节", "chapter_no": 1})
    assert chapter.status_code == 201
    async with app.state.sessionmaker() as db:
        tasks = [Task(user_id=actor_id, project_id=project["id"], chapter_id=chapter.json()["id"],
                      kind="chapter.analyze", status=task_status)
                 for actor_id, task_status in ((users["viewer"]["id"], "succeeded"),
                                               (users["stranger"]["id"], "queued"))]
        db.add_all(tasks)
        await db.flush()
        analyses = [ChapterAnalysis(project_id=project["id"], chapter_id=chapter.json()["id"], chapter_no=1,
                                   content_hash="0" * 64, task_id=task.id) for task in tasks]
        db.add_all(analyses)
        await db.flush()
        change = AnalysisChange(project_id=project["id"], analysis_id=analyses[0].id, chapter_no=1,
                                kind="foreshadow_new", status="pending", after={"title": "并集伏笔"})
        db.add(change)
        await db.flush()
        change_id = change.id
        identities = [(task.id, analysis.id) for task, analysis in zip(tasks, analyses, strict=True)]
        await db.commit()
    for actor, task_id, analysis_id, visible in (
        (viewer, *identities[0], True), (stranger, *identities[1], False),
    ):
        detail = await actor.get(f"/api/v1/analyses/{analysis_id}")
        if visible:
            assert detail.status_code == 200, detail.text
            assert detail.json()["task_id"] == task_id
        else:
            assert detail.status_code == 404
    assert (await stranger.post(f"/api/v1/analysis-changes/{change_id}/confirm")).status_code == 404
    assert (await viewer.post(f"/api/v1/analysis-changes/{change_id}/confirm")).status_code == 200


async def test_shared_agent_services_remain_author_only(app, client, make_client):
    import pytest

    from app.core.errors import NotFoundError
    from app.schemas.outline import PlansSync
    from app.services import analysis_service, outline_service, plan_service

    base, _, editor, _, users = await _collaborators(client, make_client)
    project_id = int(base.rsplit("/", 1)[-1])
    outline = await client.post(f"{base}/outlines", json={"chapter_no": 1, "title": "纲"})
    assert outline.status_code == 201
    async with app.state.sessionmaker() as db:
        with pytest.raises(NotFoundError):
            await outline_service.outline_tree(db, users["editor"]["id"], project_id)
        with pytest.raises(NotFoundError):
            await plan_service.sync_plans(
                db, users["editor"]["id"], project_id, outline.json()["id"], PlansSync(plans=[]),
            )
        with pytest.raises(NotFoundError):
            await analysis_service.project_states(db, users["editor"]["id"], project_id)
    assert (await editor.get(f"{base}/outlines/tree")).status_code == 200


async def test_corrupt_plan_cannot_read_or_delete_another_project_chapter(app, client, make_client):
    from app.models import ChapterPlan

    base, viewer, editor, _, _ = await _collaborators(client, make_client)
    outline = await client.post(f"{base}/outlines", json={"chapter_no": 1, "title": "纲"})
    path = f"{base}/outlines/{outline.json()['id']}/plans"
    plan = await client.put(path, json={"plans": [{"title": "细纲"}]})
    assert plan.status_code == 200, plan.text
    foreign = await create_project(client, title="另一部作品")
    chapter = await client.post(f"/api/v1/projects/{foreign['id']}/chapters", json={
        "title": "别书章节", "chapter_no": 6, "content": "不应暴露的正文",
    })
    assert chapter.status_code == 201
    async with app.state.sessionmaker() as db:
        row = await db.get(ChapterPlan, plan.json()[0]["id"])
        row.chapter_id = chapter.json()["id"]
        await db.commit()
    visible = await viewer.get(path)
    assert visible.status_code == 200
    assert visible.json()[0]["chapter_id"] is None
    assert visible.json()[0]["chapter_no"] is None
    assert (await editor.put(path, json={"plans": []})).status_code == 409
    assert (await client.get(f"/api/v1/projects/{foreign['id']}/chapters/{chapter.json()['id']}")).status_code == 200


async def test_planning_matrix_and_writes_exclude_foreign_plan_links(app, client, make_client):
    from app.models import Chapter, ChapterPlan
    from app.models.planning import PlanningUnit

    base, viewer, editor, _, _ = await _collaborators(client, make_client)
    foreign = await create_project(client, title="另一本")
    foreign_base = f"/api/v1/projects/{foreign['id']}"
    local_outline = await client.post(f"{base}/outlines", json={"chapter_no": 1, "title": "本书大纲"})
    foreign_outline = await client.post(f"{foreign_base}/outlines", json={"chapter_no": 1, "title": "别书大纲"})
    local_chapter = await client.post(f"{base}/chapters", json={"chapter_no": 1, "title": "本书章节"})
    assert local_outline.status_code == foreign_outline.status_code == local_chapter.status_code == 201
    async with app.state.sessionmaker() as db:
        attached_foreign = ChapterPlan(project_id=foreign["id"], outline_id=local_outline.json()["id"], seq=1,
                                       title="不得泄露的别书细纲", character_ids=[123])
        bound_foreign = ChapterPlan(project_id=foreign["id"], outline_id=local_outline.json()["id"], seq=2,
                                    chapter_id=local_chapter.json()["id"], title="别书已绑定细纲")
        db.add_all([attached_foreign, bound_foreign])
        await db.flush()
        chapter = await db.get(Chapter, local_chapter.json()["id"])
        chapter.outline_id = foreign_outline.json()["id"]
        chapter.plan_id = bound_foreign.id
        attached_id, bound_id = attached_foreign.id, bound_foreign.id
        await db.commit()

    matrix = await viewer.get(f"{base}/planning/matrix")
    assert matrix.status_code == 200, matrix.text
    rows = matrix.json()["rows"]
    assert all(row["title"] != "不得泄露的别书细纲" for row in rows)
    row = next(row for row in rows if row["chapter_id"] == local_chapter.json()["id"])
    assert row["plan_id"] is None and row["outline_id"] is None
    changed = await editor.patch(f"{base}/planning/units/batch", json={
        "expected_revision": matrix.json()["revision"],
        "targets": [{"kind": "plan", "id": attached_id}],
        "references": [{"kind": "character", "op": "clear"}],
    })
    assert changed.status_code == 404, changed.text
    chapter_change = await editor.patch(f"{base}/planning/units/batch", json={
        "expected_revision": matrix.json()["revision"],
        "targets": [{"kind": "chapter", "id": local_chapter.json()["id"]}],
        "pov": {"op": "clear"},
    })
    assert chapter_change.status_code == 200, chapter_change.text
    async with app.state.sessionmaker() as db:
        from sqlalchemy import select

        assert (await db.get(ChapterPlan, attached_id)).character_ids == [123]
        assert (await db.get(ChapterPlan, bound_id)).title == "别书已绑定细纲"
        unit = await db.scalar(select(PlanningUnit).where(PlanningUnit.chapter_id == local_chapter.json()["id"]))
        assert unit is not None and unit.plan_id is None


async def test_chapter_plan_hides_plan_with_foreign_outline(app, client, make_client):
    from app.models import Chapter, ChapterPlan

    base, viewer, _, _, _ = await _collaborators(client, make_client)
    project_id = int(base.rsplit("/", 1)[-1])
    foreign = await create_project(client, title="另一本")
    foreign_outline = await client.post(f"/api/v1/projects/{foreign['id']}/outlines", json={"title": "别书大纲"})
    chapter = await client.post(f"{base}/chapters", json={"title": "本书章节", "chapter_no": 1})
    assert foreign_outline.status_code == chapter.status_code == 201
    async with app.state.sessionmaker() as db:
        plan = ChapterPlan(project_id=project_id, outline_id=foreign_outline.json()["id"], seq=1,
                           title="异常细纲", chapter_id=chapter.json()["id"])
        db.add(plan)
        await db.flush()
        (await db.get(Chapter, chapter.json()["id"])).plan_id = plan.id
        await db.commit()
    result = await viewer.get(f"{base}/chapters/{chapter.json()['id']}/plan")
    assert result.status_code == 200, result.text
    assert result.json() is None
