"""作品备份随章节版本保存修订历史，迁移后仍能按原批次安全撤销。"""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.batch_revision import RevisionBatch
from app.schemas.book_search import SelectedHit
from app.services.project_transfer.batch_revisions import REVISION_TABLES
from tests.test_project_transfer import API, _backup, _import, _preview_raw
from tests.test_rich_document_api import MARKER, add_section, save_payload
from tests.test_scenes import setup_scene


async def _revised(client, *, same_label=False):
    project, chapter, url, scene = await setup_scene(client, "卷首🙂青石镇，引文青石镇。")
    doc_url = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(doc_url)).json())
    add_section(body)  # 锚在改名之前，正文外的作者草稿也须维持原结构。
    body["document"]["markers"] = [{"key": MARKER, "status": "active", "start": 3, "end": 6, "color": "green"}]
    body["add_marker_keys"] = [MARKER]
    saved = await client.put(doc_url, json=body)
    assert saved.status_code == 200, saved.text
    second_chapter = {"title": "后记", "content": "青石镇的旧址"}
    if same_label:
        second_chapter.update(title=chapter["title"], chapter_no=chapter["chapter_no"])
    second = await client.post(f"{API}/{project['id']}/chapters", json=second_chapter)
    assert second.status_code == 201
    base = f"{API}/{project['id']}/book-search"
    change = {"query": "青石镇", "replacement": "青城"}
    preview = await client.post(f"{base}/preview", json=change)
    assert preview.status_code == 200, preview.text
    first, _quotation, later = preview.json()["items"]
    hits = [{key: hit[key] for key in SelectedHit.model_fields} for hit in (first, later)]
    response = await client.post(f"{base}/apply", json={**change, "request_id": str(uuid4()), "hits": hits})
    assert response.status_code == 200, response.text
    return project["id"], chapter["id"], second.json()["id"], scene["id"]


async def test_batch_revision_transfer_roundtrip_and_undo(app, client):
    pid, cid, _later, sid = await _revised(client)
    backup = await _backup(client, pid)
    assert (backup["version"], backup["app_schema"]) == (18, "0079")
    batch, = backup["tables"]["revision_batches"]
    assert not {"actor_id", "request_id", "payload_hash"} & batch.keys()
    details = backup["tables"]["revision_batch_scenes"]
    assert len(details) == 2 and all(row["matches_revision"] for row in details)
    assert all("applied_revision" not in row for row in details)
    # 外来文件夹带的私有字段不能植入请求归属、去重凭据或当前 revision。
    batch.update(actor_id=675, request_id=str(uuid4()), payload_hash="a" * 64)
    details[0]["applied_revision"] = 12345
    new_pid = (await _import(client, backup))["project"]["id"]
    imported = await _backup(client, new_pid)
    new_batch, = imported["tables"]["revision_batches"]
    new_first = next(row for row in imported["tables"]["revision_batch_scenes"] if row["chapter_no_at_apply"] == 1)
    assert new_batch["id"] != batch["id"] and new_first["chapter_id"] != cid and new_first["scene_id"] != sid
    async with app.state.sessionmaker() as db:
        obj = await db.scalar(select(RevisionBatch).where(RevisionBatch.id == new_batch["id"]))
        assert (obj.actor_id, obj.request_id, obj.payload_hash) == (None, None, None)

    undone = await client.post(f"{API}/{new_pid}/book-search/batches/{new_batch['id']}/undo")
    assert undone.status_code == 200, undone.text
    url = f"{API}/{new_pid}/chapters/{new_first['chapter_id']}/scenes/{new_first['scene_id']}/document"
    restored = (await client.get(url)).json()["document"]
    assert restored == new_first["before_document"]
    assert restored["markers"][0]["status"] == "active" and restored["sections"][0]["content"] == "作者🙂"
    assert restored["content"] == "卷首🙂青石镇，引文青石镇。"
    assert (await client.get(f"{API}/{pid}/chapters/{cid}")).json()["content"] == "卷首🙂青城，引文青石镇。"
    # 已撤销状态也可继续迁移。
    after = await _backup(client, new_pid)
    second_copy = (await _import(client, after))["project"]["id"]
    history = (await client.get(f"{API}/{second_copy}/book-search/batches")).json()
    assert history["items"][0]["status"] == "undone"


@pytest.mark.parametrize("deleted", [False, True])
async def test_batch_revision_transfer_conflict_is_still_atomic(client, deleted):
    pid, cid, later, _ = await _revised(client)
    path = f"{API}/{pid}/chapters/{later}"
    if deleted:
        assert (await client.delete(path)).is_success
    else:
        # 后来改回同一文本也不能恢复之前批次的撤销资格。
        for content in ("后记已改", "青城的旧址"):
            current = (await client.get(path)).json()
            changed = await client.patch(path, json={
                "content": content, "expected_content_revision": current["content_revision"],
            })
            assert changed.status_code == 200, changed.text
    backup = await _backup(client, pid)
    details = backup["tables"]["revision_batch_scenes"]
    assert {r["chapter_no_at_apply"]: r["matches_revision"] for r in details} == {1: True, 2: False}
    new_pid = (await _import(client, backup))["project"]["id"]
    imported = await _backup(client, new_pid)
    batch, = imported["tables"]["revision_batches"]
    response = await client.post(f"{API}/{new_pid}/book-search/batches/{batch['id']}/undo")
    assert response.status_code == 409, response.text
    assert any(row["chapter_no"] == 2 for row in response.json()["conflicts"])
    current = await _backup(client, new_pid)
    assert current["tables"]["revision_batches"][0]["status"] == "applied"
    assert {r["chapter_no"]: r["content"] for r in current["tables"]["chapters"]}[1] == "卷首🙂青城，引文青石镇。"


async def test_batch_revision_transfer_rejects_corrupt_claims_and_handles_old_files(client):
    pid, _cid, _later, _sid = await _revised(client)
    data = await _backup(client, pid, versions=1)
    first, second = data["tables"]["revision_batch_scenes"]
    without = await _backup(client, pid, versions=0)
    assert not set(REVISION_TABLES).intersection(without["tables"])
    restored = (await _import(client, without))["project"]["id"]
    assert (await client.get(f"{API}/{restored}/book-search/batches")).json()["total"] == 0

    old = deepcopy(without)
    old["version"] = 13
    old["app_schema"] = "0059"
    restored = (await _import(client, old))["project"]["id"]
    assert (await _backup(client, restored))["tables"]["revision_batches"] == []
    old["tables"]["revision_batches"] = data["tables"]["revision_batches"]
    assert (await _preview_raw(client, json.dumps(old).encode())).status_code == 400
    incomplete = deepcopy(data)
    for table in REVISION_TABLES:
        incomplete["tables"].pop(table)
    assert (await _preview_raw(client, json.dumps(incomplete).encode())).status_code == 400

    for changes in (
        {"scene_id": second["scene_id"]}, {"scene_id": 987654321}, {"chapter_id": second["chapter_id"]},
        {"start_offsets": [0]}, {"hit_count": 2}, {"matches_revision": "yes"}, {"chapter_slot": second["chapter_slot"]},
        {"before_document": {**first["before_document"], "content": first["before_document"]["content"] + "伪造"}},
        {"before_document": {"content": "青石镇"}},
    ):
        broken = deepcopy(data)
        broken["tables"]["revision_batch_scenes"][0].update(changes)
        rejected = await _preview_raw(client, json.dumps(broken).encode())
        assert rejected.status_code == 400, (changes, rejected.text)
    broken = deepcopy(data)
    row = broken["tables"]["revision_batch_scenes"][0]
    row["before_document"]["sections"][0]["key"] = str(uuid4())
    row["matches_revision"] = False
    assert (await _preview_raw(client, json.dumps(broken).encode())).status_code == 400


async def test_deleted_identical_chapter_labels_remain_distinct_after_transfer(client):
    pid, cid, later, _ = await _revised(client, same_label=True)
    for chapter_id in (cid, later):
        response = await client.delete(f"{API}/{pid}/chapters/{chapter_id}")
        assert response.is_success, response.text
    backup = await _backup(client, pid)
    rows = backup["tables"]["revision_batch_scenes"]
    assert {row["chapter_id"] for row in rows} == {None}
    assert len({(row["chapter_no_at_apply"], row["chapter_title"]) for row in rows}) == 1
    assert {row["chapter_slot"] for row in rows} == {1, 2}
    restored = (await _import(client, backup))["project"]["id"]
    history = (await client.get(f"{API}/{restored}/book-search/batches")).json()
    batch = history["items"][0]
    rejected = await client.post(f"{API}/{restored}/book-search/batches/{batch['id']}/undo")
    assert rejected.status_code == 409 and len(rejected.json()["conflicts"]) == batch["chapter_count"] == 2


async def test_batch_revision_transfer_checks_json_types_before_replaying(client):
    pid, *_ = await _revised(client)
    data = await _backup(client, pid, versions=1)
    # 第二章第一个命中的合法偏移是 0，False 若走到 Python 切片会被当成相同位置。
    row = next(item for item in data["tables"]["revision_batch_scenes"] if item["start_offsets"] == [0])
    for versions in (True, False):
        data["options"]["versions"] = versions
        for field, value, message in (
            ("before_document", None, "不能为空"), ("before_document", 1, "应为对象"),
            ("before_document", [], "应为对象"), ("before_document", "正文", "应为对象"),
            ("start_offsets", None, "不能为空"), ("start_offsets", 0, "应为数组"),
            ("start_offsets", [False], "应为整数数组"), ("start_offsets", [0.0], "应为整数数组"),
            ("start_offsets", ["0"], "应为整数数组"),
        ):
            original = row[field]
            row[field] = value
            rejected = await _preview_raw(client, json.dumps(data).encode())
            row[field] = original
            assert rejected.status_code == 400, rejected.text
            assert any(f"{field} {message}" in problem for problem in rejected.json()["errors"])
