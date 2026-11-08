"""仅对选择范围外可证明的位置执行自动映射；不按重复词面猜测。"""

from copy import deepcopy
from random import Random

import pytest

from app.schemas.rich_document import SceneDocument
from app.services.rich_document_mapping import FormatLossRequired, UnsafeMapping, map_explicit_replacement
from tests.helpers import create_ai_config, parse_sse
from tests.test_rich_document_api import add_section, save_payload
from tests.test_scenes import setup_scene

SECTION = "645d44c0-cc16-4949-973b-542022da8421"
FIRST = "d4baf738-d1ca-4d51-a9cc-22fe47213105"
SECOND = "d4baf738-d1ca-4d51-a9cc-22fe47213106"


def document():
    return SceneDocument.model_validate({
        "content": "开甲乙尾😀",
        "formatting": {"inline": [{"kind": "bold", "start": 3, "end": 5}]},
        "sections": [{"key": SECTION, "anchor": 4, "order": 0, "title": "备选", "content": "不改",
                      "include_in_ai": True}],
        "markers": [
            {"key": FIRST, "status": "active", "color": "yellow", "start": 1, "end": 3},
            {"key": SECOND, "status": "active", "color": "blue", "start": 3, "end": 5},
        ],
    })


def test_known_replacement_shifts_unaffected_structures_and_invalidates_covered_marker():
    old = document()
    before = deepcopy(old)
    new = map_explicit_replacement(old, 1, 3, "新")
    assert old == before
    assert new.content == "开新尾😀"
    assert (new.sections[0].anchor, new.sections[0].content, new.sections[0].include_in_ai) == (3, "不改", True)
    assert (new.formatting.inline[0].start, new.formatting.inline[0].end) == (2, 4)
    first, second = new.markers
    assert (first.status, first.start, first.end, first.last_start, first.excerpt) == ("stale", None, None, 1, "甲乙")
    assert (second.status, second.start, second.end) == ("active", 2, 4)


@pytest.mark.parametrize("anchor", [1, 2, 3])
def test_selected_or_ambiguous_section_requires_author_location(anchor):
    old = document().model_dump(mode="json")
    old["sections"][0]["anchor"] = anchor
    with pytest.raises(UnsafeMapping):
        map_explicit_replacement(SceneDocument.model_validate(old), 1, 3, "新")


def test_insert_at_section_boundary_requires_explicit_side():
    old = document().model_dump(mode="json")
    old["sections"][0]["anchor"] = 1
    with pytest.raises(UnsafeMapping):
        map_explicit_replacement(SceneDocument.model_validate(old), 1, 1, "新")


@pytest.mark.parametrize(("start", "end", "replacement", "expected"), [
    (2, 3, "甲甲", (1, 6, "b甲甲de")),
    (0, 3, "甲甲", (2, 4, "de")),
    (3, 6, "甲甲", (1, 3, "bc")),
    (2, 2, "甲甲", (1, 7, "b甲甲cde")),
])
def test_marker_keeps_provably_surviving_partial_range(start, end, replacement, expected):
    old = SceneDocument.model_validate({
        "content": "abcdef",
        "markers": [{"key": FIRST, "status": "active", "color": "red", "start": 1, "end": 5}],
    })
    new = map_explicit_replacement(old, start, end, replacement)
    marker = new.markers[0]
    assert marker.status == "active"
    assert (marker.start, marker.end, new.content[marker.start:marker.end]) == expected


def test_full_marker_replacement_does_not_guess_repeated_text_and_unicode_keeps_partial_range():
    old = SceneDocument.model_validate({
        "content": "🙂甲🙂甲🙂",
        "markers": [{"key": FIRST, "status": "active", "color": "red", "start": 1, "end": 4}],
    })
    partially_replaced = map_explicit_replacement(old, 0, 2, "😀")
    marker = partially_replaced.markers[0]
    assert (marker.status, marker.start, marker.end) == ("active", 1, 3)
    assert partially_replaced.content[marker.start:marker.end] == "🙂甲"
    fully_replaced = map_explicit_replacement(old, 0, 4, "甲🙂甲")
    marker = fully_replaced.markers[0]
    assert (marker.status, marker.last_start, marker.excerpt) == ("stale", 1, "甲🙂甲")


def test_large_insert_keeps_reliably_mapped_marker_active():
    old = SceneDocument.model_validate({
        "content": "甲🙂",
        "markers": [{"key": FIRST, "status": "active", "color": "red", "start": 0, "end": 2}],
    })
    new = map_explicit_replacement(old, 1, 1, "乙" * 5_000)
    assert new.content == "甲" + "乙" * 5_000 + "🙂"
    marker = new.markers[0]
    assert (marker.status, marker.start, marker.end) == ("active", 0, 5_002)
    deleted = map_explicit_replacement(new, 0, len(new.content), "尾")
    marker = deleted.markers[0]
    assert marker.status == "stale" and marker.last_start == 0
    assert marker.excerpt == new.content[:5_000] and marker.excerpt_truncated is True


def test_format_overlap_requires_confirmation_but_unchanged_text_is_noop():
    old = document()
    with pytest.raises(UnsafeMapping):
        map_explicit_replacement(old, 3, 4, "新")
    assert map_explicit_replacement(old, 3, 4, "尾") == old


def test_confirmed_replacement_preserves_surviving_inline_and_same_line_block():
    old = SceneDocument.model_validate({
        "content": "头甲🙂乙尾",
        "formatting": {"inline": [{"kind": "bold", "start": 0, "end": 5},
                                  {"kind": "italic", "start": 1, "end": 4}],
                       "blocks": [{"kind": "heading", "start": 0, "end": 5, "level": 2}]},
    })
    with pytest.raises(FormatLossRequired):
        map_explicit_replacement(old, 2, 3, "X")
    result = map_explicit_replacement(old, 2, 3, "X", confirm_format_loss=True)
    assert result.content == "头甲X乙尾"
    assert [(x.kind, x.start, x.end) for x in result.formatting.inline] == [
        ("bold", 0, 2), ("bold", 3, 5), ("italic", 1, 2), ("italic", 3, 4),
    ]
    assert [(x.kind, x.start, x.end, x.level) for x in result.formatting.blocks] == [("heading", 0, 5, 2)]


def test_confirmed_replacement_clips_block_format_to_surviving_complete_lines():
    old = SceneDocument.model_validate({
        "content": "甲\r\n乙\r\n丙",
        "formatting": {"blocks": [{"kind": "bullet_list", "start": 0, "end": 7, "level": None}]},
    })
    changed = map_explicit_replacement(old, 3, 4, "一\n二", confirm_format_loss=True)
    assert changed.content == "甲\r\n一\n二\r\n丙"
    assert [(style.start, style.end) for style in changed.formatting.blocks] == [(0, 1), (8, 9)]


def test_confirmed_format_loss_drops_only_unrepresentable_crlf_seam():
    old = SceneDocument.model_validate({
        "content": "A\rX",
        "formatting": {"boundaries": [2], "inline": [{"kind": "bold", "start": 0, "end": 2}]},
    })
    with pytest.raises(FormatLossRequired):
        map_explicit_replacement(old, 2, 3, "\n")
    changed = map_explicit_replacement(old, 2, 3, "\n", confirm_format_loss=True)
    assert changed.content == "A\r\n" and changed.formatting.boundaries == []
    assert [(style.start, style.end) for style in changed.formatting.inline] == [(0, 1)]


def test_format_confirmation_cannot_authorize_ambiguous_section_anchor():
    old = document().model_dump(mode="json")
    old["sections"][0]["anchor"] = 1
    with pytest.raises(UnsafeMapping, match="区块"):
        map_explicit_replacement(SceneDocument.model_validate(old), 1, 3, "新", confirm_format_loss=True)


def test_format_confirmation_does_not_discard_marker_on_new_crlf_seam():
    old = SceneDocument.model_validate({
        "content": "A\rX",
        "formatting": {"inline": [{"kind": "bold", "start": 0, "end": 2}]},
        "markers": [{"key": FIRST, "status": "active", "color": "red", "start": 0, "end": 2}],
    })
    with pytest.raises(UnsafeMapping, match="CRLF") as error:
        map_explicit_replacement(old, 2, 3, "\n", confirm_format_loss=True)
    assert not isinstance(error.value, FormatLossRequired)


def test_confirmed_format_preserves_unchanged_unicode_characters_fixed_seed():
    random = Random(0xC0FFEE)
    alphabet = "甲乙🙂e\u0301"
    for _ in range(500):
        text = "".join(random.choice(alphabet) for _ in range(random.randrange(1, 40)))
        left = random.randrange(len(text))
        right = random.randrange(left + 1, len(text) + 1)
        start = random.randrange(len(text) + 1)
        end = random.randrange(start, len(text) + 1)
        replacement = "".join(random.choice(alphabet) for _ in range(random.randrange(6)))
        old = SceneDocument.model_validate({
            "content": text, "formatting": {"inline": [{"kind": "bold", "start": left, "end": right}]},
        })
        new = map_explicit_replacement(old, start, end, replacement, confirm_format_loss=True)
        assert new.content == text[:start] + replacement + text[end:]
        delta = len(replacement) - (end - start)
        for index in list(range(start)) + list(range(end, len(text))):
            mapped = index if index < start else index + delta
            actual = any(item.start <= mapped < item.end for item in new.formatting.inline if item.kind == "bold")
            assert actual == (left <= index < right)


def test_combining_emoji_and_crlf_offsets_are_codepoints_not_utf16():
    old = SceneDocument.model_validate({
        "content": "🙂e\u0301\r\n尾",
        "markers": [{"key": FIRST, "status": "active", "color": "red", "start": 1, "end": 3}],
        "sections": [{"key": SECTION, "anchor": 6, "order": 0, "title": "备选", "content": "笔记"}],
    })
    new = map_explicit_replacement(old, 1, 3, "字")
    assert new.content == "🙂字\r\n尾"
    assert new.markers[0].excerpt == "e\u0301" and new.sections[0].anchor == 5
    with pytest.raises(UnsafeMapping):
        map_explicit_replacement(old, 4, 5, "X")


async def test_scene_ai_rewrite_maps_safe_structures_in_one_transaction(app, client, fake_llm):
    from sqlalchemy import select

    from app.models import ChapterVersion, Project

    project, chapter, url, scene = await setup_scene(client, "开甲乙尾😀")
    await create_ai_config(client)
    document_url = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(document_url)).json())
    add_section(body, enabled=True)
    body["document"]["sections"][0]["anchor"] = 4
    body["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 3, "end": 5}]
    body["document"]["markers"] = [
        {"key": FIRST, "section_key": None, "status": "active", "color": "yellow", "start": 1, "end": 3},
        {"key": SECOND, "section_key": None, "status": "active", "color": "blue", "start": 3, "end": 5},
    ]
    body["add_marker_keys"] = [FIRST, SECOND]
    saved = await client.put(document_url, json=body)
    assert saved.status_code == 200, saved.text
    async with app.state.sessionmaker() as db:
        initial_visibility = (await db.get(Project, project["id"])).ai_visibility_revision
    revisions = {key: saved.json()[key] for key in ("content_revision", "planning_revision")}
    revisions = {f"expected_{key}": value for key, value in revisions.items()}
    fake_llm.push("新")
    preview = await client.post(f"{url}/scenes/{scene['id']}/rewrite", json={
        **revisions, "op": "rewrite", "start": 1, "end": 3,
    })
    events = parse_sse(preview.text)
    assert events[-1][0] == "done", preview.text
    done = events[-1][1]
    data = {key: done[key] for key in ("start", "end", "original", "before", "log_id")}
    result = await client.post(f"{url}/scenes/{scene['id']}/rewrite/apply", json={
        **data, **revisions, "replacement": done["text"],
    })
    assert result.status_code == 200, result.text
    changed = (await client.get(document_url)).json()["document"]
    assert changed["content"] == "开新尾😀"
    assert changed["sections"][0]["anchor"] == 3
    assert changed["formatting"]["inline"] == [{"kind": "bold", "start": 2, "end": 4}]
    assert [(m["status"], m["start"], m["end"], m["excerpt"]) for m in changed["markers"]] == [
        ("stale", None, None, "甲乙"), ("active", 2, 4, ""),
    ]
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, project["id"])).ai_visibility_revision == initial_visibility + 1
        version = (await db.scalars(select(ChapterVersion).where(
            ChapterVersion.chapter_id == chapter["id"], ChapterVersion.source == "rewrite",
        ))).one()
        assert version.scene_snapshot["scenes"][0]["document"]["markers"][0]["excerpt"] == "甲乙"


@pytest.mark.parametrize("obstacle", ["section", "formatting"])
async def test_scene_ai_rewrite_uncertain_structure_keeps_entire_original(client, fake_llm, obstacle):
    _project, _chapter, url, scene = await setup_scene(client, "开甲乙尾😀")
    await create_ai_config(client)
    document_url = f"{url}/scenes/{scene['id']}/document"
    body = save_payload((await client.get(document_url)).json())
    add_section(body, enabled=True)
    body["document"]["sections"][0]["anchor"] = 1 if obstacle == "section" else 4
    if obstacle == "formatting":
        body["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 1, "end": 2}]
    saved = await client.put(document_url, json=body)
    assert saved.status_code == 200, saved.text
    before = saved.json()
    revisions = {f"expected_{key}": before[key] for key in ("content_revision", "planning_revision")}
    fake_llm.push("替换")
    preview = await client.post(f"{url}/scenes/{scene['id']}/rewrite", json={
        **revisions, "op": "rewrite", "start": 1, "end": 3,
    })
    events = parse_sse(preview.text)
    assert events[-1][0] == "done", preview.text
    done = events[-1][1]
    data = {key: done[key] for key in ("start", "end", "original", "before", "log_id")}
    rejected = await client.post(f"{url}/scenes/{scene['id']}/rewrite/apply", json={
        **data, **revisions, "replacement": done["text"],
    })
    code = "rich_document_unmapped" if obstacle == "section" else "rich_document_confirmation_required"
    assert rejected.status_code == 409 and rejected.json()["code"] == code
    assert (await client.get(document_url)).json() == before
