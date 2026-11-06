"""作者 AI 设置的 HTTP 合同：兼容、原子 CAS、同项目有向边和字段原值。"""

from sqlalchemy import func, select, update

from app.models import Character, Project, SettingAiLink, SettingReferenceHandle, WorldSetting
from app.services.setting_ai_visibility import load_link_targets
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role
from tests.test_setting_fields import _definition, _fields, _setup, _versions


def _settings(base, kind, ident):
    return f"{base}/setting-entries/{kind}/{ident}/ai-settings"


async def _created(client, path, **data):
    response = await client.post(path, json=data)
    assert response.status_code == 201, response.text
    return response.json()


async def _edited(client, url, revision, **data):
    response = await client.patch(url, json={"expected_ai_visibility_revision": revision, **data})
    assert response.status_code == 200, response.text
    return response.json()


async def _state(app, project_id):
    async with app.state.sessionmaker() as db:
        return (await db.execute(select(Project.ai_visibility_revision, Project.ai_history_epoch)
                                 .where(Project.id == project_id))).one()


async def test_all_kinds_default_to_detected_and_hide_whitelisted_fields_only_in_ai(client, app):
    _, project, base, character = await _setup(client)
    world = await _created(client, f"{base}/world", title="密卷", category="history", content="旧历")
    org = await _created(client, f"{base}/organizations", name="学会", purpose="秘密")
    career = await _created(client, f"{base}/careers", name="航者", stages=[{"name": "初航"}])
    for revision, (kind, item, key) in enumerate([
        ("character", character, "profile"), ("world", world, "content"),
        ("organization", org, "purpose"), ("career", career, "stages"),
    ]):
        url = _settings(base, kind, item["id"])
        original = (await client.get(url)).json()
        assert original["ai_mode"] == "detected" and original["mention_enabled"] is True
        assert original["ai_visibility_revision"] == revision
        assert original["ai_hidden_builtin_fields"] == [] and key in original["hideable_builtin_fields"]
        saved = await _edited(client, url, revision, ai_mode="never", ai_hidden_builtin_fields=[key])
        assert saved["ai_mode"] == "never" and saved["mention_enabled"] is True
        assert saved["ai_hidden_builtin_fields"] == [key]
        assert await _state(app, project["id"]) == (revision + 1, revision + 1)
    assert (await client.get(f"{base}/world/{world['id']}")).json()["content"] == "旧历"
    assert (await client.get(f"{base}/organizations/{org['id']}")).json()["purpose"] == "秘密"
    assert (await client.get(f"{base}/careers/{career['id']}")).json()["stages"][0]["name"] == "初航"
    assert (await client.get(f"{base}/setting-entries")).json()["total"] == 4


async def test_invalid_and_stale_patches_leave_config_and_handles_unmodified(client, app):
    _, project, base, character = await _setup(client)
    local = await _created(client, f"{base}/world", title="本项目")
    foreign_project = await create_project(client, title="另一项目")
    foreign = await _created(client, f"/api/v1/projects/{foreign_project['id']}/world", title="他项目")
    url = _settings(base, "character", character["id"])
    world_ref = {"kind": "world", "id": local["id"]}
    for fields, status in [
        ({"ai_hidden_builtin_fields": ["name"], "links": [world_ref]}, 422),
        ({"links": [{"kind": "character", "id": character["id"]}]}, 422),
        ({"links": [world_ref, world_ref]}, 422),
        ({"links": [world_ref, {"kind": "world", "id": foreign["id"]}]}, 404),
        ({"links": [{"kind": "world", "id": i} for i in range(1, 34)]}, 422),
        ({"mention_exclusions": [" "]}, 422),
        ({"mention_exclusions": ["过" * 81]}, 422),
        ({"ai_mode": None}, 422),
        ({"mention_enabled": 1}, 422),
    ]:
        response = await client.patch(url, json={"expected_ai_visibility_revision": 0, "ai_mode": "never", **fields})
        assert response.status_code == status, response.text
    assert await _state(app, project["id"]) == (0, 0)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(SettingReferenceHandle)) == 0
        assert await db.scalar(select(func.count()).select_from(SettingAiLink)) == 0
    saved = await _edited(
        client, url, 0, ai_mode="always", mention_case_sensitive=False, mention_exclusions=["  Lan  ", "LAN"],
    )
    assert saved["mention_exclusions"] == ["Lan"]
    assert await _state(app, project["id"]) == (1, 1)  # 放宽大小写，但新增短语可能收紧。
    stale = await client.patch(url, json={"expected_ai_visibility_revision": 0, "links": [world_ref]})
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_revision" and stale.json()["scope"] == "ai_visibility"
    assert stale.json()["current_revision"] == 1
    assert (await _edited(client, url, 1, mention_exclusions=["Lan", "LAN"])) == saved
    assert await _state(app, project["id"]) == (1, 1)
    await _edited(client, url, 1, mention_exclusions=[])
    assert await _state(app, project["id"]) == (2, 2)


async def test_links_are_directional_and_target_deletion_invalidates_epoch_once(client, app):
    _, project, base, a = await _setup(client)
    b = await _created(client, f"{base}/world", title="遗迹")
    c = await _created(client, f"{base}/organizations", name="黑塔")
    pid = project["id"]
    root, intermediate, target = ("character", a["id"]), ("world", b["id"]), ("organization", c["id"])
    saved = await _edited(client, _settings(base, *root), 0, links=[{"kind": intermediate[0], "id": intermediate[1]}])
    assert saved["links"][0]["name"] == "遗迹"
    assert saved["links"][0]["reference"] == {"kind": "world", "id": b["id"]}
    await _edited(client, _settings(base, *intermediate), 1, links=[{"kind": target[0], "id": target[1]}])
    await _edited(client, _settings(base, *target), 2, ai_mode="never")
    async with app.state.sessionmaker() as db:
        assert await load_link_targets(db, pid, [root]) == {root: (intermediate,)}
        assert await load_link_targets(db, pid, [intermediate, target]) == {intermediate: (target,)}
    assert await _state(app, pid) == (3, 1)
    assert (await client.delete(f"{base}/world/{b['id']}")).status_code == 204
    assert await _state(app, pid) == (4, 2)
    assert (await client.get(_settings(base, *root))).json()["links"] == []
    async with app.state.sessionmaker() as db:
        assert await load_link_targets(db, pid, [root, intermediate, target]) == {}


async def test_casefold_may_broaden_an_exclusion_but_equivalent_spelling_does_not_seal(client, app):
    _, project, base, character = await _setup(client)
    url = _settings(base, "character", character["id"])
    await _edited(client, url, 0, mention_exclusions=["外传 LAND"])
    assert await _state(app, project["id"]) == (1, 1)
    await _edited(client, url, 1, mention_case_sensitive=False)
    assert await _state(app, project["id"]) == (2, 2)
    saved = await _edited(client, url, 2, mention_exclusions=["外传 land"])
    assert saved["mention_exclusions"] == ["外传 land"]
    assert await _state(app, project["id"]) == (3, 2)


async def test_legacy_metadata_changes_keep_compatibility_and_invalidate_tightening(client, app):
    _, project, base, character = await _setup(client)
    old_place = await _created(client, f"{base}/world", title="旧港", category="place")
    old_rule = await _created(client, f"{base}/world", title="旧约", category="rule")
    pid, ident = project["id"], character["id"]
    async with app.state.sessionmaker() as db:
        await db.execute(update(Character).where(Character.id == ident).values(ai_mode=None, always_in_context=True))
        await db.execute(update(WorldSetting).where(WorldSetting.project_id == pid).values(ai_mode=None))
        await db.commit()
    url = _settings(base, "character", ident)
    assert (await client.get(url)).json()["ai_mode"] is None
    assert (await _edited(client, url, 0, ai_hidden_builtin_fields=["profile"]))["ai_mode"] is None
    assert await _state(app, pid) == (1, 1)
    character_url = f"{base}/characters/{ident}"
    assert (await client.patch(character_url, json={"always_in_context": False})).status_code == 200
    assert await _state(app, pid) == (2, 2)
    world_url = f"{base}/world/{old_place['id']}"
    assert (await client.patch(world_url, json={"category": "history"})).status_code == 200
    assert (await client.get(_settings(base, "world", old_place["id"]))).json()["mention_enabled"] is False
    assert await _state(app, pid) == (3, 3)
    rule_settings = _settings(base, "world", old_rule["id"])
    assert (await _edited(client, rule_settings, 3, ai_mode="always"))["mention_enabled"] is False
    assert await _state(app, pid) == (4, 3)
    assert (await _edited(client, rule_settings, 4, mention_enabled=True))["mention_enabled"] is True
    assert await _state(app, pid) == (5, 4)
    assert (await client.patch(world_url, json={"category": "place"})).status_code == 200
    assert (await client.get(_settings(base, "world", old_place["id"]))).json()["mention_enabled"] is True
    assert await _state(app, pid) == (6, 5)


async def test_readonly_admin_can_read_settings_but_not_write(client, make_client, system_admin):
    _, _, base, character = await _setup(client)
    url = _settings(base, "character", character["id"])
    viewer = make_client()
    user = await register(viewer, "ai_viewer")
    assert (await viewer.get(url)).status_code == 404
    role = await create_role(system_admin, "仅看作品 AI 设置", ["projects.read"])
    await assign_roles(system_admin, user["id"], [role["id"]])
    assert (await viewer.get(url)).status_code == 200
    denied = await viewer.patch(url, json={"expected_ai_visibility_revision": 0, "ai_mode": "always"})
    assert denied.status_code == 403, denied.text


async def test_field_flags_keep_raw_values_events_and_safe_binding(client, app):
    _, project, base, character = await _setup(client)
    pid, cid = project["id"], character["id"]
    current = await _fields(client, base, character)
    timeline = f"{base}/setting-timelines/{current['timeline_id']}"
    legacy = await _created(
        client, f"{timeline}/text-fields", expected_revision=current["timeline_revision"],
        label="身份", base_value="旧值",
    )
    fid = legacy["item"]["id"]
    event = await _created(
        client, f"{timeline}/progressions", expected_revision=legacy["timeline_revision"], custom_field_id=fid,
        operation="append", value="新值", anchor_kind="planned", chapter_no=20,
    )
    definition = (await _definition(client, base, field_type="text"))["item"]
    did = definition["id"]
    entry_path = f"{base}/setting-entries/character/{cid}/legacy-fields/{fid}"
    old_patch = {
        "expected_timeline_revision": event["timeline_revision"], "expected_ai_visibility_revision": 0,
        "ai_hidden": True,
    }
    hidden = await client.patch(f"{entry_path}/ai-visibility", json=old_patch)
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["timeline_revision"] == event["timeline_revision"] + 1
    assert await _state(app, pid) == (1, 1)
    stale = await client.patch(f"{entry_path}/ai-visibility", json=old_patch)
    assert stale.status_code == 409 and stale.json()["scope"] == "ai_visibility"
    current = await _fields(client, base, character, chapter_no=21)
    assert current["legacy_fields"][0]["ai_hidden"] is True
    assert current["legacy_fields"][0]["value"] == "旧值\n新值"
    denied = await client.post(f"{entry_path}/binding", json={**_versions(current), "definition_id": did})
    assert denied.status_code == 409 and denied.json()["code"] == "field_visibility_conflict"
    definition_url = f"{base}/setting-field-definitions/{did}"
    hidden = await client.patch(definition_url, json={"expected_fields_revision": 1, "ai_hidden": True})
    assert hidden.status_code == 200 and hidden.json()["item"]["ai_hidden"] is True
    assert hidden.json()["fields_revision"] == 2 and await _state(app, pid) == (2, 2)
    noop = await client.patch(definition_url, json={"expected_fields_revision": 2, "ai_hidden": True})
    assert noop.json()["fields_revision"] == 2 and await _state(app, pid) == (2, 2)
    current = await _fields(client, base, character)
    bound = await client.post(f"{entry_path}/binding", json={**_versions(current), "definition_id": did})
    assert bound.status_code == 200, bound.text
    assert await _state(app, pid) == (3, 2)
    current = await _fields(client, base, character, chapter_no=21)
    assert current["legacy_fields"] == []
    assert current["fields"][0]["field_id"] == fid and current["fields"][0]["ai_hidden"] is True
    assert current["fields"][0]["value"] == "旧值\n新值"
    assert (await client.get(f"{timeline}/progressions")).json()["items"][0]["id"] == event["item"]["id"]
    not_legacy = await client.patch(f"{entry_path}/ai-visibility", json={
        "expected_timeline_revision": current["timeline_revision"], "expected_ai_visibility_revision": 3,
        "ai_hidden": False,
    })
    assert not_legacy.status_code == 404
    visible = await client.patch(definition_url, json={"expected_fields_revision": 2, "ai_hidden": False})
    assert visible.status_code == 200 and visible.json()["item"]["ai_hidden"] is False
    assert await _state(app, pid) == (4, 2)
