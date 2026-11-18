"""全书搜索的范围、阅读锚点及逐项替换预览。"""

import hashlib

import pytest
from pydantic import ValidationError

from app.schemas.book_search import PreviewRequest
from app.schemas.rich_document import SceneDocument
from app.services import book_search_mapping
from app.services.book_search_mapping import VerifiedPreviews, lost_marker_keys, replay_replacements
from app.services.rich_document_mapping import UnsafeMapping
from tests.helpers import create_project, register
from tests.test_rich_document_api import MARKER, save_payload
from tests.test_scenes import setup_scene


async def add_chapter(client, pid, title, content):
    response = await client.post(f"/api/v1/projects/{pid}/chapters", json={"title": title, "content": content})
    assert response.status_code == 201, response.text
    return response.json()


async def test_plain_literal_pagination_and_unicode_reader_offsets(client):
    await register(client)
    pid = (await create_project(client))["id"]
    first = await add_chapter(client, pid, "一", "🙂青石镇青石镇，旧称。")
    await add_chapter(client, pid, "二", "空格　和特殊符号.*。")
    third = await add_chapter(client, pid, "三", "青石镇")
    base = f"/api/v1/projects/{pid}/book-search"
    request = {"query": "青石镇", "page_size": 1}
    page = await client.post(f"{base}/query", json=request)
    assert page.status_code == 200, page.text
    data = page.json()
    assert data["total"] == 3
    hit = data["items"][0]
    assert (hit["chapter_id"], hit["start"], hit["end"], hit["before"]) == (first["id"], 1, 4, "🙂")
    assert hit["content_hash"] == hashlib.sha256(first["content"].encode()).hexdigest()
    for number, expected in [(2, (first["id"], 4)), (3, (third["id"], 0))]:
        following = await client.post(f"{base}/query", json={**request, "page": number, "snapshot": data["snapshot"]})
        result = following.json()["items"][0]
        assert (result["chapter_id"], result["start"]) == expected
    for term, count in [(".*", 1), ("　", 1), ("青石镇 ", 0)]:
        result = await client.post(f"{base}/query", json={"query": term})
        assert result.json()["total"] == count


async def test_scoped_snapshot_and_endpoint_validation(client):
    await register(client)
    pid = (await create_project(client))["id"]
    left, right, other = [await add_chapter(client, pid, name, "zzzz") for name in "甲乙丙"]
    base = f"/api/v1/projects/{pid}"
    url = f"{base}/book-search/query"
    req = {"query": "zz", "start_chapter_id": left["id"], "end_chapter_id": right["id"], "page_size": 1}
    page = (await client.post(url, json=req)).json()
    assert page["total"] == 4  # 相互重叠的候选只取从左起的非重叠匹配
    later = {**req, "snapshot": page["snapshot"], "page": 2}
    assert (await client.post(url, json={**req, "page": 2})).status_code == 422
    response = await client.patch(f"{base}/chapters/{other['id']}",
                                  json={"content": "新", "expected_content_revision": 0})
    assert response.status_code == 200
    assert (await client.post(url, json=later)).status_code == 200
    response = await client.patch(f"{base}/chapters/{left['id']}",
                                  json={"content": "zzzzz", "expected_content_revision": 0})
    assert response.status_code == 200
    assert (await client.post(url, json=later)).json()["code"] == "book_search_stale"
    reversed_range = {**req, "start_chapter_id": right["id"], "end_chapter_id": left["id"]}
    assert (await client.post(url, json=reversed_range)).status_code == 400
    assert (await client.post(url, json={**req, "end_chapter_id": None})).status_code == 422
    assert (await client.post(url, json={**req, "end_chapter_id": 999999})).status_code == 409
    assert (await client.post(url, json={"query": ""})).status_code == 422


async def test_literal_preview_reports_uneditable_cross_scene_and_crlf(client):
    project, _, chapter_url, scene = await setup_scene(client, "甲乙\r\n甲乙")
    split = await client.post(f"{chapter_url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 0, "offset": 1,
    })
    assert split.status_code == 200
    endpoint = f"/api/v1/projects/{project['id']}/book-search/preview"
    result = await client.post(endpoint, json={"query": "甲乙", "replacement": "丙"})
    assert result.status_code == 200, result.text
    first, second = result.json()["items"]
    assert first["token"] is None and "场景" in first["unavailable_reason"] and first["start"] == 0
    assert second["start"] == 4 and second["token"] and second["scene_id"] == split.json()["items"][1]["id"]
    partial = (await client.post(endpoint, json={"query": "\n", "replacement": " "})).json()["items"][0]
    assert not partial["token"] and "CRLF" in partial["unavailable_reason"]
    same = (await client.post(endpoint, json={"query": "甲", "replacement": "甲"})).json()
    assert all(i["unavailable_reason"] and i["token"] is None for i in same["items"])


async def test_preview_preserves_format_and_discloses_stale_marker(client):
    project, _, chapter_url, scene = await setup_scene(client, "今晨旧城遇故人，前往旧城。")
    document = f"{chapter_url}/scenes/{scene['id']}/document"
    payload = save_payload((await client.get(document)).json())
    payload["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 2, "end": 4}]
    payload["document"]["markers"] = [
        {"key": MARKER, "section_key": None, "status": "active", "start": 10, "end": 12,
         "color": "blue", "excerpt": "", "last_start": None},
    ]
    payload["add_marker_keys"] = [MARKER]
    saved = await client.put(document, json=payload)
    assert saved.status_code == 200, saved.text
    resp = await client.post(f"/api/v1/projects/{project['id']}/book-search/preview",
                             json={"query": "旧城", "replacement": "望城"})
    assert resp.status_code == 200, resp.text
    unavailable, editable = resp.json()["items"]
    assert "格式" in unavailable["unavailable_reason"] and not unavailable["token"]
    assert editable["token"] and "标记" in editable["warning"]
    assert (await client.get(document)).json() == saved.json()


async def test_empty_book_and_unknown_word(client):
    await register(client)
    pid = (await create_project(client))["id"]
    resp = await client.post(f"/api/v1/projects/{pid}/book-search/query", json={"query": "辞典外文本"})
    assert resp.status_code == 200 and resp.json()["items"] == [] and resp.json()["total"] == 0


def test_replay_uses_original_offsets_plain_text_and_rejects_overlapping_edits():
    doc = SceneDocument(content="🙂aaaaabaaaa")
    assert replay_replacements(doc, "aa", "青", [1, 3, 7]).content == "🙂青青ab青aa"
    with pytest.raises(UnsafeMapping):
        replay_replacements(doc, "aa", "青", [1, 2])
    with pytest.raises(UnsafeMapping):
        replay_replacements(doc, "aa", "青", [6])
    with pytest.raises(ValidationError):
        PreviewRequest(query="abc", replacement="\x00")


def test_marker_covered_by_combination_only_is_reported():
    doc = SceneDocument.model_validate({
        "content": "aaaa", "markers": [
            {"key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 4,
             "color": "blue", "excerpt": "", "last_start": None}],
    })
    verified = VerifiedPreviews()
    for pos in [0, 2]:
        candidate = replay_replacements(doc, "aa", "", [pos])
        assert candidate.markers[0].status == "active"
        verified.record(pos, candidate, lost_marker_keys(doc, candidate))
    for proof in (None, verified):
        with pytest.raises(UnsafeMapping, match="组合替换"):
            replay_replacements(doc, "aa", "", [0, 2], verified=proof)


def test_verified_scene_reuses_only_rightmost_document_and_checks_other_marker_changes(monkeypatch):
    doc = SceneDocument(content="旧旧旧", markers=[
        {"key": MARKER, "status": "active", "start": 1, "end": 2, "color": "blue"},
    ])
    offsets = [0, 1, 2]
    standalone = replay_replacements(doc, "旧", "新名", offsets)
    verified = VerifiedPreviews()
    for pos in reversed(offsets):
        candidate = replay_replacements(doc, "旧", "新名", [pos])
        verified.record(pos, candidate, lost_marker_keys(doc, candidate))
    calls = []
    mapper = book_search_mapping.map_explicit_replacement

    def observe(document, start, end, replacement):
        calls.append(start)
        return mapper(document, start, end, replacement)

    monkeypatch.setattr(book_search_mapping, "map_explicit_replacement", observe)
    assert replay_replacements(doc, "旧", "新名", offsets, verified=verified) == standalone
    assert calls == [1, 0]


async def test_token_is_bound_to_actual_actor_creation(app, client):
    from datetime import timedelta
    from uuid import uuid4

    from sqlalchemy import select

    from app.models import User

    project, _, chapter_url, _ = await setup_scene(client, "旧地名")
    endpoint = f"/api/v1/projects/{project['id']}/book-search"
    words = {"query": "旧", "replacement": "新"}
    page = await client.post(f"{endpoint}/preview", json=words)
    assert page.status_code == 200, page.text
    hit = page.json()["items"][0]
    selected = {key: hit[key] for key in ("chapter_id", "chapter_no", "chapter_title", "scene_id",
                                          "revision", "start", "end", "token")}
    # 隔离模拟 SQLite 主键已复用、token_version 相同但创建身份不同的账号。
    async with app.state.sessionmaker() as db:
        actor = (await db.scalars(select(User).where(User.username == "alice"))).one()
        actor.created_at += timedelta(seconds=1)
        await db.commit()
    failed = await client.post(f"{endpoint}/apply", json={**words, "request_id": str(uuid4()), "hits": [selected]})
    assert failed.status_code == 409, failed.text
    assert (await client.get(chapter_url)).json()["content"] == "旧地名"


async def test_range_ends_disambiguate_chapters_with_same_number(client):
    await register(client)
    pid = (await create_project(client))["id"]
    base = f"/api/v1/projects/{pid}"
    ids = []
    for title in ("同号甲", "同号乙"):
        resp = await client.post(f"{base}/chapters", json={"title": title, "chapter_no": 8, "content": "同名"})
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])
    url = f"{base}/book-search/preview"
    data = {"query": "同名", "replacement": "新名", "start_chapter_id": ids[1], "end_chapter_id": ids[1]}
    response = await client.post(url, json=data)
    assert response.status_code == 200, response.text
    assert [hit["chapter_id"] for hit in response.json()["items"]] == [ids[1]]
    assert (await client.post(url, json={**data, "end_chapter_id": ids[0]})).status_code == 400
