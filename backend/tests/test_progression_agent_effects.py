"""智能体效果保存、旧格式兼容与时间线撤销的真实数据库回归。"""

from copy import deepcopy

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select

from app.core.errors import ConflictError
from app.models import (
    AgentChange,
    AgentConversation,
    Chapter,
    Character,
    Project,
    Relationship,
    RelationshipType,
    SettingProgression,
    SettingTextField,
    SettingTimeline,
    User,
)
from app.schemas.progression import ProgressionCreate
from app.services import setting_progression_service, version_service
from app.services.agent import effects
from app.services.agent.service import _undo_changes
from app.services.agent.tools.base import preview_diff


@pytest.fixture
async def db_project(app):
    async with app.state.sessionmaker() as db:
        user = User(username="progression-effects", password_hash="unused")
        db.add(user)
        await db.flush()
        project = Project(user_id=user.id, title="设定演变撤销")
        db.add(project)
        await db.commit()
        yield db, project


async def _add_character_timeline(db, project_id):
    character = Character(project_id=project_id, name="林远", current_state="练气")
    db.add(character)
    await db.flush()
    timeline = SettingTimeline(
        project_id=project_id,
        subject_kind="character",
        character_id=character.id,
        baseline={"current_state": "练气", "life_status": "alive"},
        baseline_status="confirmed",
    )
    db.add(timeline)
    await db.flush()
    field = SettingTextField(timeline_id=timeline.id, label="招式", base_value="基础剑法")
    db.add(field)
    await db.flush()
    event = SettingProgression(
        timeline_id=timeline.id,
        custom_field_id=field.id,
        operation="replace",
        value="天剑式",
        anchor_kind="planned",
        chapter_no=20,
        boundary="end",
        source="manual",
        protection="manual",
    )
    db.add(event)
    await db.flush()
    return character, timeline, field, event


async def test_current_effects_restore_deleted_character_timeline_fields_events_and_hidden_relation(db_project):
    db, project = db_project
    character, timeline, field, event = await _add_character_timeline(db, project.id)
    other = Character(project_id=project.id, name="陈风")
    db.add(other)
    await db.flush()
    kind_id = await db.scalar(select(RelationshipType.id).where(RelationshipType.project_id.is_(None)).limit(1))
    relation = Relationship(
        project_id=project.id,
        a_id=character.id,
        b_id=other.id,
        pair_low=character.id,
        pair_high=other.id,
        type_id=kind_id,
        is_present=False,
    )
    db.add(relation)
    await db.commit()
    ids = character.id, timeline.id, field.id, event.id, relation.id
    before = await effects.snapshot(db, project.id)
    await db.delete(character)
    after = await effects.snapshot(db, project.id)
    captured = effects.diff(before, after)

    assert {"setting_timelines", "setting_text_fields", "setting_progressions", "relationships"} <= {
        item["table"] for item in captured
    }
    assert all(item["format_version"] == 5 for item in captured)
    await db.commit()
    await effects.undo(db, project.id, captured)
    await db.commit()

    assert await effects.snapshot(db, project.id) == before
    assert (await db.get(Relationship, ids[4])).is_present is False
    assert (await db.get(SettingTimeline, ids[1])).baseline["current_state"] == "练气"
    assert (await db.get(SettingTextField, ids[2])).label == "招式"
    assert (await db.get(SettingProgression, ids[3])).value == "天剑式"


async def test_undo_new_timeline_rejects_later_event_atomically(db_project):
    db, project = db_project
    before = await effects.snapshot(db, project.id)
    character, timeline, field, _ = await _add_character_timeline(db, project.id)
    captured = effects.diff(before, await effects.snapshot(db, project.id))
    await db.commit()
    db.add(
        SettingProgression(
            timeline_id=timeline.id,
            custom_field_id=field.id,
            operation="append",
            value="后续自创",
            anchor_kind="planned",
            chapter_no=21,
            boundary="start",
            source="manual",
            protection="manual",
        )
    )
    await db.commit()
    current = await effects.snapshot(db, project.id)

    with pytest.raises(ConflictError) as exc:
        await effects.undo(db, project.id, captured)
    assert exc.value.extra["code"] == "undo_conflict"
    assert await effects.snapshot(db, project.id) == current
    assert await db.get(Character, character.id) is not None


async def test_undo_event_recomputes_latest_projection_only_for_its_project(db_project):
    db, project = db_project
    character = Character(project_id=project.id, name="甲", current_state="练气")
    foreign = Project(user_id=project.user_id, title="另一作品")
    db.add_all([character, foreign])
    await db.flush()
    foreign_character = Character(project_id=foreign.id, name="乙", current_state="他书缓存")
    db.add(foreign_character)
    await db.flush()
    timeline = SettingTimeline(
        project_id=project.id,
        subject_kind="character",
        character_id=character.id,
        baseline={"current_state": "练气", "life_status": "alive"},
    )
    foreign_timeline = SettingTimeline(
        project_id=foreign.id,
        subject_kind="character",
        character_id=foreign_character.id,
        baseline={"current_state": "他书基础", "life_status": "alive"},
    )
    db.add_all([timeline, foreign_timeline])
    await db.flush()
    event = SettingProgression(
        timeline_id=timeline.id,
        field_key="current_state",
        operation="replace",
        value="筑基",
        anchor_kind="planned",
        chapter_no=2,
        boundary="end",
        source="manual",
        protection="manual",
    )
    db.add(event)
    await db.commit()
    before = await effects.snapshot(db, project.id)
    event.value = "金丹"
    delta = effects.diff(before, await effects.snapshot(db, project.id))
    await db.commit()
    await effects.undo(db, project.id, delta)
    await db.commit()
    assert event.value == "筑基"
    assert character.current_state == "筑基"
    assert foreign_character.current_state == "他书缓存"


async def test_undo_created_type_rejects_later_inactive_event_reference_atomically(db_project):
    db, project = db_project
    a, b = Character(project_id=project.id, name="甲"), Character(project_id=project.id, name="乙")
    db.add_all([a, b])
    await db.flush()
    preset = await db.scalar(select(RelationshipType.id).where(RelationshipType.project_id.is_(None)).limit(1))
    timeline = SettingTimeline(
        project_id=project.id,
        subject_kind="relationship",
        character_id=a.id,
        related_character_id=b.id,
        baseline={
            "exists": True,
            "identity": {"a_id": a.id, "b_id": b.id, "type_id": preset},
            "intimacy": 0,
            "status": "normal",
            "description": "",
        },
    )
    db.add(timeline)
    await db.flush()
    await setting_progression_service.materialize_latest(db, timeline)
    await db.commit()
    before = await effects.snapshot(db, project.id)
    kind = RelationshipType(project_id=project.id, role_a="师父", role_b="徒弟")
    db.add(kind)
    delta = effects.diff(before, await effects.snapshot(db, project.id))
    await db.commit()
    change = await setting_progression_service.apply_progression(
        db,
        timeline,
        ProgressionCreate(
            expected_revision=timeline.revision,
            field_key="identity",
            operation="replace",
            value={"a_id": a.id, "b_id": b.id, "type_id": kind.id},
            anchor_kind="planned",
            chapter_no=2,
        ),
    )
    await setting_progression_service.revoke_progression(db, timeline, change, timeline.revision)
    await db.commit()
    unchanged = await effects.snapshot(db, project.id)
    with pytest.raises(ConflictError) as exc:
        await effects.undo(db, project.id, delta)
    assert exc.value.extra["code"] == "undo_conflict"
    assert await effects.snapshot(db, project.id) == unchanged
    assert (await db.get(SettingProgression, change.id)).active is False


async def test_undo_repointed_event_restores_builtin_field_before_deleting_new_custom_field(db_project):
    db, project = db_project
    character = Character(project_id=project.id, name="角色", current_state="筑基")
    db.add(character)
    await db.flush()
    timeline = SettingTimeline(
        project_id=project.id,
        subject_kind="character",
        character_id=character.id,
        baseline={"current_state": "练气", "life_status": "alive"},
    )
    db.add(timeline)
    await db.flush()
    event = SettingProgression(
        timeline_id=timeline.id,
        field_key="current_state",
        operation="replace",
        value="筑基",
        anchor_kind="planned",
        chapter_no=10,
        boundary="start",
        source="manual",
        protection="manual",
    )
    db.add(event)
    await db.commit()
    before = await effects.snapshot(db, project.id)
    field = SettingTextField(timeline_id=timeline.id, label="新字段", base_value="基础")
    db.add(field)
    await db.flush()
    event.field_key, event.custom_field_id, event.value = None, field.id, "新值"
    captured = effects.diff(before, await effects.snapshot(db, project.id))
    await db.commit()

    await effects.undo(db, project.id, captured)
    await db.commit()
    assert await effects.snapshot(db, project.id) == before
    assert event.field_key == "current_state" and event.custom_field_id is None


@pytest.mark.parametrize("action", ["created", "updated", "deleted"])
async def test_legacy_setting_effect_is_rejected_before_restoring_anything(db_project, action):
    db, project = db_project
    character, timeline, _, _ = await _add_character_timeline(db, project.id)
    await db.commit()
    snapshot = await effects.snapshot(db, project.id)
    current_character = snapshot["characters"][str(character.id)]
    character_id, timeline_id = character.id, timeline.id
    if action == "created":
        legacy = {"table": "characters", "pk": {"id": character_id}, "before": None, "after": current_character}
    elif action == "updated":
        old = deepcopy(current_character)
        old["current_state"] = "旧状态"
        legacy = {"table": "characters", "pk": {"id": character_id}, "before": old, "after": current_character}
    else:
        legacy = {"table": "characters", "pk": {"id": character_id}, "before": current_character, "after": None}
        await db.delete(character)
        await db.commit()
    unchanged = await effects.snapshot(db, project.id)

    with pytest.raises(ConflictError) as exc:
        await effects.undo(db, project.id, [legacy])
    assert exc.value.extra["code"] == "legacy_effect_requires_manual_correction"
    assert await effects.snapshot(db, project.id) == unchanged
    if action != "deleted":
        assert (await db.get(SettingTimeline, timeline_id)).baseline["current_state"] == "练气"


async def test_mixed_v2_and_legacy_effects_are_rejected_before_first_restore(db_project):
    db, project = db_project
    character, _, _, _ = await _add_character_timeline(db, project.id)
    chapter = Chapter(project_id=project.id, title="原题", chapter_no=1)
    db.add(chapter)
    await db.commit()
    before = await effects.snapshot(db, project.id)
    chapter.title = "新题"
    character.current_state = "筑基"
    captured = effects.diff(before, await effects.snapshot(db, project.id))
    for effect in captured:
        if effect["table"] == "characters":
            effect.pop("format_version")
    await db.commit()
    unchanged = await effects.snapshot(db, project.id)

    with pytest.raises(ConflictError) as exc:
        await effects.undo(db, project.id, captured)
    assert exc.value.extra["code"] == "legacy_effect_requires_manual_correction"
    assert await effects.snapshot(db, project.id) == unchanged
    assert chapter.title == "新题" and character.current_state == "筑基"


async def test_undo_turn_rejects_later_legacy_item_before_any_row_restore(db_project):
    db, project = db_project
    character, _, _, _ = await _add_character_timeline(db, project.id)
    chapter = Chapter(project_id=project.id, title="旧题", chapter_no=1)
    conversation = AgentConversation(user_id=project.user_id, project_id=project.id)
    db.add_all([chapter, conversation])
    await db.commit()
    before = await effects.snapshot(db, project.id)
    chapter.title = "新题"
    after = await effects.snapshot(db, project.id)
    current_character = after["characters"][str(character.id)]
    old_character = deepcopy(current_character)
    old_character["current_state"] = "旧状态"
    first = AgentChange(
        conversation_id=conversation.id,
        project_id=project.id,
        tool_call_id="new",
        tool_name="update_chapter_meta",
        kind="small",
        status="executed",
        effects=effects.diff(before, after),
    )
    second = AgentChange(
        conversation_id=conversation.id,
        project_id=project.id,
        tool_call_id="old",
        tool_name="update_character",
        kind="small",
        status="executed",
        effects=[
            {"table": "characters", "pk": {"id": character.id}, "before": old_character, "after": current_character}
        ],
    )
    db.add_all([first, second])
    await db.commit()
    user_id, project_id, chapter_id = project.user_id, project.id, chapter.id
    change_ids = [first.id, second.id]
    updates = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lower().lstrip().startswith("update chapters"):
            updates.append(statement)

    sqlalchemy_event.listen(db.bind.sync_engine, "before_cursor_execute", record_sql)
    try:
        with pytest.raises(ConflictError) as exc:
            await _undo_changes(db, user_id, project_id, [first, second])
        assert exc.value.extra["code"] == "legacy_effect_requires_manual_correction"
    finally:
        sqlalchemy_event.remove(db.bind.sync_engine, "before_cursor_execute", record_sql)

    assert updates == []
    assert (await db.get(Chapter, chapter_id)).title == "新题"
    assert [(await db.get(AgentChange, row_id)).status for row_id in change_ids] == ["executed", "executed"]


async def test_legacy_chapter_position_rejected_but_pure_text_undo_still_works(db_project):
    db, project = db_project
    chapter = Chapter(project_id=project.id, title="原章", chapter_no=2, content="之前")
    db.add(chapter)
    await db.flush()
    original = await version_service.record(db, chapter, "baseline")
    before = await effects.snapshot(db, project.id)
    chapter.content = "之后"
    latest = await version_service.record(db, chapter, "agent")
    captured = effects.diff(before, await effects.snapshot(db, project.id))
    captured.append(
        effects.chapter_text_effect(
            chapter.id,
            original.id,
            latest.id,
            effects.content_hash(original.content),
            effects.content_hash(latest.content),
        )
    )
    legacy_text = [{key: value for key, value in row.items() if key != "format_version"} for row in captured]
    await db.commit()
    await effects.undo(db, project.id, legacy_text)
    await db.commit()
    assert chapter.content == "之前"

    before_number = await effects.snapshot(db, project.id)
    chapter.chapter_no = 4
    captured = effects.diff(before_number, await effects.snapshot(db, project.id))
    legacy_number = [{key: value for key, value in row.items() if key != "format_version"} for row in captured]
    await db.commit()
    with pytest.raises(ConflictError) as exc:
        await effects.undo(db, project.id, legacy_number)
    assert exc.value.extra["code"] == "legacy_effect_requires_manual_correction"
    assert chapter.chapter_no == 4


def test_preview_normalizes_typed_references_in_setting_json_without_changing_effects():
    def proposal(offset):
        char_a, char_b, kind, stage = (offset + number for number in (1, 2, 3, 4))
        baseline = {
            "exists": True,
            "identity": {"a_id": char_a, "b_id": char_b, "type_id": kind},
            "stage_id": stage,
            "intimacy": 101,
        }
        state = {"exists": True, "state": {"identity": baseline["identity"], "intimacy": 101}}
        rows = [
            ("characters", char_a, {"name": "甲"}),
            ("characters", char_b, {"name": "乙"}),
            ("relationship_types", kind, {"name": "师徒"}),
            ("career_stages", stage, {"name": "入门"}),
            ("setting_timelines", offset + 5, {"subject_kind": "relationship", "baseline": baseline}),
            ("setting_progressions", offset + 6, {"field_key": "presence", "value": state}),
            ("setting_progressions", offset + 7, {"field_key": "stage_id", "value": stage}),
            ("setting_progressions", offset + 8, {"field_key": "intimacy", "value": 101}),
        ]
        return [
            {"table": table, "pk": {"id": row_id}, "before": None, "after": {"id": row_id, **data}}
            for table, row_id, data in rows
        ]

    first, second = proposal(100), proposal(200)
    original_first, original_second = deepcopy(first), deepcopy(second)
    first_preview, second_preview = preview_diff(first), preview_diff(second)
    assert first_preview == second_preview
    assert first == original_first and second == original_second
    baseline = next(row["after"] for row in first_preview if row["field"].endswith(".baseline"))
    assert baseline["identity"] == {
        "a_id": "新增characters:1",
        "b_id": "新增characters:2",
        "type_id": "新增relationship_types:1",
    }
    assert baseline["stage_id"] == "新增career_stages:1"
    assert baseline["intimacy"] == 101
    values = [row["after"] for row in first_preview if row["field"].endswith(".value")]
    assert values[0]["state"]["intimacy"] == 101
    assert values[1:] == ["新增career_stages:1", 101]
