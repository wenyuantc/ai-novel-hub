import hashlib

import pytest
from sqlalchemy import delete, select, update

from app.models import AgentConversation, AgentMessage, Chapter, KbChunk, KbDocument, User, WorldSetting
from tests.helpers import create_ai_config, create_project, register, wait_task


async def seeded(client, app):
    user = await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    character = (await client.post(f"{base}/characters", json={"name": "林远", "aliases": ["小林", "小白"]})).json()
    other = (await client.post(f"{base}/characters", json={"name": "白虎", "aliases": ["小白"]})).json()
    place = (
        await client.post(f"{base}/world", json={"title": "青云城", "category": "place", "aliases": ["云城"]})
    ).json()
    item = (
        await client.post(f"{base}/world", json={"title": "青锋剑", "category": "item", "aliases": ["旧剑"]})
    ).json()
    await client.post(f"{base}/world", json={"title": "规则", "category": "rule"})
    chapters = []
    for number, content in enumerate(
        ["🙂林远在青云城拿起青锋剑。小林与小白。\r\n", "这章没人。", "", "林远重回。", "仅有风声"], 1
    ):
        response = await client.post(
            f"{base}/chapters", json={"title": f"第{number}章", "chapter_no": number, "content": content}
        )
        assert response.status_code == 201, response.text
        chapters.append(response.json())
    note_response = await client.post(f"{base}/notes", json={"title": "设想", "content": "林远写笔记"})
    assert note_response.status_code == 201, note_response.text
    async with app.state.sessionmaker() as db:
        conversation = AgentConversation(user_id=user["id"], project_id=project["id"], title="私有讨论")
        document = KbDocument(
            project_id=project["id"], filename="资料.txt", text="林远在资料中。", sha256="a" * 64, inject=False
        )
        db.add_all([conversation, document])
        await db.flush()
        messages = [
            AgentMessage(conversation_id=conversation.id, role=role, content=text)
            for role, text in [("user", "问林远"), ("assistant", "**林远**"), ("tool", '{"content":"林远林远"}')]
        ]
        db.add_all(messages)
        for seq in (1, 2):
            db.add(
                KbChunk(
                    document_id=document.id,
                    seq=seq,
                    content=document.text,
                    start_offset=0,
                    end_offset=len(document.text),
                )
            )
        await db.commit()
    return dict(
        project=project,
        base=base,
        character=character,
        other=other,
        place=place,
        item=item,
        chapters=chapters,
        conversation=conversation,
        messages=messages,
        document=document,
        note=note_response.json(),
    )


async def get_json(client, url, **params):
    response = await client.get(url, params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def test_catalog_preview_four_sources_and_complete_pagination(client, app, fake_llm):
    data = await seeded(client, app)
    url = f"{data['base']}/mentions"
    catalog = await get_json(client, f"{url}/catalog")
    assert catalog["available_source_types"] == ["chapter", "chat", "note", "knowledge"]
    assert {e["category"] for e in catalog["entities"]} == {"character", "place", "item", "rule"}
    preview = await client.post(f"{url}/preview", json={"text": "🙂云城 旧剑 小白"})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert [(s["text"], s["start"], s["end"]) for s in body["spans"]] == [
        ("云城", 1, 3),
        ("旧剑", 4, 6),
        ("小白", 7, 9),
    ]
    assert len(body["spans"][-1]["entities"]) == 2
    assert body["dictionary_hash"] == catalog["dictionary_hash"]
    assert body["content_hash"] == hashlib.sha256("🙂云城 旧剑 小白".encode()).hexdigest()
    query = {"entity_kind": "character", "entity_id": data["character"]["id"]}
    page = await get_json(client, url, **query, page_size=2)
    assert page["total"] == 8
    assert page["source_counts"] == {"chapter": 4, "chat": 2, "note": 1, "knowledge": 1}
    assert [hit["text"] for hit in page["items"]] == ["林远", "小林"]
    all_hits = []
    for number in range(1, 5):
        all_hits.extend((await get_json(client, url, **query, page_size=2, page=number))["items"])
    assert len({(hit["source"]["type"], hit["source"]["id"], hit["start"]) for hit in all_hits}) == 8
    assert {hit["source"]["id"] for hit in all_hits if hit["source"]["type"] == "chat"} == {
        m.id for m in data["messages"][:2]
    }
    assert fake_llm.calls == [] and fake_llm.embed_calls == []


async def test_summary_counts_ambiguity_empty_chapters_and_filters(client, app):
    data = await seeded(client, app)
    url = f"{data['base']}/mentions/summary"
    body = await get_json(client, url)
    entry = next(
        e
        for e in body["entries"]
        if e["entity"]["id"] == data["character"]["id"] and e["entity"]["kind"] == "character"
    )
    assert (entry["mention_count"], entry["ambiguous_count"], entry["chapter_count"]) == (3, 1, 2)
    assert (entry["last_chapter_no"], entry["gap_chapters"]) == (4, 1)
    assert [c["has_content"] for c in body["chapters"]] == [True, True, False, True, True]
    filtered = await get_json(
        client, url, entity_kind="character", entity_id=data["character"]["id"], chapter_from=1, chapter_to=3
    )
    assert filtered["total"] == 1
    assert (
        filtered["entries"][0]["mention_count"],
        filtered["entries"][0]["last_chapter_no"],
        filtered["entries"][0]["gap_chapters"],
    ) == (2, 1, 1)
    place = await get_json(client, url, category="place", page_size=1)
    assert place["total"] == 1 and place["entries"][0]["entity"]["id"] == data["place"]["id"]
    never = await get_json(client, url, q="白虎")
    assert never["entries"][0]["last_chapter_no"] is None
    assert never["entries"][0]["gap_chapters"] is None


async def test_current_text_dictionary_deletion_and_renumber_invalidate_results(client, app):
    data = await seeded(client, app)
    url = f"{data['base']}/mentions"
    query = {"entity_kind": "character", "entity_id": data["character"]["id"]}
    first = await get_json(client, url, **query)
    hit = first["items"][0]
    source_url = f"{url}/sources/chapter/{data['chapters'][0]['id']}"
    window = await get_json(client, source_url, start=hit["start"], end=hit["end"], content_hash=hit["content_hash"])
    assert window["stale"] is False and window["target_start"] == 1
    # SQL 直写模拟生成/撤销等绕过章节 PATCH 的路径；不依赖漏挂的写入钩子。
    async with app.state.sessionmaker() as db:
        await db.execute(
            update(Chapter).where(Chapter.id == data["chapters"][0]["id"]).values(content="新文", chapter_no=10)
        )
        await db.commit()
    stale = await get_json(client, source_url, start=hit["start"], end=999, content_hash=hit["content_hash"])
    assert stale["stale"] and stale["target_start"] is None and stale["target_end"] is None
    assert stale["source"]["chapter_no"] == 10
    assert (await get_json(client, url, **query))["source_counts"]["chapter"] == 1
    renamed = await client.patch(
        f"{data['base']}/characters/{data['character']['id']}", json={"name": "林新", "aliases": []}
    )
    assert renamed.status_code == 200
    assert (await get_json(client, url, **query))["total"] == 0
    assert (await get_json(client, f"{url}/catalog"))["dictionary_hash"] != first["dictionary_hash"]
    async with app.state.sessionmaker() as db:
        await db.execute(delete(Chapter).where(Chapter.id == data["chapters"][0]["id"]))
        await db.commit()
    assert (await client.get(source_url)).status_code == 404


async def test_private_chat_hidden_from_manager_and_other_users(client, app, make_client):
    data = await seeded(client, app)
    url = f"{data['base']}/mentions"
    manager = make_client()
    user = await register(manager, "manager")
    async with app.state.sessionmaker() as db:
        (await db.get(User, user["id"])).is_admin = True
        await db.commit()
    catalog = await get_json(manager, f"{url}/catalog")
    assert catalog["available_source_types"] == ["chapter", "note", "knowledge"]
    body = await get_json(manager, url)
    assert body["source_counts"]["chat"] == 0
    assert all(hit["source"]["type"] != "chat" for hit in body["items"])
    assert "私有讨论" not in str(body)
    assert (await manager.get(url, params={"source_type": "chat"})).status_code == 404
    assert (await manager.get(f"{url}/sources/chat/{data['messages'][0].id}")).status_code == 404
    stranger = make_client()
    await register(stranger, "stranger")
    for path in (url, f"{url}/catalog", f"{url}/summary", f"{url}/sources/chapter/{data['chapters'][0]['id']}"):
        assert (await stranger.get(path)).status_code == 404
    assert (await stranger.post(f"{url}/preview", json={"text": "林远"})).status_code == 404
    await client.delete(data["base"])
    assert (await manager.get(url)).status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"entity_id": 1},
        {"chapter_from": 5, "chapter_to": 1},
        {"page_size": 101},
        {"entity_kind": "bad"},
    ],
)
async def test_invalid_mention_queries(client, params):
    await register(client)
    project = await create_project(client)
    response = await client.get(f"/api/v1/projects/{project['id']}/mentions", params=params)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


async def test_source_windows_all_types_and_stale_hash(client, app):
    data = await seeded(client, app)
    url = f"{data['base']}/mentions"
    for kind, id in [
        ("chapter", data["chapters"][0]["id"]),
        ("chat", data["messages"][1].id),
        ("note", data["note"]["id"]),
        ("knowledge", data["document"].id),
    ]:
        window = await get_json(client, f"{url}/sources/{kind}/{id}")
        assert window["source"]["type"] == kind
        assert all(
            window["text"][s["start"] - window["window_start"] : s["end"] - window["window_start"]] == s["text"]
            for s in window["spans"]
        )
        stale = await get_json(client, f"{url}/sources/{kind}/{id}", content_hash="0" * 64, start=123, end=456)
        assert stale["stale"] and stale["target_start"] is None
    assert (await client.get(f"{url}/sources/chat/{data['messages'][2].id}")).status_code == 404


async def test_world_aliases_metadata_revision_and_null_semantics(client, app):
    data = await seeded(client, app)
    world = data["place"]
    assert world["aliases"] == ["云城"]
    url = f"{data['base']}/world/{world['id']}"
    timelines = (
        await client.get(
            f"{data['base']}/setting-timelines", params={"subject_kind": "world", "world_setting_id": world["id"]}
        )
    ).json()
    timeline = next(t for t in timelines["items"] if t["world_setting_id"] == world["id"])
    revision = timeline["revision"]
    response = await client.patch(
        url, json={"aliases": [" 新城 ", "新城", ""], "edit_mode": "baseline", "expected_revision": revision}
    )
    assert response.status_code == 200, response.text
    assert response.json()["aliases"] == ["新城"]
    conflict = await client.patch(
        url, json={"aliases": ["失效"], "edit_mode": "baseline", "expected_revision": revision}
    )
    assert conflict.status_code == 409
    assert (await client.patch(url, json={"aliases": None})).json()["aliases"] == ["新城"]
    assert (await client.patch(url, json={"aliases": []})).json()["aliases"] == []
    assert (await client.patch(url, json={"aliases": ["a" * 51]})).status_code == 422
    async with app.state.sessionmaker() as db:
        assert (await db.scalar(select(WorldSetting).where(WorldSetting.id == world["id"]))).content == world["content"]


async def test_read_paths_never_autoflush_pending_business_changes(client, app):
    from app.models import Character
    from app.services.mentions import query, summary, window

    data = await seeded(client, app)
    user = (await client.get("/api/v1/auth/me")).json()
    async with app.state.sessionmaker() as db:
        invalid_pending = Character(project_id=999999, name="未提交")
        db.add(invalid_pending)
        await query.catalog(db, user["id"], data["project"]["id"])
        await query.preview(db, user["id"], data["project"]["id"], "林远")
        await query.list_mentions(db, user["id"], data["project"]["id"])
        await summary.summarize(db, user["id"], data["project"]["id"])
        await window.source_window(db, user["id"], data["project"]["id"], "chapter", data["chapters"][0]["id"])
        assert invalid_pending in db.new


async def test_large_source_window_is_bounded_and_matches_run_off_event_loop(client, app, monkeypatch):
    import threading

    from app.services.mentions import matcher

    data = await seeded(client, app)
    content = "前" * 19000 + "🙂林远" + "后" * 1000
    async with app.state.sessionmaker() as db:
        await db.execute(update(KbDocument).where(KbDocument.id == data["document"].id).values(text=content))
        await db.commit()
    original = matcher.iter_matches
    workers = []

    def checked(*args):
        workers.append(threading.get_ident())
        yield from original(*args)

    matcher.cache.clear()
    monkeypatch.setattr(matcher, "iter_matches", checked)
    url = f"{data['base']}/mentions"
    result = await get_json(client, url, source_type="knowledge")
    assert result["total"] == 1
    assert workers and all(worker != threading.get_ident() for worker in workers)
    hit = result["items"][0]
    source_url = f"{url}/sources/knowledge/{data['document'].id}"
    body = await get_json(client, source_url, start=hit["start"], end=hit["end"], content_hash=hit["content_hash"])
    assert len(body["text"]) <= 8000 and body["window_start"] > 0
    assert body["target_start"] == 19001
    assert content[body["window_start"] : body["window_start"] + len(body["text"])] == body["text"]
    assert (await client.get(source_url, params={"start": 0, "end": 10000})).status_code == 422
    assert (await client.get(source_url, params={"start": 10, "end": 5})).status_code == 422


async def test_note_edit_and_deleted_chat_note_knowledge_remove_cached_mentions(client, app):
    data = await seeded(client, app)
    url = f"{data['base']}/mentions"
    query = {"entity_kind": "character", "entity_id": data["character"]["id"]}
    assert (await get_json(client, url, **query))["total"] == 8
    note_url = f"{data['base']}/notes/{data['note']['id']}"
    changed = await client.patch(note_url, json={"expected_revision": 1, "content": "新的无关想法"})
    assert changed.status_code == 200, changed.text
    assert (await get_json(client, url, **query))["source_counts"]["note"] == 0
    response = await client.delete(f"{data['base']}/agent/conversations/{data['conversation'].id}")
    assert response.status_code == 204, response.text
    assert (await get_json(client, url, **query))["source_counts"]["chat"] == 0
    response = await client.delete(f"{data['base']}/kb/documents/{data['document'].id}")
    assert response.status_code == 204, response.text
    assert (await get_json(client, url, **query))["source_counts"]["knowledge"] == 0
    assert (await client.delete(note_url, params={"expected_revision": changed.json()["revision"]})).status_code == 204
    for kind, id in [
        ("chat", data["messages"][0].id),
        ("note", data["note"]["id"]),
        ("knowledge", data["document"].id),
    ]:
        assert (await client.get(f"{url}/sources/{kind}/{id}")).status_code == 404


async def test_sources_and_entity_ids_cannot_cross_own_projects(client, app):
    data = await seeded(client, app)
    second = await create_project(client, title="另一本书")
    url = f"/api/v1/projects/{second['id']}/mentions"
    for kind, id in [
        ("chapter", data["chapters"][0]["id"]),
        ("chat", data["messages"][0].id),
        ("note", data["note"]["id"]),
        ("knowledge", data["document"].id),
    ]:
        assert (await client.get(f"{url}/sources/{kind}/{id}")).status_code == 404
    response = await client.get(url, params={"entity_kind": "character", "entity_id": data["character"]["id"]})
    assert response.status_code == 404


async def test_ai_save_and_version_restore_recompute_mentions(client, app, fake_llm):
    data = await seeded(client, app)
    await create_ai_config(client)
    url = f"{data['base']}/mentions"
    query = {"entity_kind": "character", "entity_id": data["character"]["id"], "source_type": "chapter"}
    assert (await get_json(client, url, **query))["total"] == 4
    chapter_id = data["chapters"][0]["id"]
    versions_url = f"{data['base']}/chapters/{chapter_id}/versions"
    original = (await client.get(versions_url)).json()[0]
    fake_llm.push("青云城的夜色很静。")
    response = await client.post(f"/api/v1/chapters/{chapter_id}/generate", json={})
    assert response.status_code == 202, response.text
    task = await wait_task(client, response.json()["id"])
    assert task["status"] == "succeeded", task
    assert (await get_json(client, url, **query))["total"] == 1
    current = (await client.get(f"{data['base']}/chapters/{chapter_id}")).json()
    restored = await client.post(
        f"{versions_url}/{original['id']}/restore",
        json={"expected_content_revision": current["content_revision"]},
    )
    assert restored.status_code == 200, restored.text
    assert (await get_json(client, url, **query))["total"] == 4
