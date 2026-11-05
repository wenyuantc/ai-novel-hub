"""世界观设定随章节变化的 HTTP 合同与隔离。"""

import pytest
from sqlalchemy import inspect, select

from app.core.errors import NotFoundError
from app.models import OrganizationMember, Project, SettingProgression, SettingTimeline, User, WorldSetting
from tests.helpers import create_project, register


async def _world_and_timeline(client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    response = await client.post(f"{base}/world", json={"title": "山门", "category": "place", "content": "完好"})
    assert response.status_code == 201, response.text
    world = response.json()
    response = await client.get(
        f"{base}/setting-timelines", params={"subject_kind": "world", "world_setting_id": world["id"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    timeline = response.json()["items"][0]
    assert timeline["baseline_status"] == "confirmed"
    return base, world, timeline


async def _chapter(client, base, chapter_no):
    response = await client.post(f"{base}/chapters", json={"chapter_no": chapter_no, "title": f"第{chapter_no}章"})
    assert response.status_code == 201, response.text
    return response.json()


def _change(revision, *, chapter_id=None, chapter_no=None, operation="replace", value="新状态", **kwargs):
    return {
        "expected_revision": revision,
        "field_key": "content",
        "custom_field_id": None,
        "operation": operation,
        "value": value,
        "anchor_kind": "chapter" if chapter_id is not None else "planned",
        "chapter_id": chapter_id,
        "chapter_no": chapter_no,
        "boundary": "start",
        **kwargs,
    }


async def test_world_progressions_use_chapter_boundaries_and_rematerialize_after_edit_and_delete(client):
    base, world, timeline = await _world_and_timeline(client)
    c5 = await _chapter(client, base, 5)
    c20 = await _chapter(client, base, 20)
    c21 = await _chapter(client, base, 21)
    url = f"{base}/setting-timelines/{timeline['id']}"

    before = (await client.get(f"{url}/baseline")).json()
    assert before["baseline"] == {"content": "完好"}
    first = await client.post(
        f"{url}/progressions",
        json=_change(before["timeline_revision"], chapter_id=c5["id"], operation="append", value="增设阵法"),
    )
    assert first.status_code == 201, first.text
    rev = first.json()["timeline_revision"]
    second = await client.post(
        f"{url}/progressions",
        json=_change(rev, chapter_id=c20["id"], value="山门被毁", boundary="end"),
    )
    assert second.status_code == 201, second.text
    rev = second.json()["timeline_revision"]

    async def effective(chapter_id, boundary="start"):
        r = await client.get(f"{url}/effective", params={"chapter_id": chapter_id, "boundary": boundary})
        assert r.status_code == 200, r.text
        return r.json()["fields"]["content"]

    assert await effective(c5["id"]) == "完好\n增设阵法"
    assert await effective(c20["id"]) == "完好\n增设阵法"
    assert await effective(c20["id"], "end") == "山门被毁"
    assert await effective(c21["id"]) == "山门被毁"
    assert (await client.get(f"{base}/world/{world['id']}")).json()["content"] == "山门被毁"

    first_id = first.json()["item"]["id"]
    edited = await client.patch(f"{url}/progressions/{first_id}", json={"expected_revision": rev, "value": "布下阵法"})
    assert edited.status_code == 200, edited.text
    assert await effective(c5["id"]) == "完好\n布下阵法"
    assert await effective(c21["id"]) == "山门被毁"
    removed = await client.delete(
        f"{url}/progressions/{second.json()['item']['id']}",
        params={"expected_revision": edited.json()["timeline_revision"]},
    )
    assert removed.status_code == 200, removed.text
    assert await effective(c21["id"]) == "完好\n布下阵法"
    assert (await client.get(f"{base}/world/{world['id']}")).json()["content"] == "完好\n布下阵法"
    assert (await client.get(f"{url}/baseline")).json()["baseline"] == {"content": "完好"}


async def test_world_legacy_patch_never_overwrites_baseline_and_explicit_baseline_update_is_atomic(client):
    base, world, timeline = await _world_and_timeline(client)
    url = f"{base}/setting-timelines/{timeline['id']}"
    created = await client.post(f"{url}/progressions", json=_change(timeline["revision"], chapter_no=20, value="被毁"))
    assert created.status_code == 201, created.text
    rev = created.json()["timeline_revision"]
    world_url = f"{base}/world/{world['id']}"

    same = await client.patch(world_url, json={"title": "更名的山门", "content": "被毁"})
    assert same.status_code == 200, same.text
    assert (await client.get(f"{url}/baseline")).json()["baseline"]["content"] == "完好"
    ambiguous = await client.patch(world_url, json={"title": "不应保存", "content": "偷偷改变"})
    assert ambiguous.status_code == 409
    assert ambiguous.json()["code"] == "progression_intent_required"
    assert (await client.get(world_url)).json()["title"] == "更名的山门"

    stale = await client.patch(
        world_url,
        json={"title": "不应保存", "content": "新的基础", "edit_mode": "baseline", "expected_revision": rev - 1},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_revision"
    assert (await client.get(world_url)).json()["title"] == "更名的山门"
    saved = await client.patch(
        world_url, json={"title": "已修正", "content": "新的基础", "edit_mode": "baseline", "expected_revision": rev}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["title"] == "已修正"
    assert saved.json()["content"] == "被毁"
    baseline = (await client.get(f"{url}/baseline")).json()
    assert baseline["baseline"]["content"] == "新的基础"
    assert baseline["timeline_revision"] == rev + 1


async def test_world_progressions_validate_references_and_revision_before_changes(client, make_client):
    base, world, timeline = await _world_and_timeline(client)
    other = await create_project(client, title="另一本书")
    other_base = f"/api/v1/projects/{other['id']}"
    foreign_chapter = await _chapter(client, other_base, 8)
    url = f"{base}/setting-timelines/{timeline['id']}"
    bad = await client.post(f"{url}/progressions", json=_change(timeline["revision"], chapter_id=foreign_chapter["id"]))
    assert bad.status_code in (400, 404), bad.text
    assert (await client.get(f"{url}/baseline")).json()["timeline_revision"] == timeline["revision"]
    assert (await client.get(f"{base}/world/{world['id']}")).json()["content"] == "完好"

    invalid = await client.post(
        f"{url}/progressions", json=_change(timeline["revision"], chapter_no=10, field_key="title")
    )
    assert invalid.status_code in (400, 422), invalid.text
    valid = await client.post(f"{url}/progressions", json=_change(timeline["revision"], chapter_no=10))
    assert valid.status_code == 201, valid.text
    stale = await client.post(f"{url}/progressions", json=_change(timeline["revision"], chapter_no=20, value="丢弃"))
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_revision"
    listed = (await client.get(f"{url}/progressions")).json()
    assert listed["total"] == 1
    assert listed["items"][0]["value"] == "新状态"

    other_user = make_client()
    await register(other_user, "bob")
    assert (await other_user.get(url + "/baseline")).status_code == 404
    assert (await other_user.post(url + "/progressions", json=_change(2, chapter_no=10))).status_code == 404


async def test_invalid_sort_order_never_modifies_persisted_event_or_revision(client):
    base, _, timeline = await _world_and_timeline(client)
    url = f"{base}/setting-timelines/{timeline['id']}"
    invalid = await client.post(f"{url}/progressions", json=_change(timeline["revision"], chapter_no=10, sort_order=-1))
    assert invalid.status_code == 422
    assert (await client.get(f"{url}/baseline")).json()["timeline_revision"] == timeline["revision"]
    valid = await client.post(f"{url}/progressions", json=_change(timeline["revision"], chapter_no=10))
    assert valid.status_code == 201, valid.text
    event = valid.json()["item"]
    revision = valid.json()["timeline_revision"]
    invalid_edit = await client.patch(
        f"{url}/progressions/{event['id']}", json={"expected_revision": revision, "sort_order": -1}
    )
    assert invalid_edit.status_code == 422
    detail = (await client.get(f"{url}/progressions/{event['id']}")).json()
    assert detail["item"]["sort_order"] == 0
    assert detail["timeline_revision"] == revision


async def test_custom_text_fields_preserve_identity_and_protect_referenced_changes(client):
    base, _, timeline = await _world_and_timeline(client)
    chapter = await _chapter(client, base, 10)
    url = f"{base}/setting-timelines/{timeline['id']}"
    response = await client.post(
        f"{url}/text-fields",
        json={"expected_revision": timeline["revision"], "label": "守护者", "base_value": "老守卫"},
    )
    assert response.status_code == 201, response.text
    field = response.json()["item"]
    rev = response.json()["timeline_revision"]
    duplicate = await client.post(f"{url}/text-fields", json={"expected_revision": rev, "label": " 守护者 "})
    assert duplicate.status_code == 409
    event = await client.post(
        f"{url}/progressions",
        json=_change(rev, chapter_id=chapter["id"], field_key=None, custom_field_id=field["id"], value="新守卫"),
    )
    assert event.status_code == 201, event.text
    rev = event.json()["timeline_revision"]
    renamed = await client.patch(f"{url}/text-fields/{field['id']}", json={"expected_revision": rev, "label": "守门人"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["item"]["id"] == field["id"]
    rev = renamed.json()["timeline_revision"]
    effective = (await client.get(f"{url}/effective", params={"chapter_id": chapter["id"]})).json()
    assert effective["fields"]["content"] == "完好"
    assert effective["custom_fields"][str(field["id"])] == "新守卫"
    blocked = await client.delete(f"{url}/text-fields/{field['id']}", params={"expected_revision": rev})
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "referenced_field"

    removed = await client.delete(f"{url}/progressions/{event.json()['item']['id']}", params={"expected_revision": rev})
    assert removed.status_code == 200, removed.text
    deleted = await client.delete(
        f"{url}/text-fields/{field['id']}", params={"expected_revision": removed.json()["timeline_revision"]}
    )
    assert deleted.status_code == 200, deleted.text
    assert (await client.get(f"{url}/text-fields")).json()["items"] == []


async def test_character_legacy_patch_and_explicit_baseline_save_never_corrupt_timeline(client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    character = (await client.post(f"{base}/characters", json={"name": "林舟", "current_state": "练气"})).json()
    rows = await client.get(
        f"{base}/setting-timelines", params={"subject_kind": "character", "character_id": character["id"]}
    )
    assert rows.status_code == 200, rows.text
    assert rows.json()["total"] == 1
    timeline = rows.json()["items"][0]
    assert timeline["baseline_status"] == "confirmed"
    url = f"{base}/setting-timelines/{timeline['id']}"
    event = await client.post(
        f"{url}/progressions",
        json=_change(timeline["revision"], chapter_no=20, field_key="current_state", value="金丹"),
    )
    assert event.status_code == 201, event.text
    revision = event.json()["timeline_revision"]
    character_url = f"{base}/characters/{character['id']}"

    same = await client.patch(character_url, json={"name": "林远舟", "current_state": "金丹"})
    assert same.status_code == 200, same.text
    assert (await client.get(f"{url}/baseline")).json()["baseline"]["current_state"] == "练气"
    ambiguous = await client.patch(character_url, json={"name": "不应保存", "current_state": "筑基"})
    assert ambiguous.status_code == 409
    assert ambiguous.json()["code"] == "progression_intent_required"
    assert (await client.get(character_url)).json()["name"] == "林远舟"

    stale = await client.patch(
        character_url,
        json={"name": "不应保存", "current_state": "筑基", "edit_mode": "baseline", "expected_revision": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_revision"
    assert (await client.get(character_url)).json()["name"] == "林远舟"

    # 与最新值相等但与旧基线不同，显式基础修正仍必须真正生效。
    saved = await client.patch(
        character_url,
        json={"name": "已修正", "current_state": "金丹", "edit_mode": "baseline", "expected_revision": revision},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["name"] == "已修正" and saved.json()["current_state"] == "金丹"
    baseline = (await client.get(f"{url}/baseline")).json()
    assert baseline["baseline"]["current_state"] == "金丹"
    assert baseline["timeline_revision"] == revision + 1


async def test_metadata_only_baseline_edits_claim_revision_for_world_and_character(app, client):
    base, world, world_timeline = await _world_and_timeline(client)
    async with app.state.sessionmaker() as db:
        timeline_row = await db.get(SettingTimeline, world_timeline["id"])
        timeline_row.baseline_status = "legacy_unverified"
        timeline_row.legacy_compat_allowed = True
        await db.commit()
    world_url = f"{base}/world/{world['id']}"
    timeline_url = f"{base}/setting-timelines/{world_timeline['id']}"
    saved = await client.patch(
        world_url, json={"title": "新山门", "edit_mode": "baseline", "expected_revision": world_timeline["revision"]}
    )
    assert saved.status_code == 200, saved.text
    baseline = (await client.get(f"{timeline_url}/baseline")).json()
    assert baseline["timeline_revision"] == world_timeline["revision"] + 1
    assert baseline["baseline_status"] == "legacy_unverified" and baseline["legacy_compat_allowed"] is True
    stale = await client.patch(
        world_url, json={"title": "不应覆盖", "edit_mode": "baseline", "expected_revision": world_timeline["revision"]}
    )
    assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
    assert (await client.get(world_url)).json()["title"] == "新山门"

    created = await client.post(f"{base}/characters", json={"name": "林舟"})
    character = created.json()
    rows = await client.get(
        f"{base}/setting-timelines", params={"subject_kind": "character", "character_id": character["id"]}
    )
    timeline = rows.json()["items"][0]
    character_url = f"{base}/characters/{character['id']}"
    saved = await client.patch(
        character_url, json={"name": "林远舟", "edit_mode": "baseline", "expected_revision": timeline["revision"]}
    )
    assert saved.status_code == 200, saved.text
    timeline_url = f"{base}/setting-timelines/{timeline['id']}"
    assert (await client.get(f"{timeline_url}/baseline")).json()["timeline_revision"] == timeline["revision"] + 1
    stale = await client.patch(
        character_url, json={"name": "不应覆盖", "edit_mode": "baseline", "expected_revision": timeline["revision"]}
    )
    assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
    assert (await client.get(character_url)).json()["name"] == "林远舟"


async def _membership_timeline(app, client, *, exists, note=""):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    organization = (await client.post(f"{base}/organizations", json={"name": "门派"})).json()
    character = (await client.post(f"{base}/characters", json={"name": "林舟"})).json()
    baseline = {"exists": False}
    if exists:
        baseline = {"exists": True, "position": "弟子", "status": "active", "note": note}
    async with app.state.sessionmaker() as db:
        timeline = SettingTimeline(
            project_id=project["id"],
            subject_kind="membership",
            character_id=character["id"],
            organization_id=organization["id"],
            baseline=baseline,
        )
        db.add(timeline)
        if exists:
            db.add(
                OrganizationMember(
                    organization_id=organization["id"],
                    character_id=character["id"],
                    position="弟子",
                    status="active",
                    note=note,
                )
            )
        await db.commit()
        timeline_id = timeline.id
    return base, organization, character, f"{base}/setting-timelines/{timeline_id}"


async def test_membership_rejects_changes_before_creation_without_persisting_them(app, client):
    _, _, _, url = await _membership_timeline(app, client, exists=False)
    revision = (await client.get(f"{url}/baseline")).json()["timeline_revision"]
    created = await client.post(
        f"{url}/progressions",
        json=_change(
            revision,
            chapter_no=20,
            field_key="presence",
            value={
                "exists": True,
                "state": {"position": "弟子", "status": "active", "note": ""},
            },
        ),
    )
    assert created.status_code == 201, created.text
    revision = created.json()["timeline_revision"]
    invalid = await client.post(
        f"{url}/progressions", json=_change(revision, chapter_no=10, field_key="position", value="长老")
    )
    assert invalid.status_code == 409, invalid.text
    assert invalid.json()["code"] == "progression_lifecycle_conflict"
    listed = (await client.get(f"{url}/progressions")).json()
    assert listed["total"] == 1 and listed["timeline_revision"] == revision
    assert (await client.get(f"{url}/effective", params={"chapter_no": 10})).json()["fields"] == {"exists": False}


async def test_count_unverified_timelines_is_project_scoped_and_requires_read_access(app, client, make_client):
    from app.services.setting_progression_service import count_unverified_timelines

    base, _, timeline = await _world_and_timeline(client)
    other = await create_project(client, title="另一本书")
    other_world = await client.post(
        f"/api/v1/projects/{other['id']}/world", json={"title": "另一处", "content": "另一本书的内容"}
    )
    other_timeline = await client.get(
        f"/api/v1/projects/{other['id']}/setting-timelines",
        params={"subject_kind": "world", "world_setting_id": other_world.json()["id"]},
    )
    stranger = make_client()
    await register(stranger, "stranger_count")
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, int(base.rsplit("/", 1)[1]))
        first = await db.get(SettingTimeline, timeline["id"])
        second = await db.get(SettingTimeline, other_timeline.json()["items"][0]["id"])
        first.baseline_status = second.baseline_status = "legacy_unverified"
        await db.commit()
        stranger_id = await db.scalar(select(User.id).where(User.username == "stranger_count"))
        assert await count_unverified_timelines(db, project.user_id, project.id) == 1
        assert not db.dirty
        with pytest.raises(NotFoundError):
            await count_unverified_timelines(db, stranger_id, project.id)


async def test_resolving_snapshots_neither_mutates_nor_flushes_orm_objects(app, client):
    from app.services.setting_progression_service import read_effective, resolve_timelines

    base, world, timeline_data = await _world_and_timeline(client)
    timeline_url = f"{base}/setting-timelines/{timeline_data['id']}"
    created = await client.post(
        f"{timeline_url}/progressions", json=_change(timeline_data["revision"], chapter_no=20, value="被毁")
    )
    assert created.status_code == 201, created.text
    async with app.state.sessionmaker() as db:
        timeline = await db.get(SettingTimeline, timeline_data["id"])
        event = await db.get(SettingProgression, created.json()["item"]["id"])
        world_row = await db.get(WorldSetting, world["id"])
        project = await db.get(Project, timeline.project_id)
        snapshots = await resolve_timelines(db, timeline.project_id, [timeline])
        snapshots[timeline.id].fields["content"] = "只改快照"
        assert not db.dirty
        assert timeline.baseline["content"] == "完好" and event.value == "被毁"
        event.note = "未保存的备注"
        world_row.title = "未保存的标题"
        await resolve_timelines(db, timeline.project_id, [timeline])
        assert inspect(event).attrs.note.history.has_changes()
        assert inspect(world_row).attrs.title.history.has_changes()
        response = await read_effective(
            db, project.user_id, project.id, timeline.id, chapter_id=None, chapter_no=20, boundary="start"
        )
        assert response.fields["content"] == "被毁"
        assert inspect(event).attrs.note.history.has_changes()
        assert inspect(world_row).attrs.title.history.has_changes()


@pytest.mark.parametrize(
    ("method", "path", "params"),
    [
        ("GET", "/effective", {"chapter_no": 2_147_483_648}),
        ("GET", "/effective", {"chapter_id": 9_223_372_036_854_775_808}),
        ("GET", "", {"character_id": 9_223_372_036_854_775_808}),
        ("GET", "", {"related_character_id": 2_147_483_648}),
        ("GET", "", {"world_setting_id": 9_223_372_036_854_775_808}),
        ("GET", "", {"career_id": 2_147_483_648}),
        ("GET", "", {"organization_id": 2_147_483_648}),
        ("GET", "/progressions", {"offset": 9_223_372_036_854_775_808}),
        ("GET", "/progressions", {"field": "custom:9223372036854775808"}),
        ("DELETE", "/progressions/1", {"expected_revision": 2_147_483_648}),
        ("DELETE", "/text-fields/1", {"expected_revision": 2_147_483_648}),
    ],
)
async def test_timeline_query_integers_reject_values_outside_database_range(client, method, path, params):
    base, _, timeline = await _world_and_timeline(client)
    timeline_url = f"{base}/setting-timelines/{timeline['id']}"
    url = f"{base}/setting-timelines" if path == "" else f"{timeline_url}{path}"
    response = await client.request(method, url, params=params)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/projects/2147483648/setting-timelines",
        "/setting-timelines/2147483648/baseline",
        "/setting-timelines/1/progressions/2147483648",
        "/setting-timelines/1/text-fields/2147483648",
        "/setting-timelines/0/baseline",
    ],
)
async def test_timeline_path_integers_reject_values_outside_database_range(client, path):
    base, _, _ = await _world_and_timeline(client)
    url = path if path.startswith("/api/") else base + path
    method = "PATCH" if "/text-fields/" in path else "GET"
    response = await client.request(
        method, url, json={"expected_revision": 1, "label": "x"} if method == "PATCH" else None
    )
    assert response.status_code == 422, response.text


async def test_timeline_queries_accept_valid_upper_bound(client):
    base, _, timeline = await _world_and_timeline(client)
    url = f"{base}/setting-timelines/{timeline['id']}"
    response = await client.get(f"{url}/effective", params={"chapter_no": 2_147_483_647})
    assert response.status_code == 200, response.text
    assert response.json()["position"]["chapter_no"] == 2_147_483_647
    response = await client.get(f"{url}/progressions", params={"field": "custom:2147483647"})
    assert response.status_code == 200, response.text


async def test_deleted_chapter_event_stays_orphaned_after_same_number_is_reused(client):
    base, world, timeline = await _world_and_timeline(client)
    chapter = await _chapter(client, base, 20)
    url = f"{base}/setting-timelines/{timeline['id']}"
    created = await client.post(
        f"{url}/progressions", json=_change(timeline["revision"], chapter_id=chapter["id"], value="被毁")
    )
    assert created.status_code == 201, created.text
    event_id = created.json()["item"]["id"]
    removed = await client.delete(f"{base}/chapters/{chapter['id']}")
    assert removed.status_code == 204, removed.text
    detail = await client.get(f"{url}/progressions/{event_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["item"]["chapter_id"] is None and detail.json()["item"]["orphaned"] is True
    assert (await client.get(f"{base}/world/{world['id']}")).json()["content"] == "完好"

    replacement = await _chapter(client, base, 20)
    preview = await client.get(f"{url}/effective", params={"chapter_id": replacement["id"]})
    assert preview.status_code == 200, preview.text
    assert preview.json()["fields"]["content"] == "完好"
    assert event_id in preview.json()["orphaned_event_ids"]
    assert (await client.get(f"{url}/progressions/{event_id}")).json()["item"]["chapter_id"] is None
