"""删除条目后保留的标签草稿，绝不能写到后来创建的另一条目。"""

import asyncio

import httpx
import pytest
from alembic import command

from app.main import create_app
from app.models import Base
from app.services import authorization_service
from tests.helpers import create_project, make_settings, register
from tests.test_setting_fields_migration import _config

ENDPOINTS = {
    "character": "characters",
    "world": "world",
    "organization": "organizations",
    "career": "careers",
}


@pytest.fixture(params=["create_all", "migrated"])
async def identity_client(request, tmp_path, fake_llm):
    """两条真实建库路径都必须保护身份，不能只改测试使用的 ORM。"""
    if request.param == "migrated":
        await asyncio.to_thread(command.upgrade, _config(tmp_path / "test.db"), "0036")
    application = create_app(make_settings(tmp_path), provider_factory=fake_llm.factory, start_task_engine=False)
    try:
        if request.param == "create_all":
            async with application.state.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        await authorization_service.initialize(application.state.sessionmaker)
        await application.state.system_settings.load()
        await application.state.task_engine.start()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://testserver",
        ) as client:
            yield client
    finally:
        await application.state.system_settings.close()
        await application.state.task_engine.stop()
        await application.state.engine.dispose()


async def _create_entry(client, base, kind, name):
    payload = {"title": name, "category": "place"} if kind == "world" else {"name": name}
    if kind == "career":
        payload["stages"] = [{"name": "第一阶"}]
    response = await client.post(f"{base}/{ENDPOINTS[kind]}", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("kind", ENDPOINTS)
@pytest.mark.parametrize("empty_table", [False, True], ids=["delete_highest", "delete_last"])
async def test_deleted_entry_tag_draft_cannot_target_a_recreated_entry(identity_client, kind, empty_table):
    client = identity_client
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    if not empty_table:
        await _create_entry(client, base, kind, "保留的条目")
    original = await _create_entry(client, base, kind, "后来删除的最大ID条目")
    old_tags_url = f"{base}/setting-entries/{kind}/{original['id']}/tags"
    loaded = await client.get(old_tags_url)
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["setting_tags"] == []
    old_revision = loaded.json()["categories_revision"]

    deleted = await client.delete(f"{base}/{ENDPOINTS[kind]}/{original['id']}")
    assert deleted.status_code == 204, deleted.text
    replacement = await _create_entry(client, base, kind, "全新的条目")
    new_tags_url = f"{base}/setting-entries/{kind}/{replacement['id']}/tags"
    before = await client.get(new_tags_url)
    assert before.status_code == 200, before.text
    assert before.json()["setting_tags"] == []
    assert before.json()["categories_revision"] == old_revision
    old_reference = await client.get(old_tags_url)

    # 模拟另一页面或远端删除后，旧页面仍持有原 EntityRef 和空标签草稿的版本。
    stale_write = await client.patch(old_tags_url, json={
        "expected_categories_revision": old_revision,
        "setting_tags": ["只属于已删除条目的草稿"],
    })
    current = await client.get(new_tags_url)
    assert current.status_code == 200, current.text
    assert current.json()["categories_revision"] == old_revision
    assert (
        old_reference.status_code, stale_write.status_code, current.json()["setting_tags"],
        replacement["id"] > original["id"],
    ) == (404, 404, [], True), {
        "original_id": original["id"], "replacement_id": replacement["id"],
        "stale_response": stale_write.text, "replacement_tags": current.json()["setting_tags"],
    }
