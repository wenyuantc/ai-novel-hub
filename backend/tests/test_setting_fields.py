"""共享字段配置、目录、原子首写和旧事件路由的 HTTP 合同。"""

from datetime import datetime

import pytest
from sqlalchemy import event, func, select, update

from app.models import AdminAuditLog, Character, Project, SettingProgression, SettingTextField, SettingTimeline
from app.models.setting_fields import SettingFieldDefinition, SettingReferenceHandle
from app.schemas.setting_fields import EntityRef
from app.services import setting_field_service as fields_service
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role


async def _setup(client):
    user = await register(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}"
    character = (await client.post(f"{base}/characters", json={"name": "林岚"})).json()
    return user, project, base, character


async def _definition(client, base, revision=0, **extra):
    response = await client.post(
        f"{base}/setting-field-definitions",
        json={
            "expected_fields_revision": revision,
            "label": "境界",
            "field_type": "line",
            "applicable_types": ["character"],
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _fields(client, base, character, **params):
    response = await client.get(f"{base}/setting-entries/character/{character['id']}/fields", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _versions(fields):
    return {
        "expected_timeline_revision": fields["timeline_revision"],
        "expected_fields_revision": fields["fields_revision"],
    }


async def test_definitions_options_have_stable_identity_and_project_cas(client):
    _, _, base, _ = await _setup(client)
    made = await _definition(client, base, field_type="dropdown")
    definition = made["item"]
    assert made["fields_revision"] == 1
    option = await client.post(
        f"{base}/setting-field-definitions/{definition['id']}/options",
        json={
            "expected_fields_revision": 1,
            "label": "练气",
        },
    )
    assert option.status_code == 201, option.text
    assert option.json()["fields_revision"] == 2
    stale = await client.patch(
        f"{base}/setting-field-definitions/{definition['id']}",
        json={
            "expected_fields_revision": 1,
            "label": "不应保存",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_revision" and stale.json()["scope"] == "fields"
    changed = await client.patch(
        f"{base}/setting-field-definitions/{definition['id']}",
        json={
            "expected_fields_revision": 2,
            "label": "修为",
        },
    )
    assert changed.status_code == 200, changed.text
    listed = (await client.get(f"{base}/setting-field-definitions")).json()
    assert listed["fields_revision"] == 3
    assert listed["items"][0]["label"] == "修为"
    assert listed["items"][0]["options"][0]["id"] == option.json()["item"]["id"]


async def test_empty_slots_are_read_only_and_directory_filters_all_entry_types(client, app):
    _, project, base, character = await _setup(client)
    for category in ("place", "faction", "item", "rule", "history", "other"):
        assert (
            await client.post(f"{base}/world", json={"title": f"世界{category}", "category": category})
        ).status_code == 201
    assert (await client.post(f"{base}/organizations", json={"name": "门派"})).status_code == 201
    assert (
        await client.post(f"{base}/careers", json={"name": "剑修", "stages": [{"name": "初阶"}]})
    ).status_code == 201
    made = await _definition(client, base)
    async with app.state.sessionmaker() as db:
        before = [
            await db.scalar(select(func.count()).select_from(model))
            for model in (
                SettingTextField,
                SettingTimeline,
                SettingReferenceHandle,
            )
        ]
    fields = await _fields(client, base, character)
    assert fields["fields"][0]["field_id"] is None
    assert fields["fields"][0]["definition_id"] == made["item"]["id"]
    assert fields["fields"][0]["value"] == ""
    page = await client.get(
        f"{base}/setting-entries", params={"types": ["world.place", "organization"], "page_size": 1}
    )
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 2 and len(page.json()["items"]) == 1
    all_entries = (await client.get(f"{base}/setting-entries")).json()
    assert all_entries["total"] == 9
    assert {entry["type"] for entry in all_entries["items"]} == {
        "character",
        "organization",
        "career",
        *(f"world.{v}" for v in ("place", "faction", "item", "rule", "history", "other")),
    }
    assert (await client.get(f"{base}/setting-entries", params={"q": "%"})).json()["total"] == 0
    async with app.state.sessionmaker() as db:
        after = [
            await db.scalar(select(func.count()).select_from(model))
            for model in (
                SettingTextField,
                SettingTimeline,
                SettingReferenceHandle,
            )
        ]
    assert after == before
    assert project["id"] > 0


async def test_first_baseline_and_event_are_atomic_and_keep_early_values(client, app):
    _, _, base, character = await _setup(client)
    made = await _definition(client, base)
    did = made["item"]["id"]
    fields = await _fields(client, base, character)
    baseline_url = f"{base}/setting-entries/character/{character['id']}/fields/{did}/baseline"
    invalid = await client.patch(baseline_url, json={**_versions(fields), "field_type": "line", "value": "有\n换行"})
    assert invalid.status_code in (400, 422), invalid.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(SettingTextField)) == 0
    saved = await client.patch(baseline_url, json={**_versions(fields), "field_type": "line", "value": "练气"})
    assert saved.status_code == 200, saved.text
    fields = await _fields(client, base, character)
    field_id = fields["fields"][0]["field_id"]
    assert field_id is not None and fields["fields"][0]["base_value"] == "练气"
    change = await client.post(
        f"{base}/setting-entries/character/{character['id']}/fields/{did}/progressions",
        json={
            **_versions(fields),
            "field_type": "line",
            "value": "筑基",
            "operation": "replace",
            "anchor_kind": "planned",
            "chapter_no": 20,
            "boundary": "end",
        },
    )
    assert change.status_code == 201, change.text
    assert change.json()["item"]["custom_field_id"] == field_id
    assert (await _fields(client, base, character, chapter_no=5))["fields"][0]["value"] == "练气"
    assert (await _fields(client, base, character, chapter_no=20))["fields"][0]["value"] == "练气"
    assert (await _fields(client, base, character, chapter_no=21))["fields"][0]["value"] == "筑基"


async def test_first_event_does_not_require_field_id_or_leave_binding_after_bad_anchor(client, app):
    _, _, base, character = await _setup(client)
    made = await _definition(client, base)
    fields = await _fields(client, base, character)
    url = f"{base}/setting-entries/character/{character['id']}/fields/{made['item']['id']}/progressions"
    body = {**_versions(fields), "field_type": "line", "value": "筑基", "operation": "replace"}
    rejected = await client.post(url, json={**body, "anchor_kind": "chapter", "chapter_id": 999999})
    assert rejected.status_code in (400, 404), rejected.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(SettingTextField)) == 0
    created = await client.post(url, json={**body, "anchor_kind": "planned", "chapter_no": 20})
    assert created.status_code == 201, created.text
    assert (await _fields(client, base, character, chapter_no=5))["fields"][0]["value"] == ""
    assert (await _fields(client, base, character, chapter_no=20))["fields"][0]["value"] == "筑基"


async def test_reference_public_codec_patch_omission_clear_and_archived_history(client):
    _, _, base, character = await _setup(client)
    made = await _definition(client, base, field_type="reference", reference_types=["character"])
    did = made["item"]["id"]
    fields = await _fields(client, base, character)
    ref = {"kind": "character", "id": character["id"]}
    created = await client.post(
        f"{base}/setting-entries/character/{character['id']}/fields/{did}/progressions",
        json={
            **_versions(fields),
            "field_type": "reference",
            "value": ref,
            "operation": "replace",
            "anchor_kind": "planned",
            "chapter_no": 20,
        },
    )
    assert created.status_code == 201, created.text
    item = created.json()["item"]
    assert item["value"] == ref
    timeline_url = f"{base}/setting-timelines/{item['timeline_id']}"
    event_url = f"{timeline_url}/progressions/{item['id']}"
    revision = created.json()["timeline_revision"]
    missing = await client.patch(event_url, json={"expected_revision": revision, "note": "仅备注"})
    assert missing.status_code in (400, 422), missing.text
    archived = await client.patch(
        f"{base}/setting-field-definitions/{did}",
        json={
            "expected_fields_revision": 1,
            "archived": True,
        },
    )
    assert archived.status_code == 200, archived.text
    edited = await client.patch(
        event_url,
        json={
            "expected_revision": revision,
            "expected_fields_revision": 2,
            "note": "保留原引用",
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["item"]["value"] == ref
    assert (await client.get(f"{timeline_url}/progressions")).json()["items"][0]["value"] == ref
    assert (await client.get(event_url)).json()["item"]["value"] == ref
    effective = await client.get(f"{timeline_url}/effective", params={"chapter_no": 21})
    assert effective.status_code == 200, effective.text
    assert effective.json()["custom_fields"] == {}
    assert effective.json()["field_values"][str(item["custom_field_id"])]["value"] == ref
    cleared = await client.patch(
        event_url,
        json={
            "expected_revision": edited.json()["timeline_revision"],
            "expected_fields_revision": 2,
            "value": None,
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["item"]["value"] is None


async def test_legacy_binding_retains_field_and_event_identity_and_rejects_merge(client, app):
    _, _, base, character = await _setup(client)
    fields = await _fields(client, base, character)
    url = f"{base}/setting-timelines/{fields['timeline_id']}"
    legacy = await client.post(
        f"{url}/text-fields",
        json={
            "expected_revision": fields["timeline_revision"],
            "label": "境界",
            "base_value": "旧文",
        },
    )
    assert legacy.status_code == 201, legacy.text
    fid = legacy.json()["item"]["id"]
    changed = await client.post(
        f"{url}/progressions",
        json={
            "expected_revision": legacy.json()["timeline_revision"],
            "custom_field_id": fid,
            "operation": "append",
            "value": "后来",
            "anchor_kind": "planned",
            "chapter_no": 20,
        },
    )
    assert changed.status_code == 201, changed.text
    definition = await _definition(client, base, field_type="text")
    original_time = datetime(2020, 1, 2, 3, 4, 5)
    async with app.state.sessionmaker() as db:
        await db.execute(
            update(SettingTextField)
            .where(SettingTextField.id == fid)
            .values(
                created_at=original_time,
                updated_at=original_time,
            )
        )
        await db.commit()
    fields = await _fields(client, base, character)
    assert len(fields["legacy_fields"]) == 1 and len(fields["fields"]) == 1
    linked = await client.post(
        f"{base}/setting-entries/character/{character['id']}/legacy-fields/{fid}/binding",
        json={
            **_versions(fields),
            "definition_id": definition["item"]["id"],
        },
    )
    assert linked.status_code == 200, linked.text
    async with app.state.sessionmaker() as db:
        field = await db.get(SettingTextField, fid)
        assert field.created_at == original_time and field.updated_at == original_time
    fields = await _fields(client, base, character, chapter_no=21)
    assert fields["legacy_fields"] == []
    assert fields["fields"][0]["field_id"] == fid
    assert fields["fields"][0]["value"] == "旧文\n后来"
    assert (await client.get(f"{url}/progressions")).json()["items"][0]["id"] == changed.json()["item"]["id"]
    denied = await client.patch(
        f"{url}/text-fields/{fid}",
        json={
            "expected_revision": fields["timeline_revision"],
            "label": "不应改共享配置",
        },
    )
    assert denied.status_code in (400, 409)


async def test_shared_field_routes_enforce_project_isolation_and_readonly(client, make_client, system_admin):
    _, _, base, character = await _setup(client)
    made = await _definition(client, base)
    outsider = make_client()
    await register(outsider, "outsider")
    for path in ("setting-field-definitions", "setting-entries", f"setting-entries/character/{character['id']}/fields"):
        assert (await outsider.get(f"{base}/{path}")).status_code == 404
    viewer = make_client()
    user = await register(viewer, "field_viewer")
    role = await create_role(system_admin, "仅看作品字段", ["projects.read"])
    await assign_roles(system_admin, user["id"], [role["id"]])
    fields = await _fields(viewer, base, character)
    assert (await viewer.get(f"{base}/setting-field-definitions")).status_code == 200
    denied = await viewer.patch(
        f"{base}/setting-entries/character/{character['id']}/fields/{made['item']['id']}/baseline",
        json={
            **_versions(fields),
            "field_type": "line",
            "value": "不应保存",
        },
    )
    assert denied.status_code == 403, denied.text


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "character", "id": True},
        {"kind": "character", "id": "1"},
        {"kind": "character", "id": 2_147_483_648},
        {"reference_handle_id": 1},
    ],
)
async def test_reference_rejects_pseudo_ids_and_internal_values_without_partial_binding(client, app, value):
    _, _, base, character = await _setup(client)
    made = await _definition(client, base, field_type="reference", reference_types=["character"])
    data = await _fields(client, base, character)
    url = f"{base}/setting-entries/character/{character['id']}/fields/{made['item']['id']}/baseline"
    response = await client.patch(url, json={**_versions(data), "field_type": "reference", "value": value})
    assert response.status_code in (400, 422), response.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(SettingTextField)) == 0
        assert await db.scalar(select(func.count()).select_from(SettingReferenceHandle)) == 0


async def test_option_history_inactive_and_orphan_protect_identity_and_scope(client, app):
    _, _, base, character = await _setup(client)
    made = await _definition(client, base, field_type="dropdown", applicable_types=["character", "organization"])
    did = made["item"]["id"]
    option = await client.post(
        f"{base}/setting-field-definitions/{did}/options",
        json={
            "expected_fields_revision": 1,
            "label": "练气",
        },
    )
    oid = option.json()["item"]["id"]
    data = await _fields(client, base, character)
    created = await client.post(
        f"{base}/setting-entries/character/{character['id']}/fields/{did}/progressions",
        json={
            **_versions(data),
            "field_type": "dropdown",
            "value": {"option_id": oid},
            "operation": "replace",
            "anchor_kind": "planned",
            "chapter_no": 20,
        },
    )
    assert created.status_code == 201, created.text
    item = created.json()["item"]
    event_url = f"{base}/setting-timelines/{item['timeline_id']}/progressions/{item['id']}"
    missing = await client.delete(event_url, params={"expected_revision": created.json()["timeline_revision"]})
    assert missing.status_code == 400
    stale = await client.delete(
        event_url,
        params={
            "expected_revision": created.json()["timeline_revision"],
            "expected_fields_revision": 1,
        },
    )
    assert stale.status_code == 409 and stale.json()["scope"] == "fields"
    deleted = await client.delete(
        event_url,
        params={
            "expected_revision": created.json()["timeline_revision"],
            "expected_fields_revision": 2,
        },
    )
    assert deleted.status_code == 200, deleted.text
    async with app.state.sessionmaker() as db:
        row = await db.get(SettingProgression, item["id"])
        row.anchor_kind, row.chapter_id, row.chapter_no = "chapter", None, None
        await db.commit()
    for url, payload in (
        (f"{base}/setting-field-definitions/{did}/options/{oid}", None),
        (f"{base}/setting-field-definitions/{did}", None),
        (f"{base}/setting-field-definitions/{did}", {"applicable_types": ["organization"]}),
        (f"{base}/setting-field-definitions/{did}", {"field_type": "line"}),
    ):
        if payload is None:
            response = await client.delete(url, params={"expected_fields_revision": 2})
        else:
            response = await client.patch(url, json={"expected_fields_revision": 2, **payload})
        assert response.status_code == 409, response.text
        assert response.json()["code"] in ("option_in_use", "field_in_use", "incompatible_field_scope")
    detail = await client.get(event_url)
    assert detail.status_code == 200 and detail.json()["item"]["value"] == {"option_id": oid}
    assert detail.json()["item"]["orphaned"] and not detail.json()["item"]["active"]


async def test_directory_read_refuses_metadata_with_a_new_revision(client, app, monkeypatch):
    _, project, base, _ = await _setup(client)
    made = await _definition(client, base)
    original = fields_service._metadata
    changed = False

    async def change_between_reads(db, project_id):
        nonlocal changed
        result = await original(db, project_id)
        if not changed:
            changed = True
            async with app.state.sessionmaker() as writer:
                await writer.execute(
                    update(SettingFieldDefinition)
                    .where(
                        SettingFieldDefinition.id == made["item"]["id"],
                    )
                    .values(label="并发改名")
                )
                await writer.execute(update(Project).where(Project.id == project["id"]).values(fields_revision=2))
                await writer.commit()
        return result

    monkeypatch.setattr(fields_service, "_metadata", change_between_reads)
    response = await client.get(f"{base}/setting-field-definitions")
    assert response.status_code == 409, response.text
    assert response.json()["scope"] == "fields" and response.json()["current_revision"] == 2


async def test_entry_read_refuses_a_timeline_changed_during_projection(client, app, monkeypatch):
    _, _, base, character = await _setup(client)
    made = await _definition(client, base)
    data = await _fields(client, base, character)
    url = f"{base}/setting-entries/character/{character['id']}/fields/{made['item']['id']}/baseline"
    saved = await client.patch(url, json={**_versions(data), "field_type": "line", "value": "旧值"})
    assert saved.status_code == 200, saved.text
    data = saved.json()
    from app.services import setting_progression_service as progressions

    original = progressions.resolve_timelines

    async def change_after_projection(db, project_id, timelines, position=None, **kwargs):
        result = await original(db, project_id, timelines, position, **kwargs)
        async with app.state.sessionmaker() as writer:
            await writer.execute(
                update(SettingTimeline)
                .where(
                    SettingTimeline.id == data["timeline_id"],
                )
                .values(revision=data["timeline_revision"] + 1)
            )
            await writer.execute(
                update(SettingTextField)
                .where(
                    SettingTextField.id == data["fields"][0]["field_id"],
                )
                .values(base_value="新值")
            )
            await writer.commit()
        return result

    monkeypatch.setattr(progressions, "resolve_timelines", change_after_projection)
    response = await client.get(f"{base}/setting-entries/character/{character['id']}/fields")
    assert response.status_code == 409, response.text
    assert response.json()["scope"] == "timeline"


async def test_all_field_reads_preserve_callers_pending_objects(client, app):
    user, project, base, character = await _setup(client)
    await _definition(client, base)
    async with app.state.sessionmaker() as db:
        pending = Character(project_id=project["id"], name="未提交草稿")
        db.add(pending)

        def reject_flush(*_):
            raise AssertionError("只读字段查询触发了调用方待写对象")

        event.listen(db.sync_session, "before_flush", reject_flush)
        try:
            await fields_service.list_definitions(db, user["id"], project["id"])
            await fields_service.read_entry_fields(db, user["id"], project["id"], "character", character["id"])
            await fields_service.resolve_entry_fields(
                db, project["id"], [EntityRef(kind="character", id=character["id"])]
            )
            assert pending in db.new and pending.id is None
        finally:
            event.remove(db.sync_session, "before_flush", reject_flush)


async def test_cached_metadata_never_pairs_old_definitions_with_a_new_revision(client, app):
    user, project, base, _ = await _setup(client)
    made = await _definition(client, base)
    async with app.state.sessionmaker() as reader:
        cached = await reader.get(SettingFieldDefinition, made["item"]["id"])
        assert cached.label == "境界"
        async with app.state.sessionmaker() as writer:
            await writer.execute(
                update(SettingFieldDefinition)
                .where(
                    SettingFieldDefinition.id == cached.id,
                )
                .values(label="现在的修为")
            )
            await writer.execute(update(Project).where(Project.id == project["id"]).values(fields_revision=2))
            await writer.commit()
        result = await fields_service.list_definitions(reader, user["id"], project["id"])
        assert result.fields_revision == 2
        assert result.items[0].label == "现在的修为"
        assert cached.label == "境界"  # 读投影不覆盖调用方持有的 ORM 状态。


async def test_admin_field_writes_audit_real_actor_without_recording_values(client, app, system_admin):
    _, project, base, character = await _setup(client)
    made = await _definition(system_admin, base)
    data = await _fields(system_admin, base, character)
    marker = "不进入审计的字段正文"
    saved = await system_admin.patch(
        f"{base}/setting-entries/character/{character['id']}/fields/{made['item']['id']}/baseline",
        json={**_versions(data), "field_type": "line", "value": marker},
    )
    assert saved.status_code == 200, saved.text
    created = await system_admin.post(
        f"{base}/setting-timelines/{data['timeline_id']}/progressions",
        json={
            "expected_revision": saved.json()["timeline_revision"],
            "expected_fields_revision": 1,
            "custom_field_id": saved.json()["fields"][0]["field_id"],
            "operation": "replace",
            "value": marker,
            "anchor_kind": "planned",
            "chapter_no": 20,
        },
    )
    assert created.status_code == 201, created.text
    admin_id = (await system_admin.get("/api/v1/auth/me")).json()["id"]
    async with app.state.sessionmaker() as db:
        rows = list(
            await db.scalars(
                select(AdminAuditLog).where(AdminAuditLog.project_id == project["id"]).order_by(AdminAuditLog.id)
            )
        )
        assert [row.summary["changed_fields"] for row in rows] == [
            ["field_definitions"],
            ["setting_fields"],
            ["setting_fields"],
        ]
        assert all(row.actor_user_id == admin_id and row.action == "project.update" for row in rows)
        assert marker not in str([row.summary for row in rows])
