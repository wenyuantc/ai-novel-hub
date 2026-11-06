"""卡片 v5 标签快照、旧版闸门、skip 保护与双目录 CAS。"""

import pytest

from app.models import Project, SettingFieldDefinition
from app.services import setting_category_service as categories
from tests.helpers import create_project, register
from tests.test_cards import _career, _char, _source_project
from tests.test_setting_fields_postgresql import (
    postgres_fields_app as postgres_fields_app,
)
from tests.test_setting_fields_postgresql import (
    postgres_fields_client as postgres_fields_client,
)


def _card(version=5, tags=None):
    entry = {"name": "新角色"}
    if version >= 4:
        entry["ref"] = "character.1"
    if tags is not None:
        entry["setting_tags"] = tags
    return {"format": "ainovel.cards", "version": version, "characters": [entry]}


async def _tags(client, base, kind, entry_id, tags):
    url = f"{base}/setting-entries/{kind}/{entry_id}/tags"
    current = await client.get(url)
    assert current.status_code == 200, current.text
    response = await client.patch(url, json={"setting_tags": tags,
                                           "expected_categories_revision": current.json()["categories_revision"]})
    assert response.status_code == 200, response.text
    return response.json()


async def test_cards_v6_exports_and_imports_tags_without_categories(client):
    await register(client)
    base, chars, org = await _source_project(client)
    career = await _career(client, base, "剑修", ["初阶"])
    await client.post(f"{base}/characters/{chars[0]['id']}/careers", json={"career_id": career["id"]})
    for kind, identity, tag in (("character", chars[0]["id"], "人物"), ("organization", org["id"], "组织"),
                                ("career", career["id"], "职业")):
        await _tags(client, base, kind, identity, [tag])
    exported = await client.post(f"{base}/cards/export", json={"character_ids": [chars[0]["id"]],
                                                             "organization_ids": [org["id"]]})
    assert exported.status_code == 200, exported.text
    card = exported.json()
    assert card["version"] == 6 and "setting_categories" not in card and "setting_category_rules" not in card
    assert card["characters"][0]["setting_tags"] == ["人物"]
    assert card["organizations"][0]["setting_tags"] == ["组织"]
    assert card["careers"][0]["setting_tags"] == ["职业"]
    target = await create_project(client, title="目标")
    target_base = f"/api/v1/projects/{target['id']}"
    preview = (await client.post(f"{target_base}/cards/import/preview", json={"data": card})).json()
    assert preview["categories_revision"] == 0
    response = await client.post(f"{target_base}/cards/import", json={"data": card,
        "expected_ai_visibility_revision": preview["ai_visibility_revision"],
        "expected_fields_revision": preview["fields_revision"], "expected_categories_revision": 0})
    assert response.status_code == 200, response.text
    for key, kind, tag in (("characters", "character", "人物"), ("organizations", "organization", "组织"),
                           ("careers", "career", "职业")):
        identity = response.json()[key][0]["id"]
        saved = (await client.get(f"{target_base}/setting-entries/{kind}/{identity}/tags")).json()
        assert saved["setting_tags"] == [tag] and saved["categories_revision"] == 1
    assert (await client.get(f"{target_base}/setting-categories")).json()["items"] == []


@pytest.mark.parametrize("version", [1, 2, 3, 4])
async def test_old_cards_accept_only_absent_or_exact_empty_tags(client, version):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    for tags in (["未来标签"], None, {}, False, "", [1]):
        card = _card(version)
        card["characters"][0]["setting_tags"] = tags
        response = await client.post(f"{base}/cards/import/preview", json={"data": card})
        assert response.status_code == 400, (version, tags, response.text)
        assert "旧版" in response.json()["detail"]
    for tags in (None, []):
        card = _card(version, tags)
        response = await client.post(f"{base}/cards/import", json={"data": card, "expected_fields_revision": 0})
        assert response.status_code == 200, response.text
    assert (await client.get(f"{base}/setting-categories")).json()["categories_revision"] == 0


async def test_cards_v5_skip_and_reused_career_never_replace_tags(client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    char = await _char(client, base, "新角色")
    org = (await client.post(f"{base}/organizations", json={"name": "同名组织"})).json()
    career = await _career(client, base, "同名职业", ["原阶段"])
    for kind, identity in (("character", char["id"]), ("organization", org["id"]), ("career", career["id"])):
        await _tags(client, base, kind, identity, ["保留"])
    card = _card(tags=["不能覆盖"])
    card["organizations"] = [{"ref": "o", "name": "同名组织", "setting_tags": ["不能覆盖"]}]
    card["careers"] = [{"ref": "c", "name": "同名职业", "stages": [{"name": "新阶段"}],
                        "setting_tags": ["不能覆盖"]}]
    response = await client.post(f"{base}/cards/import", json={"data": card, "expected_fields_revision": 0,
                                                             "expected_categories_revision": 3})
    assert response.status_code == 200, response.text
    for kind, identity in (("character", char["id"]), ("organization", org["id"]), ("career", career["id"])):
        saved = (await client.get(f"{base}/setting-entries/{kind}/{identity}/tags")).json()
        assert saved["setting_tags"] == ["保留"] and saved["categories_revision"] == 3


async def test_cards_v5_requires_both_versions_and_rolls_back_on_stale_catalog(client, app):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    existing = await _char(client, base, "已有")
    card = _card(tags=["新"])
    preview = (await client.post(f"{base}/cards/import/preview", json={"data": card})).json()
    missing = await client.post(f"{base}/cards/import", json={"data": card, "expected_fields_revision": 0})
    assert missing.status_code == 400 and missing.json()["code"] == "categories_revision_required"
    await _tags(client, base, "character", existing["id"], ["外部修改"])
    stale = await client.post(f"{base}/cards/import", json={"data": card,
        "expected_fields_revision": preview["fields_revision"], "expected_categories_revision": 0})
    assert stale.status_code == 409 and stale.json()["scope"] == "categories"
    async with app.state.sessionmaker() as db:
        db.add(SettingFieldDefinition(project_id=project["id"], label="外部字段", field_type="line",
                                      applicable_types=["character"]))
        (await db.get(Project, project["id"])).fields_revision = 1
        await db.commit()
    stale = await client.post(f"{base}/cards/import", json={"data": card,
        "expected_fields_revision": 0, "expected_categories_revision": 1})
    assert stale.status_code == 409 and stale.json()["scope"] == "fields"
    assert [item["name"] for item in (await client.get(f"{base}/characters")).json()] == ["已有"]


async def test_cards_v5_failed_tag_write_rolls_back_entities_fields_and_revisions(client, app, monkeypatch):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    original = categories.apply_entry_tags
    calls = 0

    async def fail_after_write(*args, **kwargs):
        nonlocal calls
        calls += 1
        await original(*args, **kwargs)
        raise ValueError("测试标签写入失败")

    monkeypatch.setattr(categories, "apply_entry_tags", fail_after_write)
    with pytest.raises(ValueError, match="测试标签写入失败"):
        await client.post(f"{base}/cards/import", json={"data": _card(tags=["新"]),
                          "expected_fields_revision": 0, "expected_categories_revision": 0})
    assert calls == 1
    assert (await client.get(f"{base}/characters")).json() == []
    async with app.state.sessionmaker() as db:
        saved = await db.get(Project, project["id"])
        assert (saved.fields_revision, saved.categories_revision) == (0, 0)


async def test_cards_v5_preview_never_pairs_stale_categories_with_field_metadata(client, monkeypatch):
    from app.services import card_service

    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    existing = await _char(client, base, "已有")
    original = card_service._target_careers
    changed = False

    async def change_between_reads(db, project_id):
        nonlocal changed
        result = await original(db, project_id)
        if not changed:
            changed = True
            await _tags(client, base, "character", existing["id"], ["并发修改"])
        return result

    monkeypatch.setattr(card_service, "_target_careers", change_between_reads)
    response = await client.post(f"{base}/cards/import/preview", json={"data": _card(tags=["新"])})
    assert response.status_code == 409 and response.json()["scope"] == "categories"


async def test_cards_v5_rejects_noncanonical_snapshot_tags(client):
    await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    for value in (None, {}, [1], [" "], [" 同盟"], ["同盟", "同盟"], ["长" * 51]):
        card = _card()
        card["characters"][0]["setting_tags"] = value
        response = await client.post(f"{base}/cards/import/preview", json={"data": card})
        assert response.status_code == 400, (value, response.text)


async def test_postgresql_cards_v6_tags_snapshot(postgres_fields_client):
    await test_cards_v6_exports_and_imports_tags_without_categories(postgres_fields_client)


async def test_postgresql_cards_v5_failure_rolls_back_both_revisions(
    postgres_fields_client, postgres_fields_app, monkeypatch,
):
    await test_cards_v5_failed_tag_write_rolls_back_entities_fields_and_revisions(
        postgres_fields_client, postgres_fields_app, monkeypatch,
    )
