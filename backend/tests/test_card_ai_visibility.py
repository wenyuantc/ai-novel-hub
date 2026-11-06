"""卡片 v6 的可见性往返、显式映射、兼容和原子确认。"""

import pytest

from app.core.errors import BadRequestError
from app.models import Character, Project
from app.schemas.setting_ai_visibility import legacy_ai_settings
from app.services import setting_ai_visibility as visibility
from app.services.card_service import parse_card
from tests.helpers import create_ai_config, create_project, register, run_task
from tests.test_cards import _career
from tests.test_setting_fields_postgresql import (
    postgres_fields_app as postgres_fields_app,
)
from tests.test_setting_fields_postgresql import (
    postgres_fields_client as postgres_fields_client,
)


def _entry(name, ref, mode=None, *, links=(), **extra):
    return {"name": name, "ref": ref, "ai_settings": {**legacy_ai_settings().model_dump(), "ai_mode": mode},
            "ai_link_refs": list(links), **extra}


def _card(**content):
    return {"format": "ainovel.cards", "version": 6, **content}


async def _settings(client, base, kind, ident, **patch):
    path = f"{base}/setting-entries/{kind}/{ident}/ai-settings"
    resp = await client.get(path)
    assert resp.status_code == 200, resp.text
    if patch:
        resp = await client.patch(path, json={"expected_ai_visibility_revision": resp.json()["ai_visibility_revision"],
                                               **patch})
        assert resp.status_code == 200, resp.text
    return resp.json()


async def _preview(client, base, card, **options):
    response = await client.post(f"{base}/cards/import/preview", json={"data": card, **options})
    assert response.status_code == 200, response.text
    return response.json()


def _versions(preview):
    return {f"expected_{key}": preview[key] for key in (
        "fields_revision", "categories_revision", "ai_visibility_revision",
    )}


async def test_v6_roundtrip_retains_original_values_but_projects_only_allowed_settings(client, app, fake_llm):
    await register(client)
    await create_ai_config(client)
    pid = (await create_project(client))["id"]
    src = f"/api/v1/projects/{pid}"
    old = await client.post(f"{src}/cards/import", json={"data": {
        "format": "ainovel.cards", "version": 3, "characters": [{
            "name": "旧版旅人", "role_type": "配角", "profile": "可用角色哨兵", "background": "保密背景哨兵",
            "custom_text_fields": [{"name": "保密旧字段", "value": "保密旧值哨兵"}],
        }],
    }})
    assert old.status_code == 200, old.text
    hero = old.json()["characters"][0]["id"]
    async with app.state.sessionmaker() as db:
        character = await db.get(Character, hero)
        assert character.ai_mode is None and not character.always_in_context
        character.always_in_context = True  # 不能靠定位在目标项目重新推断。
        await db.commit()

    org = (await client.post(f"{src}/organizations", json={
        "name": "一跳组织", "purpose": "可用组织哨兵", "description": "保密组织哨兵",
    })).json()["id"]
    career = (await _career(client, src, "禁用职业", ["保密职业阶段"], rules="保密职业哨兵"))["id"]
    assignment = await client.post(f"{src}/characters/{hero}/careers", json={"career_id": career})
    assert assignment.status_code == 201, assignment.text
    world = (await client.post(f"{src}/world", json={"title": "暗号本", "category": "item"})).json()["id"]
    await _settings(client, src, "character", hero, mention_enabled=False, mention_case_sensitive=False,
                    mention_exclusions=["旅人指令"], ai_hidden_builtin_fields=["background"],
                    links=[{"kind": "organization", "id": org}, {"kind": "world", "id": world}])
    await _settings(client, src, "organization", org, ai_mode="manual_only", ai_hidden_builtin_fields=["description"],
                    links=[{"kind": "career", "id": career}])
    await _settings(client, src, "career", career, ai_mode="never")
    entry = f"{src}/setting-entries/character/{hero}"
    fields = (await client.get(f"{entry}/fields")).json()
    hidden = await client.patch(f"{entry}/legacy-fields/{fields['legacy_fields'][0]['field_id']}/ai-visibility", json={
        "expected_ai_visibility_revision": fields["ai_visibility_revision"],
        "expected_timeline_revision": fields["timeline_revision"], "ai_hidden": True,
    })
    assert hidden.status_code == 200, hidden.text
    definition = await client.post(f"{src}/setting-field-definitions", json={
        "expected_fields_revision": 0, "label": "保密共享字段", "field_type": "line",
        "applicable_types": ["character"], "ai_hidden": True,
    })
    assert definition.status_code == 201, definition.text
    current = (await client.get(f"{entry}/fields")).json()
    field_id = definition.json()["item"]["id"]
    value = await client.patch(f"{entry}/fields/{field_id}/baseline", json={
        "expected_fields_revision": current["fields_revision"],
        "expected_timeline_revision": current["timeline_revision"],
        "field_type": "line", "value": "保密共享哨兵",
    })
    assert value.status_code == 200, value.text

    exported = await client.post(f"{src}/cards/export", json={"character_ids": [hero], "organization_ids": [org]})
    assert exported.status_code == 200, exported.text
    card = exported.json()
    assert card["version"] == 6 and "world" not in card
    assert card["characters"][0]["legacy_always_in_context"] is True
    assert card["characters"][0]["ai_settings"]["ai_mode"] is None
    assert card["characters"][0]["custom_text_fields"][0]["ai_hidden"] is True
    assert card["field_definitions"][0]["ai_hidden"] is True
    [external] = card["external_references"]
    assert (external["type"], external["name"]) == ("world.item", "暗号本")

    destination = (await create_project(client, title="接收卡片"))["id"]
    dst = f"/api/v1/projects/{destination}"
    for category in ("place", "item"):
        response = await client.post(f"{dst}/world", json={
            "title": "暗号本", "category": category, "content": "明确映射的一跳目标哨兵",
        })
        assert response.status_code == 201, response.text
        target = response.json()["id"]
        await _settings(client, dst, "world", target, ai_mode="manual_only")
        if category == "place":
            wrong = {"references": {external["ref"]: {"kind": "world", "id": target}}}
            assert not (await _preview(client, dst, card, mappings=wrong))["can_import"]
    unresolved = await _preview(client, dst, card)
    assert not unresolved["can_import"]  # 存在同名条目也不自动解析。
    outside = next(row for row in unresolved["references"] if row["ref"] == external["ref"])
    assert outside["source_links"] == ["旧版旅人"]
    mappings = {"references": {external["ref"]: {"kind": "world", "id": target}}}
    preview = await _preview(client, dst, card, mappings=mappings)
    assert preview["can_import"]
    async with app.state.sessionmaker() as db:
        history_epoch = (await db.get(Project, destination)).ai_history_epoch
    imported = await client.post(f"{dst}/cards/import", json={"data": card, "mappings": mappings, **_versions(preview)})
    assert imported.status_code == 200, imported.text
    saved = imported.json()
    hero = saved["characters"][0]["id"]
    org = saved["organizations"][0]["id"]
    career = saved["careers"][0]["id"]
    hero_settings = await _settings(client, dst, "character", hero)
    assert hero_settings["ai_visibility_revision"] == preview["ai_visibility_revision"] + 1
    assert hero_settings["ai_mode"] is None and hero_settings["mention_enabled"] is False
    assert hero_settings["mention_case_sensitive"] is False and hero_settings["mention_exclusions"] == ["旅人指令"]
    assert {row["name"] for row in hero_settings["links"]} == {"一跳组织", "暗号本"}
    assert (await _settings(client, dst, "organization", org))["links"][0]["reference"] == {
        "kind": "career", "id": career,
    }
    assert (await _settings(client, dst, "career", career))["ai_mode"] == "never"
    async with app.state.sessionmaker() as db:
        assert (await db.get(Character, hero)).always_in_context is True
        assert (await db.get(Project, destination)).ai_history_epoch == history_epoch
    fields = (await client.get(f"{dst}/setting-entries/character/{hero}/fields")).json()
    assert fields["fields"][0]["ai_hidden"] and fields["fields"][0]["value"] == "保密共享哨兵"
    assert fields["legacy_fields"][0]["ai_hidden"] and fields["legacy_fields"][0]["value"] == "保密旧值哨兵"
    chapter = (await client.post(f"{dst}/chapters", json={"title": "验证导入后的 AI 请求"})).json()
    fake_llm.push("新的正文。")
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={}))
    assert task["status"] == "succeeded", task
    prompt = "\n".join(message["content"] for message in fake_llm.calls[-1]["messages"])
    for value in ("可用角色哨兵", "可用组织哨兵", "明确映射的一跳目标哨兵"):
        assert value in prompt
    for value in ("保密背景哨兵", "保密共享字段", "保密共享哨兵", "保密旧字段", "保密旧值哨兵", "保密组织哨兵",
                  "禁用职业", "保密职业阶段", "保密职业哨兵"):
        assert value not in prompt


async def test_v6_definition_reuse_and_skip_preserve_settings_and_stale_ai_cas(client, app):
    await register(client)
    pid = (await create_project(client))["id"]
    base = f"/api/v1/projects/{pid}"
    org = (await client.post(f"{base}/organizations", json={"name": "同名组织"})).json()["id"]
    career = (await _career(client, base, "同名职业", ["旧阶段"]))["id"]
    await _settings(client, base, "organization", org, ai_mode="never")
    await _settings(client, base, "career", career, ai_mode="always")
    d = await client.post(f"{base}/setting-field-definitions", json={
        "expected_fields_revision": 0, "label": "口令", "field_type": "line", "applicable_types": ["character"],
    })
    assert d.status_code == 201, d.text
    skipped = _entry("同名组织", "org", "always", links=["out"])
    reused = _entry("同名职业", "career", "never", links=["out"], stages=[{"name": "不应追加"}])
    external = {"ref": "out", "kind": "world", "type": "world.item", "name": "无映射"}
    card = _card(organizations=[skipped], careers=[reused], external_references=[external])
    preview = await _preview(client, base, card)
    assert preview["can_import"] and not preview["references"]
    noop = await client.post(f"{base}/cards/import", json={"data": card, **_versions(preview)})
    assert noop.status_code == 200, noop.text
    saved = await _settings(client, base, "organization", org)
    assert saved["ai_visibility_revision"] == preview["ai_visibility_revision"] and saved["ai_mode"] == "never"
    existing_career = await _settings(client, base, "career", career)
    assert existing_career["ai_mode"] == "always" and not existing_career["links"]

    card["characters"] = [_entry("新角色", "hero", "always", legacy_always_in_context=False,
                                 fields=[{"definition_ref": "d", "value": "不能导入可见定义"}])]
    card["field_definitions"] = [{"ref": "d", "label": "口令", "field_type": "line", "ai_hidden": True,
                                  "applicable_types": ["character"]}]
    reuse = {"definitions": {"d": {"action": "reuse", "target_definition_id": d.json()["item"]["id"]}}}
    blocked = await _preview(client, base, card, mappings=reuse)
    assert not blocked["can_import"] and blocked["field_definitions"][0]["status"] == "incompatible"
    rename = {"definitions": {"d": {"action": "rename"}}}
    preview = await _preview(client, base, card, mappings=rename)
    assert preview["can_import"]
    versions = _versions(preview)
    missing = await client.post(f"{base}/cards/import", json={
        "data": card, "mappings": rename,
        **{k: v for k, v in versions.items() if k != "expected_ai_visibility_revision"},
    })
    assert missing.status_code == 400 and missing.json()["code"] == "ai_visibility_revision_required"
    await _settings(client, base, "organization", org, mention_enabled=False)
    stale = await client.post(f"{base}/cards/import", json={"data": card, "mappings": rename, **versions})
    assert stale.status_code == 409 and stale.json()["scope"] == "ai_visibility"
    assert (await client.get(f"{base}/characters")).json() == []
    async with app.state.sessionmaker() as db:
        assert (await db.get(Project, pid)).fields_revision == preview["fields_revision"]
    confirmed = await _preview(client, base, card, mappings=rename)
    imported = await client.post(f"{base}/cards/import", json={
        "data": card, "mappings": rename, **_versions(confirmed),
    })
    assert imported.status_code == 200, imported.text
    assert (await _settings(client, base, "organization", org))["ai_mode"] == "never"


async def test_postgresql_card_v6_roundtrip(postgres_fields_client, postgres_fields_app, fake_llm):
    engine = postgres_fields_app.state.task_engine
    await engine.start()
    try:
        await test_v6_roundtrip_retains_original_values_but_projects_only_allowed_settings(
            postgres_fields_client, postgres_fields_app, fake_llm,
        )
    finally:
        await engine.stop()


async def test_v6_ai_link_mappings_cannot_collapse_to_one_target_and_failure_rolls_back(client, monkeypatch):
    await register(client)
    pid = (await create_project(client))["id"]
    base = f"/api/v1/projects/{pid}"
    card = _card(characters=[_entry("新角色", "hero", "always", links=["x", "y"], legacy_always_in_context=False)],
                 external_references=[{"ref": ref, "kind": "world", "type": "world.item", "name": ref} for ref in "xy"])
    targets = []
    for ref in "xy":
        created = await client.post(f"{base}/world", json={"title": ref, "category": "item"})
        assert created.status_code == 201, created.text
        targets.append({"kind": "world", "id": created.json()["id"]})
    colliding = await _preview(client, base, card, mappings={"references": dict.fromkeys("xy", targets[0])})
    assert not colliding["can_import"]
    assert "同一条目" in colliding["references"][1]["reason"]
    mappings = {"references": dict(zip("xy", targets, strict=True))}
    preview = await _preview(client, base, card, mappings=mappings)
    assert preview["can_import"]
    original = visibility.replace_links

    async def fail_after_links(*args):
        await original(*args)
        raise BadRequestError("模拟关联写入失败")

    monkeypatch.setattr(visibility, "replace_links", fail_after_links)
    rejected = await client.post(f"{base}/cards/import", json={
        "data": card, "mappings": mappings, **_versions(preview),
    })
    assert rejected.status_code == 400 and "模拟关联写入失败" in rejected.text
    assert (await client.get(f"{base}/characters")).json() == []
    current = await _preview(client, base, card, mappings=mappings)
    assert _versions(preview) == _versions(current)


async def test_v6_same_name_characters_keep_separate_local_policies_and_link_identity(client):
    await register(client)
    pid = (await create_project(client))["id"]
    base = f"/api/v1/projects/{pid}"
    card = _card(characters=[
        _entry("同名角色", "first", "always", links=["second"], legacy_always_in_context=False),
        _entry("同名角色", "second", "never", legacy_always_in_context=False),
    ])
    preview = await _preview(client, base, card)
    assert preview["can_import"] and preview["references"][0]["status"] == "internal"
    response = await client.post(f"{base}/cards/import", json={"data": card, **_versions(preview)})
    assert response.status_code == 200, response.text
    first, second = (item["id"] for item in response.json()["characters"])
    assert first != second
    source = await _settings(client, base, "character", first)
    assert source["ai_mode"] == "always" and source["links"][0]["reference"]["id"] == second
    assert (await _settings(client, base, "character", second))["ai_mode"] == "never"


@pytest.mark.parametrize("version", range(1, 6))
def test_old_card_cannot_silently_drop_new_visibility(version):
    char = {"ref": "hero", "name": "旧角色"}
    base = {"format": "ainovel.cards", "version": version}
    assert parse_card({**base, "characters": [char]}).characters[0].ai_settings is None
    for extra in ({"ai_settings": {}}, {"ai_mode": "never"}, {"ai_link_refs": []}, {"legacy_always_in_context": False},
                  {"custom_text_fields": [{"name": "旧字段", "ai_hidden": True}]}):
        with pytest.raises(BadRequestError, match="旧版卡片不支持 AI 可见性配置"):
            parse_card({**base, "characters": [{**char, **extra}]})
    with pytest.raises(BadRequestError, match="旧版卡片不支持 AI 可见性配置"):
        parse_card({**base, "field_definitions": [{"ai_hidden": True}]})


@pytest.mark.parametrize("change", ["missing-mode", "missing-legacy", "missing-links", "missing-field", "name-mask",
                                    "wrong-flag-type", "self-link", "unknown-link", "duplicate-link"])
def test_v6_fails_closed_when_protection_is_incomplete_or_invalid(change):
    entry = _entry("角色", "a", "never", legacy_always_in_context=False,
                   custom_text_fields=[{"name": "口令", "value": "私密", "ai_hidden": True}])
    target = _entry("组织", "b", "always")
    if change == "missing-mode":
        entry["ai_settings"].pop("ai_mode")
    elif change == "missing-legacy":
        entry.pop("legacy_always_in_context")
    elif change == "missing-links":
        entry.pop("ai_link_refs")
    elif change == "missing-field":
        entry["custom_text_fields"][0].pop("ai_hidden")
    elif change == "name-mask":
        entry["ai_settings"]["ai_hidden_builtin_fields"] = ["name"]
    elif change == "wrong-flag-type":
        entry["custom_text_fields"][0]["ai_hidden"] = 1
    else:
        entry["ai_link_refs"] = {"self-link": ["a"], "unknown-link": ["missing"], "duplicate-link": ["b", "b"]}[change]
    with pytest.raises(BadRequestError, match="卡片文件格式不正确"):
        parse_card(_card(characters=[entry], organizations=[target]))
