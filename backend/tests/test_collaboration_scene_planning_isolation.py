"""场景规划与快照捕获不得暴露异常跨作品的规划引用。"""

import pytest

from app.core.errors import ConflictError
from app.models.planning import (
    PlanningUnit,
    PlanningUnitCareer,
    PlanningUnitCharacter,
    PlanningUnitOrganization,
    PlanningUnitRelationship,
    PlanningUnitTag,
    PlanningUnitWorld,
)
from app.services import planning_queries
from app.services.planning_scene_service import capture_scene_planning, resolve_scene_plannings
from tests.helpers import create_project
from tests.test_collaboration_planning_access import _collaborators


async def test_scene_planning_rejects_foreign_references_without_exposing_ids(app, client, make_client):
    base, viewer, _, _, _ = await _collaborators(client, make_client)
    project_id = int(base.rsplit("/", 1)[-1])
    chapter = await client.post(f"{base}/chapters", json={"title": "本书章节", "chapter_no": 1})
    assert chapter.status_code == 201
    scenes_url = f"{base}/chapters/{chapter.json()['id']}/scenes"
    scene_id = (await client.get(scenes_url)).json()["items"][0]["id"]
    other = await create_project(client, title="另一本")
    foreign = f"/api/v1/projects/{other['id']}"
    chars = [(await client.post(f"{foreign}/characters", json={"name": name})).json()["id"] for name in ("甲", "乙")]
    world = (await client.post(f"{foreign}/world", json={"title": "别书设定", "category": "place"})).json()["id"]
    org = (await client.post(f"{foreign}/organizations", json={"name": "别书组织"})).json()["id"]
    career = (await client.post(f"{foreign}/careers", json={"name": "别书职业", "stages": [{"name": "初阶"}]}))
    assert career.status_code == 201
    types = (await client.get(f"{foreign}/relationship-types")).json()
    relation = await client.post(f"{foreign}/relationships", json={
        "a_id": chars[0], "b_id": chars[1], "type_id": types[0]["id"],
    })
    assert relation.status_code == 201
    entries = await client.get(f"{foreign}/planning/entries", params={"kind": "relationship"})
    timeline_id = entries.json()["items"][0]["reference"]["timeline_id"]
    revision = (await client.get(f"{foreign}/planning/matrix")).json()["revision"]
    tag = await client.post(f"{foreign}/planning/tags", json={"expected_revision": revision, "name": "别书标签"})
    assert tag.status_code == 201
    sources = (
        ("pov", None, None, chars[0]),
        ("tags", PlanningUnitTag, "tag_id", tag.json()["item"]["id"]),
        ("character", PlanningUnitCharacter, "character_id", chars[0]),
        ("world", PlanningUnitWorld, "world_id", world),
        ("organization", PlanningUnitOrganization, "organization_id", org),
        ("career", PlanningUnitCareer, "career_id", career.json()["id"]),
        ("relationship", PlanningUnitRelationship, "timeline_id", timeline_id),
    )
    for kind, association, column, foreign_id in sources:
        async with app.state.sessionmaker() as db:
            unit = PlanningUnit(project_id=project_id, source_level="scene", scene_id=scene_id,
                                **{f"{kind}_mode": "override"})
            if kind == "pov":
                unit.pov_character_id = foreign_id
            db.add(unit)
            await db.flush()
            if association is not None:
                db.add(association(unit_id=unit.id, **{column: foreign_id}))
            unit_id = unit.id
            await db.commit()
        for route, params in ((f"{scenes_url}/{scene_id}", None),
                              (f"{base}/planning/matrix", {"granularity": "scene"})):
            response = await viewer.get(route, params=params)
            assert response.status_code == 409, (kind, response.text)
            assert response.json()["code"] == "scene_reference_invalid"
        async with app.state.sessionmaker() as db:
            with pytest.raises(ConflictError):
                await capture_scene_planning(db, project_id, [scene_id])
            await db.rollback()
            await db.delete(await db.get(PlanningUnit, unit_id))
            await db.commit()

    local = await client.post(f"{base}/characters", json={"name": "本书角色"})
    assert local.status_code == 201
    async with app.state.sessionmaker() as db:
        unit = PlanningUnit(project_id=project_id, source_level="scene", scene_id=scene_id, pov_mode="override")
        db.add(unit)
        await db.flush()
        unit_id = unit.id
        await db.commit()
    async with app.state.sessionmaker() as db:
        unit = await db.get(PlanningUnit, unit_id)
        unit.pov_character_id = chars[0]
        with pytest.raises(ConflictError):
            await resolve_scene_plannings(db, project_id, [scene_id])
        assert unit.pov_character_id == chars[0] and unit in db.dirty
        await db.rollback()
    async with app.state.sessionmaker() as db:
        unit = await db.get(PlanningUnit, unit_id)
        unit.project_id = other["id"]
        with pytest.raises(ConflictError):
            await resolve_scene_plannings(db, project_id, [scene_id])
        assert unit.project_id == other["id"] and unit in db.dirty
        await db.rollback()
    async with app.state.sessionmaker() as db:
        unit = await db.get(PlanningUnit, unit_id)
        unit.pov_character_id = local.json()["id"]
        result = await resolve_scene_plannings(db, project_id, [scene_id])
        assert result[scene_id].raw.pov_character_id == local.json()["id"] and unit in db.dirty
        await db.rollback()
    async with app.state.sessionmaker() as db:
        unit = await db.get(PlanningUnit, unit_id)
        unit.binding_epoch += 1
        with db.no_autoflush, pytest.raises(ConflictError):
            await planning_queries.rows(db, project_id)
        assert unit.binding_epoch == 1 and unit in db.dirty
        await db.rollback()
