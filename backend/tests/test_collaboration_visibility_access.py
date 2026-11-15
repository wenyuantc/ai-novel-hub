"""协作者人工维护 AI 可见性，不授予模型或私人资源能力。"""

import asyncio

import pytest
from sqlalchemy import select

from app.ai.providers import OpenAICompatProvider
from app.core.errors import ForbiddenError, NotFoundError
from app.models import AdminAuditLog, Character, Project
from app.schemas.setting_ai_visibility import EntryAiSettingsUpdate, LegacyFieldAiVisibilityUpdate
from app.schemas.setting_fields import DefinitionCreate, LegacyBinding
from app.services import ai_dispatch, setting_ai_visibility, setting_field_service
from app.services.ai_dispatch import VisibilityChangedError, guard_provider, read_visibility_stamp
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role
from tests.test_ai_visibility_dispatch import _collect, _server, _sse
from tests.test_collaboration_story_access import _invite
from tests.test_setting_fields import _definition, _fields, _setup, _versions


async def test_ai_settings_read_write_use_manual_capabilities(app, client, make_client, system_admin):
    _, project, base, character = await _setup(client)
    viewer, editor, outsider, admin_viewer = (make_client() for _ in range(4))
    viewer_user = await register(viewer, "visibility-viewer")
    editor_user = await register(editor, "visibility-editor")
    await register(outsider, "visibility-outsider")
    admin_user = await register(admin_viewer, "visibility-admin-viewer")
    role = await create_role(system_admin, "设定编辑管理员", ["projects.read", "projects.edit"])
    await assign_roles(system_admin, admin_user["id"], [role["id"]])
    for guest, name, role_name in (
        (viewer, "visibility-viewer", "viewer"),
        (editor, "visibility-editor", "editor"),
        (admin_viewer, "visibility-admin-viewer", "viewer"),
    ):
        await _invite(client, guest, project["id"], name, role_name)
    url = f"{base}/setting-entries/character/{character['id']}/ai-settings"
    for guest in (viewer, editor, admin_viewer):
        assert (await guest.get(url)).status_code == 200
    assert (await outsider.get(url)).status_code == 404
    body = {"expected_ai_visibility_revision": 0, "ai_mode": "never"}
    assert (await viewer.patch(url, json=body)).status_code == 403
    assert (await outsider.patch(url, json=body)).status_code == 404
    saved = await editor.patch(url, json=body)
    assert saved.status_code == 200 and saved.json()["ai_mode"] == "never", saved.text
    admin = await admin_viewer.patch(url, json={"expected_ai_visibility_revision": 1, "ai_mode": "always"})
    assert admin.status_code == 200, admin.text
    # 管理员写权限只影响人工配置，viewer 仍不能触发 AI。
    assert not (await admin_viewer.get(base)).json()["access"]["can_use_ai"]

    foreign_project = await create_project(client, title="另一作品")
    foreign = (await client.post(f"/api/v1/projects/{foreign_project['id']}/world", json={"title": "隐私"})).json()
    foreign_ref = {"kind": "world", "id": foreign["id"]}
    denied = await editor.patch(url, json={"expected_ai_visibility_revision": 2, "links": [foreign_ref]})
    assert denied.status_code == 404
    assert (await editor.get(url)).json()["links"] == []
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Project.ai_visibility_revision).where(Project.id == project["id"])) == 2
        # 非人工调用继续保持原归属边界，不能令 Agent 隐式获得协作能力。
        with pytest.raises(NotFoundError):
            await setting_ai_visibility.read_settings(
                db, viewer_user["id"], project["id"], "character", character["id"],
            )
        with pytest.raises(NotFoundError):
            await setting_ai_visibility.write_settings(
                db, editor_user["id"], project["id"], "character", character["id"],
                EntryAiSettingsUpdate(expected_ai_visibility_revision=2, ai_mode="never"),
            )


async def test_editor_can_manage_visibility_of_definitions_and_legacy_fields(app, client, make_client):
    _, project, base, character = await _setup(client)
    viewer, editor = make_client(), make_client()
    await register(viewer, "field-visibility-viewer")
    editor_user = await register(editor, "field-visibility-editor")
    await _invite(client, viewer, project["id"], "field-visibility-viewer", "viewer")
    await _invite(client, editor, project["id"], "field-visibility-editor", "editor")
    fields = await _fields(client, base, character)
    timeline = f"{base}/setting-timelines/{fields['timeline_id']}"
    legacy = await client.post(f"{timeline}/text-fields", json={
        "expected_revision": fields["timeline_revision"], "label": "身份", "base_value": "原值",
    })
    assert legacy.status_code == 201, legacy.text
    fid = legacy.json()["item"]["id"]
    definition = await _definition(client, base, field_type="text")
    did = definition["item"]["id"]
    legacy_url = f"{base}/setting-entries/character/{character['id']}/legacy-fields/{fid}"
    legacy_body = {"expected_timeline_revision": legacy.json()["timeline_revision"],
                   "expected_ai_visibility_revision": 0, "ai_hidden": True}
    assert (await viewer.patch(f"{legacy_url}/ai-visibility", json=legacy_body)).status_code == 403
    hidden = await editor.patch(f"{legacy_url}/ai-visibility", json=legacy_body)
    assert hidden.status_code == 200, hidden.text
    definition_url = f"{base}/setting-field-definitions/{did}"
    denied = await viewer.patch(definition_url, json={"expected_fields_revision": 1, "ai_hidden": True})
    assert denied.status_code == 403
    changed = await editor.patch(definition_url, json={"expected_fields_revision": 1, "ai_hidden": True})
    assert changed.status_code == 200 and changed.json()["item"]["ai_hidden"], changed.text
    current = await _fields(viewer, base, character)
    binding = {**_versions(current), "definition_id": did}
    assert (await viewer.post(f"{legacy_url}/binding", json=binding)).status_code == 403
    bound = await editor.post(f"{legacy_url}/binding", json=binding)
    assert bound.status_code == 200, bound.text
    assert bound.json()["fields"][0]["ai_hidden"] is True
    assert bound.json()["fields"][0]["base_value"] == "原值"
    async with app.state.sessionmaker() as db:
        with pytest.raises(NotFoundError):
            await setting_field_service.bind_legacy(
                db, editor_user["id"], project["id"], "character", character["id"], fid,
                LegacyBinding.model_validate(binding),
            )
        with pytest.raises(NotFoundError):
            await setting_field_service.write_legacy_visibility(
                db, editor_user["id"], project["id"], "character", character["id"], fid,
                LegacyFieldAiVisibilityUpdate.model_validate(legacy_body),
            )


async def test_editor_can_create_hidden_definition_and_stale_write_is_atomic(app, client, make_client):
    _, project, base, character = await _setup(client)
    editor = make_client()
    await register(editor, "visibility-definition-editor")
    await _invite(client, editor, project["id"], "visibility-definition-editor", "editor")
    body = {"expected_fields_revision": 0, "label": "秘密", "field_type": "text",
            "applicable_types": ["character"], "ai_hidden": True}
    result = await editor.post(f"{base}/setting-field-definitions", json=body)
    assert result.status_code == 201, result.text
    assert result.json()["item"]["ai_hidden"] is True
    stale = await editor.patch(f"{base}/setting-field-definitions/{result.json()['item']['id']}", json={
        "expected_fields_revision": 0, "ai_hidden": False,
    })
    assert stale.status_code == 409
    async with app.state.sessionmaker() as db:
        item = await db.get(Character, character["id"])
        assert item.name == character["name"]
        assert (await db.get(Project, project["id"])).ai_visibility_revision == 1


async def test_admin_visibility_changes_use_allowed_non_sensitive_audit_fields(app, client, system_admin):
    _, project, base, character = await _setup(client)
    fields = await _fields(client, base, character)
    timeline = f"{base}/setting-timelines/{fields['timeline_id']}"
    legacy = await client.post(f"{timeline}/text-fields", json={
        "expected_revision": fields["timeline_revision"], "label": "敏感字段", "base_value": "不应进入审计",
    })
    fid = legacy.json()["item"]["id"]
    created = await _definition(system_admin, base, field_type="text", ai_hidden=True)
    assert created["item"]["ai_hidden"] is True
    path = f"{base}/setting-entries/character/{character['id']}/legacy-fields/{fid}"
    changed = await system_admin.patch(f"{path}/ai-visibility", json={
        "expected_timeline_revision": legacy.json()["timeline_revision"],
        "expected_ai_visibility_revision": 1, "ai_hidden": True,
    })
    assert changed.status_code == 200, changed.text
    bound = await system_admin.post(f"{path}/binding", json={
        **_versions(changed.json()), "definition_id": created["item"]["id"],
    })
    assert bound.status_code == 200, bound.text
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(AdminAuditLog).where(
            AdminAuditLog.project_id == project["id"], AdminAuditLog.result == "success",
        )))
        assert len(rows) == 3
        assert {tuple(row.summary["changed_fields"]) for row in rows} == {
            ("field_definitions",), ("setting_fields",),
        }
        assert all("不应进入审计" not in str(row.summary) for row in rows)


async def test_series_visibility_conflict_does_not_disclose_hidden_series(client, make_client):
    _, project, base, _ = await _setup(client)
    guest = make_client()
    await register(guest, "visibility-series-guest")
    response = await client.post("/api/v1/series", json={"name": "作者的私有系列"})
    assert response.status_code == 201, response.text
    series = response.json()
    series_url = f"/api/v1/series/{series['id']}"
    joined = await client.post(f"{series_url}/books", json={
        "project_id": project["id"], "expected_revision": series["revision"],
    })
    assert joined.status_code == 200, joined.text
    series = joined.json()
    entry = await client.post(f"{series_url}/entries", json={
        "kind": "character", "data": {"name": "共享角色"}, "expected_revision": series["revision"],
    })
    assert entry.status_code == 201, entry.text
    adopted = await client.post(f"{series_url}/entries/{entry.json()['item']['id']}/uses", json={
        "project_id": project["id"], "expected_revision": entry.json()["revision"],
    })
    assert adopted.status_code == 200, adopted.text
    local_id = adopted.json()["ref"]["id"]
    await _invite(client, guest, project["id"], "visibility-series-guest", "editor")
    url = f"{base}/setting-entries/character/{local_id}/ai-settings"
    current = await guest.get(url)
    assert current.status_code == 200, current.text
    denied = await guest.patch(url, json={
        "expected_ai_visibility_revision": current.json()["ai_visibility_revision"], "ai_mode": "never",
    })
    assert denied.status_code == 409 and denied.json()["code"] == "setting_edit_restricted", denied.text
    assert "系列" not in denied.text


@pytest.mark.parametrize("operation", ["settings", "definition", "legacy"])
async def test_denied_visibility_write_rolls_back_pending_changes(app, client, make_client, operation):
    _, project, base, character = await _setup(client)
    viewer = make_client()
    user = await register(viewer, "visibility-pending-viewer")
    await _invite(client, viewer, project["id"], "visibility-pending-viewer", "viewer")
    async with app.state.sessionmaker() as db:
        obj = await db.get(Character, character["id"])
        original = obj.profile
        obj.profile = "不得提前写入的内容"
        with pytest.raises(ForbiddenError):
            if operation == "settings":
                await setting_ai_visibility.write_settings(
                    db, user["id"], project["id"], "character", character["id"],
                    EntryAiSettingsUpdate(expected_ai_visibility_revision=0, ai_mode="never"), manual=True,
                )
            elif operation == "definition":
                await setting_field_service.save_definition(
                    db, user["id"], project["id"], DefinitionCreate(
                        expected_fields_revision=0, label="秘密", field_type="text",
                        applicable_types=["character"], ai_hidden=True,
                    ), manual=True,
                )
            else:
                await setting_field_service.write_legacy_visibility(
                    db, user["id"], project["id"], "character", character["id"], 1,
                    LegacyFieldAiVisibilityUpdate(
                        expected_ai_visibility_revision=0, expected_timeline_revision=0, ai_hidden=True,
                    ), manual=True,
                )
        # 调用方捕获权限异常后提交，也不能带上授权前暂存的数据。
        await db.commit()
    async with app.state.sessionmaker() as db:
        assert (await db.get(Character, character["id"])).profile == original


@pytest.mark.parametrize("operation", ["settings", "definition"])
async def test_editor_visibility_write_respects_sqlite_actual_send(app, client, make_client, monkeypatch, operation):
    _, project, base, character = await _setup(client)
    editor = make_client()
    await register(editor, "visibility-sqlite-sender-editor")
    await _invite(client, editor, project["id"], "visibility-sqlite-sender-editor", "editor")
    if operation == "settings":
        method = "PATCH"
        path = f"{base}/setting-entries/character/{character['id']}/ai-settings"
        payload = {"expected_ai_visibility_revision": 0, "ai_mode": "never"}
        success = 200
    else:
        method, path, success = "POST", f"{base}/setting-field-definitions", 201
        payload = {"expected_fields_revision": 0, "label": "秘密", "field_type": "text",
                   "applicable_types": ["character"], "ai_hidden": True}
    async with app.state.sessionmaker() as db:
        stamp = await read_visibility_stamp(db, project["id"])
    monkeypatch.setattr(ai_dispatch, "_LOCK_WAIT_SECONDS", 0.2)
    async with _server(payload=_sse("openai")) as (url, received, headers, body, requests):
        provider = guard_provider(OpenAICompatProvider("local-key", f"{url}/v1/", 5), stamp, app.state.sessionmaker)
        sending = asyncio.create_task(_collect(provider.generate_stream(
            [{"role": "user", "content": "设定"}], "test",
        )))
        try:
            await asyncio.wait_for(received.wait(), 5)
            blocked = await asyncio.wait_for(editor.request(method, path, json=payload), 3)
            assert blocked.status_code == 409 and blocked.json()["code"] == "ai_visibility_busy", blocked.text
            headers.set()
            body.set()
            assert await asyncio.wait_for(sending, 5) == ["ok"]
            saved = await editor.request(method, path, json=payload)
            assert saved.status_code == success, saved.text
            with pytest.raises(VisibilityChangedError):
                await _collect(provider.generate_stream([{"role": "user", "content": "设定"}], "test"))
            assert len(requests) == 1
        finally:
            headers.set()
            body.set()
            if not sending.done():
                sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
