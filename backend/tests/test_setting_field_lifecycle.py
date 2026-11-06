"""旧创建、换类、删除和覆盖入口维持同一份字段资料图。"""

import json

import pytest
from sqlalchemy import func, select

from app.models import (
    Career,
    Character,
    CharacterCareer,
    Organization,
    OrganizationMember,
    Project,
    SettingProgression,
    SettingTextField,
    SettingTimeline,
    WorldSetting,
)
from app.models.setting_fields import SettingFieldDefinition, SettingReferenceHandle
from app.schemas.career import CareerCreate, StageIn
from app.schemas.organization import OrganizationCreate
from app.services import career_service, organization_service, project_lifecycle, setting_catalog_service, story_service
from app.services.book_import import reverse
from tests.helpers import Gate, create_ai_config, create_project, parse_sse, purge_trashed_project, register, run_task

KINDS = ("character", "world", "organization", "career")
ENDPOINTS = {"character": "characters", "world": "world", "organization": "organizations", "career": "careers"}
TARGET_COLUMNS = {
    "character": "character_id", "world": "world_setting_id", "organization": "organization_id", "career": "career_id",
}
WIZARD_KIND = {"character": "characters", "world": "world", "career": "careers"}


async def _setup(client):
    await register(client)
    project = await create_project(client)
    return project["id"], f"/api/v1/projects/{project['id']}"


async def _entry(client, base, kind, name="目标"):
    body = {"title": name, "category": "place", "content": "原有正文"} if kind == "world" else {"name": name}
    if kind == "career":
        body["stages"] = [{"name": "第一阶"}]
    response = await client.post(f"{base}/{ENDPOINTS[kind]}", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _reference(app, project_id, source_kind, source, target_kind, target, usage="baseline", label="牵挂"):
    """保留历史的真实存储形状；不依赖尚在并行实现的新 HTTP 写入口。"""
    async with app.state.sessionmaker() as db:
        await project_lifecycle.lock_active(db, project_id)
        timeline = await setting_catalog_service.find_entry_timeline(db, project_id, source_kind, source["id"])
        definition = SettingFieldDefinition(
            project_id=project_id, label=label, field_type="reference",
            applicable_types=["world.place" if source_kind == "world" else source_kind],
            reference_types=["world.place" if target_kind == "world" else target_kind],
        )
        handle = SettingReferenceHandle(
            project_id=project_id, kind=target_kind, **{TARGET_COLUMNS[target_kind]: target["id"]},
        )
        db.add_all([definition, handle])
        await db.flush()
        value = {"reference_handle_id": handle.id}
        field = SettingTextField(
            timeline_id=timeline.id, definition_id=definition.id, label=None,
            base_value=value if usage == "baseline" else None,
        )
        db.add(field)
        await db.flush()
        if usage != "baseline":
            db.add(SettingProgression(
                timeline_id=timeline.id, custom_field_id=field.id, operation="replace", value=value,
                anchor_kind="chapter" if usage == "orphan" else "planned",
                chapter_no=None if usage == "orphan" else 20, active=usage != "inactive",
            ))
            if usage == "replaced":
                db.add(SettingProgression(
                    timeline_id=timeline.id, custom_field_id=field.id, operation="replace", value=None,
                    anchor_kind="planned", chapter_no=21,
                ))
        await db.commit()
        return field.id, handle.id


@pytest.mark.parametrize("kind", KINDS)
async def test_manual_creation_initializes_only_the_entry_timeline(client, app, kind):
    project_id, base = await _setup(client)
    async with app.state.sessionmaker() as db:
        db.add(SettingFieldDefinition(
            project_id=project_id, label="空字段", field_type="line",
            applicable_types=["world.place" if kind == "world" else kind],
        ))
        await db.commit()
    item = await _entry(client, base, kind)
    async with app.state.sessionmaker() as db:
        timeline = await setting_catalog_service.find_entry_timeline(db, project_id, kind, item["id"])
        assert timeline is not None
        assert timeline.subject_kind == ("career_entry" if kind == "career" else kind)
        assert timeline.baseline_status == "confirmed" and timeline.legacy_compat_allowed is False
        if kind in ("organization", "career"):
            assert timeline.baseline == {} and timeline.character_id is None
        elif kind == "world":
            assert timeline.baseline == {"content": "原有正文"}
        else:
            assert timeline.baseline["profile"] == item["profile"]
        assert await db.scalar(select(func.count()).select_from(SettingTextField)) == 0
        assert await db.scalar(select(func.count()).select_from(CharacterCareer)) == 0
        assert await db.scalar(select(func.count()).select_from(OrganizationMember)) == 0


async def test_composite_creation_initializes_independent_timelines_and_rolls_back_together(client, app):
    project_id, base = await _setup(client)
    character = await client.post(f"{base}/characters/with-relations", json={"character": {"name": "角色"}})
    assert character.status_code == 201, character.text
    cid = character.json()["character"]["id"]
    organization = await client.post(f"{base}/organizations/with-members", json={
        "organization": {"name": "门派"}, "members": [{"character_id": cid}],
    })
    assert organization.status_code == 201, organization.text
    careers = await client.post(f"{base}/careers/batch", json={"careers": [
        {"name": "剑修", "stages": [{"name": "第一阶"}]}, {"name": "丹师", "stages": [{"name": "第一阶"}]},
    ]})
    assert careers.status_code == 201, careers.text
    async with app.state.sessionmaker() as db:
        expected = [
            ("character", cid), ("organization", organization.json()["organization"]["id"]),
            *(("career", item["id"]) for item in careers.json()),
        ]
        for kind, entry_id in expected:
            assert await setting_catalog_service.find_entry_timeline(db, project_id, kind, entry_id) is not None
        assert await db.scalar(select(func.count()).select_from(SettingTimeline)) == 5  # 四条目 + 一成员
    failed = await client.post(f"{base}/organizations/with-members", json={
        "organization": {"name": "不保留"}, "members": [{"character_id": cid}, {"character_id": cid}],
    })
    assert failed.status_code == 409, failed.text
    failed = await client.post(f"{base}/careers/batch", json={"careers": [
        {"name": "不保留", "stages": [{"name": "第一阶"}]}, {"name": "剑修", "stages": [{"name": "第一阶"}]},
    ]})
    assert failed.status_code == 409, failed.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(SettingTimeline)) == 5
        assert list(await db.scalars(select(Career.name).order_by(Career.id))) == ["剑修", "丹师"]
        assert list(await db.scalars(select(Organization.name))) == ["门派"]


async def test_creation_primitives_leave_commit_to_the_outer_transaction(client, app):
    project_id, _ = await _setup(client)
    async with app.state.sessionmaker() as db:
        await project_lifecycle.lock_active(db, project_id)
        await story_service.new_character(db, project_id, {"name": "角色"})
        await story_service.new_world(db, project_id, {"title": "世界观", "content": "起始正文"})
        await organization_service.new_org(db, project_id, OrganizationCreate(name="组织"))
        await career_service.new_career(db, project_id, CareerCreate(name="职业", stages=[StageIn(name="第一阶")]))
        assert await db.scalar(select(func.count()).select_from(SettingTimeline)) == 4
        await db.rollback()
    async with app.state.sessionmaker() as db:
        for model in (Character, WorldSetting, Organization, Career, SettingTimeline, SettingTextField):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("usage", ["baseline", "replaced", "inactive", "orphan"])
async def test_old_entity_delete_protects_every_retained_reference(client, app, kind, usage):
    project_id, base = await _setup(client)
    target = await _entry(client, base, kind)
    source = await _entry(client, base, "character", "引用来源")
    field_id, handle_id = await _reference(app, project_id, "character", source, kind, target, usage)
    response = await client.delete(f"{base}/{ENDPOINTS[kind]}/{target['id']}")
    assert response.status_code == 409, response.text
    assert "引用来源" in response.json()["detail"] and "牵挂" in response.json()["detail"]
    assert (await client.get(f"{base}/{ENDPOINTS[kind]}/{target['id']}")).status_code == 200
    async with app.state.sessionmaker() as db:
        assert await db.get(SettingTextField, field_id) is not None
        assert await db.get(SettingReferenceHandle, handle_id) is not None


@pytest.mark.parametrize("kind", KINDS)
async def test_old_entity_delete_removes_only_proven_unused_target_handles(client, app, kind):
    project_id, base = await _setup(client)
    target = await _entry(client, base, kind)
    retained = await _entry(client, base, "character", "保留")
    async with app.state.sessionmaker() as db:
        unused = SettingReferenceHandle(project_id=project_id, kind=kind, **{TARGET_COLUMNS[kind]: target["id"]})
        other = SettingReferenceHandle(project_id=project_id, kind="character", character_id=retained["id"])
        db.add_all([unused, other])
        await db.commit()
        unused_id, other_id = unused.id, other.id
    response = await client.delete(f"{base}/{ENDPOINTS[kind]}/{target['id']}")
    assert response.status_code == 204, response.text
    async with app.state.sessionmaker() as db:
        assert await db.get(SettingReferenceHandle, unused_id) is None
        assert await db.get(SettingReferenceHandle, other_id) is not None


async def test_old_world_category_patch_protects_bound_and_incoming_field_scopes(client, app):
    project_id, base = await _setup(client)
    world = await _entry(client, base, "world")
    async with app.state.sessionmaker() as db:
        timeline = await setting_catalog_service.find_entry_timeline(db, project_id, "world", world["id"])
        definition = SettingFieldDefinition(
            project_id=project_id, label="地点专属", field_type="line", applicable_types=["world.place"],
        )
        db.add(definition)
        await db.flush()
        field = SettingTextField(timeline_id=timeline.id, definition_id=definition.id, label=None, base_value="东海")
        db.add(field)
        await db.commit()
    response = await client.patch(f"{base}/world/{world['id']}", json={"category": "item", "title": "不应保存"})
    assert response.status_code == 409, response.text
    current = (await client.get(f"{base}/world/{world['id']}")).json()
    assert current["category"] == "place" and current["title"] == "目标"
    async with app.state.sessionmaker() as db:
        await db.delete(await db.get(SettingTextField, field.id))
        await db.commit()
    source = await _entry(client, base, "character", "引用来源")
    await _reference(app, project_id, "character", source, "world", world, "inactive")
    response = await client.patch(f"{base}/world/{world['id']}", json={"category": "item"})
    assert response.status_code == 409, response.text
    assert (await client.patch(f"{base}/world/{world['id']}", json={"title": "只改标题"})).status_code == 200


@pytest.mark.parametrize("target_kind", ["character", "world", "career"])
async def test_wizard_admission_checks_incoming_historical_references(client, app, fake_llm, target_kind):
    project_id, base = await _setup(client)
    await create_ai_config(client)
    target = await _entry(client, base, target_kind)
    source_kind = "world" if target_kind == "character" else "character"
    source = await _entry(client, base, source_kind, "引用来源")
    await _reference(app, project_id, source_kind, source, target_kind, target, "orphan")
    response = await client.post(f"{base}/wizard/{WIZARD_KIND[target_kind]}")
    assert response.status_code == 409, response.text
    assert "引用来源" in response.json()["detail"] and "牵挂" in response.json()["detail"]
    assert fake_llm.calls == []
    assert (await client.get(f"{base}/{ENDPOINTS[target_kind]}/{target['id']}")).status_code == 200


@pytest.mark.parametrize("target_kind", ["character", "world", "career"])
async def test_wizard_rechecks_incoming_references_added_during_generation(client, app, fake_llm, target_kind):
    project_id, base = await _setup(client)
    await create_ai_config(client)
    target = await _entry(client, base, target_kind)
    source_kind = "world" if target_kind == "character" else "character"
    source = await _entry(client, base, source_kind, "引用来源")
    raw = [{"title": "新条目"}] if target_kind == "world" else [{"name": "新条目", "stages": [{"name": "第一阶"}]}]
    gate = Gate()
    fake_llm.push([gate, json.dumps(raw, ensure_ascii=False)])
    response = await client.post(f"{base}/wizard/{WIZARD_KIND[target_kind]}")
    assert response.status_code == 202, response.text
    await gate.wait_reached()
    try:
        await _reference(app, project_id, source_kind, source, target_kind, target, "inactive")
    finally:
        gate.release()
    events = parse_sse((await client.get(f"/api/v1/tasks/{response.json()['id']}/stream")).text)
    assert events[-1][0] == "error", events[-1]
    assert "引用来源" in events[-1][1]["detail"] and "牵挂" in events[-1][1]["detail"]
    assert (await client.get(f"{base}/{ENDPOINTS[target_kind]}/{target['id']}")).status_code == 200


@pytest.mark.parametrize("target_kind", ["character", "world", "career"])
async def test_empty_shared_slots_allow_wizard_replacement_and_initialize_new_entries(
    client, app, fake_llm, target_kind,
):
    project_id, base = await _setup(client)
    await create_ai_config(client)
    old = await _entry(client, base, target_kind)
    async with app.state.sessionmaker() as db:
        db.add(SettingFieldDefinition(
            project_id=project_id, label="空槽", field_type="text",
            applicable_types=["world.place" if target_kind == "world" else target_kind],
        ))
        db.add(SettingReferenceHandle(
            project_id=project_id, kind=target_kind, **{TARGET_COLUMNS[target_kind]: old["id"]},
        ))
        await db.commit()
    raw = [{"title": "新条目", "category": "place"}] if target_kind == "world" else [
        {"name": "新条目", "stages": [{"name": "第一阶"}]},
    ]
    fake_llm.push(json.dumps(raw, ensure_ascii=False))
    task, events = await run_task(client, await client.post(f"{base}/wizard/{WIZARD_KIND[target_kind]}"))
    assert task["status"] == "succeeded", task
    new = events[-1][1]["result"]["items"][0]
    async with app.state.sessionmaker() as db:
        assert await setting_catalog_service.find_entry_timeline(db, project_id, target_kind, new["id"]) is not None
        assert await db.scalar(select(func.count()).select_from(SettingTextField)) == 0
        assert await db.scalar(select(func.count()).select_from(SettingReferenceHandle)) == 0


async def test_book_reverse_initializes_all_entry_types_and_preserves_same_name_values(client, app):
    project_id, base = await _setup(client)
    existing = await _entry(client, base, "world", "旧地点")
    final = {
        "characters": [{"name": "新角色", "profile": "角色起始资料"}],
        "organizations": [{"name": "新组织", "members": [{"name": "新角色"}]}],
        "careers": [{"name": "新职业", "stages": [{"name": "起始阶段"}]}],
        "world": [{"title": "新地点", "content": "地点起始资料"}, {"title": "旧地点", "content": "不覆盖"}],
    }
    cp = {"stats": {key: 0 for key in reverse.STAT_KEYS}, "warnings": []}
    async with app.state.sessionmaker() as db:
        project = await project_lifecycle.lock_active(db, project_id)
        await reverse.apply_final(db, project, final, cp)
        timelines = list(await db.scalars(select(SettingTimeline).where(SettingTimeline.project_id == project_id)))
        assert {row.subject_kind for row in timelines} == {
            "character", "world", "organization", "career_entry", "membership",
        }
        assert all(row.baseline_status == "confirmed" for row in timelines)
        assert await db.scalar(select(func.count()).select_from(SettingTextField)) == 0
        assert (await db.get(WorldSetting, existing["id"])).content == "原有正文"
        await db.rollback()
    async with app.state.sessionmaker() as db:
        assert list(await db.scalars(select(WorldSetting.id))) == [existing["id"]]
        assert await db.scalar(select(func.count()).select_from(SettingTimeline)) == 1
        assert await db.scalar(select(func.count()).select_from(Project)) == 1


async def test_project_soft_delete_restore_and_purge_preserve_then_remove_the_reference_graph(
    client, app, recycle_admin,
):
    project_id, base = await _setup(client)
    entries = {kind: await _entry(client, base, kind, f"环中{kind}") for kind in KINDS}
    for index, source_kind in enumerate(KINDS):
        target_kind = KINDS[(index + 1) % len(KINDS)]
        await _reference(
            app, project_id, source_kind, entries[source_kind], target_kind, entries[target_kind],
            "inactive" if index == 3 else "baseline", label=f"关联{index}",
        )
    async with app.state.sessionmaker() as db:
        before = {
            model.__tablename__: list((await db.execute(select(model.__table__).order_by(model.id))).all())
            for model in (SettingFieldDefinition, SettingReferenceHandle, SettingTextField, SettingProgression)
        }
    assert (await client.delete(base)).status_code == 204
    assert (await client.get(base)).status_code == 404
    listing = await recycle_admin.get("/api/v1/admin/recycle-bin/projects")
    assert listing.status_code == 200, listing.text
    recycled = next(item for item in listing.json()["items"] if item["id"] == project_id)
    restored = await recycle_admin.post(f"/api/v1/admin/recycle-bin/projects/{project_id}/restore", json={
        "deleted_at": recycled["deleted_at"], "confirm": True,
    })
    assert restored.status_code == 204, restored.text
    async with app.state.sessionmaker() as db:
        for model in (SettingFieldDefinition, SettingReferenceHandle, SettingTextField, SettingProgression):
            rows = list((await db.execute(select(model.__table__).order_by(model.id))).all())
            assert rows == before[model.__tablename__]
    assert (await client.delete(base)).status_code == 204
    await purge_trashed_project(recycle_admin, project_id)
    async with app.state.sessionmaker() as db:
        for model in (Project, SettingFieldDefinition, SettingReferenceHandle, SettingTimeline, SettingTextField,
                      SettingProgression, Character, WorldSetting, Organization, Career):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
