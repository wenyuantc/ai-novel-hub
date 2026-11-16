"""组合导出的设定和笔记，使用真实保存记录和领域接口。"""

import pytest
from sqlalchemy import func, select, update

from app.core.errors import ConflictError, NotFoundError
from app.models import Character, Project, ProjectNote, SettingTextField, SettingTimeline
from app.schemas.setting_fields import EntityRef
from app.services.document_export import materials
from tests.helpers import create_project, register


async def _post(client, path, body):
    response = await client.post(path, json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _setup(client):
    await register(client)
    project = await create_project(client, title="资料书")
    base = f"/api/v1/projects/{project['id']}"
    return project, base


async def _set_field(client, base, character_id, definition_id, field_type, value):
    prefix = f"{base}/setting-entries/character/{character_id}/fields"
    current = await client.get(prefix)
    assert current.status_code == 200, current.text
    data = current.json()
    response = await client.patch(
        f"{prefix}/{definition_id}/baseline",
        json={
            "expected_fields_revision": data["fields_revision"],
            "expected_timeline_revision": data["timeline_revision"],
            "field_type": field_type,
            "value": value,
        },
    )
    assert response.status_code == 200, response.text


def _text(items):
    return "\n".join(f"{section.title}\n" + "\n".join(f"{key}：{value}" for key, value in section.fields)
                     for section in items)


async def test_selected_materials_are_readable_complete_and_do_not_expand_references(client, app):
    project, base = await _setup(client)
    alice = await _post(client, f"{base}/characters", {
        "name": "林远", "profile": "林远的完整设定", "background": "完整背景", "aliases": ["小林"],
    })
    bob = await _post(client, f"{base}/characters", {"name": "阿舟", "profile": "未选档案的秘密全文"})
    world = await _post(client, f"{base}/world", {"title": "青云山", "category": "place", "content": "雨中山门"})
    org = await _post(client, f"{base}/organizations", {"name": "青云门", "purpose": "守护山门"})
    career = await _post(client, f"{base}/careers", {
        "name": "剑修", "description": "修习剑术", "stages": [{"name": "练气", "description": "感知灵气"}],
    })
    await _post(client, f"{base}/relationships", {
        "a_id": alice["id"], "b_id": bob["id"], "role_a": "朋友", "role_b": "朋友",
    })
    await _post(client, f"{base}/organizations/{org['id']}/members", {
        "character_id": alice["id"], "position": "掌门", "note": "守山者",
    })
    await _post(client, f"{base}/characters/{alice['id']}/careers", {"career_id": career["id"], "note": "瓶颈期"})

    definition = await _post(client, f"{base}/setting-field-definitions", {
        "expected_fields_revision": 0, "label": "境界", "field_type": "dropdown",
        "applicable_types": ["character"], "ai_hidden": True,
    })
    option = await _post(client, f"{base}/setting-field-definitions/{definition['item']['id']}/options", {
        "expected_fields_revision": 1, "label": "筑基",
    })
    await _set_field(client, base, alice["id"], definition["item"]["id"], "dropdown", {
        "option_id": option["item"]["id"],
    })
    reference = await _post(client, f"{base}/setting-field-definitions", {
        "expected_fields_revision": 2, "label": "同行者", "field_type": "reference",
        "applicable_types": ["character"], "reference_types": ["character"],
    })
    await _set_field(client, base, alice["id"], reference["item"]["id"], "reference", {
        "kind": "character", "id": bob["id"],
    })
    note_body = "前情😀\r\n" + "长笔记" * 100
    note = await _post(client, f"{base}/notes", {"title": "创作笔记", "content": note_body})
    await _post(client, f"{base}/notes", {"title": "未选笔记", "content": "不该包含此笔记"})
    refs = [EntityRef(kind=kind, id=entry_id) for kind, entry_id in (
        ("career", career["id"]), ("world", world["id"]), ("character", alice["id"]), ("organization", org["id"]),
    )]
    async with app.state.sessionmaker() as db:
        before = await db.scalar(select(func.count()).select_from(SettingTextField))
        settings, notes = await materials.load_materials(db, project["id"], refs, [note["id"]])
        after = await db.scalar(select(func.count()).select_from(SettingTextField))
        assert before == after
    assert len(settings) == 4 and len(notes) == 1
    text = _text(settings)
    assert [item.title for item in settings] == ["人物 · 林远", "世界观 · 青云山", "组织 · 青云门", "职业 · 剑修"]
    for expected in ("林远的完整设定", "完整背景", "小林", "雨中山门", "守护山门", "修习剑术",
                     "感知灵气", "筑基", "同行者", "阿舟", "朋友", "掌门", "守山者", "瓶颈期"):
        assert expected in text
    assert "未选档案的秘密全文" not in text
    assert text.count("人物关系") == 1
    assert notes[0].title == "创作笔记" and notes[0].fields == [("", note_body)]
    assert "不该包含此笔记" not in _text(notes)


async def test_selected_ids_must_belong_to_project_and_notes_are_sorted(client, app):
    project, base = await _setup(client)
    first = await _post(client, f"{base}/notes", {"title": "未固定", "content": "一"})
    second = await _post(client, f"{base}/notes", {"title": "固定", "content": "二"})
    response = await client.put(f"{base}/notes/{second['id']}/flags", json={"is_pinned": True})
    assert response.status_code == 200, response.text
    other = await create_project(client, title="其他书")
    other_base = f"/api/v1/projects/{other['id']}"
    foreign = await _post(client, f"{other_base}/characters", {"name": "别书角色"})
    foreign_note = await _post(client, f"{other_base}/notes", {"title": "别书笔记"})
    async with app.state.sessionmaker() as db:
        settings, notes = await materials.load_materials(db, project["id"], [], [first["id"], second["id"]])
        assert settings == [] and [item.title for item in notes] == ["固定", "未固定"]
        with pytest.raises(NotFoundError):
            await materials.load_materials(db, project["id"], [EntityRef(kind="character", id=foreign["id"])], [])
        with pytest.raises(NotFoundError):
            await materials.load_materials(db, project["id"], [], [first["id"], foreign_note["id"]])


async def test_custom_text_preserves_whitespace_and_legacy_value(client, app):
    project, base = await _setup(client)
    character = await _post(client, f"{base}/characters", {"name": "角色"})
    definition = await _post(client, f"{base}/setting-field-definitions", {
        "expected_fields_revision": 0, "label": "日记", "field_type": "text",
        "applicable_types": ["character"],
    })
    original = "  开头空格\n第二行  \n"
    await _set_field(client, base, character["id"], definition["item"]["id"], "text", original)
    current = (await client.get(f"{base}/setting-entries/character/{character['id']}/fields")).json()
    await _post(client, f"{base}/setting-timelines/{current['timeline_id']}/text-fields", {
        "expected_revision": current["timeline_revision"], "label": "旧字段", "base_value": "  原文  ",
    })
    async with app.state.sessionmaker() as db:
        sections, _ = await materials.load_materials(
            db, project["id"], [EntityRef(kind="character", id=character["id"])], [],
        )
    assert ("自定义字段 · 日记", original) in sections[0].fields
    assert ("自定义字段 · 旧字段", "  原文  ") in sections[0].fields


async def test_planned_field_event_is_reflected_in_current_value(client, app):
    project, base = await _setup(client)
    character = await _post(client, f"{base}/characters", {"name": "角色"})
    definition = await _post(client, f"{base}/setting-field-definitions", {
        "expected_fields_revision": 0, "label": "境界", "field_type": "line", "applicable_types": ["character"],
    })
    await _set_field(client, base, character["id"], definition["item"]["id"], "line", "练气")
    prefix = f"{base}/setting-entries/character/{character['id']}/fields"
    data = (await client.get(prefix)).json()
    await _post(client, f"{prefix}/{definition['item']['id']}/progressions", {
        "expected_fields_revision": data["fields_revision"], "expected_timeline_revision": data["timeline_revision"],
        "field_type": "line", "value": "筑基", "operation": "replace", "anchor_kind": "planned",
        "chapter_no": 20, "boundary": "end",
    })
    async with app.state.sessionmaker() as db:
        sections, _ = await materials.load_materials(
            db, project["id"], [EntityRef(kind="character", id=character["id"])], [],
        )
    assert ("自定义字段 · 境界", "筑基") in sections[0].fields


async def test_series_exports_adopted_book_projection_only(client, app):
    book, _ = await _setup(client)
    series = await _post(client, "/api/v1/series", {"name": "系列"})
    route = f"/api/v1/series/{series['id']}"
    joined = await client.post(f"{route}/books", json={"project_id": book["id"], "expected_revision": 0})
    assert joined.status_code == 200, joined.text
    shared = await _post(client, f"{route}/entries", {
        "kind": "character", "data": {"name": "已采用", "profile": "共享基础"},
        "expected_revision": joined.json()["revision"],
    })
    unused = await _post(client, f"{route}/entries", {
        "kind": "character", "data": {"name": "未采用", "profile": "未采用的秘密"},
        "expected_revision": shared["revision"],
    })
    adopted = await client.post(f"{route}/entries/{shared['item']['id']}/uses", json={
        "project_id": book["id"], "expected_revision": unused["revision"],
    })
    assert adopted.status_code == 200, adopted.text
    local_id = adopted.json()["ref"]["id"]
    async with app.state.sessionmaker() as db:
        sections, _ = await materials.load_materials(db, book["id"], [EntityRef(kind="character", id=local_id)], [])
    assert len(sections) == 1
    assert "共享基础" in _text(sections) and "未采用的秘密" not in _text(sections)


async def test_large_selected_collections_are_batched_without_losing_items(client, app, monkeypatch):
    project, _ = await _setup(client)
    async with app.state.sessionmaker() as db:
        chars = [Character(project_id=project["id"], name=f"角色{i:03}", sort=i) for i in range(205)]
        notes = [ProjectNote(project_id=project["id"], title=f"笔记{i:03}", content=f"正文{i}") for i in range(205)]
        db.add_all([*chars, *notes])
        await db.commit()
        refs = [EntityRef(kind="character", id=char.id) for char in chars]
        note_ids = [note.id for note in notes]
    original = materials.setting_field_service.resolve_entry_fields
    batch_sizes = []

    async def track(db, project_id, entries, **kwargs):
        batch_sizes.append(len(entries))
        return await original(db, project_id, entries, **kwargs)

    monkeypatch.setattr(materials.setting_field_service, "resolve_entry_fields", track)
    async with app.state.sessionmaker() as db:
        sections, saved_notes = await materials.load_materials(db, project["id"], refs, note_ids)
    assert batch_sizes == [200, 5]
    assert len(sections) == len(saved_notes) == 205
    assert sections[0].title == "人物 · 角色000" and sections[-1].title == "人物 · 角色204"
    assert {section.fields[0][1] for section in saved_notes} == {f"正文{i}" for i in range(205)}


@pytest.mark.parametrize("changed", ["fields", "timeline"])
async def test_multi_batch_field_projection_rejects_changed_revisions(client, app, monkeypatch, changed):
    project, base = await _setup(client)
    chars = [await _post(client, f"{base}/characters", {"name": f"人物{i}"}) for i in range(3)]
    refs = [EntityRef(kind="character", id=item["id"]) for item in chars]
    monkeypatch.setattr(materials, "_BATCH_SIZE", 2)
    original = materials.setting_field_service.resolve_entry_fields
    count = 0

    async def mutate_after_first(db, project_id, entries, **kwargs):
        nonlocal count
        result = await original(db, project_id, entries, **kwargs)
        count += 1
        if count == 1:
            async with app.state.sessionmaker() as writer:
                if changed == "fields":
                    await writer.execute(update(Project).where(Project.id == project_id)
                                         .values(fields_revision=Project.fields_revision + 1))
                else:
                    await writer.execute(update(SettingTimeline).where(
                        SettingTimeline.project_id == project_id,
                        SettingTimeline.subject_kind == "character",
                        SettingTimeline.character_id == chars[0]["id"],
                    ).values(revision=SettingTimeline.revision + 1))
                await writer.commit()
        return result

    monkeypatch.setattr(materials.setting_field_service, "resolve_entry_fields", mutate_after_first)
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await materials.load_materials(db, project["id"], refs, [])
