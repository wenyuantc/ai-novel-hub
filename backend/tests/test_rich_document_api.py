"""结构化文档保存的显式意图、版本竞争和 AI 可见性。"""

from copy import deepcopy

import pytest
from sqlalchemy import select

from app.models import Chapter, ChapterVersion, Project, SceneSection
from app.services import rich_document_core
from tests.helpers import register
from tests.test_project_transfer import _backup, _import
from tests.test_scenes import setup_scene

KEY = "0981fe8e-f4cc-4336-80a2-df11c2812834"
MARKER = "3fe7d66f-c5fd-4150-8232-99b47df96ccb"
SECTION_BEFORE = "87a7e858-6cb4-4215-b753-359ba054ff01"
SECTION_AFTER = "87a7e858-6cb4-4215-b753-359ba054ff02"


def save_payload(data, *, revision=None):
    return {
        "expected_content_revision": data["content_revision"] if revision is None else revision,
        "document": deepcopy(data["document"]),
        "add_section_keys": [], "delete_section_keys": [],
        "add_marker_keys": [], "delete_marker_keys": [],
        "mapped_section_keys": [], "mapped_marker_keys": [],
    }


def add_section(body, *, enabled=False):
    body["document"]["sections"].append({
        "key": KEY, "anchor": 1, "order": 0, "title": "备选", "content": "作者🙂",
        "formatting": {"version": 1, "boundaries": [], "inline": [], "blocks": []},
        "include_in_ai": enabled,
    })
    body["add_section_keys"].append(KEY)


async def test_document_atomic_save_history_and_conflict(app, client):
    project, chapter, url, scene = await setup_scene(client, "甲\r\n乙🙂")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    got = await client.get(endpoint)
    assert got.status_code == 200, got.text
    data = got.json()
    assert data["document"]["content"] == "甲\r\n乙🙂" and data["document"]["sections"] == []
    body = save_payload(data)
    add_section(body)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["content_revision"] == 1
    assert saved.json()["document"]["sections"][0]["include_in_ai"] is False
    assert (await client.get(url)).json()["content"] == "甲\r\n乙🙂"
    stale = await client.put(endpoint, json=body)
    assert stale.status_code == 409 and stale.json()["code"] == "stale_scene_revision"
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project["id"])).ai_visibility_revision == 0
        assert (await db.get(Chapter, chapter["id"])).content_revision == 1
        assert len(list(await db.scalars(select(SceneSection)))) == 1
        versions = list(await db.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter["id"])
                                         .order_by(ChapterVersion.seq)))
        assert versions[-1].scene_snapshot["scenes"][0]["document"]["sections"][0]["content"] == "作者🙂"


async def test_document_deletion_requires_explicit_intent_and_keeps_identity(client):
    _project, _chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    original = (await client.get(endpoint)).json()
    body = save_payload(original)
    add_section(body)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    deletion = save_payload(saved.json())
    deletion["document"]["sections"] = []
    deletion["document"]["retired_section_keys"] = [KEY]
    rejected = await client.put(endpoint, json=deletion)
    assert rejected.status_code == 409 and rejected.json()["code"] == "document_intent_mismatch"
    assert (await client.get(endpoint)).json()["document"]["sections"][0]["key"] == KEY
    deletion["delete_section_keys"] = [KEY]
    deleted = await client.put(endpoint, json=deletion)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["document"]["retired_section_keys"] == [KEY]
    reused = save_payload(deleted.json())
    reused["document"]["retired_section_keys"] = []
    add_section(reused)
    rejected = await client.put(endpoint, json=reused)
    assert rejected.status_code == 409 and rejected.json()["code"] == "document_intent_mismatch"


async def test_explicit_section_restore_preserves_identity_and_section_marker(app, client):
    _project, _chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body, enabled=True)
    body["document"]["markers"] = [{
        "key": MARKER, "section_key": KEY, "status": "active", "start": 0, "end": 3,
        "color": "blue", "excerpt": "", "last_start": None,
    }]
    body["add_marker_keys"] = [MARKER]
    created = await client.put(endpoint, json=body)
    assert created.status_code == 200, created.text
    before = created.json()["document"]
    async with app.state.sessionmaker() as db:
        source_id = await db.scalar(select(SceneSection.id).where(SceneSection.scene_id == scene["id"]))
    deletion = save_payload(created.json())
    deletion["document"]["sections"] = []
    deletion["document"]["retired_section_keys"] = [KEY]
    deletion["document"]["markers"][0].update(status="stale", start=None, end=None, last_start=0, excerpt="作者🙂")
    deletion["delete_section_keys"] = [KEY]
    deleted = await client.put(endpoint, json=deletion)
    assert deleted.status_code == 200, deleted.text

    restore = save_payload(deleted.json())
    restore["document"] = before
    restore["add_section_keys"] = [KEY]
    restore["mapped_marker_keys"] = [MARKER]
    assert (await client.put(endpoint, json=restore)).status_code == 409  # 旧身份不能冒充新建。
    restore["add_section_keys"] = []
    restore["restore_section_keys"] = [KEY]
    no_mapping = deepcopy(restore)
    no_mapping["mapped_marker_keys"] = []
    assert (await client.put(endpoint, json=no_mapping)).status_code == 409
    restored = await client.put(endpoint, json=restore)
    assert restored.status_code == 200, restored.text
    assert restored.json()["document"] == before
    assert (await client.put(endpoint, json=restore)).status_code == 409
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(SceneSection.id).where(SceneSection.scene_id == scene["id"])) == source_id


async def test_section_restore_allows_unrelated_uuid_but_rejects_live_lineage(app, client):
    _project, _chapter, url, first = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{first['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    created = await client.put(endpoint, json=body)
    section = created.json()["document"]["sections"][0]
    deletion = save_payload(created.json())
    deletion["document"]["sections"] = []
    deletion["document"]["retired_section_keys"] = [KEY]
    deletion["delete_section_keys"] = [KEY]
    deleted = await client.put(endpoint, json=deletion)
    assert deleted.status_code == 200, deleted.text
    other_scene = await client.post(f"{url}/scenes", json={
        "expected_content_revision": deleted.json()["content_revision"], "content": "新景",
    })
    assert other_scene.status_code == 201, other_scene.text
    other_endpoint = f"{url}/scenes/{other_scene.json()['id']}/document"
    other = save_payload((await client.get(other_endpoint)).json())
    add_section(other)
    assert (await client.put(other_endpoint, json=other)).status_code == 200
    restore = save_payload((await client.get(endpoint)).json())
    restore["document"]["sections"] = [section]
    restore["document"]["retired_section_keys"] = []
    restore["restore_section_keys"] = [KEY]
    unrelated = await client.put(endpoint, json=restore)
    assert unrelated.status_code == 200, unrelated.text
    deletion = save_payload(unrelated.json())
    deletion["document"]["sections"] = []
    deletion["document"]["retired_section_keys"] = [KEY]
    deletion["delete_section_keys"] = [KEY]
    assert (await client.put(endpoint, json=deletion)).status_code == 200
    async with app.state.sessionmaker() as db:
        source = await db.scalar(select(SceneSection).where(SceneSection.scene_id == first["id"]))
        descendant = await db.scalar(select(SceneSection).where(SceneSection.scene_id == other_scene.json()["id"]))
        descendant.origin_section_id = source.id
        await db.commit()
    restore = save_payload((await client.get(endpoint)).json())
    restore["document"]["sections"] = [section]
    restore["document"]["retired_section_keys"] = []
    restore["restore_section_keys"] = [KEY]
    blocked = await client.put(endpoint, json=restore)
    assert blocked.status_code == 409 and blocked.json()["code"] == "section_identity_conflict"
    assert (await client.get(endpoint)).json()["document"]["sections"] == []
    assert (await client.get(other_endpoint)).json()["document"]["sections"] == [section]


async def test_ai_visibility_version_advances_only_for_enabled_source_changes(app, client):
    project, _chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    doc = (await client.get(endpoint)).json()
    new = save_payload(doc)
    add_section(new, enabled=True)
    first = await client.put(endpoint, json=new)
    assert first.status_code == 200, first.text
    async with app.state.sessionmaker() as db:
        project_row = await db.get(Project, project["id"])
        initial_revision, initial_epoch = project_row.ai_visibility_revision, project_row.ai_history_epoch
    formatting = save_payload(first.json())
    formatting["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 0, "end": 1}]
    formatted = await client.put(endpoint, json=formatting)
    assert formatted.status_code == 200, formatted.text
    async with app.state.sessionmaker() as db:
        project_row = await db.get(Project, project["id"])
        assert (project_row.ai_visibility_revision, project_row.ai_history_epoch) == (initial_revision, initial_epoch)
    disable = save_payload(formatted.json())
    disable["document"]["sections"][0]["include_in_ai"] = False
    stopped = await client.put(endpoint, json=disable)
    assert stopped.status_code == 200, stopped.text
    async with app.state.sessionmaker() as db:
        project_row = await db.get(Project, project["id"])
        assert project_row.ai_visibility_revision == initial_revision + 1
        assert project_row.ai_history_epoch == initial_epoch + 1


async def test_document_scope_and_crlf_boundary(client, make_client):
    _project, _chapter, url, scene = await setup_scene(client, "甲\r\n乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    other = make_client()
    await register(other, "outsider")
    assert (await other.get(endpoint)).status_code == 404
    data = (await client.get(endpoint)).json()
    body = save_payload(data)
    add_section(body)
    body["document"]["sections"][0]["anchor"] = 2
    assert (await client.put(endpoint, json=body)).status_code == 422
    assert (await client.get(endpoint)).json()["content_revision"] == 0


async def test_split_crlf_rejects_only_metadata_at_chapter_boundary(client):
    _project, _chapter, url, scene = await setup_scene(client, "甲\r\n乙")
    split = await client.post(f"{url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 0, "offset": 2,
    })
    assert split.status_code == 200, split.text
    left, right = split.json()["items"]
    left_endpoint = f"{url}/scenes/{left['id']}/document"
    right_endpoint = f"{url}/scenes/{right['id']}/document"
    left_data = (await client.get(left_endpoint)).json()
    invalid_section = save_payload(left_data)
    add_section(invalid_section)
    invalid_section["document"]["sections"][0]["anchor"] = 2
    assert (await client.put(left_endpoint, json=invalid_section)).status_code == 422
    right_data = (await client.get(right_endpoint)).json()
    invalid_marker = save_payload(right_data)
    invalid_marker["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 1,
        "color": "yellow", "excerpt": "", "last_start": None,
    })
    invalid_marker["add_marker_keys"].append(MARKER)
    assert (await client.put(right_endpoint, json=invalid_marker)).status_code == 422
    assert (await client.get(url)).json()["content"] == "甲\r\n乙"
    assert (await client.get(left_endpoint)).json()["content_revision"] == 1

    # 同一章存在跨景 CRLF 时，不妨碍其余位置的有效结构。
    valid = save_payload(left_data)
    add_section(valid)
    valid["document"]["sections"][0]["anchor"] = 0
    saved = await client.put(left_endpoint, json=valid)
    assert saved.status_code == 200, saved.text
    right_data = (await client.get(right_endpoint)).json()
    valid_marker = save_payload(right_data)
    valid_marker["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "active", "start": 1, "end": 2,
        "color": "yellow", "excerpt": "", "last_start": None,
    })
    valid_marker["add_marker_keys"].append(MARKER)
    saved = await client.put(right_endpoint, json=valid_marker)
    assert saved.status_code == 200, saved.text


async def test_plain_neighbor_cannot_create_crlf_at_existing_marker(client):
    _project, _chapter, url, scene = await setup_scene(client, "甲X\n乙")
    split = await client.post(f"{url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 0, "offset": 2,
    })
    assert split.status_code == 200, split.text
    left, right = split.json()["items"]
    right_endpoint = f"{url}/scenes/{right['id']}/document"
    body = save_payload((await client.get(right_endpoint)).json())
    body["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 1,
        "color": "yellow", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    saved = await client.put(right_endpoint, json=body)
    assert saved.status_code == 200, saved.text
    revision = saved.json()["content_revision"]
    left_endpoint = f"{url}/scenes/{left['id']}"
    old_path = await client.patch(left_endpoint, json={"expected_content_revision": revision, "content": "甲\r"})
    assert old_path.status_code == 422, old_path.text
    left_doc = (await client.get(f"{left_endpoint}/document")).json()
    changed = save_payload(left_doc)
    changed["document"]["content"] = "甲\r"
    assert (await client.put(f"{left_endpoint}/document", json=changed)).status_code == 422
    assert (await client.get(url)).json()["content"] == "甲X\n乙"
    unrelated = await client.patch(left_endpoint, json={"expected_content_revision": revision, "content": "丙X"})
    assert unrelated.status_code == 200, unrelated.text
    assert (await client.get(url)).json()["content"] == "丙X\n乙"


async def test_scene_reorder_checks_new_crlf_seams_without_blocking_safe_order(client):
    _project, _chapter, url, first = await setup_scene(client, "甲\r中\n乙")
    split = await client.post(f"{url}/scenes/{first['id']}/split", json={
        "expected_content_revision": 0, "offset": 2,
    })
    assert split.status_code == 200, split.text
    left, rest = split.json()["items"]
    split_again = await client.post(f"{url}/scenes/{rest['id']}/split", json={
        "expected_content_revision": 1, "offset": 1,
    })
    assert split_again.status_code == 200, split_again.text
    middle, right = split_again.json()["items"]
    right_endpoint = f"{url}/scenes/{right['id']}/document"
    body = save_payload((await client.get(right_endpoint)).json())
    body["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 1,
        "color": "yellow", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    saved = await client.put(right_endpoint, json=body)
    assert saved.status_code == 200, saved.text
    revision = saved.json()["content_revision"]
    invalid = await client.post(f"{url}/scenes/reorder", json={
        "expected_content_revision": revision, "scene_ids": [left["id"], right["id"], middle["id"]],
    })
    assert invalid.status_code == 422, invalid.text
    assert (await client.get(url)).json()["content"] == "甲\r中\n乙"
    valid = await client.post(f"{url}/scenes/reorder", json={
        "expected_content_revision": revision, "scene_ids": [right["id"], left["id"], middle["id"]],
    })
    assert valid.status_code == 200, valid.text
    assert (await client.get(url)).json()["content"] == "\n乙甲\r中"


async def test_existing_section_cannot_be_overwritten_by_omitted_fields(client):
    _project, _chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body, enabled=True)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    for field in ("content", "formatting", "include_in_ai"):
        broken = save_payload(saved.json())
        broken["document"]["sections"][0].pop(field)
        rejected = await client.put(endpoint, json=broken)
        assert rejected.status_code in (409, 422), (field, rejected.text)
        same = (await client.get(endpoint)).json()
        assert same == saved.json()
    broken = save_payload(saved.json())
    broken["document"]["formatting"].pop("inline")
    assert (await client.put(endpoint, json=broken)).status_code in (409, 422)


async def test_changed_text_requires_mapped_positions_not_a_guess(client):
    _project, _chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    body["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 1,
        "color": "yellow", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    changed = save_payload(saved.json())
    changed["document"]["content"] = "前甲乙"
    changed["document"]["sections"][0]["anchor"] = 2
    changed["document"]["markers"][0].update(start=1, end=2)
    rejected = await client.put(endpoint, json=changed)
    assert rejected.status_code == 409 and rejected.json()["code"] == "document_mapping_required"
    changed["mapped_section_keys"] = [KEY]
    rejected = await client.put(endpoint, json=changed)
    assert rejected.status_code == 409 and rejected.json()["code"] == "document_mapping_required"
    changed["mapped_marker_keys"] = [MARKER]
    mapped = await client.put(endpoint, json=changed)
    assert mapped.status_code == 200, mapped.text
    assert mapped.json()["document"]["markers"][0]["start"] == 1
    assert mapped.json()["document"]["sections"][0]["anchor"] == 2


async def test_invalidated_marker_keeps_exact_original_excerpt(client):
    _project, _chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    body["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 1,
        "color": "yellow", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    changed = save_payload(saved.json())
    changed["document"]["content"] = "乙"
    changed["document"]["markers"][0].update(status="stale", start=None, end=None, last_start=0, excerpt="错")
    rejected = await client.put(endpoint, json=changed)
    assert rejected.status_code == 409 and rejected.json()["code"] == "document_intent_mismatch"
    changed["document"]["markers"][0]["excerpt"] = "甲"
    stale = await client.put(endpoint, json=changed)
    assert stale.status_code == 200, stale.text
    assert stale.json()["document"]["markers"][0]["excerpt"] == "甲"


async def test_storage_cap_failure_rolls_back_document_and_visibility(app, client, monkeypatch):
    project, chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    data = (await client.get(endpoint)).json()
    body = save_payload(data)
    add_section(body, enabled=True)
    monkeypatch.setattr(rich_document_core, "MAX_STORED_SECTIONS", 0)
    rejected = await client.put(endpoint, json=body)
    assert rejected.status_code == 422, rejected.text
    async with app.state.sessionmaker() as db:
        assert list(await db.scalars(select(SceneSection))) == []
        assert (await db.get(Chapter, chapter["id"])).content_revision == 0
        project_row = await db.get(Project, project["id"])
        assert (project_row.ai_visibility_revision, project_row.ai_history_epoch) == (0, 0)


async def test_maximum_marker_retains_emoji_and_crlf_when_invalidated(client):
    marked_text = "🙂" * 4_998 + "\r\n"
    _project, _chapter, url, scene = await setup_scene(client, marked_text + "尾")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    body["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 5_000,
        "color": "blue", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    changed = save_payload(saved.json())
    changed["document"]["content"] = "尾"
    changed["document"]["markers"][0].update(
        status="stale", start=None, end=None, last_start=0, excerpt=marked_text,
    )
    stale = await client.put(endpoint, json=changed)
    assert stale.status_code == 200, stale.text
    assert stale.json()["document"]["markers"][0]["excerpt"] == marked_text


async def test_long_marker_growth_invalidation_truncation_backup_and_history(client):
    # 插入后第 5000 个码点是 CR；摘录不能在 CR/LF 中间截断。
    original = "🙂" * 4_998 + "\r\n"
    project, _chapter, url, scene = await setup_scene(client, original)
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    body["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 5_000,
        "color": "blue", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"] = [MARKER]
    initial = await client.put(endpoint, json=body)
    assert initial.status_code == 200, initial.text
    growing = save_payload(initial.json())
    grown_content = "🙂" * 4_999 + "\r\n"
    growing["document"]["content"] = grown_content
    growing["document"]["markers"][0]["end"] = 5_001
    growing["mapped_marker_keys"] = [MARKER]
    grown = await client.put(endpoint, json=growing)
    assert grown.status_code == 200, grown.text
    assert grown.json()["document"]["markers"][0]["status"] == "active"
    active_version = (await client.get(f"{url}/versions")).json()[0]
    timeline = (await client.get(f"/api/v1/projects/{project['id']}/markers")).json()["items"][0]
    assert timeline["excerpt"] == "🙂" * 4_999 and timeline["excerpt_truncated"] is True

    changed = save_payload(grown.json())
    changed["document"]["content"] = ""
    changed["document"]["markers"][0].update(
        status="stale", start=None, end=None, last_start=0, excerpt="🙂" * 4_999,
        excerpt_truncated=False,
    )
    rejected = await client.put(endpoint, json=changed)
    assert rejected.status_code == 409 and rejected.json()["code"] == "document_intent_mismatch"
    changed["document"]["markers"][0]["excerpt_truncated"] = True
    stale = await client.put(endpoint, json=changed)
    assert stale.status_code == 200, stale.text
    marker = stale.json()["document"]["markers"][0]
    assert marker["excerpt"] == "🙂" * 4_999 and marker["excerpt_truncated"] is True
    fake_full = save_payload(stale.json())
    fake_full["document"]["markers"][0]["excerpt_truncated"] = False
    assert (await client.put(endpoint, json=fake_full)).status_code == 409
    timeline = (await client.get(f"/api/v1/projects/{project['id']}/markers")).json()["items"][0]
    assert timeline["status"] == "stale" and timeline["excerpt_truncated"] is True

    backup = await _backup(client, project["id"], versions=1)
    imported = await _import(client, backup)
    copied = await _backup(client, imported["project"]["id"], versions=1)
    assert copied["tables"]["scene_markers"][0]["excerpt_truncated"] is True
    assert copied["tables"]["scene_markers"][0]["excerpt"] == "🙂" * 4_999
    restored = await client.post(f"{url}/versions/{active_version['id']}/restore", json={
        "expected_content_revision": stale.json()["content_revision"], "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    recovered = (await client.get(endpoint)).json()["document"]
    assert recovered["content"] == grown_content
    assert recovered["markers"][0]["status"] == "active" and recovered["markers"][0]["end"] == 5_001


async def test_retired_section_and_stale_marker_survive_backup_with_recoverable_history(client):
    project, _chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    body["document"]["markers"].append({
        "key": MARKER, "section_key": KEY, "status": "active", "start": 0, "end": 2,
        "color": "green", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    deleted = save_payload(saved.json())
    deleted["document"]["sections"] = []
    deleted["document"]["retired_section_keys"] = [KEY]
    deleted["delete_section_keys"] = [KEY]
    deleted["document"]["markers"][0].update(
        status="stale", start=None, end=None, last_start=0, excerpt="作者",
    )
    result = await client.put(endpoint, json=deleted)
    assert result.status_code == 200, result.text
    backup = await _backup(client, project["id"], versions=1)
    imported = await _import(client, backup)
    after = await _backup(client, imported["project"]["id"], versions=1)
    section, marker = after["tables"]["scene_sections"][0], after["tables"]["scene_markers"][0]
    assert section["active"] is False and marker["status"] == "stale"
    assert marker["section_id"] == section["id"] and marker["excerpt"] == "作者"
    histories = [row["scene_snapshot"] for row in after["tables"]["chapter_versions"]
                 if row["scene_snapshot"]["schema_version"] == 2]
    assert any(snapshot["scenes"][0]["document"]["sections"] and
               snapshot["scenes"][0]["document"]["sections"][0]["content"] == "作者🙂"
               for snapshot in histories)


async def test_history_restore_requires_confirmation_and_is_itself_reversible(app, client):
    project, _chapter, url, scene = await setup_scene(client, "")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    new = save_payload((await client.get(endpoint)).json())
    add_section(new, enabled=True)
    new["document"]["sections"][0]["anchor"] = 0
    first = await client.put(endpoint, json=new)
    assert first.status_code == 200, first.text
    first_version = (await client.get(f"{url}/versions")).json()[0]
    edited = save_payload(first.json())
    edited["document"]["sections"][0]["content"] = "修改后的作者资料"
    changed = await client.put(endpoint, json=edited)
    assert changed.status_code == 200, changed.text
    newer_version = (await client.get(f"{url}/versions")).json()[0]
    preview = await client.get(f"{url}/versions/{first_version['id']}/restore-preview", params={
        "expected_content_revision": 2,
    })
    assert preview.status_code == 200, preview.text
    assert preview.json()["requires_rich_confirmation"] is True
    denied = await client.post(f"{url}/versions/{first_version['id']}/restore", json={
        "expected_content_revision": 2,
    })
    assert denied.status_code == 409 and denied.json()["code"] == "rich_document_confirmation_required"
    assert (await client.get(endpoint)).json() == changed.json()
    async with app.state.sessionmaker() as db:
        before = await db.get(Project, project["id"])
        revision, epoch = before.ai_visibility_revision, before.ai_history_epoch
    restored = await client.post(f"{url}/versions/{first_version['id']}/restore", json={
        "expected_content_revision": 2, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    assert (await client.get(endpoint)).json()["document"]["sections"][0]["content"] == "作者🙂"
    async with app.state.sessionmaker() as db:
        after = await db.get(Project, project["id"])
        assert (after.ai_visibility_revision, after.ai_history_epoch) == (revision + 1, epoch + 1)
    again = await client.post(f"{url}/versions/{newer_version['id']}/restore", json={
        "expected_content_revision": 3, "confirm_rich_document_loss": True,
    })
    assert again.status_code == 200, again.text
    assert (await client.get(endpoint)).json()["document"]["sections"][0]["content"] == "修改后的作者资料"


async def test_v1_history_restore_can_reversibly_remove_current_author_material(client):
    _project, _chapter, url, scene = await setup_scene(client, "甲乙")
    old = (await client.get(f"{url}/versions")).json()[0]
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    rich = (await client.get(f"{url}/versions")).json()[0]
    preview = await client.get(f"{url}/versions/{old['id']}/restore-preview", params={
        "expected_content_revision": 1,
    })
    assert preview.status_code == 200 and preview.json()["requires_rich_confirmation"] is True
    restored = await client.post(f"{url}/versions/{old['id']}/restore", json={
        "expected_content_revision": 1, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    assert (await client.get(endpoint)).json()["document"]["sections"] == []
    again = await client.post(f"{url}/versions/{rich['id']}/restore", json={
        "expected_content_revision": 2,
    })
    assert again.status_code == 200, again.text
    assert (await client.get(endpoint)).json()["document"]["sections"][0]["content"] == "作者🙂"


async def test_history_reinstates_section_marker_identity_and_stale_excerpt(client):
    _project, _chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    body["document"]["markers"].append({
        "key": MARKER, "section_key": KEY, "status": "active", "start": 0, "end": 2,
        "color": "blue", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    active_version = (await client.get(f"{url}/versions")).json()[0]
    deleted = save_payload(saved.json())
    deleted["document"]["sections"] = []
    deleted["document"]["retired_section_keys"] = [KEY]
    deleted["delete_section_keys"] = [KEY]
    deleted["document"]["markers"][0].update(
        status="stale", start=None, end=None, last_start=0, excerpt="作者",
    )
    result = await client.put(endpoint, json=deleted)
    assert result.status_code == 200, result.text
    stale_version = (await client.get(f"{url}/versions")).json()[0]
    restored = await client.post(f"{url}/versions/{active_version['id']}/restore", json={
        "expected_content_revision": 2, "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    active = (await client.get(endpoint)).json()["document"]
    assert active["sections"][0]["key"] == KEY
    assert active["markers"][0]["key"] == MARKER and active["markers"][0]["status"] == "active"
    assert active["markers"][0]["section_key"] == KEY
    again = await client.post(f"{url}/versions/{stale_version['id']}/restore", json={
        "expected_content_revision": 3, "confirm_rich_document_loss": True,
    })
    assert again.status_code == 200, again.text
    stale = (await client.get(endpoint)).json()["document"]
    assert stale["sections"] == [] and stale["retired_section_keys"] == [KEY]
    assert stale["markers"][0]["status"] == "stale" and stale["markers"][0]["excerpt"] == "作者"


async def test_history_refuses_overwriting_uncaptured_inactive_scene_material(app, client):
    _project, _chapter, url, first = await setup_scene(client, "首景")
    original = (await client.get(f"{url}/versions")).json()[0]
    second = await client.post(f"{url}/scenes", json={"expected_content_revision": 0, "content": "次景"})
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]
    endpoint = f"{url}/scenes/{second_id}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    rich_version = (await client.get(f"{url}/versions")).json()[0]
    retired = await client.post(f"{url}/versions/{original['id']}/restore", json={
        "expected_content_revision": 2, "confirm_rich_document_loss": True,
    })
    assert retired.status_code == 200, retired.text
    async with app.state.sessionmaker() as db:
        section = await db.scalar(select(SceneSection).where(SceneSection.scene_id == second_id))
        section.content = "无法从当前活动基线恢复的资料"
        await db.commit()
    preview = await client.get(f"{url}/versions/{rich_version['id']}/restore-preview", params={
        "expected_content_revision": 3,
    })
    assert preview.status_code == 200
    assert any(item["kind"] == "rich_document_history" for item in preview.json()["blockers"])
    denied = await client.post(f"{url}/versions/{rich_version['id']}/restore", json={
        "expected_content_revision": 3, "confirm_rich_document_loss": True,
    })
    assert denied.status_code == 409 and denied.json()["code"] == "rich_document_unmapped"
    async with app.state.sessionmaker() as db:
        section = await db.scalar(select(SceneSection).where(SceneSection.scene_id == second_id))
        assert section.content == "无法从当前活动基线恢复的资料"
    assert (await client.get(f"{url}/scenes/{first['id']}")).status_code == 200


async def test_history_missing_section_identity_rolls_back_all_changes(app, client):
    project, chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body, enabled=True)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    version_id = (await client.get(f"{url}/versions")).json()[0]["id"]
    async with app.state.sessionmaker() as db:
        version = await db.get(ChapterVersion, version_id)
        snapshot = deepcopy(version.scene_snapshot)
        snapshot["scenes"][0]["document"]["sections"][0]["key"] = "b402bf83-6973-450c-ae14-6a0e6aeaf7d3"
        version.scene_snapshot = snapshot
        await db.commit()
        before_versions = len(list(await db.scalars(select(ChapterVersion).where(
            ChapterVersion.chapter_id == chapter["id"],
        ))))
        before_project = await db.get(Project, project["id"])
        stamp = before_project.ai_visibility_revision, before_project.ai_history_epoch
    denied = await client.post(f"{url}/versions/{version_id}/restore", json={
        "expected_content_revision": 1, "confirm_rich_document_loss": True,
    })
    assert denied.status_code == 409 and denied.json()["code"] == "rich_document_identity_missing"
    assert (await client.get(endpoint)).json() == saved.json()
    async with app.state.sessionmaker() as db:
        assert len(list(await db.scalars(select(ChapterVersion).where(
            ChapterVersion.chapter_id == chapter["id"],
        )))) == before_versions
        after_project = await db.get(Project, project["id"])
        assert (after_project.ai_visibility_revision, after_project.ai_history_epoch) == stamp


async def test_history_does_not_overwrite_uncaptured_retired_section_data(app, client):
    _project, _chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    rich_version = (await client.get(f"{url}/versions")).json()[0]
    removed = save_payload(saved.json())
    removed["document"]["sections"] = []
    removed["document"]["retired_section_keys"] = [KEY]
    removed["delete_section_keys"] = [KEY]
    assert (await client.put(endpoint, json=removed)).status_code == 200
    async with app.state.sessionmaker() as db:
        section = await db.scalar(select(SceneSection).where(SceneSection.scene_id == scene["id"]))
        section.content = "退休身份里另有未归档资料"
        await db.commit()
    preview = await client.get(f"{url}/versions/{rich_version['id']}/restore-preview", params={
        "expected_content_revision": 2,
    })
    assert preview.status_code == 200
    assert any(item["kind"] == "rich_document_history" for item in preview.json()["blockers"])
    denied = await client.post(f"{url}/versions/{rich_version['id']}/restore", json={
        "expected_content_revision": 2, "confirm_rich_document_loss": True,
    })
    assert denied.status_code == 409 and denied.json()["code"] == "rich_document_unmapped"
    async with app.state.sessionmaker() as db:
        section = await db.scalar(select(SceneSection).where(SceneSection.scene_id == scene["id"]))
        assert section.content == "退休身份里另有未归档资料"


async def test_pre_scene_plain_history_can_be_confirmed_and_reversed(app, client):
    _project, chapter, url, scene = await setup_scene(client, "现在正文")
    async with app.state.sessionmaker() as db:
        legacy = ChapterVersion(chapter_id=chapter["id"], seq=2, source="manual", content="旧\r\n稿🙂", word_count=4)
        db.add(legacy)
        await db.commit()
        legacy_id = legacy.id
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    rich_version = (await client.get(f"{url}/versions")).json()[0]
    preview = await client.get(f"{url}/versions/{legacy_id}/restore-preview", params={
        "expected_content_revision": 1,
    })
    assert preview.status_code == 200 and preview.json()["requires_rich_confirmation"] is True
    assert preview.json()["blockers"] == []
    denied = await client.post(f"{url}/versions/{legacy_id}/restore", json={"expected_content_revision": 1})
    assert denied.status_code == 409 and denied.json()["code"] == "rich_document_confirmation_required"
    accepted = await client.post(f"{url}/versions/{legacy_id}/restore", json={
        "expected_content_revision": 1, "confirm_rich_document_loss": True,
    })
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["content"] == "旧\r\n稿🙂"
    assert (await client.get(endpoint)).json()["document"]["sections"] == []
    again = await client.post(f"{url}/versions/{rich_version['id']}/restore", json={
        "expected_content_revision": 2,
    })
    assert again.status_code == 200, again.text
    document = (await client.get(endpoint)).json()["document"]
    assert document["content"] == "现在正文" and document["sections"][0]["content"] == "作者🙂"


async def test_legacy_extract_confirms_target_only_and_preserves_other_scene(client, app):
    _project, chapter, url, first = await setup_scene(client, "第一景")
    async with app.state.sessionmaker() as db:
        legacy = ChapterVersion(chapter_id=chapter["id"], seq=2, source="manual", content="旧整稿", word_count=3)
        db.add(legacy)
        await db.commit()
        legacy_id = legacy.id
    created = await client.post(f"{url}/scenes", json={"expected_content_revision": 0, "content": "第二景"})
    assert created.status_code == 201, created.text
    second_id = created.json()["id"]
    first_endpoint, second_endpoint = f"{url}/scenes/{first['id']}/document", f"{url}/scenes/{second_id}/document"
    first_body = save_payload((await client.get(first_endpoint)).json())
    add_section(first_body)
    first_body["document"]["sections"][0]["content"] = "另一景资料"
    one = await client.put(first_endpoint, json=first_body)
    assert one.status_code == 200, one.text
    second_body = save_payload((await client.get(second_endpoint)).json())
    add_section(second_body)
    second_body["document"]["sections"][0]["content"] = "目标景资料"
    two = await client.put(second_endpoint, json=second_body)
    assert two.status_code == 200, two.text
    before_version = (await client.get(f"{url}/versions")).json()[0]
    preview = await client.get(f"{url}/versions/{legacy_id}/extract-preview", params={
        "scene_id": second_id, "expected_content_revision": 3,
    })
    assert preview.status_code == 200 and preview.json()["requires_rich_confirmation"] is True
    request = {"scene_id": second_id, "expected_content_revision": 3, "signature": preview.json()["signature"]}
    denied = await client.post(f"{url}/versions/{legacy_id}/extract", json=request)
    assert denied.status_code == 409 and denied.json()["code"] == "rich_document_confirmation_required"
    extracted = await client.post(f"{url}/versions/{legacy_id}/extract", json={
        **request, "confirm_rich_document_loss": True,
    })
    assert extracted.status_code == 200, extracted.text
    assert extracted.json()["content"] == "旧整稿"
    assert (await client.get(first_endpoint)).json()["document"]["sections"][0]["content"] == "另一景资料"
    assert (await client.get(second_endpoint)).json()["document"]["sections"] == []
    reverted = await client.post(f"{url}/versions/{before_version['id']}/restore", json={
        "expected_content_revision": 4,
    })
    assert reverted.status_code == 200, reverted.text
    assert (await client.get(second_endpoint)).json()["document"]["sections"][0]["content"] == "目标景资料"


async def test_explicit_section_conversion_preserves_format_markers_and_sibling_order(client):
    _project, _chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    target = body["document"]["sections"][0]
    target.update(order=1, content="段🙂", formatting={
        "version": 1, "boundaries": [], "inline": [{"kind": "bold", "start": 0, "end": 1}], "blocks": [],
    })
    for key, order in ((SECTION_BEFORE, 0), (SECTION_AFTER, 2)):
        body["document"]["sections"].append({
            "key": key, "anchor": 1, "order": order, "title": "旁边", "content": "资料",
            "formatting": {"version": 1, "boundaries": [], "inline": [], "blocks": []},
            "include_in_ai": False,
        })
        body["add_section_keys"].append(key)
    body["document"]["markers"].append({
        "key": MARKER, "section_key": KEY, "status": "active", "start": 0, "end": 2,
        "color": "yellow", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    converted = await client.post(f"{endpoint}/sections/{KEY}/convert", json={"expected_content_revision": 1})
    assert converted.status_code == 200, converted.text
    doc = converted.json()["document"]
    assert doc["content"] == "甲段🙂乙"
    assert [(item["key"], item["anchor"]) for item in doc["sections"]] == [
        (SECTION_BEFORE, 1), (SECTION_AFTER, 3),
    ]
    assert doc["formatting"]["inline"] == [{"kind": "bold", "start": 1, "end": 2}]
    assert doc["markers"][0]["section_key"] is None and doc["markers"][0]["start"] == 1
    assert doc["markers"][0]["end"] == 3 and KEY in doc["retired_section_keys"]
    assert (await client.get(url)).json()["content"] == "甲段🙂乙"


async def test_conversion_refuses_ambiguous_crossing_marker_without_partial_write(client):
    _project, _chapter, url, scene = await setup_scene(client, "甲乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    body["document"]["markers"].append({
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 2,
        "color": "green", "excerpt": "", "last_start": None,
    })
    body["add_marker_keys"].append(MARKER)
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    refused = await client.post(f"{endpoint}/sections/{KEY}/convert", json={"expected_content_revision": 1})
    assert refused.status_code == 409 and refused.json()["code"] == "document_conversion_conflict"
    assert (await client.get(endpoint)).json() == saved.json()


@pytest.mark.parametrize(("original", "inserted", "expected"), [
    ("甲乙", "备选", "甲备选乙"),
    ("\r", "\n尾", "\r\n尾"),
])
async def test_editor_style_boundary_converts_when_no_structure_depends_on_it(client, original, inserted, expected):
    _project, _chapter, url, scene = await setup_scene(client, original)
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    body["document"]["sections"][0]["content"] = inserted
    body["document"]["formatting"]["boundaries"] = [1]
    saved = await client.put(endpoint, json=body)
    assert saved.status_code == 200, saved.text
    converted = await client.post(f"{endpoint}/sections/{KEY}/convert", json={"expected_content_revision": 1})
    assert converted.status_code == 200, converted.text
    assert converted.json()["document"]["content"] == expected
    assert converted.json()["document"]["formatting"]["boundaries"] == []

async def test_explicit_marker_restore_reuses_same_scene_identity_and_rejects_stale_revision(app, client):
    _project, _chapter, url, scene = await setup_scene(client, "甲\r\n🙂乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    created = save_payload((await client.get(endpoint)).json())
    marker = {"key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 4,
              "color": "blue", "excerpt": "", "excerpt_truncated": False, "last_start": None,
              "draft_excerpt": None, "draft_excerpt_truncated": False, "draft_last_start": None}
    created["document"]["markers"] = [marker]
    created["add_marker_keys"] = [MARKER]
    created = (await client.put(endpoint, json=created)).json()
    async with app.state.sessionmaker() as db:
        from app.models import SceneMarker
        row_id = await db.scalar(select(SceneMarker.id).where(SceneMarker.logical_key == MARKER))
    deleted_body = save_payload(created)
    deleted_body["document"]["markers"] = []
    deleted_body["delete_marker_keys"] = [MARKER]
    deleted = await client.put(endpoint, json=deleted_body)
    assert deleted.status_code == 200, deleted.text
    restore = save_payload(deleted.json())
    restore["document"]["markers"] = [marker]
    restore["add_marker_keys"] = [MARKER]
    assert (await client.put(endpoint, json=restore)).status_code == 409
    restore["add_marker_keys"] = []
    restore["restore_marker_keys"] = [MARKER]
    restored = await client.put(endpoint, json=restore)
    assert restored.status_code == 200, restored.text
    assert restored.json()["document"]["markers"] == [marker]
    assert (await client.put(endpoint, json=restore)).status_code == 409
    async with app.state.sessionmaker() as db:
        from app.models import SceneMarker
        assert await db.scalar(select(SceneMarker.id).where(SceneMarker.logical_key == MARKER)) == row_id


async def test_marker_restore_allows_unrelated_same_uuid_but_rejects_live_lineage(app, client):
    from app.models import SceneMarker

    _project, _chapter, url, first = await setup_scene(client, "甲乙")
    first_endpoint = f"{url}/scenes/{first['id']}/document"
    original = save_payload((await client.get(first_endpoint)).json())
    marker = {"key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 1,
              "color": "blue", "excerpt": "", "excerpt_truncated": False, "last_start": None,
              "draft_excerpt": None, "draft_excerpt_truncated": False, "draft_last_start": None}
    original["document"]["markers"] = [marker]
    original["add_marker_keys"] = [MARKER]
    saved = await client.put(first_endpoint, json=original)
    deletion = save_payload(saved.json())
    deletion["document"]["markers"] = []
    deletion["delete_marker_keys"] = [MARKER]
    deleted = await client.put(first_endpoint, json=deletion)
    second = await client.post(f"{url}/scenes", json={"expected_content_revision": deleted.json()["content_revision"],
                                                   "content": "新景"})
    assert second.status_code == 201, second.text
    second_endpoint = f"{url}/scenes/{second.json()['id']}/document"
    other = save_payload((await client.get(second_endpoint)).json())
    other["document"]["markers"] = [marker]
    other["add_marker_keys"] = [MARKER]
    assert (await client.put(second_endpoint, json=other)).status_code == 200
    first_latest = (await client.get(first_endpoint)).json()
    restore = save_payload(first_latest)
    restore["document"]["markers"] = [marker]
    restore["restore_marker_keys"] = [MARKER]
    # 相同 UUID 本身不构成血缘冲突。
    unrelated = await client.put(first_endpoint, json=restore)
    assert unrelated.status_code == 200, unrelated.text
    deletion = save_payload(unrelated.json())
    deletion["document"]["markers"] = []
    deletion["delete_marker_keys"] = [MARKER]
    again = await client.put(first_endpoint, json=deletion)
    assert again.status_code == 200
    async with app.state.sessionmaker() as db:
        source = await db.scalar(select(SceneMarker).where(SceneMarker.scene_id == first["id"],
                                                           SceneMarker.logical_key == MARKER))
        descendant = await db.scalar(select(SceneMarker).where(SceneMarker.scene_id == second.json()["id"],
                                                               SceneMarker.logical_key == MARKER))
        descendant.origin_marker_id = source.id
        await db.commit()
    restore = save_payload((await client.get(first_endpoint)).json())
    restore["document"]["markers"] = [marker]
    restore["restore_marker_keys"] = [MARKER]
    blocked = await client.put(first_endpoint, json=restore)
    assert blocked.status_code == 409 and blocked.json()["code"] == "marker_identity_conflict"
    assert (await client.get(first_endpoint)).json()["document"]["markers"] == []
    assert (await client.get(second_endpoint)).json()["document"]["markers"] == [marker]

async def test_marker_saved_baseline_and_unverified_draft_excerpt_survive_history_backup(client):
    project, _chapter, url, scene = await setup_scene(client, "甲\r\n🙂乙")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    body["document"]["markers"] = [{
        "key": MARKER, "section_key": None, "status": "active", "start": 0, "end": 4,
        "color": "blue", "excerpt": "", "last_start": None,
    }]
    body["add_marker_keys"] = [MARKER]
    active = await client.put(endpoint, json=body)
    assert active.status_code == 200, active.text
    changed = save_payload(active.json())
    changed["document"]["content"] = "乙"
    changed["document"]["markers"][0].update(
        status="stale", start=None, end=None, last_start=0, excerpt="甲\r\n🙂", excerpt_truncated=False,
        draft_excerpt="甲X\r\n🙂", draft_excerpt_truncated=False, draft_last_start=0,
    )
    assert (await client.put(endpoint, json=changed)).json()["code"] == "document_mapping_required"
    changed["mapped_marker_keys"] = [MARKER]
    changed["document"]["markers"][0]["excerpt"] = "伪造基线"
    assert (await client.put(endpoint, json=changed)).json()["code"] == "document_intent_mismatch"
    changed["document"]["markers"][0]["excerpt"] = "甲\r\n🙂"
    saved = await client.put(endpoint, json=changed)
    assert saved.status_code == 200, saved.text
    result = saved.json()["document"]["markers"][0]
    assert (result["excerpt"], result["draft_excerpt"], result["draft_last_start"]) == ("甲\r\n🙂", "甲X\r\n🙂", 0)
    stale_version = (await client.get(f"{url}/versions")).json()[0]
    timeline = (await client.get(f"/api/v1/projects/{project['id']}/markers")).json()["items"][0]
    assert (timeline["excerpt"], timeline["draft_excerpt"], timeline["draft_last_start"]) == (
        "甲\r\n🙂", "甲X\r\n🙂", 0,
    )
    tampered = save_payload(saved.json())
    tampered["document"]["markers"][0]["draft_excerpt"] = "篡改"
    assert (await client.put(endpoint, json=tampered)).json()["code"] == "document_intent_mismatch"
    backup = await _backup(client, project["id"], versions=1)
    imported = await _import(client, backup)
    copied = await _backup(client, imported["project"]["id"], versions=1)
    copied_marker = copied["tables"]["scene_markers"][0]
    assert (copied_marker["excerpt"], copied_marker["draft_excerpt"]) == ("甲\r\n🙂", "甲X\r\n🙂")
    deletion = save_payload(saved.json())
    deletion["document"]["markers"] = []
    deletion["delete_marker_keys"] = [MARKER]
    deleted = await client.put(endpoint, json=deletion)
    assert deleted.status_code == 200
    restored = await client.post(f"{url}/versions/{stale_version['id']}/restore", json={
        "expected_content_revision": deleted.json()["content_revision"], "confirm_rich_document_loss": True,
    })
    assert restored.status_code == 200, restored.text
    recovered = (await client.get(endpoint)).json()["document"]["markers"][0]
    assert (recovered["excerpt"], recovered["draft_excerpt"]) == ("甲\r\n🙂", "甲X\r\n🙂")


async def test_legacy_backup_without_draft_excerpt_fields_still_imports(client):
    project, _chapter, url, scene = await setup_scene(client, "旧文")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    body["document"]["markers"] = [{
        "key": MARKER, "section_key": None, "status": "stale", "start": None, "end": None,
        "last_start": 0, "excerpt": "旧片段", "color": "blue",
    }]
    body["add_marker_keys"] = [MARKER]
    assert (await client.put(endpoint, json=body)).status_code == 200
    backup = await _backup(client, project["id"], versions=1)
    for row in backup["tables"]["scene_markers"]:
        for field in ("draft_excerpt", "draft_excerpt_truncated", "draft_last_start"):
            row.pop(field)
    imported = await _import(client, backup)
    copied = await _backup(client, imported["project"]["id"], versions=1)
    row = copied["tables"]["scene_markers"][0]
    assert row["excerpt"] == "旧片段" and row["draft_excerpt"] is None
    assert row["draft_excerpt_truncated"] is False and row["draft_last_start"] is None

async def test_section_marker_draft_excerpt_keeps_section_baseline_and_survives_backup(client):
    project, _chapter, url, scene = await setup_scene(client, "正文")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(endpoint)).json())
    add_section(body)
    body["document"]["sections"][0]["content"] = "甲\r\n🙂乙"
    body["document"]["markers"] = [{
        "key": MARKER, "section_key": KEY, "status": "active", "start": 0, "end": 4,
        "color": "blue", "excerpt": "", "last_start": None,
    }]
    body["add_marker_keys"] = [MARKER]
    active = await client.put(endpoint, json=body)
    assert active.status_code == 200, active.text
    changed = save_payload(active.json())
    changed["document"]["sections"][0]["content"] = "乙"
    changed["document"]["markers"][0].update(
        status="stale", start=None, end=None, last_start=0, excerpt="甲\r\n🙂",
        draft_excerpt="甲X\r\n🙂", draft_excerpt_truncated=False, draft_last_start=0,
    )
    changed["mapped_marker_keys"] = [MARKER]
    saved = await client.put(endpoint, json=changed)
    assert saved.status_code == 200, saved.text
    item = (await client.get(f"/api/v1/projects/{project['id']}/markers")).json()["items"][0]
    assert item["section_key"] == KEY and item["excerpt"] == "甲\r\n🙂" and item["draft_excerpt"] == "甲X\r\n🙂"
    copied = await _backup(client, (await _import(client, await _backup(client, project["id"])))['project']['id'])
    assert copied["tables"]["scene_markers"][0]["draft_excerpt"] == "甲X\r\n🙂"
