"""从真实稿件写入入口验证正文批注的原句身份和事务边界。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.errors import ConflictError
from app.schemas.rich_document import MAX_SECTION_CONTENT, SceneDocumentSave
from app.services import version_service
from app.services.scene_service import content_fingerprint
from tests.test_batch_revision import application, apply_batch, preview, undo_url
from tests.test_batch_revision import chapter as create_chapter
from tests.test_rich_document_api import add_section, save_payload
from tests.test_scene_move_history import _move
from tests.test_scenes import setup_scene


async def annotate(client, url, sid, start, end):
    chapter = (await client.get(url)).json()
    source = (await client.get(f"{url}/scenes/{sid}")).json()["content"]
    response = await client.post(f"{url}/annotations", json={
        "scene_id": sid, "expected_content_revision": chapter["content_revision"], "start": start, "end": end,
        "selection_hash": content_fingerprint(source[start:end]), "operation_id": str(uuid4()), "body": "审稿意见",
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def threads(client, url):
    response = await client.get(f"{url}/annotations")
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()["items"]}


def position(row):
    return row["anchor_valid"], row["start"], row["end"]


async def test_known_steps_shift_second_occurrence_and_unknown_overwrite_stays_invalid_after_aba(client):
    source = "🙂相同\r\n相同"
    _, _, url, scene = await setup_scene(client, source)
    endpoint = f"{url}/scenes/{scene['id']}/document"
    tid = await annotate(client, url, scene["id"], 5, 7)
    body = save_payload((await client.get(endpoint)).json())
    body["prose_edits"] = [{"start": 0, "end": 0, "insert": "引"}, {"start": 1, "end": 2, "insert": "😄🙂"}]
    body["document"]["content"] = "引😄" + source
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    assert position((await threads(client, url))[tid]) == (True, 7, 9)

    # 两次整景覆盖把字面恢复到原样，不能由同一指纹使旧锚点复活。
    unknown = save_payload(saved.json())
    unknown["document"]["content"] += "尾"
    saved = await client.put(endpoint, json=unknown)
    assert saved.status_code == 200, saved.text
    restored = save_payload(saved.json())
    restored["document"]["content"] = body["document"]["content"]
    assert (await client.put(endpoint, json=restored)).status_code == 200
    row = (await threads(client, url))[tid]
    assert position(row) == (False, None, None) and row["excerpt"] == "相同"


async def test_section_insert_and_bad_replay_preserve_discussion_without_jump(client):
    _, _, url, scene = await setup_scene(client, "甲乙甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    first = await annotate(client, url, scene["id"], 0, 2)
    second = await annotate(client, url, scene["id"], 2, 4)
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    rows = await threads(client, url)
    assert position(rows[first]) == (False, None, None)
    assert position(rows[second]) == (True, 2, 4)
    body = save_payload(saved.json())
    body["document"]["content"] = "甲乙甲乙！"
    body["mapped_section_keys"] = [body["document"]["sections"][0]["key"]]
    body["prose_edits"] = [{"start": 0, "end": 0, "insert": "不匹配"}]
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    assert position((await threads(client, url))[second]) == (False, None, None)


async def test_batch_revision_undo_cross_chapter_move_and_scene_retirement(client):
    project, _, url, scene = await setup_scene(client, "青石镇遇见青石镇。")
    base = f"/api/v1/projects/{project['id']}"
    sid = scene["id"]
    affected = await annotate(client, url, sid, 0, 3)
    shifted = await annotate(client, url, sid, 5, 8)
    hits = (await preview(client, base))["items"]
    batch = await apply_batch(client, base, application(hits[:1]))
    rows = await threads(client, url)
    assert position(rows[affected]) == (False, None, None)
    assert position(rows[shifted]) == (True, 4, 7)
    assert (await client.post(undo_url(base, batch))).status_code == 200
    assert position((await threads(client, url))[shifted]) == (True, 5, 8)

    target = await create_chapter(client, base, 2, "余稿")
    moved = await _move(client, url, sid, target)
    target_url = f"{base}/chapters/{target['id']}"
    assert not await threads(client, url)
    rows = await threads(client, target_url)
    assert position(rows[affected]) == (False, None, None)
    assert position(rows[shifted]) == (True, 5, 8)
    retired = await client.delete(f"{target_url}/scenes/{sid}", params={
        "expected_content_revision": moved["target_content_revision"],
    })
    assert retired.status_code == 200, retired.text
    assert position((await threads(client, target_url))[shifted]) == (False, None, None)


async def test_scene_save_failure_rolls_back_proven_shift(client, monkeypatch):
    _, _, url, scene = await setup_scene(client, "同步回滚")
    sid = scene["id"]
    tid = await annotate(client, url, sid, 2, 4)
    endpoint = f"{url}/scenes/{sid}/document"
    body = save_payload((await client.get(endpoint)).json())
    body["document"]["content"] = "开头同步回滚"
    body["prose_edits"] = [{"start": 0, "end": 0, "insert": "开头"}]

    async def fail_after_projection(*args, **kwargs):
        raise ConflictError("模拟版本冲突")

    monkeypatch.setattr(version_service, "record_manual", fail_after_projection)
    failed = await client.put(endpoint, json=body)
    assert failed.status_code == 409 and failed.json()["detail"] == "模拟版本冲突"
    assert (await client.get(url)).json()["content"] == "同步回滚"
    assert position((await threads(client, url))[tid]) == (True, 2, 4)


@pytest.mark.parametrize("offset,expected", [(2, False), (3, True)])
async def test_split_preserves_only_ranges_that_stay_in_original_scene(client, offset, expected):
    _, _, url, scene = await setup_scene(client, "前原句尾")
    tid = await annotate(client, url, scene["id"], 1, 3)
    split = await client.post(f"{url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 0, "offset": offset,
    })
    assert split.status_code == 200, split.text
    assert (await threads(client, url))[tid]["anchor_valid"] is expected
    ids = [item["id"] for item in split.json()["items"]]
    retired_thread = await annotate(client, url, ids[1], 0, 1)
    request = {"expected_content_revision": split.json()["content_revision"], "scene_ids": ids}
    previewed = await client.post(f"{url}/scenes/merge-preview", json=request)
    assert previewed.status_code == 200, previewed.text
    plan = previewed.json()
    merged = await client.post(f"{url}/scenes/merge", json={
        **request, "metadata_source": "left", "expected_planning_revision": plan["planning_revision"],
        "signature": plan["signature"],
    })
    assert merged.status_code == 200, merged.text
    rows = await threads(client, url)
    assert rows[tid]["anchor_valid"] is expected
    assert position(rows[retired_thread]) == (False, None, None)


async def test_ai_rewrite_shifts_anchor_then_history_restore_invalidates_only_changed_scene(client, fake_llm):
    from tests.helpers import create_ai_config, parse_sse

    _, _, url, scene = await setup_scene(client, "改这句。追踪原句。")
    sid = scene["id"]
    await create_ai_config(client)
    other = await client.post(f"{url}/scenes", json={"expected_content_revision": 0, "content": "另一景"})
    assert other.status_code == 201, other.text
    untouched = await annotate(client, url, other.json()["id"], 0, 3)
    tid = await annotate(client, url, sid, 4, 8)
    baseline = (await client.get(f"{url}/versions")).json()[0]["id"]
    detail = (await client.get(f"{url}/scenes/{sid}/document")).json()
    revisions = {f"expected_{key}": detail[key] for key in ("content_revision", "planning_revision")}
    fake_llm.push("改")
    previewed = await client.post(f"{url}/scenes/{sid}/rewrite", json={
        **revisions, "op": "rewrite", "start": 0, "end": 3,
    })
    kind, done = parse_sse(previewed.text)[-1]
    assert kind == "done", previewed.text
    applied = await client.post(f"{url}/scenes/{sid}/rewrite/apply", json={
        **revisions, **{key: done[key] for key in ("start", "end", "original", "before", "log_id")},
        "replacement": done["text"],
    })
    assert applied.status_code == 200, applied.text
    rows = await threads(client, url)
    assert position(rows[tid]) == (True, 2, 6)
    assert position(rows[untouched]) == (True, 0, 3)
    restored = await client.post(f"{url}/versions/{baseline}/restore", json={
        "expected_content_revision": applied.json()["content_revision"],
    })
    assert restored.status_code == 200, restored.text
    rows = await threads(client, url)
    assert position(rows[tid]) == (False, None, None)
    assert position(rows[untouched]) == (True, 0, 3)


def test_document_save_limits_total_step_evidence_even_when_each_step_fits():
    body = save_payload({"content_revision": 0, "document": {
        "content": "稿", "formatting": {"version": 1, "boundaries": [], "inline": [], "blocks": []},
        "sections": [], "retired_section_keys": [], "markers": [],
    }})
    assert SceneDocumentSave.model_validate(body).prose_edits is None
    body["prose_edits"] = [
        {"start": 0, "end": 0, "insert": "甲" * MAX_SECTION_CONTENT},
        {"start": 0, "end": MAX_SECTION_CONTENT, "insert": "乙"},
    ]
    with pytest.raises(ValidationError, match="插入总量过大"):
        SceneDocumentSave.model_validate(body)
