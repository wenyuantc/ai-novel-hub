"""effects v4、真实旧快照形状和模型可见工具输出的分类合同。"""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import inspect, select, text, update

from app.core.errors import ConflictError
from app.models import (
    AgentChange,
    AgentConversation,
    AgentMessage,
    Career,
    Chapter,
    Character,
    Organization,
    Project,
    SettingCategory,
    SettingCategoryRule,
    SettingTextField,
    SettingTimeline,
    WorldSetting,
)
from app.models.setting_fields import SettingFieldDefinition, SettingFieldOption
from app.services import setting_catalog_service
from app.services.agent import effects
from app.services.agent.service import _undo_changes
from app.services.agent.tools import get_tool
from app.services.agent.tools.base import preview_diff, serialized
from tests.helpers import wait_task
from tests.test_agent_runtime import call, send, setup_agent
from tests.test_agent_tools import apply, new_chapter
from tests.test_agent_tools import ctx as ctx
from tests.test_setting_category_query import snapshot_database as snapshot_database

MODELS = {"character": Character, "world": WorldSetting, "organization": Organization, "career": Career}
ENTITY_TABLES = {model.__tablename__ for model in MODELS.values()}
CATEGORY_TABLES = {"setting_categories", "setting_category_rules"}


async def _entry(ctx, kind="character", tags=None):
    values = {"title": "旧名", "category": "place"} if kind == "world" else {"name": "旧名"}
    entry = MODELS[kind](project_id=ctx.project_id, setting_tags=tags or [], **values)
    ctx.db.add(entry)
    await ctx.db.commit()
    return entry


async def _dropdown(ctx, types=None):
    definition = SettingFieldDefinition(
        project_id=ctx.project_id, label="阵营", field_type="dropdown", applicable_types=types or ["character"],
    )
    ctx.db.add(definition)
    await ctx.db.flush()
    option = SettingFieldOption(definition_id=definition.id, label="同盟")
    ctx.db.add(option)
    await ctx.db.commit()
    return definition, option


async def _category(ctx, definition=None, option=None, *, name="同行"):
    category = SettingCategory(project_id=ctx.project_id, name=name, applicable_types=["character"], match_mode="all")
    ctx.db.add(category)
    await ctx.db.flush()
    rule = SettingCategoryRule(category_id=category.id, kind="dropdown" if definition else "tag",
                               definition_id=definition.id if definition else None,
                               option_id=option.id if option else None, tag=None if definition else "同盟")
    ctx.db.add(rule)
    await ctx.db.commit()
    return category, rule


def _legacy(captured, version=3):
    """真实旧写入器没有分类表、标签或分类代次，不能仅修改版本数字。"""
    result = []
    for effect in deepcopy(captured):
        if effect["table"] in CATEGORY_TABLES:
            continue
        effect["format_version"] = version
        for row in (effect["before"], effect["after"]):
            if row is None:
                continue
            for key in effects._legacy_visibility_defaults(effect["table"]):
                row.pop(key, None)
            if effect["table"] in ENTITY_TABLES:
                row.pop("setting_tags", None)
            if effect["table"] == "projects":
                row.pop("categories_revision", None)
            if version == 2 and effect["table"] == "setting_text_fields":
                assert row.get("definition_id") is None, "v2 fixture只能承载真实legacy文本"
                row.pop("definition_id", None)
        result.append(effect)
    return result


async def test_v4_snapshots_tags_categories_rules_and_excludes_both_project_revisions(ctx):
    entry = await _entry(ctx)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    definition, option = await _dropdown(ctx)
    category, rule = await _category(ctx, definition, option)
    entry.setting_tags = ["同盟"]
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(
        fields_revision=4, categories_revision=7,
    ))
    after = await effects.snapshot(ctx.db, ctx.project_id)
    assert CATEGORY_TABLES <= effects.EFFECT_TABLES
    assert after["setting_categories"][str(category.id)]["name"] == "同行"
    assert after["setting_category_rules"][str(rule.id)]["option_id"] == option.id
    assert after["characters"][str(entry.id)]["setting_tags"] == ["同盟"]
    assert {"fields_revision", "categories_revision"}.isdisjoint(after["projects"][str(ctx.project_id)])
    captured = effects.diff(before, after)
    assert all(item["format_version"] == 6 for item in captured)
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, captured)
    assert await effects.snapshot(ctx.db, ctx.project_id) == before
    assert await ctx.db.scalar(select(Project.fields_revision).where(Project.id == ctx.project_id)) == 5
    assert await ctx.db.scalar(select(Project.categories_revision).where(Project.id == ctx.project_id)) == 8


async def test_v4_restore_deleted_category_retains_rule_identity_and_archived_option(ctx):
    definition, option = await _dropdown(ctx)
    category, rule = await _category(ctx, definition, option)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    category_id, rule_id = category.id, rule.id
    await ctx.db.delete(category)
    captured = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    option.archived = definition.archived = True
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(
        fields_revision=6, categories_revision=9,
    ))
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, captured)
    assert (await ctx.db.get(SettingCategory, category_id)).name == "同行"
    assert (await ctx.db.get(SettingCategoryRule, rule_id)).option_id == option.id
    assert await ctx.db.scalar(select(Project.fields_revision).where(Project.id == ctx.project_id)) == 6
    assert await ctx.db.scalar(select(Project.categories_revision).where(Project.id == ctx.project_id)) == 10


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("kind", MODELS)
async def test_legacy_update_omitted_tags_preserves_later_tags(ctx, version, kind):
    entry = await _entry(ctx, kind)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    name = "title" if kind == "world" else "name"
    setattr(entry, name, "智能体新名")
    captured = _legacy(effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id)), version)
    entry.setting_tags = ["后来整理"]
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(categories_revision=9))
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, captured)
    assert getattr(entry, name) == "旧名" and entry.setting_tags == ["后来整理"]
    assert await ctx.db.scalar(select(Project.categories_revision).where(Project.id == ctx.project_id)) == 9


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("kind", MODELS)
async def test_legacy_insert_cannot_remove_later_nonempty_tags(ctx, version, kind):
    before = await effects.snapshot(ctx.db, ctx.project_id)
    entry = await _entry(ctx, kind)
    captured = _legacy(effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id)), version)
    entry.setting_tags = ["后来整理"]
    await ctx.db.commit()
    current = await effects.snapshot(ctx.db, ctx.project_id)
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, captured)
    assert exc.value.extra["code"] == "legacy_effect_requires_manual_correction"
    assert await effects.snapshot(ctx.db, ctx.project_id) == current


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("kind", MODELS)
async def test_legacy_delete_restores_missing_row_with_original_empty_default(ctx, version, kind):
    entry = await _entry(ctx, kind)
    entry_id = entry.id
    before = await effects.snapshot(ctx.db, ctx.project_id)
    await ctx.db.delete(entry)
    captured = _legacy(effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id)), version)
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, captured)
    assert (await ctx.db.get(MODELS[kind], entry_id)).setting_tags == []


@pytest.mark.parametrize("future", [["未来语义"], None, "非法字符串"])
async def test_legacy_entity_cannot_smuggle_future_tag_semantics(ctx, future):
    entry = await _entry(ctx)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    entry.name = "新名"
    captured = _legacy(effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id)))
    for item in captured:
        item["before"]["setting_tags"] = future
        item["after"]["setting_tags"] = future
    await ctx.db.commit()
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, captured)
    assert exc.value.extra["code"] == "legacy_effect_requires_manual_correction"


@pytest.mark.parametrize("action", ["scope", "type", "definition_delete", "option_delete"])
async def test_legacy_rule_only_dependency_preflight_blocks_before_first_restore(ctx, action):
    if action == "definition_delete":
        before = await effects.snapshot(ctx.db, ctx.project_id)
        definition, option = await _dropdown(ctx)
    else:
        definition, option = await _dropdown(ctx, ["world.place"] if action == "scope" else None)
        if action == "type":
            await ctx.db.delete(option)
            definition.field_type = "line"
        elif action == "option_delete":
            await ctx.db.delete(option)
        await ctx.db.commit()
        before = await effects.snapshot(ctx.db, ctx.project_id)
        if action == "scope":
            definition.applicable_types = ["character", "world.place"]
        elif action == "type":
            definition.field_type = "dropdown"
            option = SettingFieldOption(definition_id=definition.id, label="新选项")
            ctx.db.add(option)
        else:
            option = SettingFieldOption(definition_id=definition.id, label="新选项")
            ctx.db.add(option)
    captured = _legacy(effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id)))
    await ctx.db.commit()
    await _category(ctx, definition, option)
    await _assert_mixed_preflight(ctx, captured)


async def _assert_mixed_preflight(ctx, captured):
    chapter = Chapter(project_id=ctx.project_id, title="旧章题")
    conversation = AgentConversation(user_id=ctx.user_id, project_id=ctx.project_id)
    ctx.db.add_all([chapter, conversation])
    await ctx.db.commit()
    before = await effects.snapshot(ctx.db, ctx.project_id)
    chapter.title = "新章题"
    modern = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    rows = [AgentChange(conversation_id=conversation.id, project_id=ctx.project_id, tool_call_id=f"mixed-{i}",
                        tool_name="test", kind="small", status="executed", effects=items)
            for i, items in enumerate((modern, captured))]
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
    assert (await ctx.db.get(Chapter, chapter_id)).title == "新章题"


async def test_legacy_tagged_insert_is_rejected_before_first_modern_restore(ctx):
    before = await effects.snapshot(ctx.db, ctx.project_id)
    entry = await _entry(ctx)
    captured = _legacy(effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id)))
    entry.setting_tags = ["后来整理"]
    await ctx.db.commit()
    await _assert_mixed_preflight(ctx, captured)


async def test_v4_undo_catalog_and_tags_uses_current_high_water_marks(ctx):
    entry = await _entry(ctx)
    definition, _ = await _dropdown(ctx)
    category, _ = await _category(ctx)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    definition.label, category.name, entry.setting_tags = "新字段", "新分类", ["新标签"]
    ctx.project.synopsis = "新简介"
    captured = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    for item in captured:
        if item["table"] == "projects":
            item["before"].update(fields_revision=1, categories_revision=2)
            item["after"].update(fields_revision=3, categories_revision=4)
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(
        fields_revision=11, categories_revision=17,
    ))
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, captured)
    assert entry.setting_tags == [] and category.name == "同行" and definition.label == "阵营"
    assert await ctx.db.scalar(select(Project.fields_revision).where(Project.id == ctx.project_id)) == 12
    assert await ctx.db.scalar(select(Project.categories_revision).where(Project.id == ctx.project_id)) == 18


async def test_v4_rule_graph_failure_rolls_back_both_revisions(ctx):
    entry = await _entry(ctx)
    definition, option = await _dropdown(ctx, ["world.place"])
    before = await effects.snapshot(ctx.db, ctx.project_id)
    definition.applicable_types = ["character", "world.place"]
    entry.setting_tags = ["新标签"]
    captured = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.commit()
    await _category(ctx, definition, option)
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(
        fields_revision=11, categories_revision=17,
    ))
    await ctx.db.commit()
    current = await effects.snapshot(ctx.db, ctx.project_id)
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, captured)
    assert exc.value.extra["code"] == "undo_conflict"
    assert await effects.snapshot(ctx.db, ctx.project_id) == current
    assert await ctx.db.scalar(select(Project.fields_revision).where(Project.id == ctx.project_id)) == 11
    assert await ctx.db.scalar(select(Project.categories_revision).where(Project.id == ctx.project_id)) == 17


def test_category_preview_normalizes_all_new_foreign_keys():
    def captured(offset):
        definition, option, category, rule = (offset + n for n in range(1, 5))
        return [{"table": table, "pk": {"id": row["id"]}, "before": None, "after": row} for table, row in (
            ("setting_field_definitions", {"id": definition, "label": "阵营", "field_type": "dropdown"}),
            ("setting_field_options", {"id": option, "definition_id": definition, "label": "同盟"}),
            ("setting_categories", {"id": category, "name": "同行", "match_mode": "all"}),
            ("setting_category_rules", {"id": rule, "category_id": category, "kind": "dropdown",
                                         "definition_id": definition, "option_id": option}),
        )]
    assert preview_diff(captured(0)) == preview_diff(captured(100))


@pytest.mark.parametrize("kind", MODELS)
async def test_old_read_tools_keep_tagged_entries_out_of_model_output(ctx, kind):
    entry = await _entry(ctx, kind, ["模型不可见哨兵"])
    name = {"character": "get_character", "world": "list_world_settings",
            "organization": "get_organization", "career": "list_careers"}[kind]
    args = {"id": entry.id} if kind in {"character", "organization"} else {}
    result = await get_tool(name).run(ctx, args)
    assert "error" not in result, result
    assert "setting_tags" not in json.dumps(result, ensure_ascii=False)
    assert "模型不可见哨兵" not in json.dumps(result, ensure_ascii=False)
    assert (await effects.snapshot(ctx.db, ctx.project_id))[entry.__tablename__][str(entry.id)]["setting_tags"] == [
        "模型不可见哨兵",
    ]


@pytest.mark.parametrize("kind", ["character", "world"])
async def test_real_write_tool_hides_orm_tags_but_snapshot_keeps_them(ctx, kind):
    entry = await _entry(ctx, kind, ["模型不可见哨兵"])
    before = await effects.snapshot(ctx.db, ctx.project_id)
    tool = get_tool("update_character" if kind == "character" else "update_world_setting")
    result = await tool.execute(ctx, {"id": entry.id, "name" if kind == "character" else "title": "新名"})
    assert "error" not in result, result
    assert "setting_tags" not in result
    assert "模型不可见哨兵" not in json.dumps(result, ensure_ascii=False)
    after = await effects.snapshot(ctx.db, ctx.project_id)
    captured = effects.diff(before, after)
    row = next(item for item in captured if item["table"] == entry.__tablename__)
    assert row["before"]["setting_tags"] == row["after"]["setting_tags"] == entry.setting_tags == ["模型不可见哨兵"]


def test_explicit_tag_dto_and_dict_serialization_remain_available():
    class ExplicitTags(BaseModel):
        setting_tags: list[str]

    assert serialized(ExplicitTags(setting_tags=["显式标签"])) == {"setting_tags": ["显式标签"]}
    assert serialized({"setting_tags": ["显式标签"]}) == {"setting_tags": ["显式标签"]}


@pytest.mark.parametrize("kind", ["character", "world"])
async def test_next_fake_llm_messages_do_not_receive_tags_from_write_result(client, fake_llm, app, kind):
    project, _, url = await setup_agent(client, auto=True)
    base = f"/api/v1/projects/{project['id']}"
    endpoint = "characters" if kind == "character" else "world"
    created = (await client.post(f"{base}/{endpoint}", json={
        "name" if kind == "character" else "title": "旧名",
    })).json()
    tagged = await client.patch(f"{base}/setting-entries/{kind}/{created['id']}/tags", json={
        "expected_categories_revision": 0, "setting_tags": ["模型不可见哨兵"],
    })
    assert tagged.status_code == 200, tagged.text
    fake_llm.push_tools([call("update_character" if kind == "character" else "update_world_setting", {
        "id": created["id"], "name" if kind == "character" else "title": "新名",
    })])
    fake_llm.push_tools([("text", {"text": "名称已更新"})])
    task = await wait_task(client, await send(client, url, "把「旧名」改名"), within=15)
    assert task["status"] == "succeeded", task
    messages = fake_llm.tool_calls[1]["messages"]
    assert "模型不可见哨兵" not in json.dumps(messages, ensure_ascii=False)
    tool_data = json.loads(next(item["content"] for item in messages if item["role"] == "tool"))["data"]
    assert "setting_tags" not in tool_data
    async with app.state.sessionmaker() as db:
        entry = await db.get(MODELS[kind], created["id"])
        change = await db.scalar(select(AgentChange).where(AgentChange.project_id == project["id"]))
        assert entry.setting_tags == ["模型不可见哨兵"]
        row = next(item for item in change.effects if item["table"] == entry.__tablename__)
        assert row["before"]["setting_tags"] == row["after"]["setting_tags"] == ["模型不可见哨兵"]


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("operation", ["body", "title", "legacy_text"])
async def test_safe_legacy_operations_still_work_in_tagged_category_project(ctx, version, operation):
    entry = await _entry(ctx, tags=["同盟"])
    await _category(ctx)
    chapter = await new_chapter(ctx, "旧正文")
    timeline = await setting_catalog_service.ensure_entry_timeline(ctx.db, ctx.project_id, "character", entry.id)
    field = SettingTextField(timeline_id=timeline.id, label="旧字段", base_value="旧文字")
    ctx.db.add(field)
    await ctx.db.commit()
    before = await effects.snapshot(ctx.db, ctx.project_id)
    result = None
    if operation == "body":
        result = await get_tool("replace_chapter_text").execute(ctx, {
            "chapter_id": chapter.id, "original": "旧正文", "replacement": "新正文",
        })
        assert "error" not in result, result
    elif operation == "title":
        chapter.title = "新章题"
    else:
        field.base_value = "新文字"
    captured = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    if result:
        captured.append(effects.chapter_text_effect(
            chapter.id, result["before_version_id"], result["after_version_id"],
            result["before_hash"], result["after_hash"],
        ))
    captured = _legacy(captured, version)
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, captured)
    assert chapter.content == "旧正文" and chapter.title == "启程" and field.base_value == "旧文字"
    assert entry.setting_tags == ["同盟"]


@pytest.mark.parametrize("kind", MODELS)
async def test_v4_tag_only_change_bumps_category_revision_without_field_revision(ctx, kind):
    entry = await _entry(ctx, kind)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    entry.setting_tags = ["新标签"]
    captured = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(
        fields_revision=6, categories_revision=9,
    ))
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, captured)
    assert entry.setting_tags == []
    assert await ctx.db.scalar(select(Project.fields_revision).where(Project.id == ctx.project_id)) == 6
    assert await ctx.db.scalar(select(Project.categories_revision).where(Project.id == ctx.project_id)) == 10


async def test_v4_rule_repoint_restores_mutually_exclusive_tag_payload_before_deleting_new_parent(ctx):
    _, rule = await _category(ctx)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    definition, option = await _dropdown(ctx)
    rule.kind, rule.tag, rule.definition_id, rule.option_id = "dropdown", None, definition.id, option.id
    captured = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.commit()
    await effects.undo(ctx.db, ctx.project_id, captured)
    assert rule.kind == "tag" and rule.tag == "同盟" and rule.definition_id is None and rule.option_id is None
    assert await effects.snapshot(ctx.db, ctx.project_id) == before


async def test_restore_rule_cannot_follow_parent_category_into_another_project(ctx):
    category, rule = await _category(ctx)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    category_id, rule_id = category.id, rule.id
    await ctx.db.delete(rule)
    captured = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    foreign = Project(user_id=ctx.user_id, title="另一作品")
    ctx.db.add(foreign)
    await ctx.db.flush()
    await ctx.db.execute(update(SettingCategory).where(SettingCategory.id == category_id).values(project_id=foreign.id))
    await ctx.db.commit()
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, captured)
    assert exc.value.extra["code"] == "undo_conflict"
    assert await ctx.db.get(SettingCategoryRule, rule_id) is None


async def test_missing_legacy_tags_does_not_ignore_other_modified_columns(ctx):
    entry = await _entry(ctx, "world")
    before = await effects.snapshot(ctx.db, ctx.project_id)
    entry.title = "新名"
    captured = _legacy(effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id)))
    entry.aliases, entry.setting_tags = ["后来别名"], ["后来标签"]
    await ctx.db.commit()
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, captured)
    assert exc.value.extra["code"] == "undo_conflict"
    assert entry.aliases == ["后来别名"] and entry.setting_tags == ["后来标签"]


async def test_legacy_format_cannot_claim_new_category_tables(ctx):
    before = await effects.snapshot(ctx.db, ctx.project_id)
    await _category(ctx)
    captured = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    # 故意伪装的新表载荷，不是合法旧fixture。
    for item in captured:
        item["format_version"] = 3
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, captured)
    assert exc.value.extra["code"] == "legacy_effect_requires_manual_correction"


async def test_invalid_tag_history_is_rejected_before_restore_without_normalizing(ctx):
    entry = await _entry(ctx, tags=[" 不规范历史 "])
    before = await effects.snapshot(ctx.db, ctx.project_id)
    entry.setting_tags = ["规范当前值"]
    captured = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(categories_revision=9))
    await ctx.db.commit()
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, captured)
    assert exc.value.extra["code"] == "undo_conflict"
    assert (await ctx.db.get(Character, entry.id)).setting_tags == ["规范当前值"]
    assert await ctx.db.scalar(select(Project.categories_revision).where(Project.id == ctx.project_id)) == 9


async def test_real_post_restore_field_graph_failure_refreshes_same_session_and_retains_inserted_object(ctx):
    definition = SettingFieldDefinition(project_id=ctx.project_id, label="范围", field_type="line",
                                        applicable_types=["character"])
    ctx.db.add(definition)
    await ctx.db.commit()
    before = await effects.snapshot(ctx.db, ctx.project_id)
    definition.applicable_types = ["character", "organization"]
    created = await _entry(ctx)
    captured = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    organization = Organization(project_id=ctx.project_id, name="后来使用的组织")
    ctx.db.add(organization)
    await ctx.db.flush()
    timeline = SettingTimeline(project_id=ctx.project_id, subject_kind="organization", organization_id=organization.id,
                               baseline={}, baseline_status="confirmed")
    ctx.db.add(timeline)
    await ctx.db.flush()
    ctx.db.add(SettingTextField(
        timeline_id=timeline.id, definition_id=definition.id, label=None, base_value="已存资料",
    ))
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(
        fields_revision=11, categories_revision=17,
    ))
    await ctx.db.commit()
    definition_id, created_id = definition.id, created.id
    assert not ctx.db.dirty  # 故障来自restore内部刷新，不是调用方尚未flush的脏状态。
    current = await effects.snapshot(ctx.db, ctx.project_id)
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, captured)
    assert exc.value.extra["code"] == "undo_conflict"
    assert await ctx.db.scalar(select(SettingFieldDefinition.applicable_types).where(
        SettingFieldDefinition.id == definition_id,
    )) == ["character", "organization"]
    assert (await ctx.db.get(SettingFieldDefinition, definition_id)).applicable_types == ["character", "organization"]
    assert await ctx.db.get(Character, created_id) is created and inspect(created).persistent
    assert (await ctx.db.get(Project, ctx.project_id)).fields_revision == 11
    assert (await ctx.db.get(Project, ctx.project_id)).categories_revision == 17
    assert await effects.snapshot(ctx.db, ctx.project_id) == current


async def test_cancelled_single_undo_refreshes_restored_objects_in_same_session(ctx, monkeypatch):
    category, _ = await _category(ctx)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    category.name = "作者保存的新分类名"
    captured = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(
        fields_revision=11, categories_revision=17,
    ))
    await ctx.db.commit()
    category_id, project_id = category.id, ctx.project_id
    current = await effects.snapshot(ctx.db, project_id)
    reached = asyncio.Event()
    release = asyncio.Event()

    async def pause_after_core_restore(db, pid):
        assert db is ctx.db and pid == project_id
        assert category.name == "同行"  # Core恢复后的refresh已经更新了同一个ORM对象。
        reached.set()
        await release.wait()

    monkeypatch.setattr(effects.setting_category_service, "validate_project_category_graph", pause_after_core_restore)
    pending = asyncio.create_task(effects.undo(ctx.db, project_id, captured))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

    assert await ctx.db.scalar(select(SettingCategory.name).where(
        SettingCategory.id == category_id,
    )) == "作者保存的新分类名"
    assert await ctx.db.get(SettingCategory, category_id) is category and inspect(category).persistent
    assert category.name == "作者保存的新分类名"
    assert ctx.project.fields_revision == 11 and ctx.project.categories_revision == 17
    assert await effects.snapshot(ctx.db, project_id) == current


async def test_turn_post_restore_failure_keeps_all_original_objects_and_both_revisions(ctx):
    definition = SettingFieldDefinition(project_id=ctx.project_id, label="范围", field_type="line",
                                        applicable_types=["character"])
    ctx.db.add(definition)
    entry = await _entry(ctx)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    definition.applicable_types = ["character", "organization"]
    earlier = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.commit()
    before = await effects.snapshot(ctx.db, ctx.project_id)
    definition.label, entry.setting_tags = "新字段名", ["新整理标签"]
    created = await _entry(ctx)
    later = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    organization = Organization(project_id=ctx.project_id, name="后来使用的组织")
    ctx.db.add(organization)
    await ctx.db.flush()
    timeline = SettingTimeline(project_id=ctx.project_id, subject_kind="organization", organization_id=organization.id,
                               baseline={}, baseline_status="confirmed")
    ctx.db.add(timeline)
    await ctx.db.flush()
    conversation = AgentConversation(user_id=ctx.user_id, project_id=ctx.project_id)
    ctx.db.add_all([conversation, SettingTextField(
        timeline_id=timeline.id, definition_id=definition.id, label=None, base_value="已存资料",
    )])
    await ctx.db.flush()
    rows = [AgentChange(conversation_id=conversation.id, project_id=ctx.project_id, tool_call_id=f"rollback-{i}",
                        tool_name="test", kind="small", status="executed", effects=items)
            for i, items in enumerate((later, earlier))]
    ctx.db.add_all(rows)
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(
        fields_revision=11, categories_revision=17,
    ))
    await ctx.db.commit()
    project_id, definition_id, entry_id, created_id = ctx.project_id, definition.id, entry.id, created.id
    row_ids = [row.id for row in rows]
    current = await effects.snapshot(ctx.db, project_id)
    assert not ctx.db.dirty
    with pytest.raises(ConflictError) as exc:
        await _undo_changes(ctx.db, ctx.user_id, project_id, rows)
    assert exc.value.extra["code"] == "undo_conflict"
    assert await effects.snapshot(ctx.db, project_id) == current
    assert await ctx.db.get(Character, created_id) is created and inspect(created).persistent
    assert await ctx.db.get(Character, entry_id) is entry and entry.setting_tags == ["新整理标签"]
    assert await ctx.db.get(SettingFieldDefinition, definition_id) is definition
    assert definition.applicable_types == ["character", "organization"] and definition.label == "新字段名"
    assert await ctx.db.get(Project, project_id) is ctx.project
    assert ctx.project.fields_revision == 11 and ctx.project.categories_revision == 17
    assert [(await ctx.db.get(AgentChange, row_id)).status for row_id in row_ids] == ["executed", "executed"]


async def test_turn_commit_failure_restores_identity_tags_revisions_and_change_status(ctx, monkeypatch):
    category, _ = await _category(ctx)
    entry = await _entry(ctx, tags=["作者原标签"])
    before = await effects.snapshot(ctx.db, ctx.project_id)
    category.name, entry.setting_tags = "智能体改名", ["智能体整理标签"]
    created = await _entry(ctx)
    captured = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    conversation = AgentConversation(user_id=ctx.user_id, project_id=ctx.project_id)
    ctx.db.add(conversation)
    await ctx.db.flush()
    change = AgentChange(conversation_id=conversation.id, project_id=ctx.project_id, tool_call_id="commit-failure",
                         tool_name="test", kind="small", status="executed", effects=captured)
    ctx.db.add(change)
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(
        fields_revision=11, categories_revision=17,
    ))
    await ctx.db.commit()
    project_id, category_id, entry_id, created_id = ctx.project_id, category.id, entry.id, created.id
    conversation_id, change_id = conversation.id, change.id
    current = await effects.snapshot(ctx.db, project_id)

    async def reject_commit():
        # 所有restore、状态和消息已flush，直到commit成功前仍不得detach原对象。
        assert category.name == "同行" and entry.setting_tags == ["作者原标签"]
        assert inspect(created).persistent and change.status == "undone"
        assert await ctx.db.scalar(select(Character.id).where(Character.id == created_id)) is None
        assert ctx.project.categories_revision == 18
        raise RuntimeError("受控提交失败")

    monkeypatch.setattr(ctx.db, "commit", reject_commit)
    with pytest.raises(RuntimeError, match="受控提交失败"):
        await _undo_changes(ctx.db, ctx.user_id, project_id, [change])

    assert await effects.snapshot(ctx.db, project_id) == current
    assert await ctx.db.get(SettingCategory, category_id) is category and category.name == "智能体改名"
    assert await ctx.db.get(Character, entry_id) is entry and entry.setting_tags == ["智能体整理标签"]
    assert await ctx.db.get(Character, created_id) is created and inspect(created).persistent
    assert await ctx.db.get(Project, project_id) is ctx.project
    assert ctx.project.fields_revision == 11 and ctx.project.categories_revision == 17
    assert await ctx.db.get(AgentChange, change_id) is change and change.status == "executed"
    assert change.undone_at is None
    assert list(await ctx.db.scalars(select(AgentMessage.id).where(
        AgentMessage.conversation_id == conversation_id,
    ))) == []


async def test_real_tool_preview_rollback_preserves_tags_revisions_and_unrelated_outer_edit(ctx):
    entry = await _entry(ctx, tags=["整理标签"])
    unrelated = await _entry(ctx, kind="world", tags=["世界标签"])
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(
        fields_revision=11, categories_revision=17,
    ))
    await ctx.db.commit()
    project_id, entry_id, unrelated_id = ctx.project_id, entry.id, unrelated.id
    unrelated.title = "调用方待提交的正文之外编辑"
    preview = await get_tool("update_character").preview(ctx, {"id": entry_id, "name": "仅预览新名"})
    assert "error" not in preview and preview["diff"]
    assert await ctx.db.get(Character, entry_id) is entry and inspect(entry).persistent
    assert entry.name == "旧名" and entry.setting_tags == ["整理标签"]
    assert await ctx.db.get(WorldSetting, unrelated_id) is unrelated and inspect(unrelated).persistent
    assert unrelated.title == "调用方待提交的正文之外编辑" and unrelated.setting_tags == ["世界标签"]
    assert await ctx.db.get(Project, project_id) is ctx.project
    assert ctx.project.fields_revision == 11 and ctx.project.categories_revision == 17
    assert (await ctx.db.execute(select(Project.fields_revision, Project.categories_revision).where(
        Project.id == project_id,
    ))).one() == (11, 17)
    await ctx.db.commit()
    assert (await ctx.db.get(WorldSetting, unrelated_id)).title == "调用方待提交的正文之外编辑"


async def test_real_materialization_failure_restores_same_session_career_timeline_and_revisions(ctx):
    entry = await _entry(ctx)
    first, _ = await apply(ctx, "create_career", {"name": "剑修", "stages": [{"name": "入门"}]})
    assigned, _ = await apply(ctx, "assign_career", {"character_id": entry.id, "career_id": first["id"]})
    side, _ = await apply(ctx, "create_career", {"name": "采药", "kind": "sub", "stages": [{"name": "入门"}]})
    await apply(ctx, "assign_career", {"character_id": entry.id, "career_id": side["id"]})
    timeline = await ctx.db.scalar(select(SettingTimeline).where(
        SettingTimeline.character_id == entry.id, SettingTimeline.career_id == first["id"],
    ))
    before = await effects.snapshot(ctx.db, ctx.project_id)
    result = await get_tool("remove_career_assignment").execute(ctx, {
        "character_id": entry.id, "assignment_id": assigned["id"],
    })
    assert "error" not in result, result
    captured = effects.diff(before, await effects.snapshot(ctx.db, ctx.project_id))
    await ctx.db.commit()
    later, _ = await apply(ctx, "create_career", {"name": "丹修", "stages": [{"name": "入门"}]})
    await apply(ctx, "assign_career", {"character_id": entry.id, "career_id": later["id"]})
    await ctx.db.execute(update(Project).where(Project.id == ctx.project_id).values(
        fields_revision=11, categories_revision=17,
    ))
    await ctx.db.commit()
    timeline_id = timeline.id
    baseline, revision = deepcopy(timeline.baseline), timeline.revision
    current = await effects.snapshot(ctx.db, ctx.project_id)
    assert baseline["exists"] is False and not ctx.db.dirty
    with pytest.raises(ConflictError) as exc:
        await effects.undo(ctx.db, ctx.project_id, captured)
    assert exc.value.extra["code"] == "undo_conflict"
    assert exc.value.extra["conflicts"] == [{"table": "setting_timelines", "label": "该角色已有主职业"}]
    assert await effects.snapshot(ctx.db, ctx.project_id) == current
    assert await ctx.db.get(SettingTimeline, timeline_id) is timeline and inspect(timeline).persistent
    assert timeline.baseline == baseline and timeline.revision == revision
    assert await ctx.db.scalar(select(SettingTimeline.baseline).where(SettingTimeline.id == timeline_id)) == baseline
    assert ctx.project.fields_revision == 11 and ctx.project.categories_revision == 17


async def test_real_database_deferred_rule_restore_and_legacy_preflight(snapshot_database):
    sessionmaker, seeded = snapshot_database
    async with sessionmaker() as db:
        project = Project(user_id=seeded.actor.id, title="effects隔离验证")
        db.add(project)
        await db.commit()
        scoped = SimpleNamespace(db=db, project=project, project_id=project.id, user_id=seeded.actor.id)
        before = await effects.snapshot(db, project.id)
        await _entry(scoped, tags=["同盟"])
        definition, option = await _dropdown(scoped)
        await _category(scoped, definition, option)
        captured = effects.diff(before, await effects.snapshot(db, project.id))
        await db.execute(update(Project).where(Project.id == project.id).values(
            fields_revision=11, categories_revision=17,
        ))
        await db.commit()
        await effects.undo(db, project.id, captured)
        if db.bind.dialect.name == "postgresql":
            await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        await db.commit()
        assert await effects.snapshot(db, project.id) == before
        assert project.fields_revision == 12 and project.categories_revision == 18

        definition, option = await _dropdown(scoped)
        category, rule = await _category(scoped, definition, option)
        category_id, rule_id = category.id, rule.id
        before = await effects.snapshot(db, project.id)
        await db.delete(category)
        deleted = effects.diff(before, await effects.snapshot(db, project.id))
        await db.commit()
        await effects.undo(db, project.id, deleted)
        if db.bind.dialect.name == "postgresql":
            await db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        await db.commit()
        assert (await db.get(SettingCategoryRule, rule_id)).category_id == category_id
        assert await effects.snapshot(db, project.id) == before
        assert project.categories_revision == 19

        # 释放既有规则后合法扩范围，再由后来仅规则引用构成旧v3撤销障碍。
        await db.delete(await db.get(SettingCategory, category_id))
        definition.applicable_types = ["world.place"]
        await db.commit()
        before = await effects.snapshot(db, project.id)
        definition.applicable_types = ["character", "world.place"]
        old = _legacy(effects.diff(before, await effects.snapshot(db, project.id)))
        await db.commit()
        await _category(scoped, definition, option)
        current = await effects.snapshot(db, project.id)
        with pytest.raises(ConflictError) as exc:
            await effects.undo(db, project.id, old)
        assert exc.value.extra["code"] == "legacy_effect_requires_manual_correction"
        assert await effects.snapshot(db, project.id) == current
        assert project.fields_revision == 12 and project.categories_revision == 19
