"""AI 限制在当前项目备份／效果和真实旧v4效果中的兼容、映射与原子性。"""

import json
from contextlib import aclosing
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.core.errors import ConflictError
from app.models import (
    Character,
    Project,
    SettingAiLink,
    SettingFieldDefinition,
    SettingReferenceHandle,
    SettingTextField,
    SettingTimeline,
)
from app.schemas.setting_ai_visibility import EntryAiSettingsUpdate, StoredAiSettings
from app.schemas.setting_fields import DefinitionUpdate
from app.schemas.story import WorldSettingUpdate
from app.schemas.transfer import BackupOptions
from app.services import setting_ai_visibility, setting_field_service, story_service
from app.services.agent import effects
from app.services.project_transfer import exporter
from tests.test_agent_tools import ctx as ctx
from tests.test_project_transfer import _backup, _import, _preview_raw, _without_ai_settings
from tests.test_setting_ai_visibility import _created, _edited, _settings, _state
from tests.test_setting_fields import _definition, _fields, _setup, _versions


async def _protected_backup(client):
    _, project, base, character = await _setup(client)
    world = await _created(client, f"{base}/world", title="失落秘境", content="受保护的设定原文")
    await _edited(
        client, _settings(base, "world", world["id"]), 0, ai_mode="never", ai_hidden_builtin_fields=["content"],
    )
    await _edited(
        client, _settings(base, "character", character["id"]), 1, ai_mode="manual_only", mention_enabled=False,
        mention_exclusions=["路径名"], links=[{"kind": "world", "id": world["id"]}],
    )
    current = await _fields(client, base, character)
    timeline_path = f"{base}/setting-timelines/{current['timeline_id']}"
    field = await _created(client, f"{timeline_path}/text-fields", expected_revision=current["timeline_revision"],
                           label="秘密", base_value="受保护的字段原文")
    entry_path = f"{base}/setting-entries/character/{character['id']}"
    response = await client.patch(f"{entry_path}/legacy-fields/{field['item']['id']}/ai-visibility", json={
        "expected_timeline_revision": field["timeline_revision"], "expected_ai_visibility_revision": 2,
        "ai_hidden": True,
    })
    assert response.status_code == 200, response.text
    definition = (await _definition(client, base, ai_hidden=True))["item"]
    current = await _fields(client, base, character)
    baseline = await client.patch(f"{entry_path}/fields/{definition['id']}/baseline", json={
        **_versions(current), "field_type": "line", "value": "受保护的共享字段原文",
    })
    assert baseline.status_code == 200, baseline.text
    return project, await _backup(client, project["id"])


async def test_v6_roundtrip_remaps_links_and_does_not_export_or_copy_runtime_counters(client, app):
    source, data = await _protected_backup(client)
    assert await _state(app, source["id"]) == (4, 3)
    assert data["version"] == 9 and data["app_schema"] == "0041"
    assert {"ai_visibility_revision", "ai_history_epoch"}.isdisjoint(data["project"])
    before = data["tables"]
    assert before["world_settings"][0]["content"] == "受保护的设定原文"
    assert before["setting_field_definitions"][0]["ai_hidden"] is True
    assert {row["base_value"]: row["ai_hidden"] for row in before["setting_text_fields"]} == {
        "受保护的字段原文": True, "受保护的共享字段原文": False,
    }
    imported = await _import(client, data)
    pid = imported["project"]["id"]
    assert pid != source["id"] and await _state(app, pid) == (0, 0)
    after = (await _backup(client, pid))["tables"]
    old_link, new_link = before["setting_ai_links"][0], after["setting_ai_links"][0]
    assert new_link["id"] != old_link["id"]
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(SettingAiLink.project_id).where(SettingAiLink.id == new_link["id"])) == pid
    assert new_link["source_handle_id"] != old_link["source_handle_id"]
    assert new_link["target_handle_id"] != old_link["target_handle_id"]
    handles = {row["id"]: row for row in after["setting_reference_handles"]}
    cid = after["characters"][0]["id"]
    wid = after["world_settings"][0]["id"]
    assert handles[new_link["source_handle_id"]]["character_id"] == cid
    assert handles[new_link["target_handle_id"]]["world_setting_id"] == wid
    base = f"/api/v1/projects/{pid}"
    settings = (await client.get(_settings(base, "character", cid))).json()
    assert settings["ai_mode"] == "manual_only" and settings["mention_enabled"] is False
    assert settings["mention_exclusions"] == ["路径名"] and settings["links"][0]["reference"]["id"] == wid
    assert (await client.get(_settings(base, "world", wid))).json()["ai_hidden_builtin_fields"] == ["content"]
    fields = await _fields(client, base, {"id": cid})
    assert fields["legacy_fields"][0]["value"] == "受保护的字段原文"
    assert fields["fields"][0]["value"] == "受保护的共享字段原文"
    assert fields["legacy_fields"][0]["ai_hidden"] and fields["fields"][0]["ai_hidden"]


async def test_v6_rejects_missing_invalid_cross_project_or_disguised_ai_data(client, app):
    source, data = await _protected_backup(client)
    for mutation in ("missing_config", "unknown_field", "duplicate_phrase", "missing_flag", "hidden_shared_value",
                     "missing_links", "unknown_endpoint", "scope", "self", "duplicate", "old_version", "old_hidden"):
        broken = deepcopy(data)
        tables = broken["tables"]
        link = tables["setting_ai_links"][0]
        character = tables["characters"][0]
        if mutation == "missing_config":
            character.pop("ai_mode")
        elif mutation == "unknown_field":
            character["ai_hidden_builtin_fields"] = ["name"]
        elif mutation == "duplicate_phrase":
            character.update(mention_case_sensitive=False, mention_exclusions=["Lan", "LAN"])
        elif mutation == "missing_flag":
            tables["setting_field_definitions"][0].pop("ai_hidden")
        elif mutation == "hidden_shared_value":
            next(row for row in tables["setting_text_fields"] if row["definition_id"] is not None)["ai_hidden"] = True
        elif mutation == "missing_links":
            tables.pop("setting_ai_links")
        elif mutation == "unknown_endpoint":
            link["target_handle_id"] = 2_147_483_647
        elif mutation == "scope":
            tables["setting_reference_handles"][0]["project_id"] = source["id"] + 1
        elif mutation == "self":
            link["target_handle_id"] = link["source_handle_id"]
        elif mutation == "duplicate":
            tables["setting_ai_links"].append({**link, "id": link["id"] + 1})
        elif mutation == "old_version":
            broken["version"] = 5
        else:
            _without_ai_settings(broken, 5)
            tables["setting_field_definitions"][0]["ai_hidden"] = True
        result = await _preview_raw(client, json.dumps(broken).encode())
        assert result.status_code == 400, (mutation, result.text)
    assert await _state(app, source["id"]) == (4, 3)
    old = await _import(client, _without_ai_settings(deepcopy(data), 5))
    new_pid = old["project"]["id"]
    assert await _state(app, new_pid) == (0, 0)
    after = (await _backup(client, new_pid))["tables"]
    assert after["setting_ai_links"] == []
    assert {key: after["characters"][0][key] for key in StoredAiSettings.model_fields} == {
        "ai_mode": None, "mention_enabled": None, "mention_case_sensitive": True,
        "mention_exclusions": [], "ai_hidden_builtin_fields": [],
    }
    assert all(not row["ai_hidden"] for row in after["setting_text_fields"] + after["setting_field_definitions"])
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(Character.ai_mode.is_(None)).where(Character.project_id == new_pid)) is True


async def test_streamed_export_will_not_finish_a_mix_of_two_policy_revisions(client, app):
    user, project, base, character = await _setup(client)
    async with app.state.sessionmaker() as db:
        plan = await exporter.prepare_export(db, user["id"], project["id"], BackupOptions())
    async with aclosing(exporter.stream_export(app.state.sessionmaker, plan)) as stream:
        assert (await anext(stream)).startswith(b"{")
        await _edited(client, _settings(base, "character", character["id"]), 0, ai_mode="never")
        with pytest.raises(ConflictError) as stale:
            async for chunk in stream:
                assert chunk != b"}}"
        assert stale.value.extra["scope"] == "ai_visibility"


async def _new_legacy_character(ctx, name):
    entry = await story_service.new_character(ctx.db, ctx.project_id, {"name": name, "ai_mode": None})
    await ctx.db.commit()
    return entry


def _v4(captured):
    old = deepcopy(captured)
    for effect in old:
        assert effect["table"] != "setting_ai_links"
        effect["format_version"] = 4
        for side in ("before", "after"):
            if effect[side] is not None:
                for name in effects._legacy_visibility_defaults(effect["table"]):
                    effect[side].pop(name)
    return old


async def _author_change(ctx, kind, ident, revision, **data):
    return await setting_ai_visibility.write_settings(
        ctx.db, ctx.user_id, ctx.project_id, kind, ident,
        EntryAiSettingsUpdate(expected_ai_visibility_revision=revision, **data),
    )


async def _counters(ctx):
    return (await ctx.db.execute(select(Project.ai_visibility_revision, Project.ai_history_epoch)
                                 .where(Project.id == ctx.project_id))).one()


async def test_v5_refuses_author_changes_and_v4_only_updates_its_own_content(ctx):
    entry = await _new_legacy_character(ctx, "旧名")
    before = await effects.snapshot(ctx.db, ctx.project_id)
    entry.name = "智能体改名"
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    assert all(effect["format_version"] == 6 for effect in delta)
    assert {"ai_visibility_revision", "ai_history_epoch"}.isdisjoint(before["projects"][str(ctx.project_id)])
    await ctx.db.commit()
    await _author_change(ctx, "character", entry.id, 0, ai_mode="never", ai_hidden_builtin_fields=["profile"])
    protected = await effects.snapshot(ctx.db, ctx.project_id)
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, delta)
    assert exc.value.extra["code"] == "undo_conflict"
    assert await effects.snapshot(ctx.db, ctx.project_id) == protected
    await effects.undo(ctx.db, ctx.project_id, _v4(delta))
    restored = (await effects.snapshot(ctx.db, ctx.project_id))["characters"][str(entry.id)]
    assert restored["name"] == "旧名" and restored["ai_mode"] == "never"
    assert restored["ai_hidden_builtin_fields"] == ["profile"] and await _counters(ctx) == (1, 1)


async def test_v4_undo_created_entry_refuses_destroying_later_policy_and_deleted_entry_uses_sql_null(ctx):
    baseline = await effects.snapshot(ctx.db, ctx.project_id)
    entry = await _new_legacy_character(ctx, "旧条目")
    current = await effects.snapshot(ctx.db, ctx.project_id)
    creation = _v4(effects.diff(baseline, current))
    await _author_change(ctx, "character", entry.id, 0, ai_mode="manual_only")
    protected = await effects.snapshot(ctx.db, ctx.project_id)
    with pytest.raises(ConflictError):
        await effects.undo(ctx.db, ctx.project_id, creation)
    assert await effects.snapshot(ctx.db, ctx.project_id) == protected
    await ctx.db.commit()

    other = await _new_legacy_character(ctx, "另一个旧条目")
    ident = other.id
    before = await effects.snapshot(ctx.db, ctx.project_id)
    await ctx.db.delete(other)
    deletion = _v4(effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id)))
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, deletion)
    assert await ctx.db.scalar(select(Character.ai_mode.is_(None)).where(Character.id == ident)) is True
    assert await _counters(ctx) == (1, 1)


async def test_v4_undo_cannot_cascade_a_later_link_using_the_original_handle(ctx):
    source = await _new_legacy_character(ctx, "关联源")
    target = await _new_legacy_character(ctx, "关联目标")
    baseline = await effects.snapshot(ctx.db, ctx.project_id)
    ctx.db.add(SettingReferenceHandle(project_id=ctx.project_id, kind="character", character_id=source.id))
    delta = _v4(effects.diff(baseline, await effects.snapshot(ctx.db, ctx.project_id)))
    await ctx.db.commit()
    await _author_change(ctx, "character", source.id, 0, links=[{"kind": "character", "id": target.id}])
    protected = await effects.snapshot(ctx.db, ctx.project_id)
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, delta)
    assert exc.value.extra["code"] == "undo_conflict"
    assert await effects.snapshot(ctx.db, ctx.project_id) == protected
    assert await _counters(ctx) == (1, 0)


async def test_v5_undo_deletion_restores_links_without_rewinding_visibility_generation(ctx):
    source = await _new_legacy_character(ctx, "关联源")
    target = await _new_legacy_character(ctx, "关联目标")
    ident = target.id
    await _author_change(ctx, "character", source.id, 0, links=[{"kind": "character", "id": ident}])
    before = await effects.snapshot(ctx.db, ctx.project_id)
    await story_service.delete_owned(ctx.db, story_service.CHARACTERS, ctx.user_id, ctx.project_id, ident)
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    assert [effect for effect in delta if effect["table"] == "setting_ai_links"][0]["after"] is None
    assert await _counters(ctx) == (2, 1)
    await effects.undo(ctx.db, ctx.project_id, delta)
    after = await effects.snapshot(ctx.db, ctx.project_id)
    assert after["setting_ai_links"] == before["setting_ai_links"]
    assert after["setting_reference_handles"] == before["setting_reference_handles"]
    assert await _counters(ctx) == (3, 1)


async def test_v5_undo_restoring_legacy_tracking_invalidates_previously_unique_matches(ctx):
    entry = await story_service.new_world(ctx.db, ctx.project_id, {
        "title": "归港", "category": "place", "ai_mode": None,
    })
    await ctx.db.commit()
    before = await effects.snapshot(ctx.db, ctx.project_id)
    await story_service.update_owned(
        ctx.db, story_service.WORLD, ctx.user_id, ctx.project_id, entry.id, WorldSettingUpdate(category="history"),
    )
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    assert await _counters(ctx) == (1, 1)
    await effects.undo(ctx.db, ctx.project_id, delta)
    assert entry.category == "place"
    assert await _counters(ctx) == (2, 2)


async def test_v4_undo_binding_cannot_expose_later_hidden_shared_field(ctx):
    entry = await _new_legacy_character(ctx, "字段属主")
    timeline = await ctx.db.scalar(select(SettingTimeline).where(SettingTimeline.character_id == entry.id))
    field = SettingTextField(timeline_id=timeline.id, label="旧字段", base_value="受保护原文")
    definition = SettingFieldDefinition(project_id=ctx.project_id, label="多行", field_type="text",
                                        applicable_types=["character"])
    ctx.db.add_all([field, definition])
    await ctx.db.commit()
    baseline = await effects.snapshot(ctx.db, ctx.project_id)
    field.definition_id, field.label = definition.id, None
    delta = _v4(effects.diff(baseline, await effects.snapshot(ctx.db, ctx.project_id)))
    await ctx.db.commit()
    await setting_field_service.save_definition(ctx.db, ctx.user_id, ctx.project_id, DefinitionUpdate(
        expected_fields_revision=0, ai_hidden=True,
    ), definition.id)
    protected = await effects.snapshot(ctx.db, ctx.project_id)
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, delta)
    assert exc.value.extra["code"] == "undo_conflict"
    assert await effects.snapshot(ctx.db, ctx.project_id) == protected
    assert await _counters(ctx) == (1, 1)
