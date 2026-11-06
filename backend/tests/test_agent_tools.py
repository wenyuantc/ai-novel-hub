"""工具真实服务集成：预览回滚、项目边界、正文校验和任务白名单。"""

from copy import deepcopy

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy import event as sqlalchemy_event

from app.models import (
    AIConfig,
    Career,
    CareerStage,
    Chapter,
    ChapterAnalysis,
    ChapterVersion,
    Character,
    CharacterCareer,
    Organization,
    OrganizationMember,
    Outline,
    Project,
    Relationship,
    RelationshipType,
    SettingProgression,
    SettingTextField,
    SettingTimeline,
    Task,
)
from app.schemas.story import CharacterUpdate
from app.services import analysis_service, chapter_service, retrieval_service, story_service
from app.services.agent import effects
from app.services.agent.tools import ToolCtx, get_tool, list_tools
from tests.helpers import create_ai_config, create_project, register


@pytest.fixture
async def ctx(app, client):
    user = await register(client)
    project = await create_project(client, style="")
    await create_ai_config(client)
    # 只验证提交边界，后台调度由 agent-turn 集成测试覆盖。
    await app.state.task_engine.stop()
    async with app.state.sessionmaker() as db:
        row = await db.get(Project, project["id"])
        yield ToolCtx(db, row, user["id"], app.state.task_engine.ctx)


async def apply(ctx, name, args):
    tool = get_tool(name)
    before = await effects.snapshot(ctx.db, ctx.project_id)
    original = deepcopy(args)
    preview = await tool.preview(ctx, args)
    assert "error" not in preview, preview
    assert args == original
    assert await effects.snapshot(ctx.db, ctx.project_id) == before
    result = await tool.execute(ctx, args)
    assert "error" not in result, result
    await ctx.db.commit()
    return result, preview


async def new_character(ctx, name="林远"):
    return (await apply(ctx, "create_character", {"name": name}))[0]


async def new_chapter(ctx, content="山风穿过竹林。林远握紧长剑。"):
    from app.schemas.chapter import ChapterCreate

    return await chapter_service.create_chapter(
        ctx.db,
        ctx.user_id,
        ctx.project_id,
        ChapterCreate(title="启程", content=content),
    )


def test_registry_has_only_explicit_tools():
    names = {tool.name for tool in list_tools()}
    assert len(names) == 59
    assert {"list_skills", "load_skill", "read_skill_reference"} <= names
    assert {"list_setting_timelines", "update_setting_baseline", "record_setting_progression"} <= names
    assert {"list_setting_field_definitions", "list_setting_entries", "get_setting_entry_fields",
            "update_setting_field_baseline", "record_setting_field_progression"} <= names
    assert not any(name.startswith("mcp__") for name in names)
    assert get_tool("delete_project") is None
    assert get_tool("__import__") is None
    for tool in list_tools():
        assert tool.parameters["type"] == "object"
        assert tool.parameters.get("additionalProperties") is False
    schema = get_tool("start_task").parameters
    assert "chapter_id" in schema["$defs"]["AgentTaskParams"]["properties"]


def test_gemini_setting_tool_declarations_have_typed_nonempty_objects():
    from google.genai import types

    from app.ai.providers.gemini import _gemini_schema

    def check(node):
        assert node.get("type") in {"OBJECT", "STRING", "INTEGER", "BOOLEAN"}, node
        assert "anyOf" not in node
        if node["type"] == "OBJECT":
            assert node.get("properties"), node
            for child in node["properties"].values():
                check(child)

    declarations = {}
    for name in ("update_setting_baseline", "record_setting_progression"):
        tool = get_tool(name)
        declaration = types.FunctionDeclaration(
            name=tool.name, description=tool.description, parameters=_gemini_schema(tool.parameters)
        ).model_dump(mode="json", by_alias=True, exclude_none=True)
        check(declaration["parameters"])
        declarations[name] = declaration["parameters"]["properties"]

    changes = declarations["update_setting_baseline"]["changes"]["properties"]
    assert {"current_state", "content", "identity", "intimacy", "role", "stage_id", "position", "exists"} <= set(
        changes
    )
    value = declarations["record_setting_progression"]["value"]["properties"]
    assert {"text", "intimacy", "stage_id", "identity", "presence"} <= set(value)
    assert {"a_id", "b_id", "type_id"} <= set(value["identity"]["properties"])
    assert {"exists", "state"} <= set(value["presence"]["properties"])
    assert {"identity", "intimacy", "status", "description", "role", "stage_id", "position", "note"} <= set(
        value["presence"]["properties"]["state"]["properties"]
    )


async def test_setting_tools_explicit_baseline_and_chapter_progression(ctx):
    character = await new_character(ctx)
    chapter = await new_chapter(ctx)
    result = await get_tool("list_setting_timelines").run(
        ctx, {"subject_kind": "character", "character_id": character["id"], "page_size": 1}
    )
    assert result["total"] == 1 and result["page"] == 1
    timeline = result["items"][0]
    assert timeline["project_id"] == ctx.project_id
    baseline, _ = await apply(
        ctx,
        "update_setting_baseline",
        {
            "timeline_id": timeline["id"],
            "expected_revision": timeline["revision"],
            "changes": {"current_state": "练气"},
        },
    )
    assert baseline["baseline"]["current_state"] == "练气"
    args = {
        "timeline_id": timeline["id"],
        "expected_revision": baseline["timeline_revision"],
        "field_key": "current_state",
        "operation": "replace",
        "value": {"text": "筑基"},
        "anchor_kind": "chapter",
        "chapter_id": chapter.id,
        "boundary": "end",
    }
    result, _ = await apply(ctx, "record_setting_progression", args)
    assert result["item"]["chapter_id"] == chapter.id
    assert result["item"]["chapter_no"] is None
    assert result["item"]["boundary"] == "end"
    assert (await ctx.db.get(Character, character["id"])).current_state == "筑基"
    assert "error" in await get_tool("record_setting_progression").preview(ctx, args)
    no_anchor = {key: value for key, value in args.items() if key != "chapter_id"}
    assert "error" in await get_tool("record_setting_progression").preview(ctx, no_anchor)


async def test_setting_tools_reject_foreign_references_and_wrong_custom_field(ctx):
    character = await new_character(ctx)
    local = await get_tool("list_setting_timelines").run(ctx, {"character_id": character["id"]})
    timeline = local["items"][0]
    other_character = await new_character(ctx, "陈风")
    other_local = await get_tool("list_setting_timelines").run(ctx, {"character_id": other_character["id"]})
    wrong_field = SettingTextField(timeline_id=other_local["items"][0]["id"], label="其他设定字段")
    foreign = Project(user_id=ctx.user_id, title="另一作品")
    ctx.db.add_all([wrong_field, foreign])
    await ctx.db.flush()
    foreign_character = Character(project_id=foreign.id, name="他书角色")
    foreign_chapter = Chapter(project_id=foreign.id, title="他书章节", chapter_no=1)
    foreign_type = RelationshipType(project_id=foreign.id, role_a="甲", role_b="乙")
    foreign_career = Career(project_id=foreign.id, name="他书职业")
    ctx.db.add_all([foreign_character, foreign_chapter, foreign_type, foreign_career])
    await ctx.db.flush()
    foreign_timeline = SettingTimeline(
        project_id=foreign.id, subject_kind="character", character_id=foreign_character.id, baseline={}
    )
    foreign_stage = CareerStage(career_id=foreign_career.id, position=1, name="他书阶段")
    ctx.db.add_all([foreign_timeline, foreign_stage])
    await ctx.db.flush()
    foreign_field = SettingTextField(timeline_id=foreign_timeline.id, label="他书字段")
    own_field = SettingTextField(timeline_id=timeline["id"], label="本书字段")
    ctx.db.add_all([foreign_field, own_field])
    await ctx.db.commit()

    list_tool = get_tool("list_setting_timelines")
    assert "error" in await list_tool.run(ctx, {"character_id": foreign_character.id})
    first_page = await list_tool.run(ctx, {"subject_kind": "character", "page_size": 1, "page": 1})
    second_page = await list_tool.run(ctx, {"subject_kind": "character", "page_size": 1, "page": 2})
    assert first_page["total"] == second_page["total"] == 2
    assert first_page["items"][0]["custom_fields"] == [
        {"id": own_field.id, "label": "本书字段", "field_type": "text", "definition_id": None},
    ]
    assert second_page["items"][0]["custom_fields"] == [
        {"id": wrong_field.id, "label": "其他设定字段", "field_type": "text", "definition_id": None},
    ]
    baseline_tool = get_tool("update_setting_baseline")
    assert "error" in await baseline_tool.preview(
        ctx, {"timeline_id": foreign_timeline.id, "expected_revision": 1, "changes": {"current_state": "越权"}}
    )
    tool = get_tool("record_setting_progression")
    args = {
        "timeline_id": timeline["id"],
        "expected_revision": timeline["revision"],
        "field_key": "current_state",
        "operation": "replace",
        "value": {"text": "新状态"},
        "anchor_kind": "chapter",
        "chapter_id": foreign_chapter.id,
    }
    assert "error" in await tool.preview(ctx, args)
    args = {**args, "anchor_kind": "planned", "chapter_no": 2}
    args.pop("chapter_id")
    for field in (wrong_field, foreign_field):
        assert "error" in await tool.preview(ctx, {**args, "field_key": None, "custom_field_id": field.id})
    nested = {
        **args,
        "value": {"identity": {"a_id": character["id"], "b_id": other_character["id"], "type_id": foreign_type.id}},
    }
    assert (await tool.preview(ctx, nested))["error"] == "不存在"
    nested_presence = {
        **args,
        "field_key": "presence",
        "value": {
            "presence": {
                "exists": True,
                "state": {
                    "identity": {"a_id": character["id"], "b_id": other_character["id"], "type_id": foreign_type.id}
                },
            }
        },
    }
    assert (await tool.preview(ctx, nested_presence))["error"] == "不存在"
    assert (await tool.preview(ctx, {**args, "field_key": "stage_id", "value": {"stage_id": foreign_stage.id}}))[
        "error"
    ] == "不存在"


async def test_setting_tool_rejects_ambiguous_untyped_and_mismatched_values(ctx):
    character = await new_character(ctx)
    timeline = (await get_tool("list_setting_timelines").run(ctx, {"character_id": character["id"]}))["items"][0]
    args = {
        "timeline_id": timeline["id"],
        "expected_revision": timeline["revision"],
        "field_key": "current_state",
        "operation": "replace",
        "anchor_kind": "planned",
        "chapter_no": 3,
    }
    before = await effects.snapshot(ctx.db, ctx.project_id)
    for value in (
        "未经类型包装",
        {},
        {"text": "筑基", "intimacy": 10},
        {"text": "筑基", "other": "多余"},
        {"intimacy": 10},
        {"text": None},
    ):
        assert "error" in await get_tool("record_setting_progression").preview(ctx, {**args, "value": value})
    assert await effects.snapshot(ctx.db, ctx.project_id) == before


async def test_setting_tools_type_safe_values_cover_all_subjects_and_custom_text(ctx):
    from app.schemas.progression import TextFieldCreate
    from app.services import setting_progression_service

    character, other = await new_character(ctx), await new_character(ctx, "陈风")
    world, _ = await apply(ctx, "create_world_setting", {"title": "灵界", "content": "天地初开"})
    relation, _ = await apply(
        ctx, "create_relationship", {"a_id": character["id"], "b_id": other["id"], "role_a": "师父", "role_b": "徒弟"}
    )
    career, _ = await apply(ctx, "create_career", {"name": "剑修", "stages": [{"name": "入门"}, {"name": "大师"}]})
    await apply(ctx, "assign_career", {"character_id": character["id"], "career_id": career["id"]})
    org, _ = await apply(ctx, "create_organization", {"name": "剑宗"})
    await apply(ctx, "set_org_member", {"organization_id": org["id"], "character_id": character["id"]})

    filters = {
        "character": {"character_id": character["id"]},
        "world": {"world_setting_id": world["id"]},
        "relationship": {"character_id": character["id"], "related_character_id": other["id"]},
        "career": {"character_id": character["id"], "career_id": career["id"]},
        "membership": {"character_id": character["id"], "organization_id": org["id"]},
    }
    timelines = {}
    for kind, extra in filters.items():
        result = await get_tool("list_setting_timelines").run(ctx, {"subject_kind": kind, **extra})
        assert result["total"] == 1 and result["items"][0]["custom_fields"] == []
        timelines[kind] = result["items"][0]

    for kind, field_key, value in (
        ("relationship", "intimacy", {"stage_id": career["stages"][0]["id"]}),
        ("career", "stage_id", {"intimacy": career["stages"][1]["id"]}),
    ):
        timeline = timelines[kind]
        assert "error" in await get_tool("record_setting_progression").preview(
            ctx,
            {
                "timeline_id": timeline["id"],
                "expected_revision": timeline["revision"],
                "field_key": field_key,
                "operation": "replace",
                "value": value,
                "anchor_kind": "planned",
                "chapter_no": 1,
            },
        )

    identity = {"a_id": character["id"], "b_id": other["id"], "type_id": relation["type_id"]}
    baselines = {
        "character": {"current_state": "练气"},
        "world": {"content": "天地初开"},
        "relationship": {"identity": identity, "intimacy": 10},
        "career": {"note": "御剑"},
        "membership": {"position": "内门弟子"},
    }
    for kind, changes in baselines.items():
        timeline = timelines[kind]
        result, _ = await apply(
            ctx,
            "update_setting_baseline",
            {"timeline_id": timeline["id"], "expected_revision": timeline["revision"], "changes": changes},
        )
        assert all(result["baseline"][key] == value for key, value in changes.items())
        timeline["revision"] = result["timeline_revision"]

    reverse_identity = {**identity, "a_id": other["id"], "b_id": character["id"]}
    events = {
        "character": ("current_state", {"text": "筑基"}, "筑基", "replace"),
        "world": ("content", {"text": "灾变"}, "灾变", "append"),
        "relationship": ("identity", {"identity": reverse_identity}, reverse_identity, "replace"),
        "career": ("stage_id", {"stage_id": career["stages"][1]["id"]}, career["stages"][1]["id"], "replace"),
        "membership": ("position", {"text": "长老"}, "长老", "replace"),
    }
    for kind, (field, value, raw_value, operation) in events.items():
        timeline = timelines[kind]
        result, _ = await apply(
            ctx,
            "record_setting_progression",
            {
                "timeline_id": timeline["id"],
                "expected_revision": timeline["revision"],
                "field_key": field,
                "operation": operation,
                "value": value,
                "anchor_kind": "planned",
                "chapter_no": 1,
            },
        )
        assert result["item"]["value"] == raw_value
        timeline["revision"] = result["timeline_revision"]

    lifecycle_states = {
        "relationship": {"identity": reverse_identity, "intimacy": 30, "status": "normal", "description": "师徒"},
        "career": {"role": "main", "stage_id": career["stages"][1]["id"], "note": "重修"},
        "membership": {"position": "掌门", "status": "active", "note": "重入"},
    }
    for kind, state in lifecycle_states.items():
        timeline = timelines[kind]
        for chapter_no, raw in ((2, {"exists": False}), (3, {"exists": True, "state": state})):
            result, _ = await apply(
                ctx,
                "record_setting_progression",
                {
                    "timeline_id": timeline["id"],
                    "expected_revision": timeline["revision"],
                    "field_key": "presence",
                    "operation": "replace",
                    "value": {"presence": raw},
                    "anchor_kind": "planned",
                    "chapter_no": chapter_no,
                },
            )
            assert result["item"]["value"] == raw
            timeline["revision"] = result["timeline_revision"]

    timeline = timelines["character"]
    field = await setting_progression_service.create_text_field(
        ctx.db,
        ctx.user_id,
        ctx.project_id,
        timeline["id"],
        TextFieldCreate(expected_revision=timeline["revision"], label="招式", base_value="基础剑法"),
    )
    listed = await get_tool("list_setting_timelines").run(ctx, {"subject_kind": "character", **filters["character"]})
    assert listed["items"][0]["custom_fields"] == [
        {"id": field.item.id, "label": "招式", "field_type": "text", "definition_id": None},
    ]
    result, _ = await apply(
        ctx,
        "record_setting_progression",
        {
            "timeline_id": timeline["id"],
            "expected_revision": field.timeline_revision,
            "custom_field_id": field.item.id,
            "operation": "append",
            "value": {"text": "天剑式"},
            "anchor_kind": "planned",
            "chapter_no": 2,
        },
    )
    assert result["item"]["custom_field_id"] == field.item.id and result["item"]["value"] == "天剑式"


async def test_hidden_relationship_is_not_an_ordinary_tool_target(ctx):
    a, b = await new_character(ctx), await new_character(ctx, "陈风")
    type_id = await ctx.db.scalar(select(RelationshipType.id).where(RelationshipType.project_id.is_(None)).limit(1))
    relation = Relationship(
        project_id=ctx.project_id,
        a_id=a["id"],
        b_id=b["id"],
        pair_low=a["id"],
        pair_high=b["id"],
        type_id=type_id,
        is_present=False,
    )
    ctx.db.add(relation)
    await ctx.db.commit()
    assert str(relation.id) in (await effects.snapshot(ctx.db, ctx.project_id))["relationships"]
    assert (await get_tool("update_relationship").preview(ctx, {"id": relation.id, "intimacy": 10}))[
        "error"
    ] == "不存在"


async def test_member_removal_tool_records_explicit_exit_then_can_delete_record(ctx):
    character = await new_character(ctx)
    org, _ = await apply(ctx, "create_organization", {"name": "门派"})
    keys = {"organization_id": org["id"], "character_id": character["id"]}
    member, _ = await apply(ctx, "set_org_member", {**keys, "position": "弟子"})
    timeline = (await get_tool("list_setting_timelines").run(ctx, {**keys, "subject_kind": "membership"}))["items"][0]
    chapter = await new_chapter(ctx)
    args = {
        **keys,
        "edit_mode": "progression",
        "expected_revision": timeline["revision"],
        "chapter_id": chapter.id,
        "boundary": "end",
    }
    await apply(ctx, "remove_org_member", args)
    event = await ctx.db.scalar(select(SettingProgression).where(SettingProgression.timeline_id == timeline["id"]))
    assert event.field_key == "presence" and event.value == {"exists": False}
    assert event.chapter_id == chapter.id and event.boundary == "end"
    assert await ctx.db.get(OrganizationMember, member["id"]) is None
    latest = (await get_tool("list_setting_timelines").run(ctx, {**keys, "subject_kind": "membership"}))["items"][0]
    delete_args = {**keys, "edit_mode": "delete", "expected_revision": latest["revision"]}
    await apply(ctx, "remove_org_member", delete_args)
    assert await ctx.db.get(SettingTimeline, timeline["id"]) is None
    assert await ctx.db.scalar(select(SettingProgression.id).where(SettingProgression.id == event.id)) is None


async def test_member_tool_preserves_explicit_planned_anchor_with_existing_chapter_number(ctx):
    character = await new_character(ctx)
    org, _ = await apply(ctx, "create_organization", {"name": "门派"})
    keys = {"organization_id": org["id"], "character_id": character["id"]}
    await apply(ctx, "set_org_member", {**keys, "position": "弟子"})
    chapter = await new_chapter(ctx)
    timeline = (await get_tool("list_setting_timelines").run(ctx, {**keys, "subject_kind": "membership"}))["items"][0]
    await apply(
        ctx,
        "remove_org_member",
        {
            **keys,
            "edit_mode": "progression",
            "expected_revision": timeline["revision"],
            "anchor_kind": "planned",
            "chapter_no": chapter.chapter_no,
        },
    )
    event = await ctx.db.scalar(select(SettingProgression).where(SettingProgression.timeline_id == timeline["id"]))
    assert event.anchor_kind == "planned" and event.chapter_id is None and event.chapter_no == chapter.chapter_no


async def test_relationship_tool_preserves_explicit_planned_anchor_with_existing_chapter_number(ctx):
    a, b = await new_character(ctx), await new_character(ctx, "陈风")
    chapter = await new_chapter(ctx)
    await apply(
        ctx,
        "create_relationship",
        {
            "a_id": a["id"],
            "b_id": b["id"],
            "role_a": "师父",
            "role_b": "徒弟",
            "anchor_kind": "planned",
            "chapter_no": chapter.chapter_no,
            "expected_revision": 0,
        },
    )
    timeline = (
        await get_tool("list_setting_timelines").run(
            ctx, {"subject_kind": "relationship", "character_id": a["id"], "related_character_id": b["id"]}
        )
    )["items"][0]
    event = await ctx.db.scalar(select(SettingProgression).where(SettingProgression.timeline_id == timeline["id"]))
    assert event.anchor_kind == "planned" and event.chapter_id is None and event.chapter_no == chapter.chapter_no


async def test_career_tools_preserve_explicit_position_and_revision(ctx):
    character = await new_character(ctx)
    career, _ = await apply(ctx, "create_career", {"name": "剑修", "stages": [{"name": "入门"}, {"name": "大师"}]})
    keys = {"character_id": character["id"], "career_id": career["id"]}
    assignment, _ = await apply(ctx, "assign_career", keys)
    timeline = (await get_tool("list_setting_timelines").run(ctx, {**keys, "subject_kind": "career"}))["items"][0]
    first, second = await new_chapter(ctx), await new_chapter(ctx)
    args = {
        **keys,
        "stage_id": career["stages"][1]["id"],
        "edit_mode": "progression",
        "chapter_id": first.id,
        "boundary": "end",
        "expected_revision": timeline["revision"],
    }
    assert "error" in await get_tool("assign_career").preview(
        ctx, {key: value for key, value in args.items() if key != "expected_revision"}
    )
    await apply(ctx, "assign_career", args)
    events = list(
        await ctx.db.scalars(select(SettingProgression).where(SettingProgression.timeline_id == timeline["id"]))
    )
    assert len(events) == 1 and events[0].field_key == "stage_id"
    assert events[0].chapter_id == first.id and events[0].boundary == "end"
    assert "error" in await get_tool("assign_career").preview(ctx, args)
    latest = (await get_tool("list_setting_timelines").run(ctx, {**keys, "subject_kind": "career"}))["items"][0]
    assert "error" in await get_tool("remove_career_assignment").preview(
        ctx,
        {
            "character_id": character["id"],
            "assignment_id": assignment["id"],
            "edit_mode": "progression",
            "chapter_id": second.id,
        },
    )
    await apply(
        ctx,
        "remove_career_assignment",
        {
            "character_id": character["id"],
            "assignment_id": assignment["id"],
            "edit_mode": "progression",
            "expected_revision": latest["revision"],
            "chapter_id": second.id,
            "boundary": "end",
        },
    )
    events = list(
        await ctx.db.scalars(select(SettingProgression).where(SettingProgression.timeline_id == timeline["id"]))
    )
    assert len(events) == 2 and events[-1].value == {"exists": False}
    assert events[-1].chapter_id == second.id and events[-1].boundary == "end"


async def test_agent_can_create_new_career_timeline_with_expected_zero_revision(ctx):
    character = await new_character(ctx)
    career, _ = await apply(ctx, "create_career", {"name": "医修", "stages": [{"name": "初学"}]})
    chapter = await new_chapter(ctx)
    args = {
        "character_id": character["id"],
        "career_id": career["id"],
        "edit_mode": "progression",
        "chapter_id": chapter.id,
        "expected_revision": 0,
    }
    await apply(ctx, "assign_career", args)
    timeline = (
        await get_tool("list_setting_timelines").run(
            ctx, {"subject_kind": "career", "character_id": character["id"], "career_id": career["id"]}
        )
    )["items"][0]
    event = await ctx.db.scalar(select(SettingProgression).where(SettingProgression.timeline_id == timeline["id"]))
    assert event.field_key == "presence" and event.value["exists"] is True
    assert event.chapter_id == chapter.id
    assert "error" in await get_tool("assign_career").preview(ctx, args)


async def test_career_tool_preserves_explicit_planned_anchor_with_existing_chapter_number(ctx):
    character = await new_character(ctx)
    career, _ = await apply(ctx, "create_career", {"name": "剑修", "stages": [{"name": "入门"}, {"name": "大师"}]})
    keys = {"character_id": character["id"], "career_id": career["id"]}
    assignment, _ = await apply(ctx, "assign_career", keys)
    chapter = await new_chapter(ctx)
    timeline = (await get_tool("list_setting_timelines").run(ctx, {**keys, "subject_kind": "career"}))["items"][0]
    await apply(
        ctx,
        "assign_career",
        {
            **keys,
            "stage_id": career["stages"][1]["id"],
            "edit_mode": "progression",
            "expected_revision": timeline["revision"],
            "anchor_kind": "planned",
            "chapter_no": chapter.chapter_no,
        },
    )
    latest = (await get_tool("list_setting_timelines").run(ctx, {**keys, "subject_kind": "career"}))["items"][0]
    await apply(
        ctx,
        "remove_career_assignment",
        {
            "character_id": character["id"],
            "assignment_id": assignment["id"],
            "edit_mode": "progression",
            "expected_revision": latest["revision"],
            "anchor_kind": "planned",
            "chapter_no": chapter.chapter_no,
            "boundary": "end",
        },
    )
    events = list(
        await ctx.db.scalars(select(SettingProgression).where(SettingProgression.timeline_id == timeline["id"]))
    )
    assert len(events) == 2
    assert all(event.anchor_kind == "planned" and event.chapter_id is None for event in events)
    assert [event.chapter_no for event in events] == [chapter.chapter_no, chapter.chapter_no]


async def test_real_relationship_progression_preview_is_stable_when_new_type_id_changes(ctx):
    a, b = await new_character(ctx), await new_character(ctx, "陈风")
    chapter = await new_chapter(ctx)
    args = {
        "a_id": a["id"],
        "b_id": b["id"],
        "role_a": "传剑者",
        "role_b": "承剑者",
        "chapter_id": chapter.id,
        "expected_revision": 0,
    }
    allocated = []

    def capture_insert(_conn, cursor, statement, _parameters, _context, _executemany):
        if statement.lower().lstrip().startswith("insert into relationship_types"):
            allocated.append(cursor.lastrowid)

    sqlalchemy_event.listen(ctx.db.bind.sync_engine, "after_cursor_execute", capture_insert)
    try:
        first = await get_tool("create_relationship").preview(ctx, args)
        other = Project(user_id=ctx.user_id, title="另一作品")
        ctx.db.add(other)
        await ctx.db.flush()
        ctx.db.add(RelationshipType(project_id=other.id, role_a="其他甲", role_b="其他乙"))
        await ctx.db.commit()
        second = await get_tool("create_relationship").preview(ctx, args)
    finally:
        sqlalchemy_event.remove(ctx.db.bind.sync_engine, "after_cursor_execute", capture_insert)
    assert "error" not in first and "error" not in second
    assert len(allocated) >= 3 and allocated[0] != allocated[-1]
    assert first == second
    progression = next(
        item
        for item in first["diff"]
        if item["field"].startswith("setting_progressions.") and item["field"].endswith(".value")
    )
    assert progression["after"]["state"]["identity"]["type_id"] == "新增relationship_types:1"


@pytest.mark.parametrize(
    "noun,create,update,field",
    [
        ("character", {"name": "林远", "personality": "急躁"}, {"personality": "沉稳"}, "personality"),
        ("world_setting", {"title": "天剑宗", "content": "古老宗门"}, {"content": "千年剑宗"}, "content"),
        ("outline", {"title": "启程", "summary": "出发"}, {"summary": "离开宗门"}, "summary"),
        ("organization", {"name": "天剑宗"}, {"purpose": "守护天下"}, "purpose"),
        ("career", {"name": "剑修", "stages": [{"name": "入门"}]}, {"description": "以剑修行"}, "description"),
        ("foreshadow", {"title": "玉佩"}, {"content": "刻有族徽"}, "content"),
    ],
)
async def test_crud_preview_and_execution(ctx, noun, create, update, field):
    created, preview = await apply(ctx, f"create_{noun}", create)
    assert created["id"] > 0 and preview["diff"]
    updated, preview = await apply(ctx, f"update_{noun}", {"id": created["id"], **update})
    assert updated[field] == update[field]
    assert any(d["field"].endswith("." + field) and d["after"] == update[field] for d in preview["diff"])
    _, preview = await apply(ctx, f"delete_{noun}", {"id": created["id"]})
    assert any(d["before"] is not None and d["after"] is None for d in preview["diff"])


async def test_relationship_lifecycle_and_custom_type_preview(ctx):
    a, b = await new_character(ctx), await new_character(ctx, "陈风")
    created, preview = await apply(
        ctx,
        "create_relationship",
        {
            "a_id": a["id"],
            "b_id": b["id"],
            "role_a": "传剑者",
            "role_b": "承剑者",
        },
    )
    assert any(d["field"].startswith("relationship_types.") for d in preview["diff"])
    changed, _ = await apply(ctx, "update_relationship", {"id": created["id"], "intimacy": 50})
    assert changed["intimacy"] == 50
    await apply(ctx, "delete_relationship", {"id": created["id"]})
    assert await ctx.db.get(Relationship, created["id"]) is None


async def test_project_update_allowlist(ctx):
    result, _ = await apply(ctx, "update_project", {"theme": "成长", "tags": ["冒险", "冒险"], "target_words": 90000})
    assert result["theme"] == "成长" and result["tags"] == ["冒险"]
    for args in ({"title": "不允许改书名"}, {"status": "completed"}, {"style": "绕过风格选择"}):
        assert "error" in await get_tool("update_project").preview(ctx, args)


async def test_member_and_career_assignments(ctx):
    char = await new_character(ctx)
    org, _ = await apply(ctx, "create_organization", {"name": "天剑宗"})
    keys = {"organization_id": org["id"], "character_id": char["id"]}
    member, _ = await apply(ctx, "set_org_member", {**keys, "position": "弟子", "note": "内门"})
    changed, _ = await apply(ctx, "set_org_member", {**keys, "position": "长老"})
    assert changed["id"] == member["id"] and changed["note"] == "内门"
    await apply(ctx, "remove_org_member", keys)
    assert await ctx.db.get(OrganizationMember, member["id"]) is None
    career, _ = await apply(ctx, "create_career", {"name": "剑修", "stages": [{"name": "入门"}, {"name": "大师"}]})
    assigned, _ = await apply(ctx, "assign_career", {"character_id": char["id"], "career_id": career["id"]})
    changed, _ = await apply(
        ctx,
        "assign_career",
        {
            "character_id": char["id"],
            "career_id": career["id"],
            "stage_id": career["stages"][1]["id"],
        },
    )
    assert changed["id"] == assigned["id"] and changed["stage_position"] == 2
    await apply(ctx, "remove_career_assignment", {"character_id": char["id"], "assignment_id": assigned["id"]})
    assert await ctx.db.get(CharacterCareer, assigned["id"]) is None


async def test_plans_and_chapter_meta(ctx):
    outline, _ = await apply(ctx, "create_outline", {"title": "启程"})
    result, _ = await apply(
        ctx,
        "update_chapter_plans",
        {
            "outline_id": outline["id"],
            "plans": [{"title": "出门", "summary": "去市集"}],
        },
    )
    assert result["items"][0]["title"] == "出门"
    ch = await new_chapter(ctx)
    result, preview = await apply(ctx, "update_chapter_meta", {"id": ch.id, "title": "初遇"})
    assert result["title"] == "初遇" and preview["diff"]


async def test_read_tools_and_chapter_pagination(ctx):
    char = await new_character(ctx)
    org, _ = await apply(ctx, "create_organization", {"name": "天剑宗"})
    outline, _ = await apply(ctx, "create_outline", {"title": "启程"})
    content = "甲" * 4000 + "乙" * 4000 + "丙"
    ch = await new_chapter(ctx, content)
    args_by_name = {
        "get_character": {"id": char["id"]},
        "get_setting_entry_fields": {"entry": {"kind": "character", "id": char["id"]}},
        "get_organization": {"id": org["id"]},
        "get_outline": {"id": outline["id"]},
        "read_chapter": {"id": ch.id},
        "get_chapter_analysis": {"chapter_id": ch.id},
        "search_memories": {"query": "竹林"},
        "search_knowledge": {"query": "竹林"},
        "load_skill": {"slug": "deslop"},
        "read_skill_reference": {"slug": "deslop", "index": 1},
    }
    for tool in list_tools():
        if tool.name == "read_skill_reference":
            continue  # 内置 Skill 没有参考资料，见 test_agent_mcp_skills
        if tool.kind == "read":
            result = await tool.run(ctx, args_by_name.get(tool.name, {}))
            assert "error" not in result, (tool.name, result)
    tool = get_tool("read_chapter")
    pages = [await tool.run(ctx, {"id": ch.id, "page": i}) for i in (1, 2, 3)]
    assert "".join(page["content"] for page in pages) == content
    assert all(page["pages"] == 3 for page in pages)
    assert "error" in await tool.run(ctx, {"id": ch.id, "page": 4})


async def test_search_excludes_foreign_and_disabled(ctx):
    other = Project(user_id=ctx.user_id, title="另一项目")
    ctx.db.add(other)
    await ctx.db.flush()
    for source in ("memory", "kb"):
        await retrieval_service.upsert_many(
            ctx.db,
            ctx.project_id,
            source,
            [
                retrieval_service.ChunkInput(1, "竹林深处有古剑"),
                retrieval_service.ChunkInput(2, "竹林不能召回", enabled=False),
            ],
        )
        await retrieval_service.upsert_many(ctx.db, other.id, source, [retrieval_service.ChunkInput(3, "竹林其他项目")])
    await ctx.db.commit()
    for name in ("search_memories", "search_knowledge"):
        result = await get_tool(name).run(ctx, {"query": "竹林", "top_k": 10})
        assert [hit["ref_id"] for hit in result["hits"]] == [1]
        assert "error" in await get_tool(name).run(ctx, {"query": "竹林", "top_k": 11})


@pytest.mark.parametrize("content,original", [("重复。重复。", "找不到"), ("重复。重复。", "重复"), ("哈哈哈", "哈哈")])
async def test_replacement_requires_unique_original(ctx, content, original):
    ch = await new_chapter(ctx, content)
    args = {"chapter_id": ch.id, "original": original, "replacement": "新文本"}
    for method in (get_tool("replace_chapter_text").preview, get_tool("replace_chapter_text").execute):
        result = await method(ctx, args)
        assert result == {"error": "原文未找到或不唯一，请扩大或缩小引用范围"}
    await ctx.db.refresh(ch)
    assert ch.content == content


async def test_replacement_records_agent_version_and_stales_analysis(ctx):
    ch = await new_chapter(ctx)
    cid = ch.id
    analysis = ChapterAnalysis(
        project_id=ctx.project_id, chapter_id=cid, chapter_no=1, content_hash=analysis_service.content_hash(ch.content)
    )
    ctx.db.add(analysis)
    await ctx.db.commit()
    tool = get_tool("replace_chapter_text")
    args = {"chapter_id": cid, "original": "握紧", "replacement": "放下"}
    before = await tool.preview(ctx, args)
    ch.content += "远处传来钟声。"
    await ctx.db.commit()
    refreshed = await tool.preview(ctx, args)
    assert before["content_hash"] != refreshed["content_hash"]
    result = await tool.execute(ctx, args)
    assert "error" not in result, result
    await ctx.db.commit()
    version = await ctx.db.get(ChapterVersion, result["after_version_id"])
    previous = await ctx.db.get(ChapterVersion, result["before_version_id"])
    assert version.source == "agent" and "放下" in version.content and "握紧" in previous.content
    assert (await analysis_service.chapter_state(ctx.db, ctx.user_id, cid)).status == "stale"
    ch.status = "generating"
    await ctx.db.commit()
    result = await tool.preview(ctx, {**args, "original": "放下"})
    assert "正在生成" in result["error"]


async def test_foreign_project_references_at_every_depth(ctx):
    foreign = Project(user_id=ctx.user_id, title="不能访问的项目")
    ctx.db.add(foreign)
    await ctx.db.flush()
    character = Character(project_id=foreign.id, name="别人的角色")
    chapter = Chapter(project_id=foreign.id, chapter_no=1, title="秘密", content="秘密正文")
    org = Organization(project_id=foreign.id, name="秘密组织")
    outline = Outline(project_id=foreign.id, title="秘密大纲", chapter_no=1)
    ctx.db.add_all([character, chapter, org, outline])
    await ctx.db.commit()
    cases = [
        ("get_character", {"id": character.id}),
        ("read_chapter", {"id": chapter.id}),
        ("update_character", {"id": character.id, "name": "越权"}),
        ("create_organization", {"name": "越权", "parent_id": org.id}),
        ("create_foreshadow", {"title": "越权", "character_ids": [character.id]}),
        ("update_chapter_plans", {"outline_id": outline.id, "plans": []}),
        ("replace_chapter_text", {"chapter_id": chapter.id, "original": "秘密", "replacement": "泄露"}),
        ("start_task", {"kind": "chapter.generate", "params": {"chapter_id": chapter.id}}),
        ("start_task", {"kind": "character.generate", "params": {"project_id": foreign.id}}),
        ("start_task", {"kind": "character.generate", "params": {"extra": {"character_ids": [character.id]}}}),
    ]
    for name, args in cases:
        tool = get_tool(name)
        method = tool.run if tool.kind == "read" else tool.preview
        assert await method(ctx, args) == {"error": "不存在"}, name


async def test_preview_stable_across_unrelated_generated_ids_and_batch_creates(ctx):
    tool = get_tool("create_character")
    args = {"name": "新角色"}
    before = await tool.preview(ctx, args)
    other = Project(user_id=ctx.user_id, title="另一项目")
    ctx.db.add(other)
    await ctx.db.flush()
    ctx.db.add(Character(project_id=other.id, name="占用序列"))
    await ctx.db.commit()
    assert await tool.preview(ctx, args) == before
    await new_character(ctx, "同批前一个角色")
    assert await tool.preview(ctx, args) == before


async def test_preview_failure_rolls_back_and_preserves_pending_outer_write(ctx):
    char = await new_character(ctx)
    row = await ctx.db.get(Character, char["id"])
    row.personality = "调用者待提交的编辑"
    bad = await get_tool("create_career").preview(ctx, {"name": "会失败", "stages": []})
    assert "error" in bad
    assert await ctx.db.scalar(select(func.count(Career.id))) == 0
    await ctx.db.commit()
    await ctx.db.refresh(row)
    assert row.personality == "调用者待提交的编辑"


async def test_execute_does_not_commit_outer_transaction(ctx, app):
    result = await get_tool("create_character").execute(ctx, {"name": "应随外层回滚"})
    assert "error" not in result
    await ctx.db.rollback()
    async with app.state.sessionmaker() as other:
        assert await other.scalar(select(func.count(Character.id))) == 0


@pytest.mark.parametrize(
    "kind",
    [
        "chapter.generate",
        "chapter.polish",
        "chapter.revise",
        "chapter.batch",
        "chapter.analyze",
        "outline.expand",
        "outline.continue",
        "character.generate",
        "organization.generate",
        "career.generate",
    ],
)
async def test_task_preview_uses_real_handler_without_claiming(ctx, fake_llm, kind):
    ch = await new_chapter(ctx)
    cid = ch.id
    outline, _ = await apply(ctx, "create_outline", {"title": "启程"})
    analysis = ChapterAnalysis(
        project_id=ctx.project_id, chapter_id=cid, chapter_no=1, content_hash=analysis_service.content_hash(ch.content)
    )
    ctx.db.add(analysis)
    await ctx.db.commit()
    params = {}
    if kind.startswith("chapter.") and kind != "chapter.batch":
        params["chapter_id"] = cid
    if kind == "chapter.revise":
        params.update(analysis_id=analysis.id, instruction="更紧凑")
    if kind == "chapter.batch":
        params.update(start_chapter_id=cid, end_chapter_id=cid, include_written=True)
    if kind == "outline.expand":
        params["outline_id"] = outline["id"]
    preview = await get_tool("start_task").preview(ctx, {"kind": kind, "params": params})
    assert "error" not in preview, preview
    assert "AI 调用" in preview["warnings"][0]
    await ctx.db.refresh(ch)
    assert ch.status == "draft"
    assert await ctx.db.scalar(select(func.count(Task.id))) == 0
    assert fake_llm.calls == []


async def test_start_task_whitelist_validation_and_transaction(ctx):
    tool = get_tool("start_task")
    assert "error" in await tool.preview(ctx, {"kind": "book.import", "params": {}})
    assert "error" in await tool.preview(ctx, {"kind": "chapter.generate", "params": {}})
    result = await tool.execute(ctx, {"kind": "character.generate", "params": {"instruction": "冷静的剑修"}})
    assert "error" not in result, result
    task = await ctx.db.get(Task, result["task_id"])
    assert task.project_id == ctx.project_id and task.status == "queued"
    await ctx.db.commit()
    active = await get_tool("list_active_tasks").run(ctx, {})
    assert [row["id"] for row in active["items"]] == [task.id]


async def test_task_request_always_binds_server_project(ctx):
    # 引用校验之外的第二道防线：即使参数里混入其他项目，最终请求仍绑定服务端项目。
    from app.services.agent.tools.tasks import StartArgs, request_params

    args = StartArgs.model_validate({"kind": "character.generate", "params": {"project_id": ctx.project_id + 999}})
    assert request_params(ctx, args)["project_id"] == ctx.project_id


async def test_changed_target_and_cascades_refresh_preview(ctx):
    char = await new_character(ctx)
    org, _ = await apply(ctx, "create_organization", {"name": "天剑宗"})
    await apply(ctx, "set_org_member", {"organization_id": org["id"], "character_id": char["id"]})
    tool = get_tool("update_character")
    args = {"id": char["id"], "personality": "冷静"}
    before = await tool.preview(ctx, args)
    await story_service.update_owned(
        ctx.db,
        story_service.CHARACTERS,
        ctx.user_id,
        ctx.project_id,
        char["id"],
        CharacterUpdate(background="其他编辑器补充的背景"),
    )
    refreshed = await tool.preview(ctx, args)
    assert before["diff"] == refreshed["diff"]
    assert before["guards"] != refreshed["guards"]
    assert refreshed["target"] == "林远"
    preview = await get_tool("delete_character").preview(ctx, {"id": char["id"]})
    assert any(item["field"].startswith("organization_members.") for item in preview["diff"])
    assert await ctx.db.scalar(select(func.count(OrganizationMember.id))) == 1


async def test_preview_guard_catches_other_baseline_field_changed_concurrently(ctx):
    char = await new_character(ctx)
    tool = get_tool("update_character")
    args = {"id": char["id"], "personality": "沉稳"}
    before = await tool.preview(ctx, args)
    await story_service.update_owned(
        ctx.db,
        story_service.CHARACTERS,
        ctx.user_id,
        ctx.project_id,
        char["id"],
        CharacterUpdate(background="他人补充的经历"),
    )
    refreshed = await tool.preview(ctx, args)
    assert before["diff"] == refreshed["diff"]
    old_guard = next(guard for guard in before["guards"] if guard["table"] == "setting_timelines")
    new_guard = next(guard for guard in refreshed["guards"] if guard["table"] == "setting_timelines")
    assert old_guard["hash"] != new_guard["hash"]
    result = await tool.execute(ctx, args)
    assert result["personality"] == "沉稳" and result["background"] == "他人补充的经历"


async def test_preview_does_not_change_project_timestamp(ctx):
    stamp = ctx.project.updated_at
    await get_tool("create_character").preview(ctx, {"name": "林远"})
    await ctx.db.commit()
    await ctx.db.refresh(ctx.project)
    assert ctx.project.updated_at == stamp
    assert await ctx.db.scalar(select(func.count(Character.id))) == 0


async def test_preview_reuses_business_validation_without_partial_writes(ctx):
    root, _ = await apply(ctx, "create_organization", {"name": "总会"})
    child, _ = await apply(ctx, "create_organization", {"name": "分会", "parent_id": root["id"]})
    result = await get_tool("update_organization").preview(ctx, {"id": root["id"], "parent_id": child["id"]})
    assert "下级" in result["error"]
    assert (await ctx.db.get(Organization, root["id"])).parent_id is None
    result = await get_tool("create_character").preview(ctx, {"name": "x" * 101})
    assert result["error"] == "工具参数校验失败" and result["errors"]


async def test_task_preview_real_handler_rejects_missing_config(ctx):
    await ctx.db.execute(delete(AIConfig).where(AIConfig.user_id == ctx.user_id))
    await ctx.db.commit()
    result = await get_tool("start_task").preview(ctx, {"kind": "character.generate", "params": {}})
    assert "尚未配置" in result["error"]
    assert await ctx.db.scalar(select(func.count(Task.id))) == 0


async def test_cross_user_nested_config_and_ids_are_not_found(ctx, make_client):
    other_client = make_client()
    await register(other_client, "bob")
    project = await create_project(other_client, style="")
    config = await create_ai_config(other_client)
    character = (
        await other_client.post(
            f"/api/v1/projects/{project['id']}/characters",
            json={"name": "私密角色"},
        )
    ).json()
    assert await get_tool("get_character").run(ctx, {"id": character["id"]}) == {"error": "不存在"}
    assert await get_tool("start_task").preview(
        ctx,
        {
            "kind": "character.generate",
            "params": {"ai_config_id": config["id"]},
        },
    ) == {"error": "不存在"}


async def test_task_execute_also_stays_in_outer_transaction(ctx, app):
    result = await get_tool("start_task").execute(ctx, {"kind": "character.generate", "params": {}})
    assert "task_id" in result
    await ctx.db.rollback()
    async with app.state.sessionmaker() as other:
        assert await other.scalar(select(func.count(Task.id))) == 0


def test_preview_diff_ignores_created_at_of_inserted_rows():
    from app.services.agent.tools.base import preview_diff

    row = {"id": 9, "project_id": 1, "name": "新人", "created_at": "2026-01-01T00:00:00"}
    diff = preview_diff([{"table": "characters", "pk": {"id": 9}, "before": None, "after": row}])
    assert [item["field"].rsplit(".", 1)[-1] for item in diff] == ["name"]
