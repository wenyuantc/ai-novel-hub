"""通过作品 HTTP 入口验证一次批改和整批撤销的原子性、重放与正文保护。"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.core.errors import ConflictError
from app.models import Chapter, Task
from app.services import scene_service
from app.services.generation_service import BATCH_KIND
from tests.helpers import chapter_title_save_body, create_project, register
from tests.test_rich_document_api import MARKER, add_section, save_payload

WORDS = {"query": "青石镇", "replacement": "新城"}


async def chapter(client, base, number, content):
    response = await client.post(f"{base}/chapters", json={
        "chapter_no": number, "title": f"第{number}章", "content": content,
    })
    assert response.status_code == 201, response.text
    return response.json()


async def setup_book(client, *contents):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    chapters = [await chapter(client, base, n, content) for n, content in enumerate(contents, start=1)]
    return base, chapters


def chapter_url(base, row):
    return f"{base}/chapters/{row['id']}"


async def document_url(client, base, row):
    url = chapter_url(base, row)
    response = await client.get(f"{url}/scenes")
    assert response.status_code == 200, response.text
    scene = response.json()["items"][0]
    return f"{url}/scenes/{scene['id']}/document"


async def state(client, base, chapters):
    result = []
    for row in chapters:
        response = await client.get(chapter_url(base, row))
        assert response.status_code == 200, response.text
        result.append((response.json()["content"], response.json()["content_revision"]))
    return result


async def preview(client, base, **options):
    response = await client.post(f"{base}/book-search/preview", json={**WORDS, **options})
    assert response.status_code == 200, response.text
    return response.json()


def application(items, **options):
    fields = ("chapter_id", "chapter_no", "chapter_title", "scene_id", "revision", "start", "end", "token")
    return {**WORDS, "request_id": str(uuid4()), "hits": [{k: item[k] for k in fields} for item in items], **options}


async def apply_batch(client, base, body):
    response = await client.post(f"{base}/book-search/apply", json=body)
    assert response.is_success, response.text
    return response.json()


async def history(client, base):
    response = await client.get(f"{base}/book-search/batches")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def undo_url(base, batch):
    return f"{base}/book-search/batches/{batch['id']}/undo"


async def test_selected_hits_from_different_pages_and_unrelated_edit_roundtrip(client):
    base, rows = await setup_book(client, "🙂青石镇。引文：青石镇。", "青石镇。", "青石镇。")
    page1 = await preview(client, base, page_size=1)
    page3 = await preview(client, base, page_size=1, page=3, snapshot=page1["snapshot"])
    assert page1["total"] == page3["total"] == 4
    request = application([*page1["items"], *page3["items"]])
    batch = await apply_batch(client, base, request)
    assert batch["hit_count"] == batch["chapter_count"] == 2
    assert batch["status"] == "applied"
    assert await state(client, base, rows) == [("🙂新城。引文：青石镇。", 1), ("新城。", 1), ("青石镇。", 0)]

    # 重发成功请求不能再推进章节版本或生成第二个历史批次。
    replay = await apply_batch(client, base, request)
    assert replay["id"] == batch["id"]
    changed_payload = {**request, "hits": request["hits"][:1]}
    rejected = await client.post(f"{base}/book-search/apply", json=changed_payload)
    assert rejected.status_code == 409, rejected.text
    assert [item["id"] for item in await history(client, base)] == [batch["id"]]

    edited = await client.patch(chapter_url(base, rows[2]), json={
        "content": "无关章节的新稿", "expected_content_revision": 0,
    })
    assert edited.status_code == 200, edited.text
    for _ in range(2):
        undone = await client.post(undo_url(base, batch))
        assert undone.status_code == 200 and undone.json()["status"] == "undone", undone.text
    late_replay = await apply_batch(client, base, request)
    assert late_replay["id"] == batch["id"] and late_replay["status"] == "undone"
    assert await state(client, base, rows) == [
        ("🙂青石镇。引文：青石镇。", 2), ("青石镇。", 2), ("无关章节的新稿", 1),
    ]
    assert [row["status"] for row in await history(client, base)] == ["undone"]


async def test_undo_detects_every_changed_chapter_including_title_aba(client):
    base, rows = await setup_book(client, "青石镇甲", "青石镇乙", "青石镇丙")
    batch = await apply_batch(client, base, application((await preview(client, base))["items"]))
    middle_url = chapter_url(base, rows[1])
    for title in ("别人编辑过", rows[1]["title"]):
        save = await chapter_title_save_body(client, middle_url, title)
        response = await client.patch(f"{middle_url}/editor-title", json=save)
        assert response.status_code == 200, response.text
    final_url = chapter_url(base, rows[2])
    response = await client.patch(final_url, json={"content": "保留最新一行", "expected_content_revision": 1})
    assert response.status_code == 200, response.text
    rejected = await client.post(undo_url(base, batch))
    assert rejected.status_code == 409, rejected.text
    assert "整批撤销未执行，当前修订仍然有效" in rejected.json()["detail"]
    assert {item["chapter_no"] for item in rejected.json()["conflicts"]} == {2, 3}
    assert await state(client, base, rows) == [("新城甲", 1), ("新城乙", 3), ("保留最新一行", 2)]
    assert (await history(client, base))[0]["status"] == "applied"


async def test_several_selected_hits_across_two_scenes_advance_one_chapter_revision(client):
    base, rows = await setup_book(client, "青石镇🙂青石镇")
    chapter_path = chapter_url(base, rows[0])
    added = await client.post(f"{chapter_path}/scenes", json={"expected_content_revision": 0, "content": "青石镇尾巴"})
    assert added.status_code == 201, added.text
    before = (await client.get(chapter_path)).json()
    assert before["content"] == "青石镇🙂青石镇\n\n青石镇尾巴"
    found = (await preview(client, base, replacement=""))["items"]
    assert len(found) == 3
    request = application(list(reversed(found)), replacement="")
    batch = await apply_batch(client, base, request)
    assert (batch["chapter_count"], batch["hit_count"]) == (1, 3)
    assert await state(client, base, rows) == [("🙂\n\n尾巴", 2)]
    response = await client.post(undo_url(base, batch))
    assert response.status_code == 200, response.text
    assert await state(client, base, rows) == [(before["content"], 3)]


async def test_concurrent_duplicate_submission_has_one_effect_and_a_replayable_receipt(client, make_client):
    base, rows = await setup_book(client, "青石镇")
    second = make_client()
    logged = await second.post("/api/v1/auth/login", json={"username": "alice", "password": "secret123"})
    assert logged.status_code == 200, logged.text
    request = application((await preview(client, base))["items"])
    path = f"{base}/book-search/apply"
    first_result, second_result = await asyncio.gather(client.post(path, json=request), second.post(path, json=request))
    successful = [result for result in (first_result, second_result) if result.is_success]
    assert successful, (first_result.text, second_result.text)
    for result in (first_result, second_result):
        # 数据库繁忙时允许客户端重试同一个请求，最终仍必须取得首次提交的收据。
        if result.status_code == 409:
            result = await client.post(path, json=request)
        assert result.is_success and result.json()["id"] == successful[0].json()["id"], result.text
    assert len(await history(client, base)) == 1
    assert await state(client, base, rows) == [("新城", 1)]


@pytest.mark.parametrize("operation", ["apply", "undo"])
async def test_recreated_last_chapter_is_not_a_target_even_when_id_and_revision_match(client, operation):
    base, rows = await setup_book(client, "青石镇", "青石镇")
    request = application((await preview(client, base))["items"])
    if operation == "undo":
        batch = await apply_batch(client, base, request)
    deleted = await client.delete(chapter_url(base, rows[1]))
    assert deleted.status_code == 204, deleted.text
    new = await chapter(client, base, 2, "青石镇")
    assert rows[1]["id"] == new["id"]  # SQLite 重用章数字 ID。
    if operation == "undo":
        updated = await client.patch(chapter_url(base, new), json={"content": "新城", "expected_content_revision": 0})
        assert updated.status_code == 200 and updated.json()["content_revision"] == 1
        failed = await client.post(undo_url(base, batch))
        expected = [("新城", 1), ("新城", 1)]
    else:
        failed = await client.post(f"{base}/book-search/apply", json=request)
        expected = [("青石镇", 0), ("青石镇", 0)]
    assert failed.status_code == 409, failed.text
    assert {item["chapter_no"] for item in failed.json()["conflicts"]} == {2}
    assert await state(client, base, rows) == expected
    assert [row["status"] for row in await history(client, base)] == (["applied"] if operation == "undo" else [])


async def test_two_deleted_chapters_with_the_same_labels_remain_distinct_conflicts(client):
    base, rows = await setup_book(client, "青石镇甲")
    rows.append(await chapter(client, base, 1, "青石镇乙"))
    batch = await apply_batch(client, base, application((await preview(client, base))["items"]))
    for row in rows:
        deleted = await client.delete(chapter_url(base, row))
        assert deleted.status_code == 204, deleted.text
    failed = await client.post(undo_url(base, batch))
    assert failed.status_code == 409, failed.text
    assert [(item["chapter_no"], item["chapter_title"]) for item in failed.json()["conflicts"]] == [(1, "第1章")] * 2
    assert [item["status"] for item in await history(client, base)] == ["applied"]


@pytest.mark.parametrize("operation", ["apply", "undo"])
async def test_failure_after_first_chapter_write_rolls_back_documents_versions_and_batch(
    client, monkeypatch, operation,
):
    base, rows = await setup_book(client, "青石镇前", "青石镇后")
    request = application((await preview(client, base))["items"])
    if operation == "undo":
        batch = await apply_batch(client, base, request)
    before = await state(client, base, rows)
    paths = [chapter_url(base, row) for row in rows]
    doc_paths = [await document_url(client, base, row) for row in rows]
    docs = [(await client.get(path)).json() for path in doc_paths]
    versions = [(await client.get(f"{path}/versions")).json() for path in paths]
    records = await history(client, base)
    original = scene_service.refresh_projection
    written = []

    async def fail_after_write(db, ch, *args, **kwargs):
        await original(db, ch, *args, **kwargs)
        written.append(ch.id)
        if len(written) == 2:
            raise ConflictError("测试：第二章已刷新投影，回滚本次事务")

    monkeypatch.setattr(scene_service, "refresh_projection", fail_after_write)
    if operation == "undo":
        response = await client.post(undo_url(base, batch))
    else:
        response = await client.post(f"{base}/book-search/apply", json=request)
    assert response.status_code == 409 and len(written) == 2, response.text
    assert await state(client, base, rows) == before
    assert [(await client.get(path)).json() for path in doc_paths] == docs
    assert [(await client.get(f"{path}/versions")).json() for path in paths] == versions
    assert await history(client, base) == records


@pytest.mark.parametrize("first", ["autosave", "batch"])
async def test_editor_document_autosave_and_batch_preserve_the_winner(client, make_client, first):
    base, rows = await setup_book(client, "青石镇甲", "青石镇乙")
    second = make_client()
    logged = await second.post("/api/v1/auth/login", json={"username": "alice", "password": "secret123"})
    assert logged.status_code == 200, logged.text
    doc_url = await document_url(second, base, rows[1])
    draft = save_payload((await second.get(doc_url)).json())
    draft["document"]["content"] += "。作者继续写作。"
    request = application((await preview(client, base))["items"])
    if first == "autosave":
        saved = await second.put(doc_url, json=draft)
        assert saved.status_code == 200, saved.text
        failed = await client.post(f"{base}/book-search/apply", json=request)
        assert failed.status_code == 409, failed.text
        assert "整批修订未执行" in failed.json()["detail"]
        assert {conflict["chapter_no"] for conflict in failed.json()["conflicts"]} == {2}
        assert await state(client, base, rows) == [("青石镇甲", 0), ("青石镇乙。作者继续写作。", 1)]
        assert await history(client, base) == []
    else:
        await apply_batch(client, base, request)
        stale = await second.put(doc_url, json=draft)
        assert stale.status_code == 409 and stale.json()["code"] == "stale_scene_revision", stale.text
        assert await state(client, base, rows) == [("新城甲", 1), ("新城乙", 1)]


async def test_full_marker_cover_and_section_structure_restored_by_undo(client):
    base, rows = await setup_book(client, "甲乙：青石镇。结尾")
    path = await document_url(client, base, rows[0])
    body = save_payload((await client.get(path)).json())
    add_section(body, enabled=True)
    body["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 7, "end": 9}]
    body["document"]["markers"] = [{
        "key": MARKER, "section_key": None, "status": "active", "start": 3, "end": 6,
        "color": "blue", "excerpt": "", "last_start": None,
    }]
    body["add_marker_keys"] = [MARKER]
    saved = await client.put(path, json=body)
    assert saved.status_code == 200, saved.text
    before = saved.json()["document"]
    found = (await preview(client, base))["items"]
    assert len(found) == 1 and found[0]["warning"]
    batch = await apply_batch(client, base, application(found))
    changed = (await client.get(path)).json()["document"]
    assert changed["content"] == "甲乙：新城。结尾"
    assert changed["markers"][0]["status"] == "stale"
    assert changed["formatting"]["inline"] == [{"kind": "bold", "start": 6, "end": 8}]
    assert changed["sections"] == before["sections"]
    undone = await client.post(undo_url(base, batch))
    assert undone.status_code == 200, undone.text
    assert (await client.get(path)).json()["document"] == before


async def test_replay_and_undo_require_current_write_grant(client, make_client):
    base, rows = await setup_book(client, "青石镇")
    guest = make_client()
    user = await register(guest, "batch-guest")
    members = f"{base}/collaborators"
    invite = await client.post(members, json={"username": user["username"], "role": "editor"})
    assert invite.status_code == 201, invite.text
    accepted = await guest.post(f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
                                json={"expected_revision": 1})
    assert accepted.status_code == 200, accepted.text
    request = application((await preview(guest, base))["items"])
    batch = await apply_batch(guest, base, request)
    changed = await client.patch(f"{members}/{user['id']}", json={"role": "viewer", "expected_revision": 2})
    assert changed.status_code == 200, changed.text
    lookup = await guest.post(f"{base}/book-search/query", json={"query": "新城"})
    assert lookup.status_code == 200 and lookup.json()["total"] == 1
    assert [row["id"] for row in await history(guest, base)] == [batch["id"]]
    replay = await guest.post(f"{base}/book-search/apply", json=request)
    undo = await guest.post(undo_url(base, batch))
    assert (replay.status_code, undo.status_code) == (403, 403)
    revoked = await client.delete(f"{members}/{user['id']}", params={"expected_revision": 3})
    assert revoked.status_code == 204, revoked.text
    assert (await guest.get(f"{base}/book-search/batches")).status_code == 404
    assert (await guest.post(undo_url(base, batch))).status_code == 404
    assert await state(client, base, rows) == [("新城", 1)]


@pytest.mark.parametrize("operation", ["apply", "undo"])
@pytest.mark.parametrize("busy", ["chapter", "batch_range"])
async def test_busy_chapter_blocks_only_the_selected_batch(app, client, operation, busy):
    base, rows = await setup_book(client, "青石镇一", "青石镇二", "青石镇三")
    request = application((await preview(client, base))["items"][:2])
    if operation == "undo":
        batch = await apply_batch(client, base, request)
    async with app.state.sessionmaker() as db:
        if busy == "chapter":
            await db.execute(update(Chapter).where(Chapter.id == rows[1]["id"]).values(status="generating"))
        else:
            actor = (await client.get("/api/v1/auth/me")).json()
            db.add(Task(user_id=actor["id"], project_id=rows[1]["project_id"], kind=BATCH_KIND, status="running",
                        input={"chapter_ids": [rows[1]["id"]]}))
        await db.commit()
    if operation == "undo":
        response = await client.post(undo_url(base, batch))
        expected = [("新城一", 1), ("新城二", 1), ("青石镇三", 0)]
    else:
        response = await client.post(f"{base}/book-search/apply", json=request)
        expected = [("青石镇一", 0), ("青石镇二", 0), ("青石镇三", 0)]
    assert response.status_code == 409, response.text
    assert await state(client, base, rows) == expected
    assert [row["status"] for row in await history(client, base)] == (["applied"] if operation == "undo" else [])
