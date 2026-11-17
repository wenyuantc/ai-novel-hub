"""覆盖向导应安全处理缩略图复合外键及归属。"""

import json

import pytest
from sqlalchemy import select, update

from app.models.setting_images import SettingImageResource, SettingThumbnailBinding
from app.services import wizard_service
from tests.helpers import create_ai_config, create_project, register, run_task
from tests.test_setting_thumbnails import image_data

KINDS = {
    "characters": ("characters", "character", "character_id", {"name": "旧角色"},
                   {"characters": [{"name": "新角色"}]}),
    "world": ("world", "world", "world_setting_id", {"title": "旧地点", "category": "place"},
              [{"title": "新地点", "category": "place"}]),
    "careers": ("careers", "career", "career_id", {"name": "旧职业", "stages": [{"name": "一阶"}]},
                {"careers": [{"name": "新职业", "stages": [{"name": "一阶"}]}]}),
}


async def _entity(client, project_id, kind):
    path, _, _, body, _ = KINDS[kind]
    response = await client.post(f"/api/v1/projects/{project_id}/{path}", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _upload(client, project_id, kind, entity_id):
    _, image_kind, _, _, _ = KINDS[kind]
    response = await client.put(
        f"/api/v1/projects/{project_id}/settings/{image_kind}/{entity_id}/thumbnail",
        files={"file": ("test.png", image_data(), "image/png")}, data={"expected_version": ""},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _binding(app, project_id):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(SettingThumbnailBinding).where(SettingThumbnailBinding.project_id == project_id))


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("has_image", [False, True])
async def test_wizard_replaces_local_settings_and_keeps_retained_images(app, client, fake_llm, kind, has_image):
    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    other = await create_project(client, title="另一部作品")
    old = await _entity(client, project["id"], kind)
    other_id = await _entity(client, other["id"], kind)
    await _upload(client, other["id"], kind, other_id)
    other_binding = await _binding(app, other["id"])
    old_resource = None
    if has_image:
        await _upload(client, project["id"], kind, old)
        old_resource = (await _binding(app, project["id"])).image_resource_id

    path, image_kind, _, _, model_result = KINDS[kind]
    fake_llm.push(json.dumps(model_result, ensure_ascii=False))
    task, _ = await run_task(client, await client.post(f"/api/v1/projects/{project['id']}/wizard/{kind}"))
    assert task["status"] == "succeeded", task
    rows = (await client.get(f"/api/v1/projects/{project['id']}/{path}")).json()
    assert len(rows) == 1
    assert rows[0]["thumbnail"] is None
    assert rows[0].get("name", rows[0].get("title")) == {
        "characters": "新角色", "world": "新地点", "careers": "新职业",
    }[kind]
    other_route = f"/api/v1/projects/{other['id']}/settings/{image_kind}/{other_id}/thumbnail"
    assert (await client.get(other_route)).status_code == 200
    async with app.state.sessionmaker() as db:
        assert await db.get(SettingThumbnailBinding, other_binding.id) is not None
        if old_resource:
            # 撤销或历史记录可能仍引用不可变资源；只删除旧主体上的绑定。
            assert await db.get(SettingImageResource, old_resource) is not None
            assert await db.scalar(select(SettingThumbnailBinding).where(
                SettingThumbnailBinding.image_resource_id == old_resource,
            )) is None


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("corruption", ["foreign_subject", "foreign_resource"])
async def test_wizard_rejects_corrupt_cross_project_binding(app, client, kind, corruption):
    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    other = await create_project(client, title="另一部作品")
    local_id = await _entity(client, project["id"], kind)
    other_id = await _entity(client, other["id"], kind)
    await _upload(client, other["id"], kind, other_id)
    foreign_binding = await _binding(app, other["id"])
    if corruption == "foreign_resource":
        await _upload(client, project["id"], kind, local_id)
        local_binding = await _binding(app, project["id"])
        binding_id = local_binding.id
        values = {"image_resource_id": foreign_binding.image_resource_id}
    else:
        binding_id = foreign_binding.id
        values = {KINDS[kind][2]: local_id}

    # 构造外键关闭时遗留的损坏数据，复原约束后验证防御性覆盖检查。
    async with app.state.engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await conn.execute(
            update(SettingThumbnailBinding).where(SettingThumbnailBinding.id == binding_id).values(**values)
        )
        await conn.commit()
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/{kind}")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "wizard_reference_invalid"
    path = KINDS[kind][0]
    assert (await client.get(f"/api/v1/projects/{project['id']}/{path}/{local_id}")).status_code == 200
    async with app.state.sessionmaker() as db:
        binding = await db.get(SettingThumbnailBinding, binding_id)
        assert binding is not None
        assert await db.get(SettingImageResource, foreign_binding.image_resource_id) is not None


async def test_wizard_still_rejects_unreviewed_cascade(client, monkeypatch):
    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    graph = wizard_service._wizard_delete_fk_graph
    monkeypatch.setattr(wizard_service, "_wizard_delete_fk_graph", lambda: graph() | {
        ("characters", "unreviewed_reference", "character_id", "id", "CASCADE"),
    })
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/characters")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "wizard_reference_invalid"


@pytest.mark.parametrize("kind", ["characters", "world"])
async def test_wizard_preserves_series_managed_setting_and_image(client, kind):
    from tests.test_series_transfer import _join_books

    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    subject_id = await _entity(client, project["id"], kind)
    await _upload(client, project["id"], kind, subject_id)
    route, series = await _join_books(client, project)
    series_kind = KINDS[kind][1]
    impact = await client.get(f"{route}/impact", params={
        "action": "promote", "project_id": project["id"], "kind": series_kind, "entry_id": subject_id,
    })
    assert impact.status_code == 200, impact.text
    promoted = await client.post(f"{route}/entries/promote", json={
        "project_id": project["id"], "ref": {"kind": series_kind, "id": subject_id},
        "expected_revision": series["revision"], "source_stamp": impact.json()["source_stamp"],
    })
    assert promoted.status_code == 201, promoted.text
    shared = f"{route}/entries/{promoted.json()['item']['id']}/thumbnail"
    image = await client.get(shared)
    assert image.status_code == 200
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/{kind}")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "series_setting_managed"
    assert (await client.get(shared)).content == image.content
    local = f"/api/v1/projects/{project['id']}/settings/{series_kind}/{subject_id}/thumbnail"
    assert (await client.get(local)).content == image.content


async def test_agent_undo_of_prior_deletion_restores_image_after_wizard_replacement(app, client, fake_llm):
    from app.services.agent import effects

    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    project_id = project["id"]
    old_id = await _entity(client, project_id, "characters")
    # SQLite 时间线主键可复用：保留较大 id 避免掩盖本测试的资源恢复行为。
    other = await create_project(client, title="其他作品")
    await _entity(client, other["id"], "characters")
    await _upload(client, project_id, "characters", old_id)
    image_url = f"/api/v1/projects/{project_id}/settings/character/{old_id}/thumbnail"
    original = await client.get(image_url)
    assert original.status_code == 200
    async with app.state.sessionmaker() as db:
        before = await effects.snapshot(db, project_id)
    deleted = await client.delete(f"/api/v1/projects/{project_id}/characters/{old_id}")
    assert deleted.status_code == 204, deleted.text
    async with app.state.sessionmaker() as db:
        after = await effects.snapshot(db, project_id)
    delta = effects.diff(before, after)
    assert any(effect["table"] == "setting_thumbnail_bindings" for effect in delta)
    fake_llm.push(json.dumps(KINDS["characters"][4], ensure_ascii=False))
    task, _ = await run_task(client, await client.post(f"/api/v1/projects/{project_id}/wizard/characters"))
    assert task["status"] == "succeeded", task
    async with app.state.sessionmaker() as db:
        await effects.undo(db, project_id, json.loads(json.dumps(delta)))
        await db.commit()
    restored = await client.get(image_url)
    assert restored.status_code == 200 and restored.content == original.content
    rows = (await client.get(f"/api/v1/projects/{project_id}/characters")).json()
    assert {row["name"] for row in rows} == {"旧角色", "新角色"}
    assert next(row for row in rows if row["name"] == "新角色")["thumbnail"] is None


async def test_wizard_rechecks_image_ownership_after_generation(app, client, fake_llm):
    from tests.helpers import Gate

    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    other = await create_project(client, title="其他作品")
    subject_id = await _entity(client, project["id"], "characters")
    other_id = await _entity(client, other["id"], "characters")
    await _upload(client, other["id"], "characters", other_id)
    foreign = await _binding(app, other["id"])
    gate = Gate()
    fake_llm.push([gate, json.dumps(KINDS["characters"][4], ensure_ascii=False)])
    response = await client.post(f"/api/v1/projects/{project['id']}/wizard/characters")
    assert response.status_code == 202, response.text
    await gate.wait_reached()
    try:
        async with app.state.engine.connect() as conn:
            await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
            await conn.execute(update(SettingThumbnailBinding).where(
                SettingThumbnailBinding.id == foreign.id,
            ).values(character_id=subject_id))
            await conn.commit()
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    finally:
        gate.release()
    task, _ = await run_task(client, response)
    assert task["status"] == "failed", task
    rows = (await client.get(f"/api/v1/projects/{project['id']}/characters")).json()
    assert [row["id"] for row in rows] == [subject_id]
    async with app.state.sessionmaker() as db:
        assert await db.get(SettingThumbnailBinding, foreign.id) is not None
        assert await db.get(SettingImageResource, foreign.image_resource_id) is not None


async def _assert_wizard_thumbnail_rollback(app, client, fake_llm, monkeypatch):
    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    project_id = project["id"]
    old_id = await _entity(client, project_id, "characters")
    await _upload(client, project_id, "characters", old_id)
    binding = await _binding(app, project_id)
    image_url = f"/api/v1/projects/{project_id}/settings/character/{old_id}/thumbnail"
    before = await client.get(image_url)
    assert before.status_code == 200

    async def fail_after_insertion(*_args, **_kwargs):
        raise RuntimeError("注入保存失败")

    monkeypatch.setattr(wizard_service, "_save_character_relations", fail_after_insertion)
    fake_llm.push(json.dumps(KINDS["characters"][4], ensure_ascii=False))
    task, _ = await run_task(client, await client.post(f"/api/v1/projects/{project_id}/wizard/characters"))
    assert task["status"] == "failed", task
    rows = (await client.get(f"/api/v1/projects/{project_id}/characters")).json()
    assert [row["id"] for row in rows] == [old_id]
    after = await client.get(image_url)
    assert after.status_code == 200 and after.content == before.content
    async with app.state.sessionmaker() as db:
        restored = await db.get(SettingThumbnailBinding, binding.id)
        assert restored is not None and restored.image_resource_id == binding.image_resource_id
        assert await db.get(SettingImageResource, binding.image_resource_id) is not None


async def test_failed_wizard_replacement_rolls_back_thumbnail(app, client, fake_llm, monkeypatch):
    await _assert_wizard_thumbnail_rollback(app, client, fake_llm, monkeypatch)
