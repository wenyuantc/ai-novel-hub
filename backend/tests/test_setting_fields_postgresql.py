"""显式启用的真实 PostgreSQL 服务层验证：每个测试独占随机 schema。"""

import asyncio
import json
import os
import uuid

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import main
from app.models import (
    Project,
    SettingFieldDefinition,
    SettingFieldOption,
    SettingProgression,
    SettingReferenceHandle,
    SettingTextField,
    SettingTimeline,
    WritingStyle,
)
from app.schemas.setting_fields import EntryFieldsOut
from app.services import authorization_service, retrieval_service
from app.services import setting_field_service as fields_service
from app.services.mentions.matcher import cache as mention_cache
from app.services.project_transfer import importer
from app.services.project_transfer.registry import RULES
from app.services.setting_field_formatting import format_entry_fields
from tests.helpers import create_project, make_settings, register
from tests.schema_helpers import create_isolated_schema
from tests.test_project_transfer import _backup, _canonical, _import, _preview_raw, _typed_fields_backup


@pytest.fixture
async def postgres_fields_app(tmp_path, fake_llm, monkeypatch):
    database_url = os.environ.get("AINOVEL_FIELDS_POSTGRES_TEST_URL")
    if not database_url:
        pytest.skip("设置 AINOVEL_FIELDS_POSTGRES_TEST_URL 才运行隔离 PostgreSQL 服务验证")
    parsed = make_url(database_url)
    if (parsed.drivername != "postgresql+asyncpg" or parsed.host != "127.0.0.1"
            or parsed.database != "fields_verify" or parsed.username != "field_test"):
        raise ValueError("字段服务测试只允许本机 field_test/fields_verify 专用测试数据库")
    schema = f"setting_fields_api_test_{uuid.uuid4().hex}"
    admin = create_async_engine(database_url)
    engine = create_async_engine(database_url, connect_args={"server_settings": {"search_path": schema}})
    created = False
    application = None
    retrieval_service.invalidate()
    mention_cache.clear()
    importer.clear_tokens()
    try:
        async with admin.begin() as conn:
            assert await conn.scalar(text("SELECT current_database()")) == "fields_verify"
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with engine.begin() as conn:
            assert await conn.scalar(text("SELECT current_schema()")) == schema
            await conn.run_sync(create_isolated_schema)
        monkeypatch.setattr(main, "create_engine", lambda _url: engine)
        application = main.create_app(
            make_settings(tmp_path, database_url=database_url),
            provider_factory=fake_llm.factory,
            start_task_engine=False,
        )
        await authorization_service.initialize(application.state.sessionmaker)
        await application.state.system_settings.load()
        yield application
    finally:
        if application is not None:
            await application.state.system_settings.close()
        await engine.dispose()
        try:
            if created:
                async with admin.begin() as conn:
                    await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await admin.dispose()
            retrieval_service.invalidate()
            mention_cache.clear()
            importer.clear_tokens()


@pytest.fixture
async def postgres_fields_client(postgres_fields_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=postgres_fields_app), base_url="http://testserver"
    ) as client:
        yield client


async def test_postgresql_field_api_cas_read_guard_and_failed_first_event(
    postgres_fields_app, postgres_fields_client, monkeypatch,
):
    app, client = postgres_fields_app, postgres_fields_client
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    characters = []
    for name in ("起点", "目标甲", "目标乙", "待首次变化"):
        response = await client.post(f"{base}/characters", json={"name": name})
        assert response.status_code == 201, response.text
        characters.append(response.json())
    created = await client.post(f"{base}/setting-field-definitions", json={
        "expected_fields_revision": 0, "label": "牵挂", "field_type": "reference",
        "applicable_types": ["character"], "reference_types": ["character"],
    })
    assert created.status_code == 201, created.text
    definition_url = f"{base}/setting-field-definitions/{created.json()['item']['id']}"
    concurrent = await asyncio.gather(*[
        client.patch(definition_url, json={"expected_fields_revision": 1, "label": label})
        for label in ("心念", "归处")
    ])
    assert sorted(response.status_code for response in concurrent) == [200, 409]
    conflict = next(response.json() for response in concurrent if response.status_code == 409)
    assert (conflict["code"], conflict["scope"], conflict["current_revision"]) == ("stale_revision", "fields", 2)
    fields_url = f"{base}/setting-entries/character/{characters[0]['id']}/fields"
    initial = (await client.get(fields_url)).json()
    baseline_url = f"{fields_url}/{created.json()['item']['id']}/baseline"
    concurrent = await asyncio.gather(*[
        client.patch(baseline_url, json={
            "expected_fields_revision": 2, "expected_timeline_revision": initial["timeline_revision"],
            "field_type": "reference", "value": {"kind": "character", "id": target["id"]},
        })
        for target in characters[1:3]
    ])
    assert sorted(response.status_code for response in concurrent) == [200, 409]
    winner = next(response.json() for response in concurrent if response.status_code == 200)
    conflict = next(response.json() for response in concurrent if response.status_code == 409)
    assert (conflict["code"], conflict["scope"]) == ("stale_revision", "timeline")
    assert winner["timeline_revision"] == initial["timeline_revision"] + 1
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(SettingTextField)) == 1
        assert await db.scalar(select(func.count()).select_from(SettingReferenceHandle)) == 1
        before = await db.scalar(select(Project.fields_revision).where(Project.id == project["id"]))
    # 编码后才发现章锚失效，绑定、句柄、事件和版本仍必须一起回滚。
    other = next(c for c in characters[1:3] if c["id"] != winner["fields"][0]["value"]["id"])
    empty_url = f"{base}/setting-entries/character/{characters[3]['id']}/fields"
    empty = (await client.get(empty_url)).json()
    failed = await client.post(f"{empty_url}/{created.json()['item']['id']}/progressions", json={
        "expected_fields_revision": 2, "expected_timeline_revision": empty["timeline_revision"],
        "field_type": "reference", "value": {"kind": "character", "id": other["id"]},
        "operation": "replace", "anchor_kind": "chapter", "chapter_id": 2147483647,
    })
    assert failed.status_code == 400, failed.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(SettingTextField)) == 1
        assert await db.scalar(select(func.count()).select_from(SettingReferenceHandle)) == 1
        assert await db.scalar(select(func.count()).select_from(SettingProgression)) == 0
        assert await db.scalar(select(Project.fields_revision).where(Project.id == project["id"])) == before
        assert await db.scalar(select(SettingTimeline.revision).where(
            SettingTimeline.id == empty["timeline_id"],
        )) == empty["timeline_revision"]

    # 真正的第二个 HTTP/数据库事务在字段读取中间提交配置，不能返回旧数据配新版本。
    metadata_seen, release = asyncio.Event(), asyncio.Event()
    original_metadata = fields_service._metadata
    reader = None

    async def blocked_metadata(db, project_id):
        result = await original_metadata(db, project_id)
        if asyncio.current_task() is reader and not metadata_seen.is_set():
            metadata_seen.set()
            await release.wait()
        return result

    monkeypatch.setattr(fields_service, "_metadata", blocked_metadata)
    reader = asyncio.create_task(client.get(fields_url))
    try:
        await asyncio.wait_for(metadata_seen.wait(), 5)
        renamed = await client.patch(definition_url, json={"expected_fields_revision": 2, "label": "所念"})
        assert renamed.status_code == 200, renamed.text
    finally:
        release.set()
    rejected = await asyncio.wait_for(reader, 5)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["scope"] == "fields"
    refreshed = (await client.get(fields_url)).json()
    assert refreshed["fields_revision"] == 3 and refreshed["fields"][0]["label"] == "所念"


async def test_postgresql_v4_import_remaps_history_and_rolls_back_late_failure(
    postgres_fields_app, postgres_fields_client, monkeypatch,
):
    app, client = postgres_fields_app, postgres_fields_client
    project, data = await _typed_fields_backup(client, app)
    imported = await _import(client, data)
    result = await _backup(client, imported["project"]["id"])
    assert _canonical(data) == _canonical(result)
    assert imported["task_id"] is None
    for table in ("setting_field_definitions", "setting_field_options", "setting_reference_handles",
                  "setting_timelines", "setting_text_fields", "setting_progressions"):
        assert {row["id"] for row in data["tables"][table]}.isdisjoint(row["id"] for row in result["tables"][table])
    assert any(not row["active"] and row["chapter_id"] is None
               for row in result["tables"]["setting_progressions"])
    timelines = []
    for backup in (data, result):
        field = backup["tables"]["setting_text_fields"][0]
        timelines.append(next(t for t in backup["tables"]["setting_timelines"] if t["id"] == field["timeline_id"]))
    for chapter_no in (5, 20, 21):
        snapshots = []
        for pid, timeline in zip((project["id"], imported["project"]["id"]), timelines, strict=True):
            response = await client.get(
                f"/api/v1/projects/{pid}/setting-entries/character/{timeline['character_id']}/fields",
                params={"chapter_no": chapter_no},
            )
            assert response.status_code == 200, response.text
            snapshots.append(format_entry_fields(EntryFieldsOut.model_validate(response.json())))
        assert snapshots[0] == snapshots[1]
        assert ("旧选项" if chapter_no <= 20 else "新选项") in snapshots[1]
        assert ("云梦山" in snapshots[1]) == (chapter_no >= 20)
        assert "终年云雾" not in snapshots[1]
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Project.fields_revision).where(Project.id == imported["project"]["id"])) == 0
        before = {model.__tablename__: await db.scalar(select(func.count()).select_from(model))
                  for model in (Project, WritingStyle, *[rule.model for rule in RULES])}
    preview = await _preview_raw(client, json.dumps(data).encode())
    assert preview.status_code == 200, preview.text
    token = preview.json()["token"]
    original_table = importer._Importer._table

    async def fail_after_field_graph(self, rule, rows):
        if rule.table == "setting_progressions":
            assert self.maps["setting_reference_handles"] and self.maps["setting_text_fields"]
            option = self.objects["setting_field_options"][0]
            self.db.add(SettingFieldOption(definition_id=option.definition_id, label=option.label))
            await self.db.flush()  # 真实 PG 唯一键错误发生在新资料图已插入后。
        await original_table(self, rule, rows)

    with monkeypatch.context() as patch:
        patch.setattr(importer._Importer, "_table", fail_after_field_graph)
        failed = await client.post("/api/v1/projects/import", json={"token": token})
        assert failed.status_code == 400, failed.text
    async with app.state.sessionmaker() as db:
        after = {model.__tablename__: await db.scalar(select(func.count()).select_from(model))
                 for model in (Project, WritingStyle, *[rule.model for rule in RULES])}
    assert before == after
    retried = await client.post("/api/v1/projects/import", json={"token": token})
    assert retried.status_code == 201, retried.text


async def test_postgresql_card_export_refuses_configuration_deleted_after_value_projection(
    postgres_fields_client, monkeypatch,
):
    client = postgres_fields_client
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    character = (await client.post(f"{base}/characters", json={"name": "空快照"})).json()
    created = await client.post(f"{base}/setting-field-definitions", json={
        "expected_fields_revision": 0, "label": "尚未填写", "field_type": "line",
        "applicable_types": ["character"],
    })
    assert created.status_code == 201, created.text
    definition_id = created.json()["item"]["id"]
    url = f"{base}/setting-entries/character/{character['id']}/fields"
    initial = (await client.get(url)).json()
    saved = await client.patch(f"{url}/{definition_id}/baseline", json={
        "expected_fields_revision": 1, "expected_timeline_revision": initial["timeline_revision"],
        "field_type": "line", "value": "",
    })
    assert saved.status_code == 200, saved.text
    metadata_seen, release = asyncio.Event(), asyncio.Event()
    original_scalars = AsyncSession.scalars
    reader = None

    async def blocked_definition_metadata(db, statement, *args, **kwargs):
        columns = getattr(statement, "column_descriptions", [])
        if (asyncio.current_task() is reader and not metadata_seen.is_set()
                and any(column.get("entity") is SettingFieldDefinition for column in columns)):
            metadata_seen.set()
            await release.wait()
        return await original_scalars(db, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "scalars", blocked_definition_metadata)
    reader = asyncio.create_task(client.post(f"{base}/cards/export", json={"character_ids": [character["id"]]}))
    try:
        await asyncio.wait_for(metadata_seen.wait(), 5)
        removed = await client.delete(f"{base}/setting-field-definitions/{definition_id}",
                                      params={"expected_fields_revision": 1})
        assert removed.status_code == 200, removed.text
    finally:
        release.set()
    response = await asyncio.wait_for(reader, 5)
    assert response.status_code == 409, response.text
    assert response.json()["scope"] == "fields"
