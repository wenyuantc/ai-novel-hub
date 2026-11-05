"""组织成员的章节演变，覆盖真实 HTTP、持久化投影和旧入口兼容。"""

import pytest
from sqlalchemy import select

from app.core.errors import BadRequestError
from app.models import SettingTimeline
from app.services.membership_progression_service import materialize_memberships
from app.services.setting_progression_resolver import EffectiveSnapshot
from tests.helpers import create_project, register


async def _setup(client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    org = await client.post(f"{base}/organizations", json={"name": "青云宗"})
    character = await client.post(f"{base}/characters", json={"name": "林远"})
    assert org.status_code == character.status_code == 201
    chapters = {}
    for number in (5, 10, 15, 20, 25, 30, 35):
        response = await client.post(f"{base}/chapters", json={"chapter_no": number, "title": f"第{number}章"})
        assert response.status_code == 201, response.text
        chapters[number] = response.json()["id"]
    return base, org.json(), character.json(), chapters


async def _timeline(client, base, org_id, character_id):
    response = await client.get(
        f"{base}/setting-timelines",
        params={"subject_kind": "membership", "organization_id": org_id, "character_id": character_id},
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    return response.json()["items"][0]


async def _effective(client, base, timeline_id, chapter_id, boundary="start"):
    response = await client.get(
        f"{base}/setting-timelines/{timeline_id}/effective",
        params={"chapter_id": chapter_id, "boundary": boundary},
    )
    assert response.status_code == 200, response.text
    return response.json()["fields"]


async def test_membership_lifecycle_retains_earlier_state_after_current_row_is_removed(client):
    base, org, character, chapters = await _setup(client)
    members = f"{base}/organizations/{org['id']}/members"
    created = await client.post(
        members,
        json={
            "character_id": character["id"], "position": "弟子", "status": "active", "note": "初入门",
            "edit_mode": "progression", "chapter_id": chapters[10],
        },
    )
    assert created.status_code == 201, created.text
    member_id = created.json()["id"]
    timeline = await _timeline(client, base, org["id"], character["id"])
    timeline_id = timeline["id"]
    assert (await _effective(client, base, timeline_id, chapters[5])) == {"exists": False}
    assert (await _effective(client, base, timeline_id, chapters[15])) == {
        "exists": True, "position": "弟子", "status": "active", "note": "初入门",
    }

    update = await client.patch(
        f"{members}/{member_id}",
        json={
            "edit_mode": "progression", "expected_revision": timeline["revision"], "chapter_id": chapters[20],
            "position": "长老", "status": "expelled", "note": "被逐出",
        },
    )
    assert update.status_code == 200, update.text
    timeline = await _timeline(client, base, org["id"], character["id"])
    removed = await client.delete(
        f"{members}/{member_id}",
        params={"edit_mode": "progression", "expected_revision": timeline["revision"],
                "chapter_id": chapters[20], "boundary": "end"},
    )
    assert removed.status_code == 204, removed.text
    assert (await client.get(members)).json() == []
    assert (await _effective(client, base, timeline_id, chapters[15]))["position"] == "弟子"
    assert (await _effective(client, base, timeline_id, chapters[20])) == {
        "exists": True, "position": "长老", "status": "expelled", "note": "被逐出",
    }
    assert (await _effective(client, base, timeline_id, chapters[25])) == {"exists": False}

    timeline = await _timeline(client, base, org["id"], character["id"])
    rejoined = await client.post(
        members,
        json={
            "character_id": character["id"], "position": "客卿", "note": "重回宗门",
            "edit_mode": "progression", "expected_revision": timeline["revision"], "chapter_id": chapters[30],
        },
    )
    assert rejoined.status_code == 201, rejoined.text
    assert (await _timeline(client, base, org["id"], character["id"]))["id"] == timeline_id
    assert (await _effective(client, base, timeline_id, chapters[25])) == {"exists": False}
    assert (await _effective(client, base, timeline_id, chapters[35])) == {
        "exists": True, "position": "客卿", "status": "active", "note": "重回宗门",
    }


async def test_legacy_member_edits_and_deletes_do_not_silently_destroy_history(client):
    base, org, character, chapters = await _setup(client)
    members = f"{base}/organizations/{org['id']}/members"
    created = await client.post(members, json={"character_id": character["id"], "position": "弟子"})
    assert created.status_code == 201, created.text
    member_id = created.json()["id"]
    timeline = await _timeline(client, base, org["id"], character["id"])
    changed = await client.patch(
        f"{members}/{member_id}",
        json={"edit_mode": "progression", "expected_revision": timeline["revision"],
              "chapter_id": chapters[20], "position": "长老"},
    )
    assert changed.status_code == 200, changed.text
    timeline = await _timeline(client, base, org["id"], character["id"])
    same = await client.patch(f"{members}/{member_id}", json={"position": "长老"})
    assert same.status_code == 200, same.text
    wrong = await client.patch(f"{members}/{member_id}", json={"position": "宗主", "note": "不得部分保存"})
    assert wrong.status_code == 409
    assert wrong.json()["code"] == "progression_intent_required"
    assert (await client.get(members)).json()[0]["note"] == ""
    assert (await client.delete(f"{members}/{member_id}")).status_code == 409
    assert (await _effective(client, base, timeline["id"], chapters[15]))["position"] == "弟子"

    saved = await client.patch(
        f"{members}/{member_id}",
        json={"edit_mode": "baseline", "expected_revision": timeline["revision"], "position": "杂役"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["position"] == "长老"
    assert (await _effective(client, base, timeline["id"], chapters[15]))["position"] == "杂役"
    timeline = await _timeline(client, base, org["id"], character["id"])
    deleted = await client.delete(
        f"{members}/{member_id}", params={"edit_mode": "delete", "expected_revision": timeline["revision"]}
    )
    assert deleted.status_code == 204, deleted.text
    timelines = await client.get(
        f"{base}/setting-timelines",
        params={"subject_kind": "membership", "organization_id": org["id"], "character_id": character["id"]},
    )
    assert timelines.json()["total"] == 0


async def test_membership_rejects_cross_project_anchors_and_stale_revisions(client, make_client):
    base, org, character, chapters = await _setup(client)
    other_project = await create_project(client, title="另一本书")
    foreign_base = f"/api/v1/projects/{other_project['id']}"
    foreign = await client.post(f"{foreign_base}/chapters", json={"chapter_no": 99, "title": "异章"})
    members = f"{base}/organizations/{org['id']}/members"
    invalid = await client.post(
        members,
        json={"character_id": character["id"], "edit_mode": "progression", "chapter_id": foreign.json()["id"]},
    )
    assert invalid.status_code in (400, 404), invalid.text
    assert (await client.get(members)).json() == []

    created = await client.post(members, json={"character_id": character["id"], "position": "弟子"})
    assert created.status_code == 201, created.text
    timeline = await _timeline(client, base, org["id"], character["id"])
    stale = await client.patch(
        f"{members}/{created.json()['id']}",
        json={"edit_mode": "progression", "expected_revision": timeline["revision"] + 1,
              "chapter_id": chapters[20], "position": "掌门", "note": "不可写入"},
    )
    assert stale.status_code == 409, stale.text
    assert (await client.get(members)).json()[0]["position"] == "弟子"
    assert (await _effective(client, base, timeline["id"], chapters[20]))["note"] == ""
    stale_noop = await client.patch(
        f"{members}/{created.json()['id']}",
        json={"edit_mode": "baseline", "expected_revision": timeline["revision"] + 1},
    )
    assert stale_noop.status_code == 409, stale_noop.text
    foreign_noop = await client.patch(
        f"{members}/{created.json()['id']}",
        json={"edit_mode": "progression", "expected_revision": timeline["revision"],
              "chapter_id": foreign.json()["id"]},
    )
    assert foreign_noop.status_code in (400, 404), foreign_noop.text

    stranger = make_client()
    await register(stranger, "other_membership_user")
    forbidden = await stranger.patch(
        f"{members}/{created.json()['id']}",
        json={"edit_mode": "baseline", "expected_revision": timeline["revision"], "position": "窃改"},
    )
    assert forbidden.status_code == 404


async def test_revoking_member_events_restores_projection_and_checks_dependencies(client):
    base, org, character, chapters = await _setup(client)
    members = f"{base}/organizations/{org['id']}/members"
    created = await client.post(
        members,
        json={"character_id": character["id"], "position": "弟子", "note": "最初",
              "edit_mode": "progression", "chapter_id": chapters[10]},
    )
    assert created.status_code == 201, created.text
    member_id = created.json()["id"]
    timeline = await _timeline(client, base, org["id"], character["id"])
    url = f"{base}/setting-timelines/{timeline['id']}"
    updated = await client.patch(
        f"{members}/{member_id}",
        json={"edit_mode": "progression", "expected_revision": timeline["revision"],
              "chapter_id": chapters[20], "position": "长老"},
    )
    assert updated.status_code == 200, updated.text
    timeline = await _timeline(client, base, org["id"], character["id"])
    removed = await client.delete(
        f"{members}/{member_id}",
        params={"edit_mode": "progression", "expected_revision": timeline["revision"], "chapter_id": chapters[25]},
    )
    assert removed.status_code == 204, removed.text
    assert (await client.get(members)).json() == []
    events = (await client.get(f"{url}/progressions")).json()["items"]
    joined = next(event for event in events if event["field_key"] == "presence" and event["value"]["exists"])
    left = next(event for event in events if event["field_key"] == "presence" and not event["value"]["exists"])
    changed = next(event for event in events if event["field_key"] == "position")
    timeline = await _timeline(client, base, org["id"], character["id"])

    restored = await client.delete(
        f"{url}/progressions/{left['id']}", params={"expected_revision": timeline["revision"]}
    )
    assert restored.status_code == 200, restored.text
    assert (await client.get(members)).json()[0]["position"] == "长老"
    assert (await _effective(client, base, timeline["id"], chapters[15]))["position"] == "弟子"

    revision = restored.json()["timeline_revision"]
    blocked = await client.delete(f"{url}/progressions/{joined['id']}", params={"expected_revision": revision})
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "progression_lifecycle_conflict"
    assert (await _timeline(client, base, org["id"], character["id"]))["revision"] == revision
    assert (await client.get(members)).json()[0]["position"] == "长老"
    assert (await _effective(client, base, timeline["id"], chapters[15]))["position"] == "弟子"

    cancel_change = await client.delete(
        f"{url}/progressions/{changed['id']}", params={"expected_revision": revision}
    )
    assert cancel_change.status_code == 200, cancel_change.text
    cancel_join = await client.delete(
        f"{url}/progressions/{joined['id']}",
        params={"expected_revision": cancel_change.json()["timeline_revision"]},
    )
    assert cancel_join.status_code == 200, cancel_join.text
    assert (await client.get(members)).json() == []
    assert await _effective(client, base, timeline["id"], chapters[35]) == {"exists": False}


async def test_member_long_notes_remain_usable_in_baselines_and_progressions(client):
    base, org, character, chapters = await _setup(client)
    members = f"{base}/organizations/{org['id']}/members"
    original_note = "过往经历" * 80
    next_note = "后续经历" * 80
    created = await client.post(members, json={"character_id": character["id"], "note": original_note})
    assert created.status_code == 201, created.text
    timeline = await _timeline(client, base, org["id"], character["id"])
    assert (await _effective(client, base, timeline["id"], chapters[15]))["note"] == original_note
    updated = await client.patch(
        f"{members}/{created.json()['id']}",
        json={"edit_mode": "progression", "expected_revision": timeline["revision"],
              "chapter_id": chapters[20], "note": next_note},
    )
    assert updated.status_code == 200, updated.text
    assert (await _effective(client, base, timeline["id"], chapters[15]))["note"] == original_note
    assert (await _effective(client, base, timeline["id"], chapters[25]))["note"] == next_note
    assert (await client.get(members)).json()[0]["note"] == next_note


async def test_member_materializer_validates_all_snapshots_before_changing_rows(client, app):
    base, org, first, _ = await _setup(client)
    second = (await client.post(f"{base}/characters", json={"name": "沈舟"})).json()
    members = f"{base}/organizations/{org['id']}/members"
    for character in (first, second):
        created = await client.post(members, json={"character_id": character["id"], "position": "弟子"})
        assert created.status_code == 201, created.text

    def snapshot(fields):
        return EffectiveSnapshot(
            fields=fields, custom_fields={}, applied_event_ids=(), orphaned_event_ids=(), baseline_status="confirmed"
        )

    async with app.state.sessionmaker() as db:
        timelines = list(
            (await db.scalars(select(SettingTimeline).where(SettingTimeline.subject_kind == "membership"))).all()
        )
        by_character = {timeline.character_id: timeline for timeline in timelines}
        t1, t2 = by_character[first["id"]], by_character[second["id"]]
        updated = snapshot({"exists": True, "position": "长老", "status": "active", "note": ""})
        invalid = snapshot({"exists": True, "position": "长老", "status": "未知", "note": ""})
        with pytest.raises(BadRequestError):
            await materialize_memberships(db, org["project_id"], timelines, {t1.id: updated, t2.id: invalid})
        assert [member["position"] for member in (await client.get(members)).json()] == ["弟子", "弟子"]

        absent = snapshot({"exists": False})
        await materialize_memberships(db, org["project_id"], timelines, {t1.id: updated, t2.id: absent})
        await db.commit()
    current = (await client.get(members)).json()
    assert [(member["character_id"], member["position"]) for member in current] == [(first["id"], "长老")]
    assert (await _timeline(client, base, org["id"], second["id"]))["id"] == t2.id


async def test_stale_member_creation_cannot_recreate_a_deleted_timeline(client):
    base, org, character, _ = await _setup(client)
    members = f"{base}/organizations/{org['id']}/members"
    created = await client.post(members, json={"character_id": character["id"]})
    assert created.status_code == 201, created.text
    timeline = await _timeline(client, base, org["id"], character["id"])
    deleted = await client.delete(
        f"{members}/{created.json()['id']}",
        params={"edit_mode": "delete", "expected_revision": timeline["revision"]},
    )
    assert deleted.status_code == 204, deleted.text
    stale = await client.post(
        members,
        json={"character_id": character["id"], "edit_mode": "baseline", "expected_revision": timeline["revision"]},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "stale_revision"
    assert (await client.get(members)).json() == []
    response = await client.get(
        f"{base}/setting-timelines",
        params={"subject_kind": "membership", "organization_id": org["id"], "character_id": character["id"]},
    )
    assert response.json()["total"] == 0


async def test_delete_absent_member_record_by_stable_subject_checks_revision_and_ownership(client, make_client):
    base, org, character, chapters = await _setup(client)
    members = f"{base}/organizations/{org['id']}/members"
    created = await client.post(
        members,
        json={"character_id": character["id"], "position": "弟子", "edit_mode": "progression",
              "chapter_id": chapters[10]},
    )
    assert created.status_code == 201, created.text
    timeline = await _timeline(client, base, org["id"], character["id"])
    removed = await client.delete(
        f"{members}/{created.json()['id']}",
        params={"edit_mode": "progression", "expected_revision": timeline["revision"], "chapter_id": chapters[20]},
    )
    assert removed.status_code == 204, removed.text
    assert (await client.get(members)).json() == []
    timeline = await _timeline(client, base, org["id"], character["id"])
    url = f"{base}/organizations/{org['id']}/membership-records/{character['id']}"
    stranger = make_client()
    await register(stranger, "stranger_deleting_history")
    assert (await stranger.delete(url, params={"expected_revision": timeline["revision"]})).status_code == 404
    stale = await client.delete(url, params={"expected_revision": timeline["revision"] + 1})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
    assert (await _effective(client, base, timeline["id"], chapters[15]))["position"] == "弟子"

    other_project = await create_project(client, title="另一个世界")
    other_base = f"/api/v1/projects/{other_project['id']}"
    other_org = (await client.post(f"{other_base}/organizations", json={"name": "玄天宗"})).json()
    other_character = (await client.post(f"{other_base}/characters", json={"name": "楚天"})).json()
    other_members = f"{other_base}/organizations/{other_org['id']}/members"
    assert (await client.post(other_members, json={"character_id": other_character["id"]})).status_code == 201
    foreign_url = f"{other_base}/organizations/{other_org['id']}/membership-records/{character['id']}"
    assert (await client.delete(foreign_url, params={"expected_revision": timeline["revision"]})).status_code == 404
    deleted = await client.delete(url, params={"expected_revision": timeline["revision"]})
    assert deleted.status_code == 204, deleted.text
    assert (await client.get(f"{base}/setting-timelines/{timeline['id']}/baseline")).status_code == 404
    assert len((await client.get(other_members)).json()) == 1
    other_timeline = await _timeline(client, other_base, other_org["id"], other_character["id"])
    assert other_timeline["baseline_status"] == "confirmed"


async def test_delete_present_member_record_by_stable_subject_also_removes_current_row(client):
    base, org, character, _ = await _setup(client)
    members = f"{base}/organizations/{org['id']}/members"
    assert (await client.post(members, json={"character_id": character["id"]})).status_code == 201
    timeline = await _timeline(client, base, org["id"], character["id"])
    url = f"{base}/organizations/{org['id']}/membership-records/{character['id']}"
    response = await client.delete(url, params={"expected_revision": timeline["revision"]})
    assert response.status_code == 204, response.text
    assert (await client.get(members)).json() == []
    assert (await client.get(f"{base}/setting-timelines/{timeline['id']}/baseline")).status_code == 404


async def test_legacy_delete_does_not_discard_even_cancelled_member_events(client):
    base, org, character, chapters = await _setup(client)
    members = f"{base}/organizations/{org['id']}/members"
    created = await client.post(members, json={"character_id": character["id"], "position": "弟子"})
    assert created.status_code == 201, created.text
    timeline = await _timeline(client, base, org["id"], character["id"])
    updated = await client.patch(
        f"{members}/{created.json()['id']}",
        json={"edit_mode": "progression", "expected_revision": timeline["revision"],
              "chapter_id": chapters[20], "position": "长老"},
    )
    assert updated.status_code == 200, updated.text
    url = f"{base}/setting-timelines/{timeline['id']}"
    events = (await client.get(f"{url}/progressions")).json()
    revoked = await client.delete(
        f"{url}/progressions/{events['items'][0]['id']}",
        params={"expected_revision": events["timeline_revision"]},
    )
    assert revoked.status_code == 200, revoked.text
    blocked = await client.delete(f"{members}/{created.json()['id']}")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "progression_intent_required"
    assert (await client.get(f"{url}/progressions/{events['items'][0]['id']}")).status_code == 200
    confirmed = await client.delete(
        f"{members}/{created.json()['id']}",
        params={"edit_mode": "delete", "expected_revision": revoked.json()["timeline_revision"]},
    )
    assert confirmed.status_code == 204, confirmed.text


async def test_reordering_chapters_cannot_move_member_change_before_join(client):
    base, org, character, chapters = await _setup(client)
    members = f"{base}/organizations/{org['id']}/members"
    created = await client.post(
        members,
        json={"character_id": character["id"], "position": "弟子", "edit_mode": "progression",
              "chapter_id": chapters[10]},
    )
    assert created.status_code == 201, created.text
    timeline = await _timeline(client, base, org["id"], character["id"])
    changed = await client.patch(
        f"{members}/{created.json()['id']}",
        json={"edit_mode": "progression", "expected_revision": timeline["revision"],
              "chapter_id": chapters[20], "position": "长老"},
    )
    assert changed.status_code == 200, changed.text
    bad_order = await client.patch(f"{base}/chapters/{chapters[20]}", json={"chapter_no": 7})
    assert bad_order.status_code == 409, bad_order.text
    assert bad_order.json()["code"] == "progression_lifecycle_conflict"
    assert (await client.get(f"{base}/chapters/{chapters[20]}")).json()["chapter_no"] == 20
    assert (await _effective(client, base, timeline["id"], chapters[15]))["position"] == "弟子"
    assert (await client.get(members)).json()[0]["position"] == "长老"

    reordered = await client.patch(f"{base}/chapters/{chapters[20]}", json={"chapter_no": 26})
    assert reordered.status_code == 200, reordered.text
    assert (await _effective(client, base, timeline["id"], chapters[25]))["position"] == "弟子"
    assert (await _effective(client, base, timeline["id"], chapters[30]))["position"] == "长老"


async def test_member_creation_revision_zero_means_no_existing_timeline(client):
    base, org, character, chapters = await _setup(client)
    members = f"{base}/organizations/{org['id']}/members"
    # 客户端读到空结果，此时另一写入者完成加入与退出，当前列表仍为空。
    before = await client.get(
        f"{base}/setting-timelines",
        params={"subject_kind": "membership", "organization_id": org["id"], "character_id": character["id"]},
    )
    assert before.json()["total"] == 0
    created = await client.post(
        members,
        json={"character_id": character["id"], "expected_revision": 0, "edit_mode": "progression",
              "chapter_id": chapters[10], "position": "弟子"},
    )
    assert created.status_code == 201, created.text
    timeline = await _timeline(client, base, org["id"], character["id"])
    duplicate = await client.post(members, json={"character_id": character["id"], "expected_revision": 0})
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "stale_revision"
    stale_duplicate = await client.post(
        members, json={"character_id": character["id"], "expected_revision": timeline["revision"] + 1}
    )
    assert stale_duplicate.status_code == 409 and stale_duplicate.json()["code"] == "stale_revision"
    removed = await client.delete(
        f"{members}/{created.json()['id']}",
        params={"edit_mode": "progression", "expected_revision": timeline["revision"], "chapter_id": chapters[20]},
    )
    assert removed.status_code == 204, removed.text
    stale = await client.post(
        members,
        json={"character_id": character["id"], "expected_revision": 0, "edit_mode": "progression",
              "chapter_id": chapters[30], "position": "掌门"},
    )
    assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
    assert (await client.get(members)).json() == []
    assert (await _effective(client, base, timeline["id"], chapters[15]))["position"] == "弟子"
    assert await _effective(client, base, timeline["id"], chapters[35]) == {"exists": False}

    second = (await client.post(f"{base}/characters", json={"name": "顾云"})).json()
    baseline = await client.post(members, json={"character_id": second["id"], "expected_revision": 0})
    assert baseline.status_code == 201, baseline.text


async def test_explicit_planned_member_anchors_survive_same_number_chapter_replacement(client):
    base, org, character, chapters = await _setup(client)
    members = f"{base}/organizations/{org['id']}/members"
    created = await client.post(
        members,
        json={"character_id": character["id"], "position": "弟子", "expected_revision": 0,
              "edit_mode": "progression", "anchor_kind": "planned", "chapter_no": 10},
    )
    assert created.status_code == 201, created.text
    timeline = await _timeline(client, base, org["id"], character["id"])
    updated = await client.patch(
        f"{members}/{created.json()['id']}",
        json={"position": "长老", "edit_mode": "progression", "expected_revision": timeline["revision"],
              "anchor_kind": "planned", "chapter_no": 20},
    )
    assert updated.status_code == 200, updated.text
    timeline = await _timeline(client, base, org["id"], character["id"])
    removed = await client.delete(
        f"{members}/{created.json()['id']}",
        params={"edit_mode": "progression", "expected_revision": timeline["revision"],
                "anchor_kind": "planned", "chapter_no": 30},
    )
    assert removed.status_code == 204, removed.text
    events_url = f"{base}/setting-timelines/{timeline['id']}/progressions"

    async def assert_planned_events():
        response = await client.get(events_url)
        assert response.status_code == 200, response.text
        events = response.json()["items"]
        assert [(e["anchor_kind"], e["chapter_id"], e["chapter_no"], e["orphaned"]) for e in events] == [
            ("planned", None, 10, False), ("planned", None, 20, False), ("planned", None, 30, False),
        ]
        assert (await _effective(client, base, timeline["id"], chapters[15]))["position"] == "弟子"
        assert (await _effective(client, base, timeline["id"], chapters[25]))["position"] == "长老"
        assert await _effective(client, base, timeline["id"], chapters[35]) == {"exists": False}
        assert (await client.get(members)).json() == []

    await assert_planned_events()
    for number in (10, 20, 30):
        deleted = await client.delete(f"{base}/chapters/{chapters[number]}")
        assert deleted.status_code == 204, deleted.text
    await assert_planned_events()
    for number in (10, 20, 30):
        recreated = await client.post(f"{base}/chapters", json={"chapter_no": number, "title": "重新创建"})
        assert recreated.status_code == 201, recreated.text
    await assert_planned_events()


async def test_member_legacy_number_anchors_still_bind_matching_chapters(client):
    base, org, character, chapters = await _setup(client)
    members = f"{base}/organizations/{org['id']}/members"
    created = await client.post(
        members,
        json={"character_id": character["id"], "position": "弟子", "chapter_no": 10},
    )
    assert created.status_code == 201, created.text
    timeline = await _timeline(client, base, org["id"], character["id"])
    changed = await client.patch(
        f"{members}/{created.json()['id']}",
        json={"position": "长老", "expected_revision": timeline["revision"], "chapter_no": 20},
    )
    assert changed.status_code == 200, changed.text
    timeline = await _timeline(client, base, org["id"], character["id"])
    removed = await client.delete(
        f"{members}/{created.json()['id']}",
        params={"expected_revision": timeline["revision"], "chapter_no": 30},
    )
    assert removed.status_code == 204, removed.text
    events = (await client.get(f"{base}/setting-timelines/{timeline['id']}/progressions")).json()["items"]
    assert [(e["anchor_kind"], e["chapter_id"], e["chapter_no"]) for e in events] == [
        ("chapter", chapters[10], None), ("chapter", chapters[20], None), ("chapter", chapters[30], None),
    ]


async def test_member_explicit_anchor_kind_validates_chapter_identity_and_position(client):
    base, org, character, chapters = await _setup(client)
    members = f"{base}/organizations/{org['id']}/members"
    for position in (
        {"anchor_kind": "chapter"},
        {"anchor_kind": "chapter", "chapter_no": 10},
        {"anchor_kind": "planned", "chapter_id": chapters[10]},
        {"anchor_kind": "planned"},
    ):
        result = await client.post(members, json={"character_id": character["id"], **position})
        assert result.status_code == 422, result.text
    other = await create_project(client, title="异世界")
    foreign = await client.post(
        f"/api/v1/projects/{other['id']}/chapters", json={"chapter_no": 10, "title": "异章"}
    )
    bad_foreign = await client.post(
        members,
        json={"character_id": character["id"], "anchor_kind": "chapter", "chapter_id": foreign.json()["id"]},
    )
    assert bad_foreign.status_code in (400, 404), bad_foreign.text
    assert (await client.get(members)).json() == []

    created = await client.post(members, json={"character_id": character["id"]})
    assert created.status_code == 201, created.text
    member_url = f"{members}/{created.json()['id']}"
    timeline = await _timeline(client, base, org["id"], character["id"])
    bad_patch = await client.patch(
        member_url,
        json={"position": "长老", "expected_revision": timeline["revision"],
              "anchor_kind": "chapter", "chapter_no": 20},
    )
    assert bad_patch.status_code == 422, bad_patch.text
    bad_delete = await client.delete(
        member_url,
        params={"expected_revision": timeline["revision"], "anchor_kind": "chapter", "chapter_no": 20},
    )
    assert bad_delete.status_code in (400, 422), bad_delete.text
    bad_foreign_delete = await client.delete(
        member_url,
        params={"expected_revision": timeline["revision"], "anchor_kind": "chapter",
                "chapter_id": foreign.json()["id"]},
    )
    assert bad_foreign_delete.status_code in (400, 404), bad_foreign_delete.text
    empty_delete = await client.delete(member_url, params={"anchor_kind": "planned"})
    assert empty_delete.status_code in (400, 422), empty_delete.text
    assert (await _timeline(client, base, org["id"], character["id"]))["revision"] == timeline["revision"]
    assert (await client.get(members)).json()[0]["position"] == ""
    valid = await client.patch(
        member_url,
        json={"position": "长老", "expected_revision": timeline["revision"],
              "anchor_kind": "chapter", "chapter_id": chapters[20]},
    )
    assert valid.status_code == 200, valid.text
    events = (await client.get(f"{base}/setting-timelines/{timeline['id']}/progressions")).json()["items"]
    assert events[0]["anchor_kind"] == "chapter" and events[0]["chapter_id"] == chapters[20]
    timeline = await _timeline(client, base, org["id"], character["id"])
    valid_delete = await client.delete(
        member_url,
        params={"expected_revision": timeline["revision"], "anchor_kind": "chapter", "chapter_id": chapters[30]},
    )
    assert valid_delete.status_code == 204, valid_delete.text
    events = (await client.get(f"{base}/setting-timelines/{timeline['id']}/progressions")).json()["items"]
    assert events[1]["anchor_kind"] == "chapter" and events[1]["chapter_id"] == chapters[30]

    second = (await client.post(f"{base}/characters", json={"name": "顾云"})).json()
    explicit = await client.post(
        members,
        json={"character_id": second["id"], "anchor_kind": "chapter", "chapter_id": chapters[10]},
    )
    assert explicit.status_code == 201, explicit.text
    second_timeline = await _timeline(client, base, org["id"], second["id"])
    events = (await client.get(f"{base}/setting-timelines/{second_timeline['id']}/progressions")).json()["items"]
    assert events[0]["anchor_kind"] == "chapter" and events[0]["chapter_id"] == chapters[10]
