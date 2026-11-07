"""管理员项目内容权限：真实身份、人工维护、隔离与模型副作用。"""

import pytest
from sqlalchemy import func, select

from app.models import Chapter, Project, RetrievalChunk, Task, User
from tests.cover_fixtures import PNG
from tests.helpers import create_ai_config, create_project, register, wait_task

PROJECTS = "/api/v1/projects"
ADMIN_PROJECTS = "/api/v1/admin/projects"


@pytest.fixture
async def admin_client(app, make_client):
    client = make_client()
    user = await register(client, "manager")
    async with app.state.sessionmaker() as db:
        record = await db.get(User, user["id"])
        record.is_admin = True
        await db.commit()
    return client


async def test_admin_project_list_auth_and_filters(admin_client, client, make_client):
    assert (await client.get(ADMIN_PROJECTS)).status_code == 401
    alice = await register(client, "alice")
    assert (await client.get(ADMIN_PROJECTS)).status_code == 403
    first = await create_project(client, title="字面%_\\作品")
    await client.post(f"{PROJECTS}/{first['id']}/chapters", json={"title": "正文", "content": "甲 乙\n丙"})
    bob = make_client()
    await register(bob, "bob")
    second = await create_project(bob, title="另一部作品")
    await bob.patch(f"{PROJECTS}/{second['id']}", json={"status": "writing"})
    resp = await admin_client.get(ADMIN_PROJECTS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    rows = {item["id"]: item for item in body["items"]}
    assert rows[first["id"]]["owner_user_id"] == alice["id"]
    assert rows[first["id"]]["owner_username"] == "alice"
    assert (rows[first["id"]]["chapter_count"], rows[first["id"]]["total_words"]) == (1, 3)
    assert (rows[second["id"]]["chapter_count"], rows[second["id"]]["total_words"]) == (0, 0)
    for query, expected in [({"q": "%_\\"}, first), ({"q": "ALICE"}, first), ({"status": "writing"}, second)]:
        result = (await admin_client.get(ADMIN_PROJECTS, params=query)).json()
        assert result["total"] == 1 and result["items"][0]["id"] == expected["id"]
    combined = await admin_client.get(ADMIN_PROJECTS, params={"owner_user_id": alice["id"], "status": "writing"})
    assert combined.json() == {"items": [], "total": 0}
    page = (await admin_client.get(ADMIN_PROJECTS, params={"page_size": 1, "page": 2})).json()
    assert page["total"] == 2 and len(page["items"]) == 1
    assert (await admin_client.get(ADMIN_PROJECTS, params={"page_size": 101})).status_code == 422


async def test_admin_edits_content_owner_sees_result_and_other_user_isolated(admin_client, client, make_client):
    alice = await register(client, "alice")
    project = await create_project(client)
    base = f"{PROJECTS}/{project['id']}"
    chapter = (await client.post(f"{base}/chapters", json={"title": "原章节", "content": "原文"})).json()
    await client.put(f"{base}/reading-progress", json={"chapter_id": chapter["id"], "ratio": 0.7})
    resp = await admin_client.get(base)
    assert resp.status_code == 200, resp.text
    assert resp.json()["owner"] == {"id": alice["id"], "username": "alice"}
    assert resp.json()["reading"] is None
    resp = await admin_client.patch(base, json={"title": "管理后书名", "status": "writing"})
    assert resp.status_code == 200 and resp.json()["owner"]["id"] == alice["id"]
    resp = await admin_client.patch(
        f"{base}/chapters/{chapter['id']}", json={"title": "新标题", "content": "甲 乙\n丙丁"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["word_count"] == 4
    world = await admin_client.post(f"{base}/world", json={"title": "管理设定", "content": "新设定"})
    assert world.status_code == 201, world.text
    assert (await client.get(base)).json()["title"] == "管理后书名"
    assert (await client.get(f"{base}/chapters/{chapter['id']}")).json()["content"] == "甲 乙\n丙丁"
    assert (await client.get(f"{base}/world")).json()[0]["title"] == "管理设定"
    assert (await admin_client.get(PROJECTS)).json() == []
    bob = make_client()
    await register(bob, "bob")
    assert (await bob.get(base)).status_code == 404
    assert (await bob.patch(f"{base}/chapters/{chapter['id']}", json={"content": "越权"})).status_code == 404


async def test_admin_demotion_immediately_revokes_content_access(app, admin_client, client):
    await register(client, "alice")
    project = await create_project(client)
    base = f"{PROJECTS}/{project['id']}"
    assert (await admin_client.get(base)).status_code == 200
    async with app.state.sessionmaker() as db:
        manager = await db.scalar(select(User).where(User.username == "manager"))
        manager.is_admin = False
        await db.commit()
    assert (await admin_client.get(base)).status_code == 404
    assert (await admin_client.patch(base, json={"title": "越权"})).status_code == 404
    assert (await admin_client.get(ADMIN_PROJECTS)).status_code == 403


async def _json(client, method, path, body=None, *, status=200):
    response = await client.request(method, path, json=body)
    assert response.status_code == status, (method, path, response.text)
    return response.json() if status != 204 else None


async def test_admin_manual_setting_modules_and_versions(admin_client, client):
    await register(client, "alice")
    project = await create_project(client)
    base = f"{PROJECTS}/{project['id']}"
    a = await _json(admin_client, "POST", f"{base}/characters", {"name": "甲"}, status=201)
    b = await _json(admin_client, "POST", f"{base}/characters", {"name": "乙"}, status=201)
    await _json(admin_client, "PATCH", f"{base}/characters/{a['id']}", {"name": "甲改"})
    org = await _json(admin_client, "POST", f"{base}/organizations", {"name": "天剑宗"}, status=201)
    member = await _json(
        admin_client, "POST", f"{base}/organizations/{org['id']}/members", {"character_id": a["id"]}, status=201
    )
    changed_member = await _json(
        admin_client, "PATCH", f"{base}/organizations/{org['id']}/members/{member['id']}", {"position": "掌门"}
    )
    assert changed_member["position"] == "掌门"
    rel = await _json(
        admin_client, "POST", f"{base}/relationships", {"a_id": a["id"], "b_id": b["id"], "role_a": "朋友"}, status=201
    )
    await _json(admin_client, "PATCH", f"{base}/relationships/{rel['id']}", {"intimacy": 80})
    career = await _json(
        admin_client, "POST", f"{base}/careers", {"name": "剑修", "stages": [{"name": "练气"}]}, status=201
    )
    await _json(
        admin_client, "POST", f"{base}/characters/{a['id']}/careers", {"career_id": career["id"]}, status=201
    )
    outline = await _json(admin_client, "POST", f"{base}/outlines", {"title": "第一卷"}, status=201)
    plans = await _json(
        admin_client, "PUT", f"{base}/outlines/{outline['id']}/plans", {"plans": [{"title": "细纲"}]}
    )
    await _json(
        admin_client, "PATCH", f"{base}/outlines/{outline['id']}/plans/{plans[0]['id']}", {"summary": "人工补充"}
    )
    built = await _json(admin_client, "POST", f"{base}/outlines/{outline['id']}/plans/build")
    assert len(built["chapters"]) == 1
    chapter = (await client.get(f"{base}/chapters")).json()[0]
    await _json(admin_client, "PUT", f"{base}/chapters/{chapter['id']}/cast", {"character_ids": [a["id"]]})
    await _json(admin_client, "PATCH", f"{base}/chapters/{chapter['id']}", {"content": "管理员正文"})
    versions = await _json(admin_client, "GET", f"{base}/chapters/{chapter['id']}/versions")
    version = await _json(admin_client, "GET", f"{base}/chapters/{chapter['id']}/versions/{versions[0]['id']}")
    assert version["content"] == "管理员正文"
    current = await _json(admin_client, "GET", f"{base}/chapters/{chapter['id']}")
    restored = await _json(
        admin_client, "POST", f"{base}/chapters/{chapter['id']}/versions/{versions[0]['id']}/restore",
        {"expected_content_revision": current["content_revision"]},
    )
    assert restored["content"] == "管理员正文"
    fs = await _json(
        admin_client, "POST", f"{base}/foreshadows", {"title": "玉佩", "character_ids": [a["id"]]}, status=201
    )
    await _json(admin_client, "POST", f"{base}/foreshadows/{fs['id']}/plant", {"chapter_no": 1})
    snapshot = await _json(admin_client, "POST", f"{base}/outline-snapshots", {}, status=201)
    await _json(admin_client, "GET", f"{base}/outline-snapshots/{snapshot['id']}")
    for path in [
        "characters", "organizations", f"organizations/{org['id']}/members",
        f"characters/{a['id']}/memberships", "relationships", f"relationships/{rel['id']}/history",
        "relationship-graph", "careers", f"careers/{career['id']}/holders", "character-careers",
        f"characters/{a['id']}/careers", f"characters/{a['id']}/career-history", "outlines/tree",
        f"chapters/{chapter['id']}/plan", f"chapters/{chapter['id']}/foreshadow-reminders",
        "foreshadows/stats", f"foreshadows/{fs['id']}/history", "reader/toc",
    ]:
        await _json(admin_client, "GET", f"{base}/{path}")
    cover = await admin_client.put(f"{base}/cover", files={"file": ("cover.png", PNG)})
    assert cover.status_code == 200 and cover.json()["owner"]["username"] == "alice"
    assert (await admin_client.get(f"{base}/cover")).content == PNG
    assert (await admin_client.delete(f"{base}/cover")).status_code == 200
    assert (await client.get(f"{base}/characters/{a['id']}")).json()["name"] == "甲改"
    assert (await client.get(f"{base}/foreshadows/{fs['id']}")).json()["status"] == "planted"


async def test_admin_memory_and_kb_edits_never_enqueue_owner_models(app, admin_client, client, fake_llm):
    await register(client, "alice")
    project = await create_project(client)
    base = f"{PROJECTS}/{project['id']}"
    upload = await client.post(f"{base}/kb/documents", files={"file": ("设定.md", "# 天剑宗\n\n山中门派".encode())})
    assert upload.status_code == 202, upload.text
    document = upload.json()["document"]
    await wait_task(client, upload.json()["task"]["id"])
    await create_ai_config(client, model_type="embedding")
    async with app.state.sessionmaker() as db:
        before = await db.scalar(select(func.count(Task.id)))
    memory = await _json(
        admin_client, "POST", f"{base}/memories", {"chapter_no": 1, "content": "管理员手动记忆"}, status=201
    )
    await _json(admin_client, "PATCH", f"{base}/memories/{memory['id']}", {"content": "人工修订", "pinned": True})
    memories = await _json(admin_client, "GET", f"{base}/memories")
    assert memories["items"][0]["content"] == "人工修订"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id))) == before
        chunk = await db.scalar(select(RetrievalChunk).where(RetrievalChunk.source == "memory"))
        assert "人工修订" in chunk.text
    assert not fake_llm.calls and not fake_llm.embed_calls and not fake_llm.image_calls
    documents = await _json(admin_client, "GET", f"{base}/kb/documents")
    assert documents[0]["id"] == document["id"]
    chunks = await _json(admin_client, "GET", f"{base}/kb/documents/{document['id']}/chunks")
    assert chunks["total"] > 0
    changed = await _json(admin_client, "PATCH", f"{base}/kb/documents/{document['id']}", {"inject": False})
    assert changed["inject"] is False
    await _json(admin_client, "GET", f"{base}/kb/settings")
    settings = await _json(admin_client, "PATCH", f"{base}/kb/settings", {"top_k": 3})
    assert settings["top_k"] == 3
    assert (await admin_client.post(f"{base}/kb/documents/{document['id']}/reindex")).status_code == 404
    await _json(admin_client, "DELETE", f"{base}/kb/documents/{document['id']}", status=204)
    await _json(admin_client, "DELETE", f"{base}/memories/{memory['id']}", status=204)


async def test_admin_denied_ai_private_resources_and_progress(admin_client, client, fake_llm):
    await register(client, "alice")
    project = await create_project(client)
    base = f"{PROJECTS}/{project['id']}"
    chapter = await _json(client, "POST", f"{base}/chapters", {"title": "第一章", "content": "正文"}, status=201)
    config = await create_ai_config(client)
    for path in [
        f"/api/v1/ai-configs/{config['id']}", f"/api/v1/styles/{project['default_style_id']}",
        f"{base}/export", f"{base}/backup", f"{base}/outlines/export", f"{base}/cover/prompt",
        f"{base}/chapters/{chapter['id']}/generation-logs", f"/api/v1/chapters/{chapter['id']}/analysis-status",
    ]:
        assert (await admin_client.get(path)).status_code == 404, path
    requests = [
        (f"/api/v1/chapters/{chapter['id']}/generate", {}),
        (f"/api/v1/chapters/{chapter['id']}/polish", {}),
        (f"/api/v1/chapters/{chapter['id']}/analyze", {}),
        (f"/api/v1/chapters/{chapter['id']}/revise", {"analysis_id": 1, "instruction": "修改"}),
        (f"/api/v1/chapters/{chapter['id']}/rewrite", {"op": "rewrite", "start": 0, "end": 1}),
        (f"{base}/wizard/outline", {}), (f"{base}/characters/generate", {}),
        (f"{base}/organizations/generate", {}), (f"{base}/careers/generate", {}),
        (f"{base}/outlines/continue", {}), (f"{base}/cover/generate", {"prompt": "封面"}),
        (f"{base}/batch-generate", {"start_chapter_id": chapter["id"], "end_chapter_id": chapter["id"]}),
        (f"{base}/kb/rebuild", {}), (f"{base}/kb/search", {"query": "正文"}),
        (f"{base}/memories/search", {"query": "正文"}),
    ]
    for path, body in requests:
        response = await admin_client.post(path, json=body)
        assert response.status_code == 404, (path, response.text)
    assert (await admin_client.put(
        f"{base}/reading-progress", json={"chapter_id": chapter["id"], "ratio": 0.5}
    )).status_code == 404
    assert not fake_llm.calls and not fake_llm.embed_calls and not fake_llm.image_calls


async def test_admin_style_references_and_generating_lock_preserved(app, admin_client, client):
    await register(client, "alice")
    project = await create_project(client)
    base = f"{PROJECTS}/{project['id']}"
    chapter = await _json(client, "POST", f"{base}/chapters", {"content": "原始正文"}, status=201)
    for body in [{"default_style_id": None}, {"style": "新风格"}, {"auto_analyze": False}]:
        assert (await admin_client.patch(base, json=body)).status_code == 403
    assert (await admin_client.patch(f"{base}/chapters/{chapter['id']}", json={"style_id": None})).status_code == 403
    await _json(admin_client, "PATCH", base, {"title": "只改标题"})
    assert (await client.get(base)).json()["default_style_id"] == project["default_style_id"]
    async with app.state.sessionmaker() as db:
        row = await db.get(Chapter, chapter["id"])
        row.status = "generating"
        await db.commit()
    for body in [{"content": "禁止覆盖"}, {"status": "draft"}]:
        response = await admin_client.patch(f"{base}/chapters/{chapter['id']}", json=body)
        assert response.status_code == 409
    assert (await client.get(f"{base}/chapters/{chapter['id']}")).json()["content"] == "原始正文"


async def test_admin_subresources_stay_in_project_and_delete_only_target(app, admin_client, client, make_client):
    alice = await register(client, "alice")
    first = await create_project(client)
    first_base = f"{PROJECTS}/{first['id']}"
    chapter = await _json(client, "POST", f"{first_base}/chapters", {"content": "正文"}, status=201)
    char = await _json(client, "POST", f"{first_base}/characters", {"name": "甲"}, status=201)
    bob = make_client()
    await register(bob, "bob")
    second = await create_project(bob)
    second_base = f"{PROJECTS}/{second['id']}"
    assert (await admin_client.get(f"{second_base}/chapters/{chapter['id']}")).status_code == 404
    assert (await admin_client.get(f"{second_base}/characters/{char['id']}")).status_code == 404
    assert (await admin_client.get(f"{PROJECTS}/999999")).status_code == 404
    assert (await admin_client.delete(f"{PROJECTS}/999999")).status_code == 404
    await _json(admin_client, "DELETE", first_base, status=204)
    assert (await client.get(first_base)).status_code == 404
    assert (await bob.get(second_base)).status_code == 200
    async with app.state.sessionmaker() as db:
        assert await db.get(User, alice["id"]) is not None
        assert (await db.get(Project, first["id"])).deleted_at is not None
        assert await db.get(Chapter, chapter["id"]) is not None
    item = (await admin_client.get("/api/v1/admin/recycle-bin/projects")).json()["items"][0]
    assert (await admin_client.post(
        f"/api/v1/admin/recycle-bin/projects/{first['id']}/purge",
        json={"deleted_at": item["deleted_at"], "title": item["title"], "confirm": True},
    )).status_code == 204
    async with app.state.sessionmaker() as db:
        assert await db.get(Project, first["id"]) is None
        assert await db.get(Chapter, chapter["id"]) is None
        assert await db.get(Project, second["id"]) is not None
