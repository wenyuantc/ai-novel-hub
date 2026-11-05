"""职业剧情变化：稳定主体、历史约束与阶段引用。"""

import pytest
from sqlalchemy import func, select

from app.core.errors import ConflictError
from app.models import SettingProgression, SettingTimeline
from app.schemas.progression import ProgressionCreate, StoryPosition
from app.services.career_progression_service import validate_career_intervals
from app.services.setting_progression_service import apply_progression, resolve_timelines
from tests.helpers import create_project, register


async def _setup(client):
    await register(client)
    project = await create_project(client, title="职业时间线")
    base = f"/api/v1/projects/{project['id']}"
    response = await client.post(f"{base}/characters", json={"name": "林远"})
    assert response.status_code == 201, response.text
    return project, base, response.json()


async def _career(client, base, name, kind="main"):
    response = await client.post(
        f"{base}/careers",
        json={"name": name, "kind": kind, "stages": [{"name": "初阶"}, {"name": "中阶"}, {"name": "高阶"}]},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _timeline(app, project_id, character_id, career_id):
    async with app.state.sessionmaker() as db:
        timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project_id,
                SettingTimeline.subject_kind == "career",
                SettingTimeline.character_id == character_id,
                SettingTimeline.career_id == career_id,
            )
        )
        assert timeline is not None
        return timeline.id


async def _at(app, project_id, timeline_id, chapter_no):
    async with app.state.sessionmaker() as db:
        timeline = await db.get(SettingTimeline, timeline_id)
        result = await resolve_timelines(db, project_id, [timeline], StoryPosition(chapter_no=chapter_no))
        return result[timeline_id].fields


async def test_assignment_lifecycle_remains_queryable_after_detach_and_reattach(app, client):
    project, base, character = await _setup(client)
    career = await _career(client, base, "剑修")
    path = f"{base}/characters/{character['id']}/careers"
    first = await client.post(
        path, json={"career_id": career["id"], "stage_id": career["stages"][0]["id"], "chapter_no": 10}
    )
    assert first.status_code == 201, first.text
    timeline_id = await _timeline(app, project["id"], character["id"], career["id"])

    removed = await client.delete(f"{path}/{first.json()['id']}", params={"chapter_no": 20})
    assert removed.status_code == 204, removed.text
    assert (await client.get(path)).json() == []
    listed = await client.get(
        f"{base}/setting-timelines",
        params={"subject_kind": "career", "character_id": character["id"], "career_id": career["id"]},
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [timeline_id]
    assert (await _at(app, project["id"], timeline_id, 15))["stage_id"] == career["stages"][0]["id"]
    assert (await _at(app, project["id"], timeline_id, 25))["exists"] is False

    again = await client.post(
        path, json={"career_id": career["id"], "stage_id": career["stages"][2]["id"], "chapter_no": 30}
    )
    assert again.status_code == 201, again.text
    assert await _timeline(app, project["id"], character["id"], career["id"]) == timeline_id
    assert (await _at(app, project["id"], timeline_id, 35))["stage_id"] == career["stages"][2]["id"]
    assert (await _at(app, project["id"], timeline_id, 25))["exists"] is False


async def test_earlier_stage_change_does_not_overwrite_later_projection(app, client):
    project, base, character = await _setup(client)
    career = await _career(client, base, "剑修")
    path = f"{base}/characters/{character['id']}/careers"
    first = await client.post(path, json={"career_id": career["id"]})
    assert first.status_code == 201, first.text
    item = f"{path}/{first.json()['id']}"
    later = await client.patch(item, json={"stage_id": career["stages"][2]["id"], "chapter_no": 20})
    assert later.status_code == 200, later.text
    earlier = await client.patch(item, json={"stage_id": career["stages"][1]["id"], "chapter_no": 10})
    assert earlier.status_code == 200, earlier.text
    assert earlier.json()["stage_id"] == career["stages"][2]["id"]
    timeline_id = await _timeline(app, project["id"], character["id"], career["id"])
    assert (await _at(app, project["id"], timeline_id, 15))["stage_id"] == career["stages"][1]["id"]
    assert (await _at(app, project["id"], timeline_id, 25))["stage_id"] == career["stages"][2]["id"]


async def test_historical_stage_cannot_be_deleted_after_latest_stage_changes(app, client):
    _, base, character = await _setup(client)
    career = await _career(client, base, "剑修")
    path = f"{base}/characters/{character['id']}/careers"
    first = await client.post(path, json={"career_id": career["id"]})
    assert first.status_code == 201, first.text
    changed = await client.patch(
        f"{path}/{first.json()['id']}", json={"stage_id": career["stages"][1]["id"], "chapter_no": 20}
    )
    assert changed.status_code == 200, changed.text
    kept = [{"id": stage["id"], "name": stage["name"]} for stage in career["stages"][1:]]
    deleted = await client.patch(f"{base}/careers/{career['id']}", json={"stages": kept})
    assert deleted.status_code == 409, deleted.text
    assert "历史设定引用" in deleted.json()["detail"]
    assert len((await client.get(f"{base}/careers/{career['id']}")).json()["stages"]) == 3


async def test_inactive_historical_stage_reference_remains_protected(app, client):
    project, base, character = await _setup(client)
    career = await _career(client, base, "剑修")
    path = f"{base}/characters/{character['id']}/careers"
    assigned = await client.post(path, json={"career_id": career["id"]})
    assert assigned.status_code == 201, assigned.text
    item = f"{path}/{assigned.json()['id']}"
    assert (await client.patch(item, json={"stage_id": career["stages"][1]["id"], "chapter_no": 5})).status_code == 200
    assert (await client.patch(item, json={"stage_id": career["stages"][2]["id"], "chapter_no": 10})).status_code == 200
    timeline_id = await _timeline(app, project["id"], character["id"], career["id"])
    url = f"{base}/setting-timelines/{timeline_id}"
    listed = (await client.get(f"{url}/progressions")).json()
    event = next(item for item in listed["items"] if item["value"] == career["stages"][1]["id"])
    revoked = await client.delete(
        f"{url}/progressions/{event['id']}", params={"expected_revision": listed["timeline_revision"]}
    )
    assert revoked.status_code == 200, revoked.text
    kept = [{"id": stage["id"], "name": stage["name"]} for stage in (career["stages"][0], career["stages"][2])]
    deleted = await client.patch(f"{base}/careers/{career['id']}", json={"stages": kept})
    assert deleted.status_code == 409, deleted.text


async def test_kind_change_checks_historical_assignments_even_when_currently_detached(app, client):
    _, base, character = await _setup(client)
    primary = await _career(client, base, "剑修")
    secondary = await _career(client, base, "丹师", kind="sub")
    path = f"{base}/characters/{character['id']}/careers"
    assert (await client.post(path, json={"career_id": primary["id"]})).status_code == 201
    assigned = await client.post(path, json={"career_id": secondary["id"]})
    assert assigned.status_code == 201, assigned.text
    detached = await client.delete(f"{path}/{assigned.json()['id']}", params={"chapter_no": 10})
    assert detached.status_code == 204, detached.text
    changed = await client.patch(f"{base}/careers/{secondary['id']}", json={"kind": "main"})
    assert changed.status_code == 409, changed.text
    assert (await client.get(f"{base}/careers/{secondary['id']}")).json()["kind"] == "sub"
    assert [item["career_name"] for item in (await client.get(path)).json()] == ["剑修"]


async def test_conflicting_historical_main_careers_are_rejected_atomically(app, client):
    _, base, character = await _setup(client)
    first_career = await _career(client, base, "剑修")
    second_career = await _career(client, base, "法修")
    path = f"{base}/characters/{character['id']}/careers"
    first = await client.post(path, json={"career_id": first_career["id"]})
    assert first.status_code == 201, first.text
    removed = await client.delete(f"{path}/{first.json()['id']}", params={"chapter_no": 20})
    assert removed.status_code == 204, removed.text
    overlap = await client.post(path, json={"career_id": second_career["id"], "chapter_no": 10})
    assert overlap.status_code == 409, overlap.text
    assert overlap.json().get("code") != "career_history_requires_timeline"
    assert (await client.get(path)).json() == []


async def test_past_assignment_without_latest_projection_requires_timeline_and_rolls_back(app, client):
    project, base, character = await _setup(client)
    career = await _career(client, base, "剑修")
    path = f"{base}/characters/{character['id']}/careers"
    assigned = await client.post(path, json={"career_id": career["id"], "chapter_no": 10})
    assert assigned.status_code == 201, assigned.text
    timeline_id = await _timeline(app, project["id"], character["id"], career["id"])
    assert (await client.delete(f"{path}/{assigned.json()['id']}", params={"chapter_no": 20})).status_code == 204
    url = f"{base}/setting-timelines/{timeline_id}"
    before = (await client.get(f"{url}/progressions")).json()
    audit_url = f"{base}/characters/{character['id']}/career-history"
    before_audit = (await client.get(audit_url)).json()

    failed = await client.post(
        path,
        json={
            "career_id": career["id"],
            "stage_id": career["stages"][1]["id"],
            "edit_mode": "progression",
            "anchor_kind": "planned",
            "chapter_no": 5,
            "expected_revision": before["timeline_revision"],
        },
    )
    assert failed.status_code == 409, failed.text
    assert failed.json()["code"] == "career_history_requires_timeline"
    assert "职业时间线" in failed.json()["detail"]
    assert (await client.get(f"{url}/progressions")).json() == before
    assert (await client.get(audit_url)).json() == before_audit
    assert (await _at(app, project["id"], timeline_id, 5))["exists"] is False
    assert (await client.get(path)).json() == []

    # 时间线面板所用的通用事件接口可补录该记录，而不产生最新挂接。
    saved = await client.post(
        f"{url}/progressions",
        json={
            "expected_revision": before["timeline_revision"],
            "field_key": "presence",
            "operation": "replace",
            "value": {"exists": True, "state": {"role": "main", "stage_id": career["stages"][1]["id"], "note": ""}},
            "anchor_kind": "planned",
            "chapter_no": 5,
            "boundary": "start",
        },
    )
    assert saved.status_code == 201, saved.text
    assert (await _at(app, project["id"], timeline_id, 5))["stage_id"] == career["stages"][1]["id"]
    assert (await _at(app, project["id"], timeline_id, 25))["exists"] is False
    assert (await client.get(path)).json() == []


async def test_automatic_stage_before_manual_creation_at_same_boundary_rolls_back(app, client):
    project, base, character = await _setup(client)
    career = await _career(client, base, "剑修")
    path = f"{base}/characters/{character['id']}/careers"
    created = await client.post(path, json={"career_id": career["id"], "chapter_no": 10})
    assert created.status_code == 201, created.text
    timeline_id = await _timeline(app, project["id"], character["id"], career["id"])

    async with app.state.sessionmaker() as db:
        timeline = await db.get(SettingTimeline, timeline_id)
        previous_revision = timeline.revision
        with pytest.raises(ConflictError, match="职业变化在该章节无法回溯"):
            async with db.begin_nested():
                await apply_progression(
                    db,
                    timeline,
                    ProgressionCreate(
                        expected_revision=previous_revision,
                        field_key="stage_id",
                        operation="replace",
                        value=career["stages"][1]["id"],
                        anchor_kind="planned",
                        chapter_no=10,
                    ),
                    source="analysis",
                    protection="automatic",
                    materialize=False,
                )
                await validate_career_intervals(db, project["id"], [character["id"]])
        await db.refresh(timeline)
        assert timeline.revision == previous_revision
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SettingProgression)
                .where(SettingProgression.timeline_id == timeline_id)
            )
            == 1
        )


async def test_chapter_anchor_ambiguous_change_and_explicit_baseline(app, client):
    project, base, character = await _setup(client)
    career = await _career(client, base, "剑修")
    chapter = await client.post(f"{base}/chapters", json={"chapter_no": 10, "title": "初遇"})
    assert chapter.status_code == 201, chapter.text
    path = f"{base}/characters/{character['id']}/careers"
    assigned = await client.post(path, json={"career_id": career["id"]})
    assert assigned.status_code == 201, assigned.text
    item = f"{path}/{assigned.json()['id']}"
    changed = await client.patch(
        item, json={"stage_id": career["stages"][2]["id"], "chapter_id": chapter.json()["id"], "boundary": "end"}
    )
    assert changed.status_code == 200, changed.text
    timeline_id = await _timeline(app, project["id"], character["id"], career["id"])
    assert (await _at(app, project["id"], timeline_id, 10))["stage_id"] == career["stages"][0]["id"]
    assert (await _at(app, project["id"], timeline_id, 11))["stage_id"] == career["stages"][2]["id"]
    ambiguous = await client.patch(item, json={"stage_id": career["stages"][1]["id"]})
    assert ambiguous.status_code == 409
    assert ambiguous.json()["code"] == "progression_intent_required"
    assert (await _at(app, project["id"], timeline_id, 5))["stage_id"] == career["stages"][0]["id"]
    revision = (await client.get(f"{base}/setting-timelines/{timeline_id}/baseline")).json()["timeline_revision"]
    baseline = await client.patch(
        item, json={"stage_id": career["stages"][1]["id"], "edit_mode": "baseline", "expected_revision": revision}
    )
    assert baseline.status_code == 200, baseline.text
    assert (await _at(app, project["id"], timeline_id, 5))["stage_id"] == career["stages"][1]["id"]
    assert (await _at(app, project["id"], timeline_id, 11))["stage_id"] == career["stages"][2]["id"]


async def test_explicit_delete_discards_timeline_after_story_changes(app, client):
    project, base, character = await _setup(client)
    career = await _career(client, base, "剑修")
    path = f"{base}/characters/{character['id']}/careers"
    assigned = await client.post(path, json={"career_id": career["id"], "chapter_no": 10})
    assert assigned.status_code == 201, assigned.text
    timeline_id = await _timeline(app, project["id"], character["id"], career["id"])
    item = f"{path}/{assigned.json()['id']}"
    assert (await client.delete(item)).status_code == 409
    revision = (await client.get(f"{base}/setting-timelines/{timeline_id}/baseline")).json()["timeline_revision"]
    removed = await client.delete(item, params={"edit_mode": "delete", "expected_revision": revision})
    assert removed.status_code == 204, removed.text
    async with app.state.sessionmaker() as db:
        assert await db.get(SettingTimeline, timeline_id) is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SettingProgression)
                .where(SettingProgression.timeline_id == timeline_id)
            )
            == 0
        )


async def test_historical_secondary_limit_includes_previously_detached_subjects(app, client):
    _, base, character = await _setup(client)
    careers = [await _career(client, base, f"副修{index}", kind="sub") for index in range(4)]
    path = f"{base}/characters/{character['id']}/careers"
    assignments = []
    for career in careers[:3]:
        response = await client.post(path, json={"career_id": career["id"]})
        assert response.status_code == 201, response.text
        assignments.append(response.json())
    assert (await client.delete(f"{path}/{assignments[0]['id']}", params={"chapter_no": 20})).status_code == 204
    overlap = await client.post(path, json={"career_id": careers[3]["id"], "chapter_no": 10})
    assert overlap.status_code == 409, overlap.text
    assert len((await client.get(path)).json()) == 2


async def test_kind_change_updates_existing_and_detached_history(app, client):
    project, base, character = await _setup(client)
    career = await _career(client, base, "丹师", kind="sub")
    path = f"{base}/characters/{character['id']}/careers"
    assigned = await client.post(path, json={"career_id": career["id"], "chapter_no": 10})
    assert assigned.status_code == 201, assigned.text
    timeline_id = await _timeline(app, project["id"], character["id"], career["id"])
    assert (await client.delete(f"{path}/{assigned.json()['id']}", params={"chapter_no": 20})).status_code == 204
    changed = await client.patch(f"{base}/careers/{career['id']}", json={"kind": "main"})
    assert changed.status_code == 200, changed.text
    assert (await _at(app, project["id"], timeline_id, 15))["role"] == "main"
    assert (await _at(app, project["id"], timeline_id, 25))["exists"] is False
    reattached = await client.post(path, json={"career_id": career["id"], "chapter_no": 30})
    assert reattached.status_code == 201, reattached.text
    assert reattached.json()["role"] == "main"


async def test_generic_event_write_validates_all_career_intervals_atomically(app, client):
    project, base, character = await _setup(client)
    career = await _career(client, base, "剑修")
    path = f"{base}/characters/{character['id']}/careers"
    assigned = await client.post(path, json={"career_id": career["id"], "chapter_no": 10})
    assert assigned.status_code == 201, assigned.text
    timeline_id = await _timeline(app, project["id"], character["id"], career["id"])
    timeline_url = f"{base}/setting-timelines/{timeline_id}"
    before = (await client.get(f"{timeline_url}/baseline")).json()
    invalid = await client.post(
        f"{timeline_url}/progressions",
        json={
            "expected_revision": before["timeline_revision"],
            "field_key": "stage_id",
            "operation": "replace",
            "value": career["stages"][1]["id"],
            "anchor_kind": "planned",
            "chapter_no": 5,
            "boundary": "start",
        },
    )
    assert invalid.status_code == 409, invalid.text
    after = (await client.get(f"{timeline_url}/baseline")).json()
    assert after["timeline_revision"] == before["timeline_revision"]
    assert (await _at(app, project["id"], timeline_id, 5))["exists"] is False
    assert (await client.get(path)).json()[0]["stage_id"] == career["stages"][0]["id"]


async def test_unchanged_assignment_still_checks_expected_revision(app, client):
    project, base, character = await _setup(client)
    career = await _career(client, base, "剑修")
    path = f"{base}/characters/{character['id']}/careers"
    assigned = await client.post(path, json={"career_id": career["id"]})
    assert assigned.status_code == 201, assigned.text
    timeline_id = await _timeline(app, project["id"], character["id"], career["id"])
    item = f"{path}/{assigned.json()['id']}"
    changed = await client.patch(item, json={"stage_id": career["stages"][1]["id"], "chapter_no": 10})
    assert changed.status_code == 200, changed.text
    before = (await client.get(f"{base}/setting-timelines/{timeline_id}/baseline")).json()
    stale = await client.patch(
        item, json={"stage_id": career["stages"][1]["id"], "expected_revision": before["timeline_revision"] - 1}
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_revision"
    assert (await _at(app, project["id"], timeline_id, 5))["stage_id"] == career["stages"][0]["id"]


async def test_chapter_reordering_cannot_create_historical_overlap(app, client):
    project, base, character = await _setup(client)
    first_career = await _career(client, base, "剑修")
    second_career = await _career(client, base, "法修")
    chapters = []
    for number in (20, 30):
        response = await client.post(f"{base}/chapters", json={"chapter_no": number, "title": f"第{number}章"})
        assert response.status_code == 201, response.text
        chapters.append(response.json())
    path = f"{base}/characters/{character['id']}/careers"
    first = await client.post(path, json={"career_id": first_career["id"]})
    assert first.status_code == 201, first.text
    detached = await client.delete(f"{path}/{first.json()['id']}", params={"chapter_id": chapters[0]["id"]})
    assert detached.status_code == 204, detached.text
    second = await client.post(path, json={"career_id": second_career["id"], "chapter_id": chapters[1]["id"]})
    assert second.status_code == 201, second.text
    second_timeline = await _timeline(app, project["id"], character["id"], second_career["id"])
    reordered = await client.patch(f"{base}/chapters/{chapters[1]['id']}", json={"chapter_no": 10})
    assert reordered.status_code == 409, reordered.text
    assert (await client.get(f"{base}/chapters/{chapters[1]['id']}")).json()["chapter_no"] == 30
    assert (await _at(app, project["id"], second_timeline, 15))["exists"] is False
    assert (await client.get(path)).json()[0]["career_id"] == second_career["id"]


async def test_generic_event_rejects_stage_from_another_career(app, client):
    project, base, character = await _setup(client)
    sword = await _career(client, base, "剑修")
    alchemist = await _career(client, base, "丹师", kind="sub")
    path = f"{base}/characters/{character['id']}/careers"
    assigned = await client.post(path, json={"career_id": sword["id"]})
    assert assigned.status_code == 201, assigned.text
    timeline_id = await _timeline(app, project["id"], character["id"], sword["id"])
    url = f"{base}/setting-timelines/{timeline_id}"
    before = (await client.get(f"{url}/baseline")).json()
    invalid = await client.post(
        f"{url}/progressions",
        json={
            "expected_revision": before["timeline_revision"],
            "field_key": "stage_id",
            "operation": "replace",
            "value": alchemist["stages"][0]["id"],
            "anchor_kind": "planned",
            "chapter_no": 10,
        },
    )
    assert invalid.status_code == 400, invalid.text
    assert (await client.get(f"{url}/baseline")).json()["timeline_revision"] == before["timeline_revision"]
    assert (await _at(app, project["id"], timeline_id, 15))["stage_id"] == sword["stages"][0]["id"]


async def test_create_precondition_requires_absent_timeline_and_explicit_modes_require_revision(app, client):
    project, base, character = await _setup(client)
    career = await _career(client, base, "剑修")
    path = f"{base}/characters/{character['id']}/careers"
    missing = await client.post(path, json={"career_id": career["id"], "edit_mode": "baseline"})
    assert missing.status_code == 400, missing.text
    assigned = await client.post(
        path, json={"career_id": career["id"], "edit_mode": "baseline", "expected_revision": 0}
    )
    assert assigned.status_code == 201, assigned.text
    timeline_id = await _timeline(app, project["id"], character["id"], career["id"])
    stale_live = await client.post(
        path, json={"career_id": career["id"], "edit_mode": "baseline", "expected_revision": 0}
    )
    assert stale_live.status_code == 409
    assert stale_live.json()["code"] == "stale_revision"
    assert (await client.delete(f"{path}/{assigned.json()['id']}", params={"chapter_no": 20})).status_code == 204
    stale = await client.post(
        path, json={"career_id": career["id"], "edit_mode": "progression", "chapter_no": 30, "expected_revision": 0}
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "stale_revision"
    assert (await client.get(path)).json() == []
    baseline = (await client.get(f"{base}/setting-timelines/{timeline_id}/baseline")).json()
    recreated = await client.post(
        path,
        json={
            "career_id": career["id"],
            "edit_mode": "progression",
            "chapter_no": 30,
            "expected_revision": baseline["timeline_revision"],
        },
    )
    assert recreated.status_code == 201, recreated.text
    without_version = await client.delete(f"{path}/{recreated.json()['id']}", params={"edit_mode": "delete"})
    assert without_version.status_code == 400


async def test_existing_revision_never_recreates_a_deleted_timeline(app, client):
    project, base, character = await _setup(client)
    career = await _career(client, base, "剑修")
    path = f"{base}/characters/{character['id']}/careers"
    assigned = await client.post(path, json={"career_id": career["id"]})
    assert assigned.status_code == 201, assigned.text
    timeline_id = await _timeline(app, project["id"], character["id"], career["id"])
    async with app.state.sessionmaker() as db:
        timeline = await db.get(SettingTimeline, timeline_id)
        await db.delete(timeline)
        await db.commit()
    stale = await client.patch(
        f"{path}/{assigned.json()['id']}", json={"note": "误写", "edit_mode": "baseline", "expected_revision": 1}
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "stale_revision"
    listed = await client.get(
        f"{base}/setting-timelines",
        params={"subject_kind": "career", "character_id": character["id"], "career_id": career["id"]},
    )
    assert listed.json()["items"] == []
    assert (await client.get(path)).json()[0]["note"] == ""


async def test_explicit_planned_anchor_does_not_bind_existing_chapter(app, client):
    project, base, character = await _setup(client)
    career = await _career(client, base, "剑修")
    chapter = await client.post(f"{base}/chapters", json={"chapter_no": 10, "title": "第十章"})
    assert chapter.status_code == 201, chapter.text
    path = f"{base}/characters/{character['id']}/careers"
    assigned = await client.post(path, json={"career_id": career["id"]})
    assert assigned.status_code == 201, assigned.text
    timeline_id = await _timeline(app, project["id"], character["id"], career["id"])
    revision = (await client.get(f"{base}/setting-timelines/{timeline_id}/baseline")).json()["timeline_revision"]
    changed = await client.patch(
        f"{path}/{assigned.json()['id']}",
        json={
            "stage_id": career["stages"][1]["id"],
            "edit_mode": "progression",
            "expected_revision": revision,
            "anchor_kind": "planned",
            "chapter_no": 10,
        },
    )
    assert changed.status_code == 200, changed.text
    events = (await client.get(f"{base}/setting-timelines/{timeline_id}/progressions")).json()["items"]
    assert events[0]["anchor_kind"] == "planned"
    assert events[0]["chapter_no"] == 10 and events[0]["chapter_id"] is None
    assert (await client.delete(f"{base}/chapters/{chapter.json()['id']}")).status_code == 204
    assert (await _at(app, project["id"], timeline_id, 15))["stage_id"] == career["stages"][1]["id"]
