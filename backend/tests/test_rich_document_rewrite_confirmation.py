"""正文选区改写的格式损失确认不能绕过版本、结构或原文校验。"""

import pytest

from tests.helpers import create_ai_config, parse_sse
from tests.test_rich_document_api import MARKER, add_section, save_payload
from tests.test_scenes import setup_scene


async def _prepare(client, fake_llm, *, anchor=5, legacy=False):
    _project, chapter, url, scene = await setup_scene(client, "前甲乙尾🙂")
    await create_ai_config(client)
    endpoint = f"{url}/scenes/{scene['id']}/document"
    payload = save_payload((await client.get(endpoint)).json())
    add_section(payload, enabled=True)
    payload["document"]["sections"][0]["anchor"] = anchor
    payload["document"]["formatting"] = {
        "version": 1, "boundaries": [],
        "inline": [{"kind": "bold", "start": 0, "end": 3}, {"kind": "italic", "start": 3, "end": 5}],
        "blocks": [{"kind": "heading", "start": 0, "end": 5, "level": 2}],
    }
    payload["document"]["markers"] = [{"key": MARKER, "section_key": None, "status": "active", "color": "red",
                                          "start": 1, "end": 3}]
    payload["add_marker_keys"] = [MARKER]
    saved = await client.put(endpoint, json=payload)
    assert saved.status_code == 200, saved.text
    original = saved.json()
    revisions = {f"expected_{key}": original[key] for key in ("content_revision", "planning_revision")}
    fake_llm.push("新")
    preview_url = f"/api/v1/chapters/{chapter['id']}/rewrite" if legacy else f"{url}/scenes/{scene['id']}/rewrite"
    preview = await client.post(preview_url, json={**revisions, "op": "rewrite", "start": 1, "end": 3})
    assert preview.status_code == 200, preview.text
    events = parse_sse(preview.text)
    assert events[-1][0] == "done", preview.text
    done = events[-1][1]
    apply_url = f"{url}/rewrite/apply" if legacy else f"{url}/scenes/{scene['id']}/rewrite/apply"
    request = {**revisions, **{key: done[key] for key in ("start", "end", "original", "before", "log_id")},
               "replacement": done["text"]}
    return endpoint, apply_url, original, request


async def test_scene_rewrite_format_loss_confirmation_is_atomic_and_preserves_safe_content(client, fake_llm):
    endpoint, apply_url, original, body = await _prepare(client, fake_llm)
    denied = await client.post(apply_url, json=body)
    assert denied.status_code == 409 and denied.json()["code"] == "rich_document_confirmation_required"
    assert (await client.get(endpoint)).json() == original
    for invalid in (1, "true"):
        response = await client.post(apply_url, json={**body, "confirm_format_loss": invalid})
        assert response.status_code == 422
        assert (await client.get(endpoint)).json() == original

    accepted = await client.post(apply_url, json={**body, "confirm_format_loss": True})
    assert accepted.status_code == 200, accepted.text
    changed = (await client.get(endpoint)).json()
    document = changed["document"]
    assert document["content"] == "前新尾🙂"
    assert document["sections"][0]["content"] == "作者🙂" and document["sections"][0]["anchor"] == 4
    assert document["formatting"]["inline"] == [
        {"kind": "bold", "start": 0, "end": 1}, {"kind": "italic", "start": 2, "end": 4},
    ]
    assert document["formatting"]["blocks"] == [{"kind": "heading", "start": 0, "end": 4, "level": 2}]
    assert document["markers"][0]["status"] == "stale" and document["markers"][0]["excerpt"] == "甲乙"
    duplicate = await client.post(apply_url, json={**body, "confirm_format_loss": True})
    assert duplicate.status_code == 409
    assert (await client.get(endpoint)).json() == changed


async def test_format_confirmation_cannot_bypass_unmapped_section_anchor(client, fake_llm):
    endpoint, apply_url, original, body = await _prepare(client, fake_llm, anchor=2)
    for confirmed in (False, True):
        response = await client.post(apply_url, json={**body, "confirm_format_loss": confirmed})
        assert response.status_code == 409 and response.json()["code"] == "rich_document_unmapped"
        assert (await client.get(endpoint)).json() == original


async def test_scene_rewrite_confirmation_rejects_concurrent_document_change(client, fake_llm):
    endpoint, apply_url, original, body = await _prepare(client, fake_llm)
    assert (await client.post(apply_url, json=body)).json()["code"] == "rich_document_confirmation_required"
    other = save_payload(original)
    other["document"]["sections"][0]["title"] = "另一端的新标题"
    changed = await client.put(endpoint, json=other)
    assert changed.status_code == 200, changed.text
    rejected = await client.post(apply_url, json={**body, "confirm_format_loss": True})
    assert rejected.status_code == 409
    assert (await client.get(endpoint)).json() == changed.json()


async def test_legacy_chapter_format_confirmation_requires_bound_preview_and_versions(client, fake_llm):
    endpoint, apply_url, original, body = await _prepare(client, fake_llm, legacy=True)
    assert (await client.post(apply_url, json=body)).json()["code"] == "rich_document_confirmation_required"
    for omitted in ("log_id", "expected_content_revision", "expected_planning_revision"):
        insufficient = {key: value for key, value in body.items() if key != omitted}
        response = await client.post(apply_url, json={**insufficient, "confirm_format_loss": True})
        assert response.status_code == 409 and response.json()["code"] == "rich_document_confirmation_context_required"
        assert (await client.get(endpoint)).json() == original
    accepted = await client.post(apply_url, json={**body, "confirm_format_loss": True})
    assert accepted.status_code == 200, accepted.text
    assert (await client.get(endpoint)).json()["document"]["content"] == "前新尾🙂"


@pytest.mark.parametrize("invalid", [1, "true"])
async def test_legacy_chapter_format_confirmation_requires_a_boolean(client, fake_llm, invalid):
    endpoint, apply_url, original, body = await _prepare(client, fake_llm, legacy=True)
    response = await client.post(apply_url, json={**body, "confirm_format_loss": invalid})
    assert response.status_code == 422
    assert (await client.get(endpoint)).json() == original
