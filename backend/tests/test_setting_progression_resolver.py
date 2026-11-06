"""剧情设定的纯时点求值：这些断言只依赖作者可观察到的状态。"""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.schemas.progression import ProgressionCreate, ProgressionUpdate, StoryPosition
from app.services.setting_progression_resolver import (
    EffectiveSnapshot,
    ProgressionEvent,
    resolve_progressions,
    validate_baseline,
    validate_progression_value,
)


def _planned(event_id, chapter_no, field_key, value, *, operation="replace", **kwargs):
    return ProgressionEvent(
        id=event_id,
        field_key=field_key,
        operation=operation,
        value=value,
        anchor_kind="planned",
        chapter_no=chapter_no,
        **kwargs,
    )


def _at(chapter_no, boundary="start"):
    return StoryPosition(chapter_no=chapter_no, boundary=boundary)


def test_chapter_start_and_end_prevent_future_state_from_leaking():
    baseline = {"current_state": "练气", "life_status": "alive"}
    # 故意按写入时间而不是故事顺序传入：先写第20章，再补第5章。
    events = [
        _planned(1, 20, "current_state", "金丹", boundary="end", protection="automatic"),
        _planned(2, 5, "current_state", "筑基"),
        _planned(3, 21, "life_status", "dead"),
    ]

    def state(no, boundary="start"):
        return resolve_progressions("character", baseline, {}, events, _at(no, boundary)).fields

    assert state(4)["current_state"] == "练气"
    assert state(5)["current_state"] == "筑基"
    assert state(20)["current_state"] == "筑基"
    assert state(20, "end")["current_state"] == "金丹"
    assert state(21)["current_state"] == "金丹"
    assert state(20, "end")["life_status"] == "alive"
    assert state(21)["life_status"] == "dead"


def test_replace_and_append_are_per_field_and_replay_in_story_order():
    baseline = {"content": "A"}
    custom = {7: "原始气味", 8: "不变的颜色"}
    events = [
        _planned(4, 12, "content", "D", operation="append"),
        _planned(3, 8, "content", "C"),
        _planned(1, 5, "content", "B", operation="append"),
        _planned(2, 6, None, "潮湿", operation="append", custom_field_id=7),
        _planned(5, 10, None, "清空前", custom_field_id=7),
        _planned(6, 11, None, "", custom_field_id=7),
    ]

    before = resolve_progressions("world", baseline, custom, events, _at(4))
    added = resolve_progressions("world", baseline, custom, events, _at(7))
    replaced = resolve_progressions("world", baseline, custom, events, _at(8))
    after = resolve_progressions("world", baseline, custom, events, _at(12))

    assert before.fields["content"] == "A"
    assert added.fields["content"] == "A\nB"
    assert added.custom_fields == {7: "原始气味\n潮湿", 8: "不变的颜色"}
    assert replaced.fields["content"] == "C"
    assert after.fields["content"] == "C\nD"
    assert after.custom_fields == {7: "", 8: "不变的颜色"}


def test_same_boundary_automatic_then_author_but_later_chapter_wins():
    events = [
        _planned(1, 20, "current_state", "作者定稿", sort_order=1, protection="manual"),
        _planned(2, 20, "current_state", "分析推断", sort_order=100, protection="automatic"),
        _planned(3, 20, "current_state", "已确认", sort_order=0, protection="confirmed"),
        _planned(4, 21, "current_state", "新章分析", protection="automatic"),
    ]
    baseline = {"current_state": "起点"}

    same_chapter = resolve_progressions("character", baseline, {}, events, _at(20))
    next_chapter = resolve_progressions("character", baseline, {}, events, _at(21))
    assert same_chapter.fields["current_state"] == "作者定稿"
    assert same_chapter.applied_event_ids == (2, 3, 1)
    assert next_chapter.fields["current_state"] == "新章分析"


def test_stable_sort_order_and_id_make_same_location_deterministic():
    events = [
        _planned(3, 3, "content", "3", operation="append", sort_order=2),
        _planned(4, 3, "content", "4", operation="append", sort_order=1),
        _planned(2, 3, "content", "2", operation="append", sort_order=2),
    ]
    result = resolve_progressions("world", {"content": "1"}, {}, events, _at(3))
    assert result.fields["content"] == "1\n4\n2\n3"
    assert result.applied_event_ids == (4, 2, 3)


def test_orphaned_inactive_and_excluded_events_do_not_take_effect():
    events = [
        _planned(1, 5, "content", "有效"),
        _planned(2, 6, "content", "撤销", active=False),
        ProgressionEvent(
            id=3,
            field_key="content",
            operation="replace",
            value="已删章节",
            anchor_kind="chapter",
            chapter_id=None,
            # 保守处理不一致的联查结果，也绝不按同号复活。
            resolved_chapter_no=7,
        ),
        ProgressionEvent(
            id=4,
            field_key="content",
            operation="replace",
            value="改号后的章节",
            anchor_kind="chapter",
            chapter_id=99,
            resolved_chapter_no=10,
        ),
        ProgressionEvent(
            id=5,
            field_key="content",
            operation="replace",
            value="连表缺失的章节",
            anchor_kind="chapter",
            chapter_id=101,
            resolved_chapter_no=None,
        ),
    ]
    before = resolve_progressions("world", {"content": "起点"}, {}, events, _at(9))
    latest = resolve_progressions("world", {"content": "起点"}, {}, events, None)
    excluded = resolve_progressions("world", {"content": "起点"}, {}, events, None, exclude_event_ids={4})

    assert before.fields["content"] == "有效"
    assert latest.fields["content"] == "改号后的章节"
    assert latest.applied_event_ids == (1, 4)
    assert latest.orphaned_event_ids == (3, 5)
    assert excluded.fields["content"] == "有效"


def test_remove_and_readd_preserve_old_career_without_leaking_stale_fields():
    events = [
        _planned(1, 10, "presence", {"exists": True, "state": {"role": "main", "stage_id": 1, "note": "初阶"}}),
        _planned(2, 20, "stage_id", 2),
        _planned(3, 25, "presence", {"exists": False}, boundary="end"),
        _planned(4, 30, "presence", {"exists": True, "state": {"role": "main", "stage_id": 3, "note": "重入"}}),
    ]
    baseline = {"exists": False}

    assert resolve_progressions("career", baseline, {}, events, _at(9)).fields == {"exists": False}
    assert resolve_progressions("career", baseline, {}, events, _at(15)).fields["stage_id"] == 1
    assert resolve_progressions("career", baseline, {}, events, _at(25)).fields["stage_id"] == 2
    assert resolve_progressions("career", baseline, {}, events, _at(25, "end")).fields == {"exists": False}
    assert resolve_progressions("career", baseline, {}, events, _at(35)).fields == {
        "exists": True,
        "role": "main",
        "stage_id": 3,
        "note": "重入",
    }


def test_reverting_earlier_event_does_not_revert_a_later_absolute_value():
    events = [
        _planned(1, 5, "current_state", "重伤"),
        _planned(2, 20, "current_state", "痊愈"),
    ]
    at_ten = resolve_progressions("character", {"current_state": "健康"}, {}, events, _at(10), exclude_event_ids={1})
    at_twenty = resolve_progressions("character", {"current_state": "健康"}, {}, events, _at(20), exclude_event_ids={1})
    assert at_ten.fields["current_state"] == "健康"
    assert at_twenty.fields["current_state"] == "痊愈"


def test_snapshot_is_detached_and_keeps_unverified_legacy_status():
    baseline = {"exists": True, "identity": {"a_id": 1, "b_id": 2, "type_id": 4}, "description": "旧"}
    value = {"a_id": 2, "b_id": 1, "type_id": 4}
    event = _planned(1, 5, "identity", value)
    before, before_value = deepcopy(baseline), deepcopy(value)
    result = resolve_progressions(
        "relationship", baseline, {}, [event], _at(5), baseline_status="legacy_unverified", subject_identity=(1, 2)
    )
    assert result.baseline_status == "legacy_unverified"
    assert result.fields["identity"] == {"a_id": 2, "b_id": 1, "type_id": 4}
    result.fields["identity"]["type_id"] = 999
    assert baseline == before
    assert value == before_value


@pytest.mark.parametrize(
    ("subject", "field_key", "value", "operation"),
    [
        ("character", "name", "未来名字", "replace"),
        ("world", "life_status", "dead", "replace"),
        ("character", "life_status", "dead", "append"),
        ("character", "life_status", "zombie", "replace"),
        ("world", "content", " \n ", "append"),
        ("relationship", "intimacy", True, "replace"),
        ("relationship", "intimacy", 101, "replace"),
        ("career", "stage_id", -1, "replace"),
        ("membership", "status", "unknown", "replace"),
    ],
)
def test_invalid_field_modes_and_values_are_not_silently_applied(subject, field_key, value, operation):
    event = _planned(1, 5, field_key, value, operation=operation)
    with pytest.raises(ValueError):
        resolve_progressions(subject, {}, {}, [event], _at(5))


def test_relationship_identity_cannot_leave_the_stable_pair():
    baseline = {"exists": True, "identity": {"a_id": 1, "b_id": 2, "type_id": 4}}
    event = _planned(1, 5, "identity", {"a_id": 1, "b_id": 3, "type_id": 4})
    with pytest.raises(ValueError):
        resolve_progressions("relationship", baseline, {}, [event], _at(5), subject_identity=(1, 2))


def test_custom_field_reference_must_exist_and_be_text():
    missing = _planned(1, 5, None, "新内容", custom_field_id=99)
    invalid = _planned(2, 5, None, 5, custom_field_id=1)
    with pytest.raises(ValueError):
        resolve_progressions("world", {"content": ""}, {1: "旧"}, [missing], _at(5))
    with pytest.raises(ValueError):
        resolve_progressions("world", {"content": ""}, {1: "旧"}, [invalid], _at(5))


def test_public_create_validates_field_reference_and_anchor_as_one_contract():
    valid = {
        "expected_revision": 1,
        "field_key": "content",
        "operation": "append",
        "value": "新增",
        "anchor_kind": "planned",
        "chapter_no": 5,
    }
    assert ProgressionCreate.model_validate(valid).boundary == "start"
    with pytest.raises(ValidationError):
        ProgressionCreate.model_validate({**valid, "custom_field_id": 1})
    with pytest.raises(ValidationError):
        ProgressionCreate.model_validate({**valid, "anchor_kind": "chapter", "chapter_id": 7})
    with pytest.raises(ValidationError):
        ProgressionCreate.model_validate({**valid, "anchor_kind": "planned", "chapter_no": 0})
    with pytest.raises(ValidationError):
        ProgressionCreate.model_validate({**valid, "operation": "append", "value": " \n "})
    with pytest.raises(ValidationError):
        ProgressionCreate.model_validate({**valid, "chapter_no": True})
    with pytest.raises(ValidationError):
        ProgressionCreate.model_validate({**valid, "expected_revision": 2**31})


def test_public_patch_distinguishes_omitted_from_explicit_null():
    omitted = ProgressionUpdate.model_validate({"expected_revision": 2})
    cleared = ProgressionUpdate.model_validate({"expected_revision": 2, "custom_field_id": None})
    assert "custom_field_id" not in omitted.model_fields_set
    assert "custom_field_id" in cleared.model_fields_set


def test_baseline_validation_keeps_identity_complete_and_scoped_to_its_subject():
    relationship = {
        "exists": True,
        "identity": {"a_id": 2, "b_id": 1, "type_id": 3},
        "intimacy": 3,
        "status": "normal",
        "description": "同行者",
    }
    validate_baseline("relationship", relationship, subject_identity=(1, 2))
    validate_baseline("career", {"exists": False})
    with pytest.raises(ValueError):
        validate_baseline("relationship", relationship, subject_identity=(1, 4))
    with pytest.raises(ValueError):
        validate_baseline("career", {"exists": True, "role": "main", "note": "缺少阶段"})
    with pytest.raises(ValueError):
        validate_baseline("world", {"content": "正常", "unexpected": "不支持"})


@pytest.mark.parametrize("sort_order", [-1, 2**31, True, "1"])
def test_public_create_and_patch_reject_invalid_sort_order(sort_order):
    create = {
        "expected_revision": 1,
        "field_key": "content",
        "operation": "replace",
        "value": "变化",
        "anchor_kind": "planned",
        "chapter_no": 5,
        "sort_order": sort_order,
    }
    with pytest.raises(ValidationError):
        ProgressionCreate.model_validate(create)
    with pytest.raises(ValidationError):
        ProgressionUpdate.model_validate({"expected_revision": 1, "sort_order": sort_order})


@pytest.mark.parametrize("sort_order", [0, 2**31 - 1])
def test_public_create_and_patch_accept_int32_nonnegative_sort_order(sort_order):
    create = {
        "expected_revision": 1,
        "field_key": "content",
        "operation": "replace",
        "value": "变化",
        "anchor_kind": "planned",
        "chapter_no": 5,
        "sort_order": sort_order,
    }
    assert ProgressionCreate.model_validate(create).sort_order == sort_order
    assert ProgressionUpdate.model_validate({"expected_revision": 1, "sort_order": sort_order}).sort_order == sort_order


@pytest.mark.parametrize("bad_id", [2**31, True])
def test_career_stage_identifiers_must_be_positive_int32_at_every_boundary(bad_id):
    with pytest.raises(ValueError):
        validate_progression_value("career", "stage_id", "replace", bad_id)
    with pytest.raises(ValueError):
        validate_progression_value(
            "career", "presence", "replace", {"exists": True, "state": {"role": "main", "stage_id": bad_id, "note": ""}}
        )
    with pytest.raises(ValueError):
        validate_baseline("career", {"exists": True, "role": "main", "stage_id": bad_id, "note": ""})
    with pytest.raises(ValueError):
        validate_progression_value("world", None, "replace", "文本", custom_field_id=bad_id)


@pytest.mark.parametrize("field", ["a_id", "b_id", "type_id"])
@pytest.mark.parametrize("bad_id", [2**31, True])
def test_relationship_nested_ids_cannot_overflow_or_be_boolean(field, bad_id):
    identity = {"a_id": 1, "b_id": 2, "type_id": 3}
    identity[field] = bad_id
    # 当 a/b 超界时，故意把主体对设为相同值，避免其他校验掩盖 ID 越界。
    subject_pair = (identity["a_id"], identity["b_id"])
    with pytest.raises(ValueError):
        validate_progression_value("relationship", "identity", "replace", identity, subject_identity=subject_pair)
    with pytest.raises(ValueError):
        validate_baseline(
            "relationship",
            {"exists": True, "identity": identity, "intimacy": 0, "status": "normal", "description": ""},
            subject_identity=subject_pair,
        )


def test_planned_anchor_chapter_number_must_fit_database_integer():
    event = _planned(1, 2**31, "content", "未来")
    with pytest.raises(ValueError):
        resolve_progressions("world", {"content": "原始"}, {}, [event], None)


def test_membership_long_legacy_note_and_multiple_appends_remain_valid():
    baseline = {"exists": True, "position": "长老", "status": "active", "note": "旧" * 250}
    validate_baseline("membership", baseline)
    validate_progression_value("membership", "note", "replace", "新" * 250)
    events = [
        _planned(1, 5, "note", "甲" * 210, operation="append"),
        _planned(2, 6, "note", "乙" * 220, operation="append"),
    ]
    before = deepcopy(baseline)
    result = resolve_progressions("membership", baseline, {}, events, _at(6))
    assert result.fields["note"] == "旧" * 250 + "\n" + "甲" * 210 + "\n" + "乙" * 220
    assert baseline == before


def test_career_note_still_enforces_200_character_database_limit():
    baseline = {"exists": True, "role": "main", "stage_id": 1, "note": "甲" * 200}
    validate_baseline("career", baseline)
    validate_progression_value("career", "note", "replace", "乙" * 200)
    with pytest.raises(ValueError):
        validate_baseline("career", {**baseline, "note": "甲" * 201})
    with pytest.raises(ValueError):
        validate_progression_value("career", "note", "replace", "乙" * 201)
    with pytest.raises(ValueError):
        validate_progression_value(
            "career",
            "presence",
            "replace",
            {"exists": True, "state": {"role": "main", "stage_id": 1, "note": "甲" * 201}},
        )


@pytest.mark.parametrize("subject", ["organization", "career_entry"])
def test_entry_subjects_have_empty_baselines_and_never_create_assignments(subject):
    validate_baseline(subject, {})
    events = [_planned(1, 20, None, "公开招生", custom_field_id=7, boundary="end")]
    before = resolve_progressions(subject, {}, {7: "不招收"}, events, _at(20))
    after = resolve_progressions(subject, {}, {7: "不招收"}, events, _at(21))
    assert before.fields == after.fields == {}
    assert before.custom_fields == {7: "不招收"}
    assert after.custom_fields == {7: "公开招生"}
    with pytest.raises(ValueError):
        validate_baseline(subject, {"exists": True})
    with pytest.raises(ValueError):
        validate_progression_value(subject, "presence", "replace", {"exists": True})


@pytest.mark.parametrize("subject", ["character", "world", "organization", "career_entry"])
def test_all_four_field_types_replay_at_5_20_and_21_with_explicit_clears(subject):
    from app.services.setting_field_values import FieldSpec

    base = {1: "旧多行", 2: "旧单行", 3: {"option_id": 30}, 4: {"reference_handle_id": 40}}
    specs = {1: FieldSpec("text"), 2: FieldSpec("line"),
             3: FieldSpec("dropdown", option_ids=frozenset({30, 31})),
             4: FieldSpec("reference", reference_handle_ids=frozenset({40, 41}))}
    values = {1: "新多行", 2: "新单行", 3: {"option_id": 31}, 4: {"reference_handle_id": 41}}
    events = [_planned(field_id, 20, None, value, custom_field_id=field_id, boundary="end")
              for field_id, value in values.items()]
    for chapter in (5, 20):
        before = resolve_progressions(subject, {}, base, events, _at(chapter), field_specs=specs)
        assert before.field_values == base
        assert before.custom_fields == {1: "旧多行"}
    after = resolve_progressions(subject, {}, base, events, _at(21), field_specs=specs)
    assert after.field_values == values
    assert after.custom_fields == {1: "新多行"}
    assert resolve_progressions(subject, {}, base, events, _at(20, "end"), field_specs=specs).field_values == values
    clears = [_planned(10 + field_id, 22, None, value, custom_field_id=field_id)
              for field_id, value in {1: "", 2: "", 3: None, 4: None}.items()]
    assert resolve_progressions(subject, {}, base, events + clears, None, field_specs=specs).field_values == {
        1: "", 2: "", 3: None, 4: None,
    }


def test_typed_replay_deep_copies_baselines_and_events_and_keeps_legacy_events_untouched():
    from app.services.setting_field_values import FieldSpec

    base = {1: {"option_id": 2}, 3: "历史原文"}
    value = {"option_id": 4}
    events = [_planned(1, 20, None, value, custom_field_id=1),
              _planned(2, 20, None, "旧字符串事件", custom_field_id=3, operation="append")]
    original_base, original_value = deepcopy(base), deepcopy(value)
    result = resolve_progressions("world", {}, base, events, None, field_specs={
        1: FieldSpec("dropdown", option_ids=frozenset({2, 4})), 3: FieldSpec("text"),
    })
    assert result.custom_fields == {3: "历史原文\n旧字符串事件"}
    result.field_values[1]["option_id"] = 99
    assert base == original_base
    assert value == original_value
    assert events[1].value == "旧字符串事件"


def test_typed_replay_inherits_manual_priority_orphans_and_missing_field_checks():
    from app.services.setting_field_values import FieldSpec

    spec = {1: FieldSpec("dropdown", option_ids=frozenset({2, 3, 4}))}
    events = [_planned(1, 20, None, {"option_id": 3}, custom_field_id=1, protection="manual"),
              _planned(2, 20, None, {"option_id": 4}, custom_field_id=1, protection="automatic", sort_order=99),
              ProgressionEvent(id=3, field_key=None, custom_field_id=1, operation="replace", value=None,
                               anchor_kind="chapter", chapter_id=None),
              _planned(4, 21, None, None, custom_field_id=1, active=False)]
    result = resolve_progressions("organization", {}, {1: {"option_id": 2}}, events, None, field_specs=spec)
    assert result.field_values == {1: {"option_id": 3}}
    assert result.applied_event_ids == (2, 1)
    assert result.orphaned_event_ids == (3,)
    with pytest.raises(ValueError):
        resolve_progressions("organization", {}, {}, events, None, field_specs=spec)


def test_typed_progression_requires_prefetched_spec_and_foreign_options_are_rejected():
    from app.services.setting_field_values import FieldSpec

    event = _planned(1, 20, None, {"option_id": 99}, custom_field_id=1)
    with pytest.raises(ValueError):
        resolve_progressions("world", {}, {1: None}, [event], None, field_specs={1: FieldSpec("dropdown")})
    with pytest.raises(ValueError):
        resolve_progressions("world", {}, {1: {"option_id": 2}}, [], None)


def test_legacy_snapshot_construction_keeps_typed_values_optional_and_independent():
    first = EffectiveSnapshot({}, {1: "旧字段"}, (), (), "confirmed")
    second = EffectiveSnapshot({}, {}, (), (), "legacy_unverified")
    assert first.field_values == second.field_values == {}
    first.field_values[1] = "新字段"
    assert second.field_values == {}
    assert first.custom_fields == {1: "旧字段"}


