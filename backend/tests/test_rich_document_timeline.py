"""全书标记时间线只展示已保存标记，按章景和正文位置稳定分页。"""

from sqlalchemy import update

from app.models import Chapter
from app.services import marker_timeline_service
from tests.helpers import register
from tests.test_rich_document_api import KEY, MARKER, SECTION_AFTER, add_section, save_payload
from tests.test_scenes import setup_scene

SECOND_MARKER = "8d3e0849-680b-4303-897a-481267a3e54a"
THIRD_MARKER = "8d3e0849-680b-4303-897a-481267a3e54b"


async def test_book_timeline_orders_targets_and_retains_stale_excerpt(client, make_client):
    project, _chapter, url, first = await setup_scene(client, "甲🙂乙")
    endpoint = f"{url}/scenes/{first['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    body["document"]["markers"] = [
        {"key": SECOND_MARKER, "section_key": KEY, "status": "active", "start": 0, "end": 2,
         "color": "green", "excerpt": "", "last_start": None},
        {"key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 2,
         "color": "yellow", "excerpt": "", "last_start": None},
    ]
    body["add_marker_keys"] = [SECOND_MARKER, MARKER]
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    chapter2 = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={
        "title": "第二章", "content": "失效前",
    })
    assert chapter2.status_code == 201, chapter2.text
    url2 = f"/api/v1/projects/{project['id']}/chapters/{chapter2.json()['id']}"
    second_scene = (await client.get(f"{url2}/scenes")).json()["items"][0]
    doc2 = f"{url2}/scenes/{second_scene['id']}/document"
    next_body = save_payload((await client.get(doc2)).json())
    next_body["document"]["markers"].append({
        "key": THIRD_MARKER, "section_key": None, "status": "stale", "start": None, "end": None,
        "color": "purple", "excerpt": "旧🙂片段", "last_start": 1,
    })
    next_body["add_marker_keys"].append(THIRD_MARKER)
    saved2 = await client.put(doc2, json=next_body)
    assert saved2.status_code == 200, saved2.text

    path = f"/api/v1/projects/{project['id']}/markers"
    response = await client.get(path, params={"limit": 2})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 3 and [item["key"] for item in page["items"]] == [MARKER, SECOND_MARKER]
    assert page["items"][0]["excerpt"] == "甲🙂" and page["items"][0]["section_key"] is None
    assert page["items"][1]["excerpt"] == "作者" and page["items"][1]["section_key"] == KEY
    last = (await client.get(path, params={"offset": 2, "limit": 2, "snapshot": page["snapshot"]})).json()["items"][0]
    assert last["key"] == THIRD_MARKER and last["status"] == "stale" and last["excerpt"] == "旧🙂片段"
    assert last["start"] is None and last["chapter_id"] == chapter2.json()["id"]
    other = make_client()
    await register(other, "other")
    assert (await other.get(path)).status_code == 404


async def test_long_marker_previews_stream_sources_and_preserve_nul_crlf(client, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    value = "甲\x00" + "🙂" * 4_997 + "\r\n" + "尾" * 10_000
    project, _chapter, url, scene = await setup_scene(client, value)
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    body["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": len(value),
        "color": "yellow", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"] = [MARKER]
    assert (await client.put(endpoint, json=body)).status_code == 200
    calls = []
    original = AsyncSession.stream

    async def observe(self, statement, *args, **kwargs):
        calls.append(statement.get_execution_options().get("yield_per"))
        return await original(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "stream", observe)
    response = await client.get(f"/api/v1/projects/{project['id']}/markers")
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["excerpt"] == "甲\x00" + "🙂" * 4_997
    assert item["excerpt_truncated"] is True
    assert calls == [1]


async def test_same_anchor_sections_precede_following_prose_and_keep_stale_order(client):
    project, _chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    body["document"]["sections"].append({
        "key": SECTION_AFTER, "anchor": 1, "order": 1, "title": "后区块", "content": "别处",
        "formatting": {"version": 1, "boundaries": [], "inline": [], "blocks": []},
        "include_in_ai": False,
    })
    body["add_section_keys"].append(SECTION_AFTER)
    body["document"]["markers"] = [
        {"key": MARKER, "section_key": None, "status": "active", "start": 1, "end": 2,
         "color": "yellow", "excerpt": "", "last_start": None},
        {"key": SECOND_MARKER, "section_key": KEY, "status": "stale", "start": None, "end": None,
         "color": "green", "excerpt": "旧标记", "last_start": 1},
        {"key": THIRD_MARKER, "section_key": SECTION_AFTER, "status": "active", "start": 0, "end": 1,
         "color": "blue", "excerpt": "", "last_start": None},
    ]
    body["add_marker_keys"] = [MARKER, SECOND_MARKER, THIRD_MARKER]
    assert (await client.put(endpoint, json=body)).status_code == 200
    result = await client.get(f"/api/v1/projects/{project['id']}/markers", params={"limit": 2})
    assert [row["key"] for row in result.json()["items"]] == [SECOND_MARKER, THIRD_MARKER]
    last = await client.get(f"/api/v1/projects/{project['id']}/markers", params={
        "offset": 2, "limit": 2, "snapshot": result.json()["snapshot"],
    })
    assert [row["key"] for row in last.json()["items"]] == [MARKER]


async def test_retired_scene_markers_remain_findable_and_reactivate_after_restore(client):
    project, _chapter, url, _scene = await setup_scene(client, "首景")
    initial = (await client.get(f"{url}/versions")).json()[0]
    created = await client.post(f"{url}/scenes", json={"expected_content_revision": 0, "content": "退休🙂景"})
    assert created.status_code == 201, created.text
    scene_id = created.json()["id"]
    endpoint = f"{url}/scenes/{scene_id}/document"
    body = save_payload((await client.get(endpoint)).json())
    body["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 3,
        "color": "blue", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    assert (await client.put(endpoint, json=body)).status_code == 200
    with_marker = (await client.get(f"{url}/versions")).json()[0]
    retired = await client.post(f"{url}/versions/{initial['id']}/restore", json={
        "expected_content_revision": 2, "confirm_rich_document_loss": True,
    })
    assert retired.status_code == 200, retired.text
    path = f"/api/v1/projects/{project['id']}/markers"
    item = (await client.get(path)).json()["items"][0]
    assert item["key"] == MARKER and item["scene_id"] == scene_id and item["scene_active"] is False
    assert item["status"] == "stale" and item["excerpt"] == "退休🙂" and item["start"] is None
    restored = await client.post(f"{url}/versions/{with_marker['id']}/restore", json={
        "expected_content_revision": 3,
    })
    assert restored.status_code == 200, restored.text
    active = (await client.get(path)).json()["items"][0]
    assert active["scene_active"] is True and active["status"] == "active" and active["start"] == 0


async def test_timeline_rejects_mixed_page_and_total_after_another_chapter_changes(client, monkeypatch):
    project, _chapter, url, first = await setup_scene(client, "第一章")
    first_url = f"{url}/scenes/{first['id']}/document"
    body = save_payload((await client.get(first_url)).json())
    body["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 1,
        "color": "blue", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    assert (await client.put(first_url, json=body)).status_code == 200
    second = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "二", "content": "乙"})
    assert second.status_code == 201
    url2 = f"/api/v1/projects/{project['id']}/chapters/{second.json()['id']}"
    scene2 = (await client.get(f"{url2}/scenes")).json()["items"][0]
    doc2 = f"{url2}/scenes/{scene2['id']}/document"
    new = save_payload((await client.get(doc2)).json())
    new["document"]["markers"].append({
        "key": SECOND_MARKER, "section_key": None, "status": "active", "start": 0, "end": 1,
        "color": "green", "excerpt": "", "last_start": None,
    })
    new["add_marker_keys"].append(SECOND_MARKER)
    original = marker_timeline_service._project_marker_stamp
    calls = 0

    async def concurrent_change(db, project_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            response = await client.put(doc2, json=new)
            assert response.status_code == 200, response.text
        return await original(db, project_id)

    monkeypatch.setattr(marker_timeline_service, "_project_marker_stamp", concurrent_change)
    response = await client.get(f"/api/v1/projects/{project['id']}/markers", params={"limit": 1})
    assert response.status_code == 409 and response.json()["code"] == "scene_context_changed"


async def test_later_page_rejects_stale_or_missing_whole_book_snapshot(client):
    project, _chapter, url, first = await setup_scene(client, "第一章")
    first_url = f"{url}/scenes/{first['id']}/document"
    body = save_payload((await client.get(first_url)).json())
    body["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 1,
        "color": "blue", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    assert (await client.put(first_url, json=body)).status_code == 200
    path = f"/api/v1/projects/{project['id']}/markers"
    first_page = await client.get(path, params={"limit": 1})
    assert first_page.status_code == 200
    snapshot = first_page.json()["snapshot"]
    second = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={"title": "二", "content": "乙"})
    assert second.status_code == 201
    stale = await client.get(path, params={"offset": 1, "limit": 1, "snapshot": snapshot})
    assert stale.status_code == 409 and stale.json()["code"] == "marker_timeline_changed"
    missing = await client.get(path, params={"offset": 1, "limit": 1})
    assert missing.status_code == 400 and missing.json()["code"] == "marker_timeline_snapshot_required"


async def test_chinese_title_change_invalidates_snapshot_even_without_chapter_revision(app, client):
    project, chapter, _url, _scene = await setup_scene(client, "正文")
    path = f"/api/v1/projects/{project['id']}/markers"
    initial = await client.get(path, params={"limit": 1})
    assert initial.status_code == 200
    async with app.state.sessionmaker() as db:
        # 隔离标题自身变化；不依赖现有更新路径恰好递增规划版本。
        await db.execute(update(Chapter).where(Chapter.id == chapter["id"]).values(title="中文章名🙂"))
        await db.commit()
    stale = await client.get(path, params={"offset": 1, "snapshot": initial.json()["snapshot"]})
    assert stale.status_code == 409 and stale.json()["code"] == "marker_timeline_changed"
    fresh = await client.get(path)
    assert fresh.status_code == 200 and fresh.json()["snapshot"] != initial.json()["snapshot"]
