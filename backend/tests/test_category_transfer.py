"""分类 v5 完整备份：真实旧结构、严格版本闸门、引用图与提交阶段回滚。"""

import asyncio
import json
import sqlite3
import threading
import time
from copy import deepcopy

import pytest
from sqlalchemy import func, select, update

from app.models import (
    Career,
    Character,
    Organization,
    Project,
    SettingCategory,
    SettingCategoryRule,
    SettingFieldDefinition,
    SettingFieldOption,
    WorldSetting,
)
from app.services import setting_category_service as categories
from app.services.backups.database import snapshot_database
from app.services.project_transfer import importer
from tests.helpers import create_project
from tests.test_project_transfer import (
    _backup,
    _canonical,
    _import,
    _preview_raw,
    _setup,
    _typed_fields_backup,
    _without_ai_settings,
)
from tests.test_recycle_bin import item as recycle_item
from tests.test_setting_fields_postgresql import (
    postgres_fields_app as postgres_fields_app,
)
from tests.test_setting_fields_postgresql import (
    postgres_fields_client as postgres_fields_client,
)

ENTITY_TABLES = ("characters", "world_settings", "organizations", "careers")
CATEGORY_TABLES = ("setting_categories", "setting_category_rules")


async def _category_backup(client, app):
    project, _ = await _typed_fields_backup(client, app)
    async with app.state.sessionmaker() as db:
        for model in (Character, WorldSetting, Organization, Career):
            for entity in await db.scalars(select(model).where(model.project_id == project["id"])):
                entity.setting_tags = ["同盟", model.__tablename__]
        definition = await db.scalar(select(SettingFieldDefinition).where(
            SettingFieldDefinition.project_id == project["id"], SettingFieldDefinition.field_type == "dropdown",
        ))
        option = await db.scalar(select(SettingFieldOption).where(
            SettingFieldOption.definition_id == definition.id, SettingFieldOption.archived.is_(True),
        ))
        definition.archived = True
        category = SettingCategory(project_id=project["id"], name="同盟", match_mode="any", sort=7,
                                   applicable_types=["character", "world.place", "organization", "career"])
        db.add(category)
        await db.flush()
        db.add_all([
            SettingCategoryRule(category_id=category.id, kind="tag", tag="同盟", sort=1),
            SettingCategoryRule(category_id=category.id, kind="dropdown", definition_id=definition.id,
                                option_id=option.id, sort=2),
        ])
        (await db.get(Project, project["id"])).categories_revision = 73
        await db.commit()
    return project, await _backup(client, project["id"])


def _old_backup(original, version):
    data = deepcopy(original)
    _without_ai_settings(data, version)
    for table in CATEGORY_TABLES:
        data["tables"].pop(table, None)
    for table in ENTITY_TABLES:
        for row in data["tables"][table]:
            row.pop("setting_tags", None)
    if version < 4:
        for table in importer.FIELD_TABLES:
            data["tables"].pop(table, None)
        data["tables"]["setting_timelines"] = [row for row in data["tables"]["setting_timelines"]
            if row["subject_kind"] not in ("organization", "career_entry")]
        for row in data["tables"]["setting_text_fields"]:
            row.pop("definition_id", None)
    if version == 1:
        for table in ("setting_timelines", "setting_text_fields", "setting_progressions"):
            data["tables"].pop(table, None)
    return data


async def test_categories_v5_roundtrip_preserves_tags_rules_and_resets_both_revisions(client, app):
    project, original = await _category_backup(client, app)
    assert (original["version"], original["app_schema"]) == (6, "0038")
    assert not {"fields_revision", "categories_revision"}.intersection(original["project"])
    imported = await _import(client, original)
    result = await _backup(client, imported["project"]["id"])
    assert _canonical(original) == _canonical(result)
    for table in CATEGORY_TABLES:
        assert {row["id"] for row in original["tables"][table]}.isdisjoint(
            row["id"] for row in result["tables"][table])
    rule = next(row for row in result["tables"]["setting_category_rules"] if row["kind"] == "dropdown")
    definition = next(row for row in result["tables"]["setting_field_definitions"] if row["field_type"] == "dropdown")
    option = next(row for row in result["tables"]["setting_field_options"] if row["archived"])
    assert rule["definition_id"] == definition["id"] and rule["option_id"] == option["id"]
    assert rule["category_id"] == result["tables"]["setting_categories"][0]["id"]
    for table in ENTITY_TABLES:
        assert [row["setting_tags"] for row in result["tables"][table]] == [
            row["setting_tags"] for row in original["tables"][table]]
    async with app.state.sessionmaker() as db:
        source = await db.get(Project, project["id"])
        target = await db.get(Project, imported["project"]["id"])
        assert (source.fields_revision, source.categories_revision) == (42, 73)
        assert (target.fields_revision, target.categories_revision) == (0, 0)
        await categories.validate_project_category_graph(db, target.id)


@pytest.mark.parametrize("version", [1, 2, 3, 4])
async def test_real_old_backups_gain_only_empty_category_state(client, app, version):
    if version == 4:
        project, original = await _typed_fields_backup(client, app)
    else:
        project, _ = await _setup(client, app)
        original = await _backup(client, project["id"])
    data = _old_backup(original, version)
    # 运行代次不属于语义，不信任来源，也不因来源形状阻止合法旧版导入。
    data["project"].update(fields_revision={"ignored": True}, categories_revision=None)
    imported = await _import(client, data)
    result = await _backup(client, imported["project"]["id"])
    assert result["version"] == 6
    assert all(result["tables"][table] == [] for table in CATEGORY_TABLES)
    assert all(row["setting_tags"] == [] for table in ENTITY_TABLES for row in result["tables"][table])
    if version == 4:
        assert len(result["tables"]["setting_progressions"]) == len(original["tables"]["setting_progressions"])


@pytest.mark.parametrize("version", [1, 2, 3, 4])
async def test_old_backups_reject_future_category_payload_or_non_array_empty(client, app, version):
    project, _ = await _setup(client, app)
    original = _old_backup(await _backup(client, project["id"]), version)
    for value in (None, {}, False, "", ["同盟"], [1]):
        data = deepcopy(original)
        data["tables"]["characters"][0]["setting_tags"] = value
        response = await _preview_raw(client, json.dumps(data).encode())
        assert response.status_code == 400, (version, value, response.text)
        assert "旧版" in response.json()["detail"]
    for table in CATEGORY_TABLES:
        for value in (None, {}, False, [{"id": 1}]):
            data = deepcopy(original)
            data["tables"][table] = value
            response = await _preview_raw(client, json.dumps(data).encode())
            assert response.status_code == 400, (version, table, value, response.text)
            assert "旧版" in response.json()["detail"]
    empty = deepcopy(original)
    empty["tables"].update({table: [] for table in CATEGORY_TABLES})
    for table in ENTITY_TABLES:
        for row in empty["tables"][table]:
            row["setting_tags"] = []
    assert (await _preview_raw(client, json.dumps(empty).encode())).status_code == 200


async def test_categories_v5_preview_rejects_missing_core_and_invalid_graph(client, app):
    _, original = await _category_backup(client, app)
    variants = []
    for table in CATEGORY_TABLES:
        missing = deepcopy(original)
        missing["tables"].pop(table)
        variants.append(missing)
    for value in ([], ["world.history"]):
        bad = deepcopy(original)
        bad["tables"]["setting_categories"][0]["applicable_types"] = value
        variants.append(bad)
    for value in (None, [" 同盟"], ["同盟", "同盟"]):
        bad = deepcopy(original)
        bad["tables"]["characters"][0]["setting_tags"] = value
        variants.append(bad)
    bad = deepcopy(original)
    bad["tables"]["setting_category_rules"] = []
    variants.append(bad)
    bad = deepcopy(original)
    bad["tables"]["setting_category_rules"][1]["option_id"] = 2147483647
    variants.append(bad)
    bad = deepcopy(original)
    bad["tables"]["setting_category_rules"][0]["definition_id"] = bad["tables"]["setting_field_definitions"][0]["id"]
    variants.append(bad)
    bad = deepcopy(original)
    line = next(row for row in bad["tables"]["setting_field_definitions"] if row["field_type"] == "line")
    bad["tables"]["setting_category_rules"][1]["definition_id"] = line["id"]
    variants.append(bad)
    bad = deepcopy(original)
    definition = next(row for row in bad["tables"]["setting_field_definitions"] if row["field_type"] == "dropdown")
    other = {**definition, "id": 2147483646, "label": "另一组下拉"}
    bad["tables"]["setting_field_definitions"].append(other)
    bad["tables"]["setting_field_options"].append(
        {"id": 2147483646, "definition_id": other["id"], "label": "另一组选项"}
    )
    bad["tables"]["setting_category_rules"][1]["option_id"] = 2147483646
    variants.append(bad)
    bad = deepcopy(original)
    rule = bad["tables"]["setting_category_rules"][0]
    bad["tables"]["setting_category_rules"] = [{**rule, "id": 1000 + index} for index in range(21)]
    variants.append(bad)
    for data in variants:
        response = await _preview_raw(client, json.dumps(data).encode())
        assert response.status_code == 400, response.text
        assert response.json()["errors"]


async def _rollback_and_retry(client, app, monkeypatch):
    _, data = await _category_backup(client, app)
    preview = await _preview_raw(client, json.dumps(data).encode())
    assert preview.status_code == 200, preview.text
    token = preview.json()["token"]
    original = categories.validate_project_category_graph
    corrupt_once = True
    reached_deferred_flush = False

    async def corrupt_after_validation(db, project_id):
        nonlocal corrupt_once, reached_deferred_flush
        await original(db, project_id)
        if corrupt_once:
            corrupt_once = False
            ids = select(SettingCategory.id).where(SettingCategory.project_id == project_id)
            await db.execute(update(SettingCategoryRule).where(
                SettingCategoryRule.category_id.in_(ids), SettingCategoryRule.kind == "dropdown",
            ).values(option_id=2147483647))
            await db.flush()
            reached_deferred_flush = True

    monkeypatch.setattr(categories, "validate_project_category_graph", corrupt_after_validation)
    failed = await client.post("/api/v1/projects/import", json={"token": token})
    assert failed.status_code == 400, failed.text
    assert reached_deferred_flush
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Project)) == 1
        assert await db.scalar(select(func.count()).select_from(SettingCategory)) == 1
        assert await db.scalar(select(func.count()).select_from(SettingCategoryRule)) == 2
    retry = await client.post("/api/v1/projects/import", json={"token": token})
    assert retry.status_code == 201, retry.text


async def test_category_import_commit_failure_rolls_back_and_keeps_preview_token(client, app, monkeypatch):
    await _rollback_and_retry(client, app, monkeypatch)


async def test_postgresql_category_backup_roundtrip(postgres_fields_client, postgres_fields_app):
    await test_categories_v5_roundtrip_preserves_tags_rules_and_resets_both_revisions(
        postgres_fields_client, postgres_fields_app,
    )


async def test_postgresql_category_import_deferred_commit_failure(
    postgres_fields_client, postgres_fields_app, monkeypatch,
):
    await _rollback_and_retry(postgres_fields_client, postgres_fields_app, monkeypatch)


async def test_category_graph_survives_native_whole_sqlite_backup(client, app, tmp_path):
    _, data = await _category_backup(client, app)
    destination = tmp_path / "category-snapshot.sqlite3"
    result = await asyncio.to_thread(
        snapshot_database, str(app.state.engine.url), destination,
        deadline=time.monotonic() + 10, stop_event=threading.Event(),
    )
    assert result.engine == "sqlite" and result.schema_revision is None  # create_all 不捏造迁移事实。
    with sqlite3.connect(destination) as restored:
        assert restored.execute("PRAGMA foreign_key_check").fetchall() == []
        for table in CATEGORY_TABLES:
            assert restored.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == len(data["tables"][table])
        for table in ENTITY_TABLES:
            values = restored.execute(f"SELECT setting_tags FROM {table} ORDER BY id").fetchall()
            assert [json.loads(row[0]) for row in values] == [row["setting_tags"] for row in data["tables"][table]]


async def test_category_graph_soft_restore_and_purge_preserve_then_remove_whole_graph(client, app, recycle_admin):
    project, data = await _category_backup(client, app)
    pid = project["id"]
    other = await create_project(client, title="保留作品")
    assert (await client.delete(f"/api/v1/projects/{pid}")).status_code == 204
    row = await recycle_item(recycle_admin, pid)
    async with app.state.sessionmaker() as db:
        await categories.validate_project_category_graph(db, pid)
        assert await db.scalar(select(func.count()).select_from(SettingCategory)) == 1
    payload = {"deleted_at": row["deleted_at"], "confirm": True}
    assert (
        await recycle_admin.post(f"/api/v1/admin/recycle-bin/projects/{pid}/restore", json=payload)
    ).status_code == 204
    assert _canonical(await _backup(client, pid)) == _canonical(data)
    assert (await client.delete(f"/api/v1/projects/{pid}")).status_code == 204
    row = await recycle_item(recycle_admin, pid)
    response = await recycle_admin.post(f"/api/v1/admin/recycle-bin/projects/{pid}/purge", json={
        "deleted_at": row["deleted_at"], "confirm": True, "title": row["title"],
    })
    assert response.status_code == 204, response.text
    async with app.state.sessionmaker() as db:
        assert await db.get(Project, pid) is None and await db.get(Project, other["id"]) is not None
        assert await db.scalar(select(func.count()).select_from(SettingCategory)) == 0
        assert await db.scalar(select(func.count()).select_from(SettingCategoryRule)) == 0
