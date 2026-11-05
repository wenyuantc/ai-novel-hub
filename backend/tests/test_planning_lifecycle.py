"""规划身份在真实建章／删除／快照／备份／智能体撤销链路中保持一致。"""

import json

import pytest
from sqlalchemy import select

from app.core.errors import BadRequestError, ConflictError
from app.models import (
    Chapter,
    ChapterCharacter,
    ChapterPlan,
    Character,
    Outline,
    OutlineSnapshot,
    Project,
    SettingTimeline,
)
from app.models.entities import WORLD_CATEGORIES
from app.models.planning import (
    PlanningTag,
    PlanningUnit,
    PlanningUnitCareer,
    PlanningUnitOrganization,
    PlanningUnitRelationship,
    PlanningUnitTag,
    PlanningUnitWorld,
    Subplot,
    SubplotCrossing,
    SubplotCrossingParticipant,
    SubplotNode,
)
from app.schemas.chapter import ChapterUpdate
from app.schemas.outline import OutlineImportRequest, PlanIn, PlansSync, PlanUpdate
from app.services import (
    cast_service,
    chapter_service,
    outline_snapshot_service,
    outline_transfer_service,
    plan_service,
    planning_lifecycle,
    setting_progression_service,
    story_service,
    wizard_service,
)
from app.services.agent import effects
from app.services.project_transfer import importer
from tests.helpers import create_project, register


@pytest.fixture
async def planning_db(app, client):
    await register(client)
    data = await create_project(client)
    async with app.state.sessionmaker() as db:
        project = await db.get(Project, data["id"])
        yield db, project.id, project.user_id


async def _seed(db, project_id, *, built=False):
    outline = Outline(project_id=project_id, title="出发", chapter_no=1, volume_no=1, volume_title="启程")
    a, b = Character(project_id=project_id, name="甲"), Character(project_id=project_id, name="乙")
    db.add_all([outline, a, b])
    await db.flush()
    await setting_progression_service.create_character_timeline(db, a)
    await setting_progression_service.create_character_timeline(db, b)
    plan = ChapterPlan(project_id=project_id, outline_id=outline.id, seq=1, title="前往港口", character_ids=[a.id])
    db.add(plan)
    await db.flush()
    chapter = None
    if built:
        chapter = Chapter(project_id=project_id, outline_id=outline.id, plan_id=plan.id, chapter_no=1, title=plan.title)
        db.add(chapter)
        await db.flush()
        plan.chapter_id = chapter.id
        db.add(ChapterCharacter(chapter_id=chapter.id, character_id=a.id))
    unit = PlanningUnit(project_id=project_id, plan_id=plan.id,
                        chapter_id=chapter.id if chapter else None, pov_character_id=a.id)
    plots = [Subplot(project_id=project_id, name=name) for name in ("寻亲", "追凶", "商战")]
    db.add_all([unit, *plots])
    await db.flush()
    node = SubplotNode(unit_id=unit.id, subplot_id=plots[0].id, content="在港口找到线索")
    crossing = SubplotCrossing(
        project_id=project_id, unit_id=unit.id, title="同一封信", description="两条线索指向同一人"
    )
    db.add_all([node, crossing])
    await db.flush()
    db.add_all(SubplotCrossingParticipant(crossing_id=crossing.id, subplot_id=p.id) for p in plots[:2])
    await db.commit()
    return {"outline": outline.id, "plan": plan.id, "chapter": chapter.id if chapter else None,
            "unit": unit.id, "a": a.id, "b": b.id, "plots": [p.id for p in plots],
            "node": node.id, "crossing": crossing.id}


async def _unit(db, unit_id):
    return await db.scalar(select(PlanningUnit).where(PlanningUnit.id == unit_id)
                           .execution_options(populate_existing=True))


async def _seed_references(db, client, pid, ids):
    base = f"/api/v1/projects/{pid}"
    world_ids = []
    for category in WORLD_CATEGORIES:
        response = await client.post(f"{base}/world", json={"title": category, "category": category})
        assert response.status_code == 201, response.text
        world_ids.append(response.json()["id"])
    organization = await client.post(f"{base}/organizations", json={"name": "商会"})
    career = await client.post(f"{base}/careers", json={"name": "信使", "stages": [{"name": "见习"}]})
    assert organization.status_code == career.status_code == 201
    types = (await client.get(f"{base}/relationship-types")).json()
    relation = await client.post(f"{base}/relationships", json={
        "a_id": ids["a"], "b_id": ids["b"], "type_id": types[0]["id"],
    })
    assert relation.status_code == 201, relation.text
    timeline_id = await db.scalar(select(SettingTimeline.id).where(
        SettingTimeline.project_id == pid, SettingTimeline.subject_kind == "relationship",
    ))
    tag = PlanningTag(project_id=pid, name="转折", tone="purple")
    db.add(tag)
    await db.flush()
    db.add_all([
        *(PlanningUnitWorld(unit_id=ids["unit"], world_id=wid) for wid in world_ids),
        PlanningUnitTag(unit_id=ids["unit"], tag_id=tag.id),
        PlanningUnitOrganization(unit_id=ids["unit"], organization_id=organization.json()["id"]),
        PlanningUnitCareer(unit_id=ids["unit"], career_id=career.json()["id"]),
        PlanningUnitRelationship(unit_id=ids["unit"], timeline_id=timeline_id),
    ])
    await db.commit()
    return {"world_ids": world_ids, "organization_id": organization.json()["id"],
            "career_id": career.json()["id"], "relationship_id": relation.json()["id"],
            "timeline_id": timeline_id, "tag_id": tag.id}


async def test_build_transfers_same_unit_nodes_and_crossing(planning_db):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid)
    before = await db.scalar(select(Project.planning_revision).where(Project.id == pid))
    result = await plan_service.build_chapters(db, user_id, pid, ids["outline"])
    unit = await _unit(db, ids["unit"])
    assert unit.chapter_id == result.chapters[0].id and unit.plan_id == ids["plan"]
    assert await db.scalar(select(SubplotNode.unit_id).where(SubplotNode.id == ids["node"])) == unit.id
    assert await db.scalar(select(SubplotCrossing.unit_id).where(SubplotCrossing.id == ids["crossing"])) == unit.id
    assert result.plans[0].character_ids == [ids["a"]]
    assert await db.scalar(select(Project.planning_revision).where(Project.id == pid)) > before


async def test_cast_and_plan_edit_share_authority_and_delete_hands_back(planning_db):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid, built=True)
    await cast_service.set_cast(db, user_id, pid, ids["chapter"], [ids["b"]])
    assert await db.scalar(select(ChapterPlan.character_ids).where(ChapterPlan.id == ids["plan"])) == [ids["b"]]
    await plan_service.update_plan(db, user_id, pid, ids["outline"], ids["plan"], PlanUpdate(character_ids=[ids["a"]]))
    assert await cast_service.get_cast(db, user_id, pid, ids["chapter"]) == [ids["a"]]
    await cast_service.set_cast(db, user_id, pid, ids["chapter"], [ids["b"]])
    await chapter_service.delete_chapter(db, user_id, pid, ids["chapter"])
    unit = await _unit(db, ids["unit"])
    assert unit.chapter_id is None and unit.plan_id == ids["plan"]
    assert await db.scalar(select(ChapterPlan.character_ids).where(ChapterPlan.id == ids["plan"])) == [ids["b"]]
    assert unit.detached_character_ids == []


async def test_outline_deletion_keeps_live_chapter_planning(planning_db):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid, built=True)
    await story_service.delete_owned(db, story_service.OUTLINES, user_id, pid, ids["outline"], manage=True)
    unit = await _unit(db, ids["unit"])
    assert unit.plan_id is None and unit.chapter_id == ids["chapter"]
    assert await db.get(SubplotNode, ids["node"]) is not None
    assert await db.get(SubplotCrossing, ids["crossing"]) is not None


async def test_reexpand_detaches_both_sources_without_id_reclaim(planning_db):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid, built=True)
    await plan_service.sync_plans(db, user_id, pid, ids["outline"], PlansSync(plans=[]))
    unit = await _unit(db, ids["unit"])
    assert unit.chapter_id is None and unit.plan_id is None and unit.detached_character_ids == [ids["a"]]
    assert "前往港口" in unit.last_source_label
    new = await plan_service.sync_plans(db, user_id, pid, ids["outline"], PlansSync(plans=[PlanIn(title="另一章")]))
    assert new[0].id == ids["plan"]  # SQLite 的真实复用情形。
    assert (await _unit(db, ids["unit"])).plan_id is None
    assert await db.get(SubplotCrossing, ids["crossing"]) is not None


async def test_chapter_reparent_keeps_unit_on_chapter(planning_db):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid, built=True)
    other = Outline(project_id=pid, title="另一卷", chapter_no=2)
    db.add(other)
    await db.commit()
    await chapter_service.update_chapter(db, user_id, pid, ids["chapter"], ChapterUpdate(outline_id=other.id))
    unit = await _unit(db, ids["unit"])
    assert unit.chapter_id == ids["chapter"] and unit.plan_id is None
    assert await db.scalar(select(ChapterPlan.chapter_id).where(ChapterPlan.id == ids["plan"])) is None


async def test_build_conflict_is_preflight_and_does_not_partially_build(planning_db):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid)
    existing = Chapter(project_id=pid, outline_id=ids["outline"], chapter_no=1, title="旧章")
    db.add(existing)
    await db.flush()
    existing_id = existing.id
    db.add_all([PlanningUnit(project_id=pid, chapter_id=existing_id, pov_character_id=ids["b"]),
                ChapterPlan(project_id=pid, outline_id=ids["outline"], seq=2, title="后续细纲")])
    await db.commit()
    with pytest.raises(ConflictError) as caught:
        await plan_service.build_chapters(db, user_id, pid, ids["outline"])
    assert caught.value.extra["code"] == "planning_merge_conflict"
    await db.rollback()
    assert list(await db.scalars(select(Chapter.id).where(Chapter.project_id == pid))) == [existing_id]
    assert await db.scalar(select(Chapter.plan_id).where(Chapter.id == existing_id)) is None


async def test_snapshot_reconnects_plan_but_preserves_new_content(planning_db):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid)
    snapshot = await outline_snapshot_service.create_manual(db, user_id, pid, "原计划")
    await plan_service.sync_plans(db, user_id, pid, ids["outline"], PlansSync(plans=[]))
    node = await db.get(SubplotNode, ids["node"])
    node.content = "作者后来改过的推进"
    await db.commit()
    result = await outline_snapshot_service.restore_snapshot(db, user_id, pid, snapshot.id)
    assert (await _unit(db, ids["unit"])).plan_id == ids["plan"]
    assert (await db.get(SubplotNode, ids["node"])).content == "作者后来改过的推进"
    assert not result.warnings


async def test_binding_epoch_rejects_rebind_then_orphan_again(planning_db):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid)
    snapshot = await outline_snapshot_service.create_manual(db, user_id, pid, "P1")
    second = ChapterPlan(project_id=pid, outline_id=ids["outline"], seq=2, title="P2")
    db.add(second)
    await db.flush()
    second_id = second.id
    unit = await _unit(db, ids["unit"])
    unit.plan_id = second_id
    unit.binding_epoch += 1  # 显式改挂的已确认领域契约。
    await db.commit()
    await planning_lifecycle.detach_sources(db, pid, plan_ids=[second_id])
    await db.delete(second)
    await db.commit()
    result = await outline_snapshot_service.restore_snapshot(db, user_id, pid, snapshot.id)
    assert (await _unit(db, ids["unit"])).plan_id is None
    assert any("位置已变化" in warning for warning in result.warnings)


async def test_snapshot_does_not_attach_old_unit_to_reused_chapter_id(planning_db):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid, built=True)
    snapshot = await outline_snapshot_service.create_manual(db, user_id, pid, "旧章")
    await chapter_service.delete_chapter(db, user_id, pid, ids["chapter"])
    await story_service.delete_owned(db, story_service.OUTLINES, user_id, pid, ids["outline"], manage=True)
    new = Chapter(project_id=pid, title="与旧章无关的新章", chapter_no=1)
    db.add(new)
    await db.commit()
    assert new.id == ids["chapter"]
    result = await outline_snapshot_service.restore_snapshot(db, user_id, pid, snapshot.id)
    unit = await _unit(db, ids["unit"])
    assert unit.chapter_id is None and unit.plan_id is None
    assert any("位置已变化" in warning for warning in result.warnings)


async def test_live_chapter_snapshot_uses_current_cast(planning_db):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid, built=True)
    snapshot = await outline_snapshot_service.create_manual(db, user_id, pid, "原角色")
    await cast_service.set_cast(db, user_id, pid, ids["chapter"], [ids["b"]])
    await outline_snapshot_service.restore_snapshot(db, user_id, pid, snapshot.id)
    unit = await _unit(db, ids["unit"])
    assert unit.chapter_id == ids["chapter"] and unit.plan_id == ids["plan"]
    assert await cast_service.get_cast(db, user_id, pid, ids["chapter"]) == [ids["b"]]
    assert await db.scalar(select(ChapterPlan.character_ids).where(ChapterPlan.id == ids["plan"])) == [ids["b"]]


async def test_v3_backup_round_trip_and_v2_does_not_smuggle_planning(planning_db, client, app):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid)
    references = await _seed_references(db, client, pid, ids)
    (await _unit(db, ids["unit"])).binding_epoch = 7
    await outline_snapshot_service.create_manual(db, user_id, pid, "规划备份")
    response = await client.get(f"/api/v1/projects/{pid}/backup?snapshots=1")
    assert response.status_code == 200
    backup = response.json()
    assert backup["version"] == 3
    preview = await client.post("/api/v1/projects/import/preview", files={"file": ("book.json", response.content)})
    assert preview.status_code == 200, preview.text
    imported = await client.post("/api/v1/projects/import", json={"token": preview.json()["token"]})
    assert imported.status_code == 201, imported.text
    new_pid = imported.json()["project"]["id"]
    async with app.state.sessionmaker() as fresh:
        unit = await fresh.scalar(select(PlanningUnit).where(PlanningUnit.project_id == new_pid))
        assert unit.id != ids["unit"] and unit.plan_id != ids["plan"]
        assert (await fresh.get(ChapterPlan, unit.plan_id)).character_ids == [unit.pov_character_id]
        assert unit.pov_character_id != ids["a"]
        assert unit.binding_epoch == 7
        await planning_lifecycle.validate_project(fresh, new_pid)
        assert await fresh.scalar(select(Project.planning_revision).where(Project.id == new_pid)) == 0
        crossing = await fresh.scalar(select(SubplotCrossing).where(SubplotCrossing.project_id == new_pid))
        assert crossing.unit_id == unit.id
        new_world_ids = list(await fresh.scalars(select(PlanningUnitWorld.world_id).where(
            PlanningUnitWorld.unit_id == unit.id
        )))
        assert len(new_world_ids) == len(WORLD_CATEGORIES)
        assert set(new_world_ids).isdisjoint(references["world_ids"])
        for model, column, old_key in (
            (PlanningUnitTag, "tag_id", "tag_id"),
            (PlanningUnitOrganization, "organization_id", "organization_id"),
            (PlanningUnitCareer, "career_id", "career_id"),
            (PlanningUnitRelationship, "timeline_id", "timeline_id"),
        ):
            target_id = await fresh.scalar(select(getattr(model, column)).where(model.unit_id == unit.id))
            assert target_id is not None and target_id != references[old_key]
        snapshot = await fresh.scalar(select(OutlineSnapshot).where(OutlineSnapshot.project_id == new_pid))
        assert snapshot.data["planning_bindings"] == [{
            "unit_id": unit.id, "plan_id": unit.plan_id, "binding_epoch": 7,
        }]
    old = json.loads(response.content)
    old["version"] = 2
    parsed = importer.parse_backup(json.dumps(old).encode())
    assert parsed.tables["planning_units"] == [] and parsed.tables["subplot_crossings"] == []
    assert all("planning_bindings" not in row["data"] for row in parsed.tables["outline_snapshots"])


async def test_v3_rejects_invalid_crossing_and_wrong_timeline_kind(planning_db, client):
    db, pid, _ = planning_db
    ids = await _seed(db, pid)
    response = await client.get(f"/api/v1/projects/{pid}/backup")
    data = response.json()
    data["tables"]["subplot_crossing_participants"].pop()
    with pytest.raises(BadRequestError, match="2–20"):
        importer.parse_backup(json.dumps(data).encode())
    data = response.json()
    character_timeline = data["tables"]["setting_timelines"][0]["id"]
    data["tables"]["planning_unit_relationships"] = [{"unit_id": ids["unit"], "timeline_id": character_timeline}]
    with pytest.raises(BadRequestError, match="不是关系主体"):
        importer.parse_backup(json.dumps(data).encode())


@pytest.mark.parametrize("changed", ["node", "crossing", "participants"])
async def test_undo_source_delete_detects_later_child_content(planning_db, changed):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid, built=True)
    before = await effects.snapshot(db, pid)
    await chapter_service.delete_chapter(db, user_id, pid, ids["chapter"])
    after = await effects.snapshot(db, pid)
    delta = effects.diff(before, after)
    if changed == "node":
        (await db.get(SubplotNode, ids["node"])).content = "作者新决定"
    elif changed == "crossing":
        (await db.get(SubplotCrossing, ids["crossing"])).description = "另一个事件方向"
    else:
        db.add(SubplotCrossingParticipant(crossing_id=ids["crossing"], subplot_id=ids["plots"][2]))
    await db.commit()
    with pytest.raises(ConflictError) as caught:
        await effects.undo(db, pid, json.loads(json.dumps(delta)))
    assert caught.value.extra["code"] == "undo_conflict"
    await db.rollback()
    assert await db.get(Chapter, ids["chapter"]) is None


async def test_undo_restores_planning_but_never_rolls_back_versions(planning_db):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid, built=True)
    before = await effects.snapshot(db, pid)
    await chapter_service.delete_chapter(db, user_id, pid, ids["chapter"])
    after = await effects.snapshot(db, pid)
    revision = await db.scalar(select(Project.planning_revision).where(Project.id == pid))
    delta = effects.diff(before, after)
    assert any("_planning_content_hash" in (e["after"] or {}) for e in delta if e["table"] == "planning_units")
    await effects.undo(db, pid, delta)
    await db.commit()
    unit = await _unit(db, ids["unit"])
    assert unit.chapter_id == ids["chapter"] and unit.plan_id == ids["plan"]
    assert unit.binding_epoch > 0
    assert await db.scalar(select(Project.planning_revision).where(Project.id == pid)) > revision
    assert (await db.get(SubplotNode, ids["node"])).content == "在港口找到线索"
    await planning_lifecycle.validate_project(db, pid)


async def test_legacy_effect_cannot_delete_newly_added_planning(planning_db):
    db, pid, _ = planning_db
    before = await effects.snapshot(db, pid)
    chapter = Chapter(project_id=pid, title="旧效果创建的章", chapter_no=1)
    db.add(chapter)
    await db.commit()
    after = await effects.snapshot(db, pid)
    delta = effects.diff(before, after)  # 没有规划表的旧版 v2 effect。
    assert all(not e["table"].startswith("planning_") for e in delta)
    db.add(PlanningUnit(project_id=pid, chapter_id=chapter.id))
    await db.commit()
    with pytest.raises(ConflictError):
        await effects.undo(db, pid, delta)


async def test_true_character_delete_cleans_detached_and_plan_json(planning_db):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid)
    detached = PlanningUnit(project_id=pid, detached_character_ids=[ids["a"], ids["b"]])
    db.add(detached)
    await db.commit()
    await story_service.delete_owned(db, story_service.CHARACTERS, user_id, pid, ids["a"], manage=True)
    assert (await _unit(db, detached.id)).detached_character_ids == [ids["b"]]
    assert await db.scalar(select(ChapterPlan.character_ids).where(ChapterPlan.id == ids["plan"])) == []
    assert (await _unit(db, ids["unit"])).pov_character_id is None


@pytest.mark.parametrize("built", [False, True])
async def test_old_snapshot_does_not_infer_planning_binding(planning_db, built):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid, built=built)
    snapshot = await outline_snapshot_service.create_manual(db, user_id, pid, "旧格式")
    record = await db.get(OutlineSnapshot, snapshot.id)
    record.data = {key: value for key, value in record.data.items() if key != "planning_bindings"}
    await db.commit()
    if not built:
        await plan_service.sync_plans(db, user_id, pid, ids["outline"], PlansSync(plans=[]))
    result = await outline_snapshot_service.restore_snapshot(db, user_id, pid, snapshot.id)
    unit = await _unit(db, ids["unit"])
    assert unit.plan_id is None and unit.chapter_id == ids["chapter"]
    if not built:
        assert any("旧快照缺少规划绑定" in warning for warning in result.warnings)


async def test_retired_unit_cannot_be_resurrected_by_old_snapshot(planning_db):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid)
    snapshot = await outline_snapshot_service.create_manual(db, user_id, pid, "合并前")
    retired = await _unit(db, ids["unit"])
    retired.plan_id = retired.pov_character_id = None
    retired.retired = True
    retired.binding_epoch += 1
    await db.flush()
    survivor = PlanningUnit(project_id=pid, plan_id=ids["plan"], pov_character_id=ids["a"])
    db.add(survivor)
    await db.flush()
    (await db.get(SubplotNode, ids["node"])).unit_id = survivor.id
    (await db.get(SubplotCrossing, ids["crossing"])).unit_id = survivor.id
    await db.commit()
    result = await outline_snapshot_service.restore_snapshot(db, user_id, pid, snapshot.id)
    retired = await _unit(db, ids["unit"])
    assert retired.retired and retired.plan_id is None and retired.chapter_id is None
    assert any("位置已变化" in warning for warning in result.warnings)
    assert (await db.get(SubplotNode, ids["node"])).unit_id == survivor.id
    await planning_lifecycle.validate_project(db, pid)


async def test_snapshot_keeps_explicitly_empty_detached_cast(planning_db):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid)
    snapshot = await outline_snapshot_service.create_manual(db, user_id, pid, "有角色时")
    await plan_service.sync_plans(db, user_id, pid, ids["outline"], PlansSync(plans=[]))
    (await _unit(db, ids["unit"])).detached_character_ids = []
    await db.commit()
    await outline_snapshot_service.restore_snapshot(db, user_id, pid, snapshot.id)
    assert await db.scalar(select(ChapterPlan.character_ids).where(ChapterPlan.id == ids["plan"])) == []


async def test_orphan_backup_can_restore_explicit_snapshot_binding(planning_db, client, app):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid)
    await outline_snapshot_service.create_manual(db, user_id, pid, "仍有计划")
    await plan_service.sync_plans(db, user_id, pid, ids["outline"], PlansSync(plans=[]))
    response = await client.get(f"/api/v1/projects/{pid}/backup?snapshots=1")
    preview = await client.post("/api/v1/projects/import/preview", files={"file": ("orphan.json", response.content)})
    assert preview.status_code == 200, preview.text
    imported = await client.post("/api/v1/projects/import", json={"token": preview.json()["token"]})
    assert imported.status_code == 201, imported.text
    new_pid = imported.json()["project"]["id"]
    async with app.state.sessionmaker() as fresh:
        unit = await fresh.scalar(select(PlanningUnit).where(PlanningUnit.project_id == new_pid))
        assert unit.plan_id is None and unit.chapter_id is None
        snapshot = await fresh.scalar(select(OutlineSnapshot).where(
            OutlineSnapshot.project_id == new_pid, OutlineSnapshot.note == "仍有计划"
        ))
        binding = snapshot.data["planning_bindings"][0]
        assert binding["unit_id"] == unit.id and binding["binding_epoch"] == unit.binding_epoch
        await outline_snapshot_service.restore_snapshot(fresh, user_id, new_pid, snapshot.id)
        restored = await _unit(fresh, unit.id)
        assert restored.plan_id is not None
        assert await fresh.scalar(select(SubplotNode.id).where(SubplotNode.unit_id == unit.id)) is not None


async def test_whole_project_delete_cascades_crossing_participants(planning_db):
    db, pid, _ = planning_db
    ids = await _seed(db, pid, built=True)
    project = await db.get(Project, pid)
    await db.delete(project)
    await db.commit()
    assert await db.scalar(select(SubplotCrossingParticipant.crossing_id).where(
        SubplotCrossingParticipant.crossing_id == ids["crossing"]
    )) is None


@pytest.mark.parametrize("built", [False, True])
@pytest.mark.parametrize("source", ["wizard", "import"])
async def test_outline_replacement_preserves_planning_and_current_cast(planning_db, built, source):
    db, pid, user_id = planning_db
    ids = await _seed(db, pid, built=built)
    if built:
        await cast_service.set_cast(db, user_id, pid, ids["chapter"], [ids["b"]])
    revision = await db.scalar(select(Project.planning_revision).where(Project.id == pid))
    if source == "wizard":
        await wizard_service.save_wizard_result(db, pid, "outline", '[{"title":"新大纲"}]')
    else:
        await outline_transfer_service.import_outline(db, user_id, pid, OutlineImportRequest(
            mode="replace", data={"format": "ainovel.outline", "version": 1, "volumes": [{
                "outlines": [{"title": "新大纲", "plans": [{"title": "新细纲"}]}],
            }]},
        ))
    unit = await _unit(db, ids["unit"])
    assert unit.plan_id is None and unit.chapter_id == ids["chapter"]
    assert unit.detached_character_ids == ([] if built else [ids["a"]])
    if built:
        assert await cast_service.get_cast(db, user_id, pid, ids["chapter"]) == [ids["b"]]
    assert (await db.get(SubplotNode, ids["node"])).unit_id == unit.id
    assert (await db.get(SubplotCrossing, ids["crossing"])).unit_id == unit.id
    assert await db.scalar(select(Project.planning_revision).where(Project.id == pid)) > revision
    await planning_lifecycle.validate_project(db, pid)


async def test_wizard_character_replacement_cleans_json_before_id_reuse(planning_db):
    db, pid, _ = planning_db
    ids = await _seed(db, pid)
    detached = PlanningUnit(project_id=pid, detached_character_ids=[ids["a"], ids["b"]])
    db.add(detached)
    await db.commit()
    revision = await db.scalar(select(Project.planning_revision).where(Project.id == pid))
    await wizard_service.save_wizard_result(db, pid, "characters", '[{"name":"新角色"}]')
    replacement = await db.scalar(select(Character.id).where(Character.project_id == pid))
    assert replacement == ids["a"]  # 角色也会实际复用被删除的 SQLite 整数 ID。
    assert (await _unit(db, detached.id)).detached_character_ids == []
    assert (await _unit(db, ids["unit"])).pov_character_id is None
    assert await db.scalar(select(ChapterPlan.character_ids).where(ChapterPlan.id == ids["plan"])) == []
    assert await db.scalar(select(Project.planning_revision).where(Project.id == pid)) > revision


async def test_setting_deletion_clears_each_typed_reference_and_advances_revision(planning_db, client):
    db, pid, _ = planning_db
    ids = await _seed(db, pid)
    refs = await _seed_references(db, client, pid, ids)
    for path, target_id, model in (
        ("world", refs["world_ids"][0], PlanningUnitWorld),
        ("organizations", refs["organization_id"], PlanningUnitOrganization),
        ("careers", refs["career_id"], PlanningUnitCareer),
        ("relationships", refs["relationship_id"], PlanningUnitRelationship),
    ):
        before = await db.scalar(select(Project.planning_revision).where(Project.id == pid))
        response = await client.delete(f"/api/v1/projects/{pid}/{path}/{target_id}")
        assert response.status_code == 204, response.text
        assert await db.scalar(select(Project.planning_revision).where(Project.id == pid)) > before
        remaining = list(await db.scalars(select(model.unit_id).where(model.unit_id == ids["unit"])))
        assert len(remaining) == (len(WORLD_CATEGORIES) - 1 if path == "world" else 0)
    assert await db.get(SubplotNode, ids["node"]) is not None
    assert await db.get(SubplotCrossing, ids["crossing"]) is not None
    await planning_lifecycle.validate_project(db, pid)


async def test_undo_build_releases_sources_before_restoring_retired_empty_unit(planning_db):
    db, pid, user_id = planning_db
    chapter = Chapter(project_id=pid, title="旧有空章", chapter_no=1)
    db.add(chapter)
    await db.flush()
    empty = PlanningUnit(project_id=pid, chapter_id=chapter.id)
    db.add(empty)
    await db.commit()
    empty_id, chapter_id = empty.id, chapter.id
    ids = await _seed(db, pid)
    chapter.outline_id = ids["outline"]
    await db.commit()
    before = await effects.snapshot(db, pid)
    await plan_service.build_chapters(db, user_id, pid, ids["outline"])
    after = await effects.snapshot(db, pid)
    assert (await _unit(db, empty_id)).retired
    assert (await _unit(db, ids["unit"])).chapter_id == chapter_id
    await effects.undo(db, pid, effects.diff(before, after))
    await db.commit()
    empty = await _unit(db, empty_id)
    assert not empty.retired and empty.chapter_id == chapter_id and empty.plan_id is None
    plan_unit = await _unit(db, ids["unit"])
    assert plan_unit.chapter_id is None and plan_unit.plan_id == ids["plan"]
    assert empty.binding_epoch > 1 and plan_unit.binding_epoch > 0
    await planning_lifecycle.validate_project(db, pid)
