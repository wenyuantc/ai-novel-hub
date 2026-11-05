"""人物关系的剧情位置、当前可见性和历史载体回归。"""

import pytest
from sqlalchemy import event, func, select

from app.core.errors import BadRequestError, ConflictError
from app.models import Relationship, RelationshipHistory, SettingProgression, SettingTimeline
from app.schemas.progression import ProgressionCreate, StoryPosition
from app.services.relationship_progression_service import (
    get_or_create_relationship_timeline,
    materialize_relationships,
    validate_relationship_references,
)
from app.services.relationship_service import relations_among
from app.services.setting_progression_resolver import EffectiveSnapshot
from app.services.setting_progression_service import apply_progression, resolve_timelines
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role


async def _setup(client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    first = await client.post(f"{base}/characters", json={"name": "甲"})
    second = await client.post(f"{base}/characters", json={"name": "乙"})
    assert first.status_code == second.status_code == 201
    rows = (await client.get(f"{base}/relationship-types")).json()
    friend = next(row["id"] for row in rows if row["role_a"] == row["role_b"] == "朋友")
    enemy = next(row["id"] for row in rows if row["role_a"] == row["role_b"] == "仇人")
    return project["id"], base, first.json()["id"], second.json()["id"], friend, enemy


async def _snapshot(app, project_id, a_id, b_id, chapter_no):
    async with app.state.sessionmaker() as db:
        timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project_id,
                SettingTimeline.subject_kind == "relationship",
                SettingTimeline.character_id == min(a_id, b_id),
                SettingTimeline.related_character_id == max(a_id, b_id),
            )
        )
        assert timeline is not None
        result = await resolve_timelines(db, project_id, [timeline], StoryPosition(chapter_no=chapter_no))
        return result[timeline.id].fields


async def test_relationship_creation_at_a_future_chapter_is_absent_earlier(app, client):
    project_id, base, a_id, b_id, friend, _ = await _setup(client)
    response = await client.post(
        f"{base}/relationships",
        json={"a_id": a_id, "b_id": b_id, "type_id": friend, "chapter_no": 10, "intimacy": 25},
    )
    assert response.status_code == 201, response.text
    before = await _snapshot(app, project_id, a_id, b_id, 5)
    after = await _snapshot(app, project_id, a_id, b_id, 10)
    assert before == {"exists": False}
    assert after["exists"] is True
    assert after["intimacy"] == 25
    assert after["identity"] == {"a_id": a_id, "b_id": b_id, "type_id": friend}


async def test_explicit_planned_relationship_anchors_never_auto_bind_to_same_number_chapter(app, client):
    project_id, base, a_id, b_id, friend, _ = await _setup(client)
    chapter = (await client.post(f"{base}/chapters", json={"chapter_no": 10, "title": "相遇"})).json()
    created = await client.post(
        f"{base}/relationships",
        json={"a_id": a_id, "b_id": b_id, "type_id": friend, "anchor_kind": "planned", "chapter_no": 10},
    )
    assert created.status_code == 201, created.text
    rel_id = created.json()["id"]
    updated = await client.patch(
        f"{base}/relationships/{rel_id}",
        json={"description": "先相识", "anchor_kind": "planned", "chapter_no": 10},
    )
    assert updated.status_code == 200, updated.text
    legacy = await client.patch(f"{base}/relationships/{rel_id}", json={"intimacy": 30, "chapter_no": 10})
    assert legacy.status_code == 200, legacy.text
    timeline_response = await client.get(
        f"{base}/setting-timelines",
        params={
            "subject_kind": "relationship",
            "character_id": min(a_id, b_id),
            "related_character_id": max(a_id, b_id),
        },
    )
    timeline = timeline_response.json()["items"][0]
    url = f"{base}/setting-timelines/{timeline['id']}"
    items = (await client.get(f"{url}/progressions")).json()["items"]
    assert {item["field_key"] for item in items if item["anchor_kind"] == "planned"} == {"presence", "description"}
    assert all(item["chapter_id"] is None for item in items if item["anchor_kind"] == "planned")
    bound = next(item for item in items if item["field_key"] == "intimacy")
    assert bound["anchor_kind"] == "chapter" and bound["chapter_id"] == chapter["id"]
    deleted = await client.delete(f"{base}/chapters/{chapter['id']}")
    assert deleted.status_code == 204, deleted.text
    items = (await client.get(f"{url}/progressions")).json()["items"]
    assert {item["field_key"] for item in items if item["anchor_kind"] == "planned"} == {"presence", "description"}
    assert next(item for item in items if item["field_key"] == "intimacy")["orphaned"] is True
    assert (await _snapshot(app, project_id, a_id, b_id, 10))["description"] == "先相识"


async def test_explicit_relationship_anchor_requires_matching_position(client):
    _, base, a_id, b_id, friend, _ = await _setup(client)
    chapter = (await client.post(f"{base}/chapters", json={"chapter_no": 10, "title": "相遇"})).json()
    for anchor in (
        {"anchor_kind": "planned", "chapter_id": chapter["id"]},
        {"anchor_kind": "chapter", "chapter_no": 10},
        {"anchor_kind": "planned"},
    ):
        response = await client.post(
            f"{base}/relationships", json={"a_id": a_id, "b_id": b_id, "type_id": friend, **anchor}
        )
        assert response.status_code == 400, response.text
    assert (await client.get(f"{base}/relationships")).json() == []
    valid = await client.post(
        f"{base}/relationships",
        json={"a_id": a_id, "b_id": b_id, "type_id": friend, "anchor_kind": "chapter", "chapter_id": chapter["id"]},
    )
    assert valid.status_code == 201, valid.text


@pytest.mark.parametrize("chapter_no", [None, 10])
async def test_manual_creation_updates_preexisting_empty_timeline_and_checks_revision(app, client, chapter_no):
    project_id, base, a_id, b_id, friend, _ = await _setup(client)
    async with app.state.sessionmaker() as db:
        timeline = await get_or_create_relationship_timeline(db, project_id, a_id, b_id)
        timeline_id, revision = timeline.id, timeline.revision
        await db.commit()
    values = {"a_id": a_id, "b_id": b_id, "type_id": friend, "chapter_no": chapter_no}
    assumed_absent = await client.post(f"{base}/relationships", json={**values, "expected_revision": 0})
    assert assumed_absent.status_code == 409 and assumed_absent.json()["code"] == "stale_revision"
    stale = await client.post(f"{base}/relationships", json={**values, "expected_revision": revision + 1})
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "stale_revision"
    assert (await client.get(f"{base}/relationships")).json() == []

    created = await client.post(f"{base}/relationships", json={**values, "expected_revision": revision})
    assert created.status_code == 201, created.text
    baseline = (await client.get(f"{base}/setting-timelines/{timeline_id}/baseline")).json()
    assert baseline["timeline_revision"] > revision
    if chapter_no is None:
        assert baseline["baseline"]["identity"] == {"a_id": a_id, "b_id": b_id, "type_id": friend}
        assert baseline["baseline"]["exists"] is True
    else:
        assert baseline["baseline"] == {"exists": False}
        assert (await _snapshot(app, project_id, a_id, b_id, chapter_no))["exists"] is True


async def test_first_relationship_creation_checks_absent_subject_intent(app, client):
    project_id, base, a_id, b_id, friend, _ = await _setup(client)
    values = {"a_id": a_id, "b_id": b_id, "type_id": friend, "expected_revision": 0}
    first = await client.post(f"{base}/relationships", json=values)
    assert first.status_code == 201, first.text
    stale = await client.post(f"{base}/relationships", json=values)
    assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Relationship.id))) == 1
        assert (
            await db.scalar(
                select(func.count(SettingTimeline.id)).where(
                    SettingTimeline.project_id == project_id, SettingTimeline.subject_kind == "relationship"
                )
            )
            == 1
        )


async def test_historical_relationship_updates_preserve_later_values(app, client):
    project_id, base, a_id, b_id, friend, enemy = await _setup(client)
    response = await client.post(f"{base}/relationships", json={"a_id": a_id, "b_id": b_id, "type_id": friend})
    assert response.status_code == 201, response.text
    rel_id = response.json()["id"]

    late = await client.patch(
        f"{base}/relationships/{rel_id}",
        json={"chapter_no": 20, "a_id": b_id, "type_id": enemy, "intimacy": 90},
    )
    assert late.status_code == 200, late.text
    early = await client.patch(f"{base}/relationships/{rel_id}", json={"chapter_no": 5, "intimacy": 15})
    assert early.status_code == 200, early.text
    assert early.json()["intimacy"] == 90
    fifth = await _snapshot(app, project_id, a_id, b_id, 5)
    twentieth = await _snapshot(app, project_id, a_id, b_id, 20)
    assert fifth["intimacy"] == 15
    assert fifth["identity"] == {"a_id": a_id, "b_id": b_id, "type_id": friend}
    assert twentieth["intimacy"] == 90
    assert twentieth["identity"] == {"a_id": b_id, "b_id": a_id, "type_id": enemy}

    # 模糊的旧编辑不能把最新值回写成初始状态；原值重发则是无操作。
    same = await client.patch(f"{base}/relationships/{rel_id}", json={"intimacy": 90})
    assert same.status_code == 200, same.text
    conflict = await client.patch(f"{base}/relationships/{rel_id}", json={"intimacy": 95})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "progression_intent_required"
    assert (await _snapshot(app, project_id, a_id, b_id, 5))["intimacy"] == 15


async def test_explicit_relationship_edit_intent_requires_and_checks_revision(app, client):
    project_id, base, a_id, b_id, friend, _ = await _setup(client)
    created = await client.post(f"{base}/relationships", json={"a_id": a_id, "b_id": b_id, "type_id": friend})
    assert created.status_code == 201, created.text
    rel_id = created.json()["id"]
    timeline_list = await client.get(
        f"{base}/setting-timelines",
        params={
            "subject_kind": "relationship",
            "character_id": min(a_id, b_id),
            "related_character_id": max(a_id, b_id),
        },
    )
    revision = timeline_list.json()["items"][0]["revision"]
    for values in (
        {"edit_mode": "baseline", "intimacy": 10},
        {"edit_mode": "progression", "intimacy": 10, "chapter_no": 10},
    ):
        rejected = await client.patch(f"{base}/relationships/{rel_id}", json=values)
        assert rejected.status_code == 400, rejected.text
    stale = await client.patch(
        f"{base}/relationships/{rel_id}",
        json={"edit_mode": "baseline", "intimacy": 10, "expected_revision": revision + 1},
    )
    assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
    assert (await _snapshot(app, project_id, a_id, b_id, 10))["intimacy"] == 0
    saved = await client.patch(
        f"{base}/relationships/{rel_id}",
        json={"edit_mode": "progression", "intimacy": 10, "chapter_no": 10, "expected_revision": revision},
    )
    assert saved.status_code == 200, saved.text
    assert (await _snapshot(app, project_id, a_id, b_id, 10))["intimacy"] == 10


async def test_hidden_relationship_is_excluded_from_current_views_but_history_and_recreation_survive(app, client):
    project_id, base, a_id, b_id, friend, _ = await _setup(client)
    response = await client.post(
        f"{base}/relationships", json={"a_id": a_id, "b_id": b_id, "type_id": friend, "chapter_no": 10}
    )
    assert response.status_code == 201, response.text
    rel_id = response.json()["id"]
    timelines = await client.get(
        f"{base}/setting-timelines",
        params={
            "subject_kind": "relationship",
            "character_id": min(a_id, b_id),
            "related_character_id": max(a_id, b_id),
        },
    )
    timeline = timelines.json()["items"][0]
    timeline_url = f"{base}/setting-timelines/{timeline['id']}"
    changes = (await client.get(f"{timeline_url}/progressions")).json()
    assert len(changes["items"]) == 1 and changes["items"][0]["field_key"] == "presence"
    removed = await client.delete(
        f"{timeline_url}/progressions/{changes['items'][0]['id']}",
        params={"expected_revision": changes["timeline_revision"]},
    )
    assert removed.status_code == 200, removed.text

    assert (await client.get(f"{base}/relationships")).json() == []
    assert (await client.get(f"{base}/relationship-graph")).json()["edges"] == []
    assert len((await client.get(f"{base}/relationships/{rel_id}/history")).json()) == 1

    key = await client.post(
        "/api/v1/api-keys", json={"name": "测试", "all_projects": False, "project_ids": [project_id]}
    )
    assert key.status_code == 201, key.text
    headers = {"X-API-Key": key.json()["key"]}
    open_base = f"/api/open/v1/novels/{project_id}"
    page = await client.get(f"{open_base}/relationships", headers=headers)
    assert page.status_code == 200, page.text
    assert page.json()["items"] == [] and page.json()["total"] == 0
    assert (await client.get(f"{open_base}/relationships/{rel_id}", headers=headers)).status_code == 404
    async with app.state.sessionmaker() as db:
        assert await relations_among(db, [a_id, b_id]) == []
        assert not (await db.get(Relationship, rel_id)).is_present
        assert (
            await db.scalar(select(func.count(SettingProgression.id)).where(SettingProgression.active.is_(False))) == 1
        )

    recreated = await client.post(f"{base}/relationships", json={"a_id": a_id, "b_id": b_id, "type_id": friend})
    assert recreated.status_code == 201, recreated.text
    assert recreated.json()["id"] == rel_id
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Relationship.id))) == 1
        assert await db.scalar(select(func.count(RelationshipHistory.id))) >= 1


async def test_hidden_relationship_with_active_history_is_recreated_only_at_explicit_chapter(app, client):
    project_id, base, a_id, b_id, friend, enemy = await _setup(client)
    created = await client.post(
        f"{base}/relationships", json={"a_id": a_id, "b_id": b_id, "type_id": friend, "chapter_no": 10}
    )
    assert created.status_code == 201, created.text
    rel_id = created.json()["id"]
    timeline_response = await client.get(
        f"{base}/setting-timelines",
        params={
            "subject_kind": "relationship",
            "character_id": min(a_id, b_id),
            "related_character_id": max(a_id, b_id),
        },
    )
    timeline = timeline_response.json()["items"][0]
    url = f"{base}/setting-timelines/{timeline['id']}"
    ended = await client.post(
        f"{url}/progressions",
        json={
            "expected_revision": timeline["revision"],
            "field_key": "presence",
            "operation": "replace",
            "value": {"exists": False},
            "anchor_kind": "planned",
            "chapter_no": 20,
        },
    )
    assert ended.status_code == 201, ended.text
    assert (await client.get(f"{base}/relationships")).json() == []
    unspecified = await client.post(f"{base}/relationships", json={"a_id": a_id, "b_id": b_id, "type_id": enemy})
    assert unspecified.status_code == 409
    stale = await client.post(
        f"{base}/relationships",
        json={
            "a_id": b_id,
            "b_id": a_id,
            "type_id": enemy,
            "chapter_no": 25,
            "expected_revision": timeline["revision"],
        },
    )
    assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
    assert (await client.get(f"{base}/relationships")).json() == []
    recreated = await client.post(
        f"{base}/relationships",
        json={
            "a_id": b_id,
            "b_id": a_id,
            "type_id": enemy,
            "chapter_no": 25,
            "expected_revision": ended.json()["timeline_revision"],
        },
    )
    assert recreated.status_code == 201, recreated.text
    assert recreated.json()["id"] == rel_id
    assert (await _snapshot(app, project_id, a_id, b_id, 20)) == {"exists": False}
    assert (await _snapshot(app, project_id, a_id, b_id, 25))["identity"] == {
        "a_id": b_id,
        "b_id": a_id,
        "type_id": enemy,
    }
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Relationship.id))) == 1
        assert (
            await db.scalar(select(func.count(SettingProgression.id)).where(SettingProgression.active.is_(True))) == 3
        )


async def test_pair_history_is_readable_for_hidden_relationship_and_authorized_managers(
    client, make_client, system_admin
):
    _, base, a_id, b_id, friend, _ = await _setup(client)
    third = (await client.post(f"{base}/characters", json={"name": "丙"})).json()["id"]
    created = await client.post(
        f"{base}/relationships", json={"a_id": a_id, "b_id": b_id, "type_id": friend, "chapter_no": 10}
    )
    assert created.status_code == 201, created.text
    rel_id = created.json()["id"]
    timelines = await client.get(
        f"{base}/setting-timelines",
        params={
            "subject_kind": "relationship",
            "character_id": min(a_id, b_id),
            "related_character_id": max(a_id, b_id),
        },
    )
    timeline = timelines.json()["items"][0]
    url = f"{base}/setting-timelines/{timeline['id']}"
    changes = (await client.get(f"{url}/progressions")).json()
    removed = await client.delete(
        f"{url}/progressions/{changes['items'][0]['id']}",
        params={"expected_revision": changes["timeline_revision"]},
    )
    assert removed.status_code == 200, removed.text
    assert (await client.get(f"{base}/relationships")).json() == []

    query = {"character_id": b_id, "related_character_id": a_id}
    history_url = f"{base}/relationship-history"
    owner_result = await client.get(history_url, params=query)
    assert owner_result.status_code == 200, owner_result.text
    assert owner_result.json() == (await client.get(f"{base}/relationships/{rel_id}/history")).json()
    assert len(owner_result.json()) == 1 and owner_result.json()[0]["relationship_id"] == rel_id
    assert (await client.get(history_url, params={"character_id": a_id, "related_character_id": third})).json() == []
    assert (
        await client.get(history_url, params={"character_id": a_id, "related_character_id": a_id})
    ).status_code == 400

    viewer = make_client()
    viewer_user = await register(viewer, "history_reader")
    role = await create_role(system_admin, "关系历史只读", ["projects.read"])
    await assign_roles(system_admin, viewer_user["id"], [role["id"]])
    assert (await viewer.get(history_url, params=query)).json() == owner_result.json()
    assert (
        await viewer.post(f"{base}/relationships", json={"a_id": a_id, "b_id": third, "type_id": friend})
    ).status_code == 403

    outsider = make_client()
    await register(outsider, "history_outsider")
    foreign_project = await create_project(outsider)
    foreign = (
        await outsider.post(f"/api/v1/projects/{foreign_project['id']}/characters", json={"name": "异书"})
    ).json()
    assert (await outsider.get(history_url, params=query)).status_code == 404
    assert (await make_client().get(history_url, params=query)).status_code == 401
    assert (
        await viewer.get(history_url, params={"character_id": a_id, "related_character_id": foreign["id"]})
    ).status_code == 404


async def test_relationship_patch_rejects_another_character_without_writing_events(app, client):
    project_id, base, a_id, b_id, friend, _ = await _setup(client)
    third = (await client.post(f"{base}/characters", json={"name": "丙"})).json()["id"]
    created = await client.post(f"{base}/relationships", json={"a_id": a_id, "b_id": b_id, "type_id": friend})
    rel_id = created.json()["id"]
    bad = await client.patch(f"{base}/relationships/{rel_id}", json={"a_id": third, "chapter_no": 5})
    assert bad.status_code == 400
    async with app.state.sessionmaker() as db:
        timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project_id,
                SettingTimeline.subject_kind == "relationship",
                SettingTimeline.character_id == min(a_id, b_id),
                SettingTimeline.related_character_id == max(a_id, b_id),
            )
        )
        assert timeline is not None
        assert (
            await db.scalar(
                select(func.count(SettingProgression.id)).where(SettingProgression.timeline_id == timeline.id)
            )
            == 0
        )


async def test_historical_type_reference_cannot_point_to_another_project_even_if_later_masked(app, client):
    project_id, base, a_id, b_id, friend, enemy = await _setup(client)
    foreign_project = await create_project(client, title="另一作品")
    foreign_type = await client.post(
        f"/api/v1/projects/{foreign_project['id']}/relationship-types",
        json={"category": "social", "role_a": "盟友甲", "role_b": "盟友乙"},
    )
    assert foreign_type.status_code == 201, foreign_type.text
    created = await client.post(f"{base}/relationships", json={"a_id": a_id, "b_id": b_id, "type_id": friend})
    rel_id = created.json()["id"]
    later = await client.patch(f"{base}/relationships/{rel_id}", json={"chapter_no": 20, "type_id": enemy})
    assert later.status_code == 200, later.text

    async with app.state.sessionmaker() as db:
        timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project_id,
                SettingTimeline.subject_kind == "relationship",
                SettingTimeline.character_id == min(a_id, b_id),
                SettingTimeline.related_character_id == max(a_id, b_id),
            )
        )
        assert timeline is not None
        revision = timeline.revision
        with pytest.raises(BadRequestError, match="不属于该项目"):
            async with db.begin_nested():
                await apply_progression(
                    db,
                    timeline,
                    ProgressionCreate(
                        expected_revision=revision,
                        field_key="identity",
                        operation="replace",
                        value={"a_id": a_id, "b_id": b_id, "type_id": foreign_type.json()["id"]},
                        anchor_kind="planned",
                        chapter_no=5,
                    ),
                    materialize=False,
                )
        await db.refresh(timeline)
        assert timeline.revision == revision
        assert (
            await db.scalar(
                select(func.count(SettingProgression.id)).where(SettingProgression.timeline_id == timeline.id)
            )
            == 1
        )


async def test_initial_and_inactive_historical_type_references_are_checked(app, client):
    project_id, base, a_id, b_id, friend, _ = await _setup(client)
    other_project = await create_project(client, title="另一作品")
    foreign = await client.post(
        f"/api/v1/projects/{other_project['id']}/relationship-types",
        json={"category": "social", "role_a": "本地甲", "role_b": "本地乙"},
    )
    foreign_id = foreign.json()["id"]
    async with app.state.sessionmaker() as db:
        invalid = {
            "exists": True,
            "identity": {"a_id": a_id, "b_id": b_id, "type_id": foreign_id},
            "intimacy": 0,
            "status": "normal",
            "description": "",
        }
        with pytest.raises(BadRequestError, match="不属于该项目"):
            await get_or_create_relationship_timeline(db, project_id, a_id, b_id, initial_state=invalid)
        assert (
            await db.scalar(
                select(func.count(SettingTimeline.id)).where(SettingTimeline.subject_kind == "relationship")
            )
            == 0
        )

        valid = dict(invalid, identity={"a_id": a_id, "b_id": b_id, "type_id": friend})
        timeline = await get_or_create_relationship_timeline(db, project_id, a_id, b_id, initial_state=valid)
        event_row = SettingProgression(
            timeline_id=timeline.id,
            field_key="identity",
            operation="replace",
            value=invalid["identity"],
            anchor_kind="planned",
            chapter_no=5,
            boundary="start",
            sort_order=0,
            source="manual",
            protection="manual",
            active=False,
        )
        db.add(event_row)
        await db.flush()
        with pytest.raises(BadRequestError, match="不属于该项目"):
            await validate_relationship_references(db, project_id, [timeline])


async def test_relationship_type_referenced_only_by_history_cannot_be_deleted(client):
    _, base, a_id, b_id, friend, enemy = await _setup(client)
    custom = await client.post(
        f"{base}/relationship-types", json={"category": "work", "role_a": "主公", "role_b": "谋士"}
    )
    assert custom.status_code == 201, custom.text
    type_id = custom.json()["id"]
    created = await client.post(f"{base}/relationships", json={"a_id": a_id, "b_id": b_id, "type_id": friend})
    assert created.status_code == 201, created.text
    rel_id = created.json()["id"]
    for chapter_no, new_type in ((5, type_id), (20, enemy)):
        changed = await client.patch(
            f"{base}/relationships/{rel_id}", json={"chapter_no": chapter_no, "type_id": new_type}
        )
        assert changed.status_code == 200, changed.text
    timeline = (
        await client.get(
            f"{base}/setting-timelines",
            params={
                "subject_kind": "relationship",
                "character_id": min(a_id, b_id),
                "related_character_id": max(a_id, b_id),
            },
        )
    ).json()["items"][0]
    url = f"{base}/setting-timelines/{timeline['id']}"
    assert (await client.delete(f"{base}/relationship-types/{type_id}")).status_code == 409
    history = (await client.get(f"{url}/progressions")).json()
    old = next(item for item in history["items"] if item["value"]["type_id"] == type_id)
    revoked = await client.delete(
        f"{url}/progressions/{old['id']}", params={"expected_revision": history["timeline_revision"]}
    )
    assert revoked.status_code == 200, revoked.text
    assert (await client.delete(f"{base}/relationship-types/{type_id}")).status_code == 409


@pytest.mark.parametrize(
    ("chapter_no", "source", "protection"), [(5, "manual", "manual"), (10, "analysis", "automatic")]
)
async def test_invalid_event_before_relationship_creation_rolls_back_without_dirtying_projection(
    app, client, chapter_no, source, protection
):
    project_id, base, a_id, b_id, friend, _ = await _setup(client)
    created = await client.post(
        f"{base}/relationships", json={"a_id": a_id, "b_id": b_id, "type_id": friend, "chapter_no": 10}
    )
    assert created.status_code == 201, created.text
    async with app.state.sessionmaker() as db:
        timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project_id,
                SettingTimeline.subject_kind == "relationship",
                SettingTimeline.character_id == min(a_id, b_id),
                SettingTimeline.related_character_id == max(a_id, b_id),
            )
        )
        assert timeline is not None
        revision = timeline.revision
        with pytest.raises(ConflictError) as caught:
            async with db.begin_nested():
                await apply_progression(
                    db,
                    timeline,
                    ProgressionCreate(
                        expected_revision=revision,
                        field_key="intimacy",
                        operation="replace",
                        value=75,
                        anchor_kind="planned",
                        chapter_no=chapter_no,
                    ),
                    source=source,
                    protection=protection,
                )
        assert caught.value.extra["code"] == "progression_lifecycle_conflict"
        await db.refresh(timeline)
        assert timeline.revision == revision
        relation = await db.get(Relationship, created.json()["id"])
        assert relation.is_present is True and relation.intimacy == 0
        assert (
            await db.scalar(
                select(func.count(SettingProgression.id)).where(SettingProgression.timeline_id == timeline.id)
            )
            == 1
        )


async def test_edit_revoke_and_renumber_cannot_invalidate_lifecycle(app, client):
    project_id, base, a_id, b_id, friend, _ = await _setup(client)
    first = (await client.post(f"{base}/chapters", json={"chapter_no": 10, "title": "相识"})).json()
    second = (await client.post(f"{base}/chapters", json={"chapter_no": 20, "title": "交心"})).json()
    created = await client.post(
        f"{base}/relationships", json={"a_id": a_id, "b_id": b_id, "type_id": friend, "chapter_id": first["id"]}
    )
    assert created.status_code == 201, created.text
    updated = await client.patch(
        f"{base}/relationships/{created.json()['id']}", json={"chapter_id": second["id"], "intimacy": 50}
    )
    assert updated.status_code == 200, updated.text
    timelines = await client.get(
        f"{base}/setting-timelines",
        params={
            "subject_kind": "relationship",
            "character_id": min(a_id, b_id),
            "related_character_id": max(a_id, b_id),
        },
    )
    timeline = timelines.json()["items"][0]
    url = f"{base}/setting-timelines/{timeline['id']}"
    changes = (await client.get(f"{url}/progressions")).json()
    creation = next(row for row in changes["items"] if row["field_key"] == "presence")
    revision = changes["timeline_revision"]
    rejected = [
        await client.delete(f"{url}/progressions/{creation['id']}", params={"expected_revision": revision}),
        await client.patch(
            f"{url}/progressions/{creation['id']}",
            json={"expected_revision": revision, "anchor_kind": "planned", "chapter_id": None, "chapter_no": 30},
        ),
        await client.patch(f"{base}/chapters/{first['id']}", json={"chapter_no": 30}),
    ]
    for response in rejected:
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "progression_lifecycle_conflict"
    unchanged = (await client.get(f"{url}/progressions")).json()
    assert unchanged["timeline_revision"] == revision
    assert unchanged["total"] == 2
    assert (await client.get(f"{base}/chapters/{first['id']}")).json()["chapter_no"] == 10
    assert (await _snapshot(app, project_id, a_id, b_id, 20))["intimacy"] == 50
    assert (await client.get(f"{base}/relationships")).json()[0]["intimacy"] == 50


async def test_relation_materialization_queries_in_bulk_and_keeps_each_subject_separate(app, client):
    project_id, base, first, second, friend, _ = await _setup(client)
    others = [second]
    for index in range(5):
        response = await client.post(f"{base}/characters", json={"name": f"角色{index}"})
        assert response.status_code == 201
        others.append(response.json()["id"])
    async with app.state.sessionmaker() as db:
        timelines = [await get_or_create_relationship_timeline(db, project_id, first, other) for other in others]
        snapshots = {
            timeline.id: EffectiveSnapshot(
                fields={
                    "exists": True,
                    "identity": {"a_id": first, "b_id": other, "type_id": friend},
                    "intimacy": index,
                    "status": "normal",
                    "description": f"关系{index}",
                },
                custom_fields={},
                applied_event_ids=(),
                orphaned_event_ids=(),
                baseline_status="confirmed",
            )
            for index, (timeline, other) in enumerate(zip(timelines, others, strict=True))
        }
        selects = []

        def count_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)

        engine = app.state.engine.sync_engine
        event.listen(engine, "before_cursor_execute", count_selects)
        try:
            carriers = await materialize_relationships(db, project_id, timelines, snapshots)
        finally:
            event.remove(engine, "before_cursor_execute", count_selects)
        assert len(selects) <= 3
        assert [carriers[timeline.id].intimacy for timeline in timelines] == list(range(len(others)))
