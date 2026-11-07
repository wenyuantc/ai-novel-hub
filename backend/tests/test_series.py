import asyncio

from app.models import Character, OutlineSnapshot, Project, SettingTimeline
from tests.helpers import create_project, register

_SERIES = "/api/v1/series"


async def _new_series(client, name="系列"):
    response = await client.post(_SERIES, json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


async def _join(client, series, project):
    response = await client.post(
        f"{_SERIES}/{series['id']}/books",
        json={"project_id": project["id"], "expected_revision": series["revision"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _entry(client, path, revision, kind, **data):
    response = await client.post(f"{path}/entries", json={"expected_revision": revision, "kind": kind, "data": data})
    assert response.status_code == 201, response.text
    return response.json()


async def _use(client, path, revision, pid):
    response = await client.post(f"{path}/uses", json={"expected_revision": revision, "project_id": pid})
    assert response.status_code == 200, response.text
    return response.json()


async def test_series_owner_revision_and_one_membership(client, make_client):
    await register(client)
    outsider = make_client()
    await register(outsider, "bob")
    book = await create_project(client)
    another_owners_book = await create_project(outsider)
    series = await _new_series(client, "  云境  ")
    assert series == {"id": series["id"], "name": "云境", "revision": 0, "book_count": 0, "entry_count": 0}
    path = f"{_SERIES}/{series['id']}"
    assert (await outsider.get(_SERIES)).json() == []
    assert (await outsider.get(path)).status_code == 404
    forbidden = await client.post(
        f"{path}/books", json={"project_id": another_owners_book["id"], "expected_revision": 0},
    )
    assert forbidden.status_code == 404

    series = await _join(client, series, book)
    assert (series["revision"], series["book_count"]) == (1, 1)
    assert await _join(client, series, book) == series
    other = await _new_series(client, "第二系列")
    overlap = await client.post(f"{_SERIES}/{other['id']}/books", json={
        "project_id": book["id"], "expected_revision": 0,
    })
    assert overlap.status_code == 409 and overlap.json()["code"] == "series_book_already_joined"
    stale = await client.patch(path, json={"name": "并发草稿", "expected_revision": 0})
    assert stale.status_code == 409 and stale.json()["current_revision"] == 1
    assert (await client.delete(path, params={"expected_revision": 1})).json()["code"] == "series_not_empty"
    result = await client.delete(f"{path}/books/{book['id']}", params={"expected_revision": 1})
    assert result.status_code == 200 and result.json()["book_count"] == 0
    assert (await client.delete(path, params={"expected_revision": 2})).status_code == 204


async def _timeline(client, path, kind, entity_id):
    params = {"subject_kind": kind, "character_id" if kind == "character" else "world_setting_id": entity_id}
    response = await client.get(f"{path}/setting-timelines", params=params)
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


async def _append(client, path, timeline, value):
    response = await client.post(f"{path}/setting-timelines/{timeline['id']}/progressions", json={
        "expected_revision": timeline["revision"], "field_key": "profile",
        "operation": "append", "value": value, "anchor_kind": "planned",
        "chapter_no": 1, "boundary": "end",
    })
    assert response.status_code == 201, response.text


async def test_promote_keeps_original_id_and_basis_then_reprojects_each_book(client, app):
    await register(client)
    a, b, c = [await create_project(client, title=title) for title in ("甲", "乙", "独立作品")]
    paths = {book["id"]: f"/api/v1/projects/{book['id']}" for book in (a, b, c)}
    series = await _new_series(client)
    path = f"{_SERIES}/{series['id']}"
    series = await _join(client, series, a)
    series = await _join(client, series, b)
    old = (await client.post(f"{paths[a['id']]}/characters", json={"name": "林青", "profile": "初始"})).json()
    b_local = (await client.post(f"{paths[b['id']]}/characters", json={"name": "林青", "profile": "本书同名"})).json()
    await _append(client, paths[a["id"]], await _timeline(client, paths[a["id"]], "character", old["id"]), "甲书事件")
    chapter = (await client.post(f"{paths[a['id']]}/chapters", json={"chapter_no": 1, "title": "第一章"})).json()
    cast_path = f"{paths[a['id']]}/chapters/{chapter['id']}/cast"
    assert (await client.put(cast_path, json={"character_ids": [old["id"]]})).status_code == 200
    impact = await client.get(f"{path}/impact", params={
        "action": "promote", "project_id": a["id"], "kind": "character", "entry_id": old["id"],
    })
    assert impact.status_code == 200, impact.text
    payload = {"project_id": a["id"], "ref": {"kind": "character", "id": old["id"]},
               "expected_revision": series["revision"], "source_stamp": impact.json()["source_stamp"]}
    stale = await client.post(f"{path}/entries/promote", json={**payload, "source_stamp": "a" * 64})
    assert stale.status_code == 409 and stale.json()["code"] == "series_source_changed"
    promoted = await client.post(f"{path}/entries/promote", json=payload)
    assert promoted.status_code == 201, promoted.text
    p = promoted.json()
    assert p["ref"]["id"] == old["id"]
    assert p["item"]["data"]["profile"] == "初始"
    assert (await client.get(cast_path)).json()["character_ids"] == [old["id"]]
    assert [x["id"] for x in (await client.get(f"{paths[b['id']]}/characters")).json()] == [b_local["id"]]

    entry_path = f"{path}/entries/{p['item']['id']}"
    nonmember = await client.post(f"{entry_path}/uses", json={
        "project_id": c["id"], "expected_revision": p["revision"],
    })
    assert nonmember.status_code == 404
    use = await _use(client, entry_path, p["revision"], b["id"])
    bid = use["ref"]["id"]
    assert bid != b_local["id"]
    assert await _use(client, entry_path, use["revision"], b["id"]) == use
    await _append(client, paths[b["id"]], await _timeline(client, paths[b["id"]], "character", bid), "乙书事件")
    before = {}
    async with app.state.sessionmaker() as db:
        for book in (a, b):
            row = await db.get(Project, book["id"])
            before[book["id"]] = (row.ai_visibility_revision, row.ai_history_epoch)
    edited = await client.patch(entry_path, json={
        "expected_revision": use["revision"], "data": {"profile": "新简介"}, "ai_settings": {"ai_mode": "never"},
    })
    assert edited.status_code == 200, edited.text
    assert edited.json()["item"]["ai_settings"]["ai_mode"] == "never"
    for book, ident, expected in ((a, old["id"], "甲书事件"), (b, bid, "乙书事件")):
        url = f"{paths[book['id']]}/characters/{ident}"
        result = await client.get(url)
        assert result.json()["profile"] == f"新简介\n{expected}", result.text
    assert (await client.get(f"{paths[b['id']]}/characters/{b_local['id']}")).json()["profile"] == "本书同名"
    async with app.state.sessionmaker() as db:
        for book in (a, b):
            row = await db.get(Project, book["id"])
            assert (row.ai_visibility_revision, row.ai_history_epoch) == tuple(n + 1 for n in before[book["id"]])


async def test_recoverable_snapshot_and_recycled_book_block_shared_entry_move(client, app):
    await register(client)
    a, b = [await create_project(client, title=title) for title in ("甲", "乙")]
    series = await _new_series(client)
    path = f"{_SERIES}/{series['id']}"
    for book in (a, b):
        series = await _join(client, series, book)
    entry = await _entry(client, path, series["revision"], "character", name="林青")
    url = f"{path}/entries/{entry['item']['id']}"
    first = await _use(client, url, entry["revision"], a["id"])
    second = await _use(client, url, first["revision"], b["id"])
    async with app.state.sessionmaker() as db:
        snapshot = OutlineSnapshot(
            project_id=b["id"], reason="manual", data={"plans": [{"character_ids": [second["ref"]["id"]]}]},
        )
        db.add(snapshot)
        await db.flush()
        snap_id = snapshot.id
        book = await db.get(Project, b["id"])
        book.deleted_at = book.created_at
        await db.commit()
    impact = await client.get(f"{path}/impact", params={
        "action": "move", "project_id": a["id"], "entry_id": entry["item"]["id"],
    })
    assert impact.status_code == 200, impact.text
    assert impact.json()["blockers"] == [{
        "project_id": b["id"], "title": "乙", "recycled": True, "reasons": ["可恢复的大纲快照"],
    }]
    payload = {"expected_revision": second["revision"], "project_id": a["id"]}
    blocked = await client.post(f"{url}/move-to-book", json=payload)
    assert blocked.status_code == 409 and blocked.json()["blockers"] == impact.json()["blockers"]
    async with app.state.sessionmaker() as db:
        await db.delete(await db.get(OutlineSnapshot, snap_id))
        await db.commit()
    moved = await client.post(f"{url}/move-to-book", json=payload)
    assert moved.status_code == 200, moved.text
    assert moved.json()["ref"] == first["ref"]
    assert (await client.get(url)).status_code == 404


async def test_competing_join_is_one_commit(client):
    await register(client)
    book = await create_project(client)
    one = await _new_series(client, "甲系列")
    two = await _new_series(client, "乙系列")
    outcomes = await asyncio.gather(*[
        client.post(f"{_SERIES}/{series['id']}/books", json={"expected_revision": 0, "project_id": book["id"]})
        for series in (one, two)
    ])
    assert sorted(response.status_code for response in outcomes) == [200, 409]
    assert sum(series["book_count"] for series in (await client.get(_SERIES)).json()) == 1


async def test_promotion_of_legacy_entry_requires_calibration_and_explicit_ai_choice(client, app):
    await register(client)
    book = await create_project(client)
    base = f"/api/v1/projects/{book['id']}"
    item = (await client.post(f"{base}/characters", json={"name": "旧人物"})).json()
    timeline = await _timeline(client, base, "character", item["id"])
    async with app.state.sessionmaker() as db:
        stored = await db.get(Character, item["id"])
        stored.ai_mode = None
        stored.ai_hidden_builtin_fields = ["profile"]
        (await db.get(SettingTimeline, timeline["id"])).baseline_status = "legacy_unverified"
        await db.commit()
    series = await _join(client, await _new_series(client), book)
    path = f"{_SERIES}/{series['id']}"
    params = {"action": "promote", "project_id": book["id"], "entry_id": item["id"], "kind": "character"}
    unverified = await client.get(f"{path}/impact", params=params)
    assert unverified.status_code == 409 and unverified.json()["code"] == "series_baseline_unverified"
    async with app.state.sessionmaker() as db:
        (await db.get(SettingTimeline, timeline["id"])).baseline_status = "confirmed"
        await db.commit()
    preview = (await client.get(f"{path}/impact", params=params)).json()
    assert preview["requires_ai_settings"] is True
    payload = {
        "project_id": book["id"], "ref": {"kind": "character", "id": item["id"]},
        "source_stamp": preview["source_stamp"], "expected_revision": series["revision"],
    }
    unset = await client.post(f"{path}/entries/promote", json=payload)
    assert unset.status_code == 409 and unset.json()["code"] == "series_ai_confirmation_required"
    explicit = await client.post(f"{path}/entries/promote", json={
        **payload, "ai_settings": {"ai_mode": "manual_only", "mention_enabled": False},
    })
    assert explicit.status_code == 201, explicit.text
    ai = explicit.json()["item"]["ai_settings"]
    assert ai["ai_mode"] == "manual_only" and ai["mention_enabled"] is False
    assert ai["ai_hidden_builtin_fields"] == ["profile"]


async def test_late_projection_validation_rolls_back_other_books_and_series(client, app):
    await register(client)
    a, b = [await create_project(client, title=title) for title in ("甲", "乙")]
    series = await _new_series(client)
    for book in (a, b):
        series = await _join(client, series, book)
    path = f"{_SERIES}/{series['id']}"
    entry = await _entry(client, path, series["revision"], "world", title="白塔", category="place", content="旧简介")
    url = f"{path}/entries/{entry['item']['id']}"
    first = await _use(client, url, entry["revision"], a["id"])
    second = await _use(client, url, first["revision"], b["id"])
    b_path = f"/api/v1/projects/{b['id']}"
    definition = await client.post(f"{b_path}/setting-field-definitions", json={
        "expected_fields_revision": 0, "label": "地标", "field_type": "line", "applicable_types": ["world.place"],
    })
    assert definition.status_code == 201, definition.text
    fields_url = f"{b_path}/setting-entries/world/{second['ref']['id']}/fields"
    fields = (await client.get(fields_url)).json()
    did = definition.json()["item"]["id"]
    changed = await client.patch(f"{fields_url}/{did}/baseline", json={
        "expected_fields_revision": fields["fields_revision"],
        "expected_timeline_revision": fields["timeline_revision"],
        "field_type": "line", "value": "城市中央",
    })
    assert changed.status_code == 200, changed.text
    async with app.state.sessionmaker() as db:
        a_before = (await db.get(Project, a["id"])).ai_visibility_revision
    result = await client.patch(url, json={
        "expected_revision": second["revision"], "data": {"category": "item", "content": "更新失败"},
    })
    assert result.status_code == 409 and result.json()["code"] == "incompatible_field_scope"
    assert (await client.get(url)).json()["data"]["category"] == "place"
    assert (await client.get(path)).json()["revision"] == second["revision"]
    for book, used in ((a, first), (b, second)):
        entry_url = f"/api/v1/projects/{book['id']}/world/{used['ref']['id']}"
        data = (await client.get(entry_url)).json()
        assert (data["category"], data["content"]) == ("place", "旧简介")
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, a["id"])).ai_visibility_revision == a_before
