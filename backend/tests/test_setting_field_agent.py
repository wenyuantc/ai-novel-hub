"""类型化字段工具与当前 effects 的权限、历史依赖和事务合同。"""

from copy import deepcopy

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select, update

from app.core.errors import ConflictError
from app.models import (
    AgentChange,
    AgentConversation,
    Chapter,
    Character,
    Organization,
    Project,
    SettingProgression,
    SettingTextField,
    SettingTimeline,
)
from app.models.setting_fields import SettingFieldDefinition, SettingFieldOption, SettingReferenceHandle
from app.services import setting_field_service
from app.services.agent import effects
from app.services.agent.service import _undo_changes
from app.services.agent.tools import get_tool
from app.services.agent.tools.base import preview_diff
from tests.test_agent_tools import ctx as ctx
from tests.test_agent_tools import new_chapter, new_character


async def _field(ctx, field_type="line", *, binding=True):
    character = await new_character(ctx)
    definition = SettingFieldDefinition(
        project_id=ctx.project_id, label="字段", field_type=field_type,
        applicable_types=["character"], reference_types=["character"] if field_type == "reference" else [],
    )
    ctx.db.add(definition)
    await ctx.db.flush()
    timeline = await ctx.db.scalar(select(SettingTimeline).where(SettingTimeline.character_id == character["id"]))
    field = None
    if binding:
        field = SettingTextField(
            timeline_id=timeline.id, definition_id=definition.id, label=None,
            base_value="" if field_type in ("text", "line") else None,
        )
        ctx.db.add(field)
    await ctx.db.commit()
    return character, timeline, definition, field


async def test_effects_capture_catalog_and_never_snapshot_field_revision(ctx):
    before = await effects.snapshot(ctx.db, ctx.project_id)
    _, _, definition, _ = await _field(ctx, "dropdown")
    ctx.db.add(SettingFieldOption(definition_id=definition.id, label="初阶"))
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(fields_revision=8))
    after = await effects.snapshot(ctx.db, ctx.project_id)
    assert {"setting_field_definitions", "setting_field_options", "setting_reference_handles"} <= effects.EFFECT_TABLES
    assert "fields_revision" not in after["projects"][str(ctx.project_id)]
    assert all(item["format_version"] == 6 for item in effects.diff(before, after))


@pytest.mark.parametrize("value", [{"line": "筑基"}, {"dropdown": {"option_id": 2}}, {"dropdown": None},
                                      {"reference": {"kind": "world", "id": 3}}, {"reference": None}])
def test_registered_progression_accepts_explicit_custom_values(value):
    data = get_tool("record_setting_progression").schema.model_validate({
        "timeline_id": 1, "expected_revision": 1, "expected_fields_revision": 0,
        "custom_field_id": 2, "operation": "replace", "value": value,
        "anchor_kind": "planned", "chapter_no": 20,
    })
    assert data.value.unpack() == next(iter(value.values()))


async def test_tools_read_shared_slots_and_record_typed_values_at_position(ctx):
    character, timeline, definition, field = await _field(ctx, "dropdown")
    option = SettingFieldOption(definition_id=definition.id, label="筑基")
    ctx.db.add(option)
    await ctx.db.commit()
    directory = await get_tool("list_setting_field_definitions").run(ctx, {"page_size": 1})
    assert directory["items"][0]["field_type"] == "dropdown"
    assert directory["items"][0]["options"][0]["id"] == option.id
    listed = await get_tool("list_setting_timelines").run(ctx, {"character_id": character["id"]})
    assert listed["items"][0]["custom_fields"][0]["field_type"] == "dropdown"
    entry = {"kind": "character", "id": character["id"]}
    args = {
        "timeline_id": timeline.id, "expected_revision": timeline.revision, "expected_fields_revision": 0,
        "custom_field_id": field.id, "operation": "replace", "value": {"dropdown": {"option_id": option.id}},
        "anchor_kind": "planned", "chapter_no": 20, "boundary": "end",
    }
    tool = get_tool("record_setting_progression")
    first, second = await tool.preview(ctx, args), await tool.preview(ctx, args)
    assert "error" not in first, first
    assert first == second
    result = await tool.execute(ctx, args)
    assert "error" not in result, result
    await ctx.db.commit()
    early = await get_tool("get_setting_entry_fields").run(ctx, {"entry": entry, "chapter_no": 5})
    late = await get_tool("get_setting_entry_fields").run(ctx, {"entry": entry, "chapter_no": 21})
    assert early["fields"][0]["value"] is None
    assert late["fields"][0]["value"] == {"option_id": option.id}
    cleared = await tool.execute(ctx, {**args, "expected_revision": result["timeline_revision"], "chapter_no": 22,
                                       "value": {"dropdown": None}})
    assert "error" not in cleared, cleared
    assert cleared["item"]["value"] is None


async def test_first_shared_baseline_and_progression_use_cas_without_committing(ctx):
    character, timeline, definition, _ = await _field(ctx, "reference", binding=False)
    target = await new_character(ctx, "引用目标")
    args = {
        "entry": {"kind": "character", "id": character["id"]}, "definition_id": definition.id,
        "expected_timeline_revision": timeline.revision, "expected_fields_revision": 0,
        "field_type": "reference", "value": {"reference": {"kind": "character", "id": target["id"]}},
    }
    tool = get_tool("update_setting_field_baseline")
    preview = await tool.preview(ctx, args)
    assert "error" not in preview, preview
    assert preview == await tool.preview(ctx, args)
    result = await tool.execute(ctx, args)
    assert "error" not in result, result
    assert result["fields"][0]["value"] == {"kind": "character", "id": target["id"]}
    assert "profile" not in result["fields"][0]["reference"]
    definition_id = definition.id
    await ctx.db.rollback()
    assert await ctx.db.scalar(select(SettingTextField.id).where(
        SettingTextField.definition_id == definition_id,
    )) is None


async def test_nested_entity_and_option_references_are_checked_before_schema(ctx):
    _, timeline, _, field = await _field(ctx, "line")
    foreign = Project(user_id=ctx.user_id, title="他书")
    ctx.db.add(foreign)
    await ctx.db.flush()
    target = Character(project_id=foreign.id, name="越权")
    definition = SettingFieldDefinition(
        project_id=foreign.id, label="他书字段", field_type="dropdown", applicable_types=["character"],
    )
    ctx.db.add_all([target, definition])
    await ctx.db.flush()
    option = SettingFieldOption(definition_id=definition.id, label="他书选项")
    ctx.db.add(option)
    await ctx.db.commit()
    args = {
        "timeline_id": timeline.id, "expected_revision": timeline.revision, "expected_fields_revision": 0,
        "custom_field_id": field.id, "operation": "replace", "anchor_kind": "planned", "chapter_no": 1,
        "value": {"line": "安全", "unknown": {"reference": {"kind": "character", "id": target.id}}},
    }
    assert await get_tool("record_setting_progression").execute(ctx, args) == {"error": "不存在"}
    args["value"] = {"line": "安全", "unknown": {"option_id": option.id}}
    assert await get_tool("record_setting_progression").execute(ctx, args) == {"error": "不存在"}


def test_preview_normalizes_new_ids_in_typed_baseline_and_event():
    def changes(oid, hid):
        return [
            {"table": "setting_field_options", "pk": {"id": oid}, "before": None,
             "after": {"id": oid, "definition_id": 1, "label": "选项"}},
            {"table": "setting_reference_handles", "pk": {"id": hid}, "before": None,
             "after": {"id": hid, "project_id": 1, "kind": "character", "character_id": 1}},
            {"table": "setting_text_fields", "pk": {"id": 1}, "before": None,
             "after": {"id": 1, "definition_id": 1, "base_value": {"reference_handle_id": hid}}},
            {"table": "setting_progressions", "pk": {"id": 1}, "before": None,
             "after": {"id": 1, "field_key": None, "value": {"option_id": oid}}},
        ]
    assert preview_diff(changes(3, 4)) == preview_diff(changes(30, 40))


@pytest.mark.parametrize("field_type", ["dropdown", "reference"])
@pytest.mark.parametrize("historical", [False, True])
async def test_undo_parent_rejects_later_canonical_json_references(ctx, field_type, historical):
    character, timeline, definition, field = await _field(ctx, field_type)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    if field_type == "dropdown":
        parent = SettingFieldOption(definition_id=definition.id, label="后来引用的选项")
        key = "option_id"
    else:
        parent = SettingReferenceHandle(project_id=ctx.project_id, kind="character", character_id=character["id"])
        key = "reference_handle_id"
    ctx.db.add(parent)
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    value = {key: parent.id}
    if historical:
        ctx.db.add(SettingProgression(
            timeline_id=timeline.id, custom_field_id=field.id, operation="replace", value=value,
            anchor_kind="chapter", chapter_id=None, boundary="end", active=False, note="失效孤儿仍保护",
        ))
    else:
        field.base_value = value
    await ctx.db.commit()
    current = await effects.snapshot(ctx.db, ctx.project_id)
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, delta)
    assert exc.value.extra["code"] == "undo_conflict"
    assert await effects.snapshot(ctx.db, ctx.project_id) == current


async def test_undo_catalog_revision_uses_current_high_water_mark(ctx):
    _, _, definition, _ = await _field(ctx)
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(fields_revision=3))
    before = await effects.snapshot(ctx.db, ctx.project_id)
    definition.label = "智能体改名"
    ctx.project.synopsis = "智能体简介"
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(fields_revision=4))
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    # 即使历史项目快照夹带旧代次，恢复也不能写回该列。
    for item in delta:
        if item["table"] == "projects":
            item["before"]["fields_revision"], item["after"]["fields_revision"] = 3, 4
    await ctx.db.commit()
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(fields_revision=9))
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, delta)
    assert await ctx.db.scalar(select(Project.fields_revision).where(Project.id == ctx.project_id)) == 10
    assert definition.label == "字段"
    assert ctx.project.synopsis == before["projects"][str(ctx.project_id)]["synopsis"]


async def test_legacy_v2_typed_effect_rejected_before_any_restore(ctx):
    _, _, definition, field = await _field(ctx)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    field.base_value = "新值"
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.commit()
    legacy = deepcopy(delta)
    for item in legacy:
        item["format_version"] = 2
        if item["table"] == "setting_text_fields":
            item["before"].pop("definition_id", None)
            item["after"].pop("definition_id", None)
    current = await effects.snapshot(ctx.db, ctx.project_id)
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, legacy)
    assert exc.value.extra["code"] == "legacy_effect_requires_manual_correction"
    assert await effects.snapshot(ctx.db, ctx.project_id) == current
    assert (await ctx.db.get(SettingFieldDefinition, definition.id)).label == "字段"


async def test_first_field_progression_and_wrong_type_leave_no_partial_binding(ctx):
    character, timeline, definition, _ = await _field(ctx, "line", binding=False)
    args = {
        "entry": {"kind": "character", "id": character["id"]}, "definition_id": definition.id,
        "expected_timeline_revision": timeline.revision, "expected_fields_revision": 0,
        "field_type": "line", "value": {"line": "筑基"}, "operation": "replace",
        "anchor_kind": "planned", "chapter_no": 20, "boundary": "end",
    }
    tool = get_tool("record_setting_field_progression")
    before = await effects.snapshot(ctx.db, ctx.project_id)
    assert "error" in await tool.execute(ctx, {**args, "value": {"text": "不能伪装类型"}})
    assert "error" in await tool.execute(ctx, {**args, "value": {"line": "不应\n保存"}})
    assert await effects.snapshot(ctx.db, ctx.project_id) == before
    result = await tool.execute(ctx, args)
    assert "error" not in result, result
    assert result["item"]["value"] == "筑基"
    assert result["item"]["field_type"] == "line"
    field_id = result["item"]["custom_field_id"]
    assert field_id and (await ctx.db.get(SettingTextField, field_id)).base_value == ""


async def test_v3_undo_self_reference_graph_restores_exact_data(ctx):
    before = await effects.snapshot(ctx.db, ctx.project_id)
    character, _, definition, field = await _field(ctx, "reference")
    handle = SettingReferenceHandle(project_id=ctx.project_id, kind="character", character_id=character["id"])
    ctx.db.add(handle)
    await ctx.db.flush()
    field.base_value = {"reference_handle_id": handle.id}
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, delta)
    assert await effects.snapshot(ctx.db, ctx.project_id) == before
    assert await ctx.db.scalar(select(Project.fields_revision).where(Project.id == ctx.project_id)) == 1
    assert await ctx.db.get(SettingFieldDefinition, definition.id) is None


async def test_undo_scope_change_revalidates_later_values_and_preserves_revision_on_failure(ctx):
    _, _, definition, _ = await _field(ctx)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    definition.applicable_types = ["character", "organization"]
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    organization = Organization(project_id=ctx.project_id, name="新组织")
    ctx.db.add(organization)
    await ctx.db.flush()
    timeline = SettingTimeline(project_id=ctx.project_id, subject_kind="organization", organization_id=organization.id,
                               baseline={}, baseline_status="confirmed")
    ctx.db.add(timeline)
    await ctx.db.flush()
    ctx.db.add(SettingTextField(
        timeline_id=timeline.id, definition_id=definition.id, label=None, base_value="后来保存",
    ))
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(fields_revision=9))
    await ctx.db.commit()
    current = await effects.snapshot(ctx.db, ctx.project_id)
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, delta)
    assert exc.value.extra["code"] == "undo_conflict"
    assert await effects.snapshot(ctx.db, ctx.project_id) == current
    assert await ctx.db.scalar(select(Project.fields_revision).where(Project.id == ctx.project_id)) == 9


async def test_safe_v2_legacy_text_snapshot_without_new_column_remains_undoable(ctx):
    _, timeline, _, _ = await _field(ctx, "line", binding=False)
    field = SettingTextField(timeline_id=timeline.id, label="旧字段", base_value="旧值")
    ctx.db.add(field)
    await ctx.db.commit()
    before = await effects.snapshot(ctx.db, ctx.project_id)
    field.base_value = "新值"
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.commit()
    for effect in delta:
        effect["format_version"] = 2
        effect["before"].pop("definition_id")
        effect["after"].pop("definition_id")
    await effects.undo(ctx.db, ctx.project_id, delta)
    assert field.base_value == "旧值"
    assert await effects.snapshot(ctx.db, ctx.project_id) == before


async def test_safe_v2_body_effect_remains_undoable_in_typed_project(ctx):
    await _field(ctx)
    chapter = await new_chapter(ctx, "旧正文")
    before = await effects.snapshot(ctx.db, ctx.project_id)
    result = await get_tool("replace_chapter_text").execute(
        ctx, {"chapter_id": chapter.id, "original": "旧正文", "replacement": "新正文"},
    )
    assert "error" not in result, result
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    delta.append(effects.chapter_text_effect(chapter.id, result["before_version_id"], result["after_version_id"],
                                            result["before_hash"], result["after_hash"]))
    for effect in delta:
        effect["format_version"] = 2
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, delta)
    assert chapter.content == "旧正文"


async def test_mixed_turn_preflights_legacy_typed_effect_before_first_restore(ctx):
    _, _, _, field = await _field(ctx)
    chapter = await new_chapter(ctx)
    conversation = AgentConversation(user_id=ctx.user_id, project_id=ctx.project_id)
    ctx.db.add(conversation)
    await ctx.db.commit()
    before = await effects.snapshot(ctx.db, ctx.project_id)
    chapter.title, field.base_value = "新标题", "新值"
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    text_delta = [item for item in delta if item["table"] == "chapters"]
    field_delta = [item for item in delta if item["table"] == "setting_text_fields"]
    for item in field_delta:
        item["format_version"] = 2
        item["before"].pop("definition_id")
        item["after"].pop("definition_id")
    rows = [AgentChange(
        conversation_id=conversation.id, project_id=ctx.project_id, tool_call_id=f"effect-{i}", tool_name="test",
        kind="small", status="executed", effects=items,
    ) for i, items in enumerate((text_delta, field_delta))]
    ctx.db.add_all(rows)
    await ctx.db.commit()
    project_id, user_id, chapter_id = ctx.project_id, ctx.user_id, chapter.id
    updates = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lower().lstrip().startswith("update chapters"):
            updates.append(statement)

    sqlalchemy_event.listen(ctx.db.bind.sync_engine, "before_cursor_execute", record_sql)
    try:
        with pytest.raises(ConflictError) as exc:
            await _undo_changes(ctx.db, user_id, project_id, rows)
        assert exc.value.extra["code"] == "legacy_effect_requires_manual_correction"
    finally:
        sqlalchemy_event.remove(ctx.db.bind.sync_engine, "before_cursor_execute", record_sql)
    assert updates == []
    assert (await ctx.db.get(Chapter, chapter_id)).title == "新标题"


async def test_restore_option_cannot_follow_reused_parent_id_into_other_project(ctx):
    _, _, definition, _ = await _field(ctx, "dropdown", binding=False)
    option = SettingFieldOption(definition_id=definition.id, label="旧选项")
    ctx.db.add(option)
    await ctx.db.commit()
    before = await effects.snapshot(ctx.db, ctx.project_id)
    await ctx.db.delete(option)
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.commit()
    definition_id, option_id = definition.id, option.id
    await ctx.db.delete(definition)
    await ctx.db.flush()
    foreign = Project(user_id=ctx.user_id, title="他书")
    ctx.db.add(foreign)
    await ctx.db.flush()
    ctx.db.add(SettingFieldDefinition(id=definition_id, project_id=foreign.id, label="复用主键", field_type="dropdown",
                                     applicable_types=["character"]))
    await ctx.db.commit()
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, delta)
    assert exc.value.extra["code"] == "undo_conflict"
    assert await ctx.db.get(SettingFieldOption, option_id) is None


@pytest.mark.parametrize("active,anchor_kind", [(False, "planned"), (True, "chapter"), (False, "chapter")])
async def test_undo_retained_event_cannot_follow_field_reused_by_another_timeline(ctx, active, anchor_kind):
    _, timeline, definition, field = await _field(ctx)
    field_id = field.id
    event = SettingProgression(
        timeline_id=timeline.id, custom_field_id=field_id, operation="replace", value="原主体历史",
        anchor_kind=anchor_kind, chapter_no=20 if anchor_kind == "planned" else None, active=active,
    )
    ctx.db.add(event)
    await ctx.db.commit()
    event_id = event.id
    before = await effects.snapshot(ctx.db, ctx.project_id)
    await ctx.db.delete(event)
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.commit()
    # 已没有任何值/事件的定义可合法删除；再模拟旧库或 Core 恢复显式复用主键。
    await setting_field_service.delete_definition(ctx.db, ctx.user_id, ctx.project_id, definition.id, 0)
    other = await new_character(ctx, "另一主体")
    other_timeline = await ctx.db.scalar(select(SettingTimeline).where(
        SettingTimeline.character_id == other["id"],
    ))
    replacement = SettingFieldDefinition(
        project_id=ctx.project_id, label="新的字段", field_type="line", applicable_types=["character"],
    )
    ctx.db.add(replacement)
    await ctx.db.flush()
    new_field = SettingTextField(id=field_id, timeline_id=other_timeline.id,
                                definition_id=replacement.id, label=None, base_value="")
    ctx.db.add(new_field)
    await ctx.db.commit()
    assert new_field.id == field_id
    current = await effects.snapshot(ctx.db, ctx.project_id)
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, delta)
    assert exc.value.extra["code"] == "undo_conflict"
    assert await ctx.db.get(SettingProgression, event_id) is None
    assert await effects.snapshot(ctx.db, ctx.project_id) == current


async def test_undo_retained_event_cannot_attach_to_a_new_definition_on_the_same_timeline(ctx):
    from app.schemas import setting_fields as field_schemas

    character, timeline, definition, field = await _field(ctx)
    field_id, definition_id = field.id, definition.id
    event = SettingProgression(
        timeline_id=timeline.id, custom_field_id=field_id, operation="replace", value="旧字段的历史",
        anchor_kind="planned", chapter_no=20, active=False,
    )
    ctx.db.add(event)
    await ctx.db.commit()
    before = await effects.snapshot(ctx.db, ctx.project_id)
    await ctx.db.delete(event)
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.commit()
    # 目录删除及新定义/空绑定都走正式服务；value类型相容、条目身份相同也不能代替绑定身份。
    await setting_field_service.delete_definition(ctx.db, ctx.user_id, ctx.project_id, definition_id, 0)
    created = await setting_field_service.save_definition(
        ctx.db, ctx.user_id, ctx.project_id,
        field_schemas.DefinitionCreate(expected_fields_revision=1, label="新的定义", field_type="line",
                                       applicable_types=["character"]),
    )
    saved = await setting_field_service.write_baseline(
        ctx.db, ctx.user_id, ctx.project_id, "character", character["id"], created.item.id,
        field_schemas.FieldValueWrite(expected_fields_revision=created.fields_revision,
                                      expected_timeline_revision=timeline.revision, field_type="line", value=""),
    )
    current = await effects.snapshot(ctx.db, ctx.project_id)
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, delta)
    assert exc.value.extra["code"] == "undo_conflict"
    assert saved.fields[0].field_id > field_id
    assert saved.fields[0].definition_id != definition_id
    assert await effects.snapshot(ctx.db, ctx.project_id) == current


async def test_undo_new_shared_definition_restores_legacy_label_atomically(ctx):
    character = await new_character(ctx)
    timeline = await ctx.db.scalar(select(SettingTimeline).where(SettingTimeline.character_id == character["id"]))
    field = SettingTextField(timeline_id=timeline.id, label="旧字段", base_value="旧文字")
    ctx.db.add(field)
    await ctx.db.commit()
    before = await effects.snapshot(ctx.db, ctx.project_id)
    definition = SettingFieldDefinition(project_id=ctx.project_id, label="共享字段", field_type="text",
                                        applicable_types=["character"])
    ctx.db.add(definition)
    await ctx.db.flush()
    field.definition_id, field.label = definition.id, None
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, delta)
    assert field.definition_id is None and field.label == "旧字段"
    assert await effects.snapshot(ctx.db, ctx.project_id) == before


@pytest.mark.parametrize("field_type", ["dropdown", "reference"])
async def test_undo_new_json_parent_allows_values_restored_in_same_effect(ctx, field_type):
    character, _, definition, field = await _field(ctx, field_type)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    if field_type == "dropdown":
        parent = SettingFieldOption(definition_id=definition.id, label="新选项")
        key = "option_id"
    else:
        parent = SettingReferenceHandle(project_id=ctx.project_id, kind="character", character_id=character["id"])
        key = "reference_handle_id"
    ctx.db.add(parent)
    await ctx.db.flush()
    field.base_value = {key: parent.id}
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, delta)
    assert field.base_value is None
    assert await effects.snapshot(ctx.db, ctx.project_id) == before


@pytest.mark.parametrize("field_type", ["dropdown", "reference"])
@pytest.mark.parametrize("changed", ["note", "chapter_no"])
async def test_undo_note_or_anchor_preserves_archived_canonical_reference(ctx, field_type, changed):
    character, timeline, definition, field = await _field(ctx, field_type)
    if field_type == "dropdown":
        parent = SettingFieldOption(definition_id=definition.id, label="停用的历史选项")
        key = "option_id"
    else:
        parent = SettingReferenceHandle(project_id=ctx.project_id, kind="character", character_id=character["id"])
        key = "reference_handle_id"
    ctx.db.add(parent)
    await ctx.db.flush()
    value = {key: parent.id}
    event = SettingProgression(
        timeline_id=timeline.id, custom_field_id=field.id, operation="replace", value=value,
        anchor_kind="planned", chapter_no=20, boundary="end", note="原注",
    )
    ctx.db.add(event)
    await ctx.db.commit()
    before = await effects.snapshot(ctx.db, ctx.project_id)
    setattr(event, changed, "新注" if changed == "note" else 21)
    delta = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    definition.archived = True
    if field_type == "dropdown":
        parent.archived = True
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(fields_revision=9))
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, delta)
    assert event.value == value and event.note == "原注" and event.chapter_no == 20
    assert await ctx.db.scalar(select(Project.fields_revision).where(Project.id == ctx.project_id)) == 9
