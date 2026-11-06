"""恢复前的纯分类图校验与查询共同的规则/元数据契约。"""

from copy import deepcopy

import pytest

from app.core.errors import AppError
from app.schemas.setting_categories import CategorySpec


def _graph():
    return {
        "categories": [
            {
                "id": 11,
                "project_id": 7,
                "name": "同行",
                "applicable_types": ["character"],
                "match_mode": "all",
                "created_at": "额外标量列不参与判断",
            }
        ],
        "rules": [{"id": 21, "category_id": 11, "kind": "dropdown", "tag": None, "definition_id": 31, "option_id": 41}],
        "definitions": [
            {
                "id": 31,
                "project_id": 7,
                "field_type": "dropdown",
                "applicable_types": ["character"],
                "label": "显示名不参与身份判断",
            }
        ],
        "options": [{"id": 41, "definition_id": 31, "label": "当前显示名"}],
    }


def _validate(graph):
    from app.services.setting_category_validation import validate_category_graph_snapshot

    validate_category_graph_snapshot(7, **graph)


def test_snapshot_validation_is_synchronous_immutable_and_accepts_full_rows_and_empty_graph():
    graph = _graph()
    graph["definitions"][0]["archived"] = True
    graph["options"][0]["archived"] = True
    before = deepcopy(graph)
    assert _validate(graph) is None
    assert graph == before
    assert _validate({key: [] for key in graph}) is None


@pytest.mark.parametrize(
    "damage",
    [
        "definition_type",
        "definition_scope",
        "foreign_definition",
        "missing_definition",
        "missing_option",
        "wrong_option_group",
        "orphan_option",
        "foreign_category",
        "orphan_rule",
        "empty_rules",
        "too_many_rules",
        "bad_match_mode",
        "unknown_applicable_type",
        "tag_mixed_payload",
        "dropdown_mixed_payload",
        "tag_whitespace",
        "tag_empty",
        "tag_too_long",
        "unknown_rule_kind",
        "bool_id",
        "string_id",
        "duplicate_category",
        "duplicate_rule",
        "duplicate_definition",
        "duplicate_option",
        "missing_category",
        "bool_project_id",
    ],
)
def test_proposed_restore_graph_rejects_unsafe_rows_before_any_database_write(damage):
    graph = _graph()
    if damage == "definition_type":
        graph["definitions"][0]["field_type"] = "line"
    elif damage == "definition_scope":
        graph["definitions"][0]["applicable_types"] = ["organization"]
    elif damage == "foreign_definition":
        graph["definitions"][0]["project_id"] = 8
    elif damage == "missing_definition":
        graph["definitions"] = []
    elif damage == "missing_option":
        graph["options"] = []
    elif damage == "wrong_option_group":
        graph["definitions"].append({**graph["definitions"][0], "id": 32})
        graph["options"][0]["definition_id"] = 32
    elif damage == "orphan_option":
        graph["options"].append({"id": 42, "definition_id": 999})
    elif damage == "foreign_category":
        graph["categories"][0]["project_id"] = 8
    elif damage == "orphan_rule":
        graph["rules"][0]["category_id"] = 999
    elif damage == "missing_category":
        graph["categories"] = []
    elif damage == "empty_rules":
        graph["rules"] = []
    elif damage == "too_many_rules":
        graph["rules"] = [{**graph["rules"][0], "id": index + 100} for index in range(21)]
    elif damage == "bad_match_mode":
        graph["categories"][0]["match_mode"] = "invalid"
    elif damage == "unknown_applicable_type":
        graph["categories"][0]["applicable_types"] = ["unknown"]
    elif damage == "tag_mixed_payload":
        graph["rules"][0].update(kind="tag", tag="同盟")
    elif damage == "dropdown_mixed_payload":
        graph["rules"][0]["tag"] = "不应存在"
    elif damage.startswith("tag_"):
        graph["rules"] = [
            {
                "id": 21,
                "category_id": 11,
                "kind": "tag",
                "tag": {
                    "tag_whitespace": " 同盟 ",
                    "tag_empty": "",
                    "tag_too_long": "长" * 51,
                }[damage],
            }
        ]
    elif damage == "unknown_rule_kind":
        graph["rules"][0]["kind"] = "script"
    elif damage == "bool_id":
        graph["definitions"][0]["id"] = True
    elif damage == "string_id":
        graph["options"][0]["id"] = "41"
    elif damage == "bool_project_id":
        graph["categories"][0]["project_id"] = True
    else:
        table = {
            "duplicate_category": "categories",
            "duplicate_rule": "rules",
            "duplicate_definition": "definitions",
            "duplicate_option": "options",
        }[damage]
        graph[table].append(deepcopy(graph[table][0]))
    before = deepcopy(graph)
    with pytest.raises(AppError):
        _validate(graph)
    assert graph == before


def test_tag_rules_are_canonical_and_unused_non_dropdown_definitions_are_legal():
    graph = _graph()
    graph["rules"] = [
        {"id": 21, "category_id": 11, "kind": "tag", "tag": "同盟", "definition_id": None, "option_id": None}
    ]
    graph["definitions"][0]["field_type"] = "line"
    assert _validate(graph) is None


@pytest.mark.parametrize(
    ("change", "status", "code"),
    [
        ("missing", 404, None),
        ("type", 400, "invalid_category_rule"),
        ("option", 400, "invalid_category_rule"),
        ("scope", 400, "incompatible_category_rule"),
        ("archived", 409, "category_rule_archived"),
    ],
)
def test_shared_metadata_core_preserves_existing_query_error_contract(change, status, code):
    from app.services.setting_category_validation import validate_category_spec_metadata

    graph = _graph()
    spec = CategorySpec(
        name="同行",
        applicable_types=["character"],
        match_mode="all",
        rules=[
            {
                "kind": "dropdown",
                "definition_id": 31,
                "option_id": 41,
            }
        ],
    )
    if change == "missing":
        graph["definitions"] = []
    elif change == "type":
        graph["definitions"][0]["field_type"] = "line"
    elif change == "option":
        graph["options"] = []
    elif change == "scope":
        graph["definitions"][0]["applicable_types"] = ["organization"]
    else:
        graph["options"][0]["archived"] = True
    with pytest.raises(AppError) as caught:
        validate_category_spec_metadata(7, spec, graph["definitions"], graph["options"])
    assert caught.value.status_code == status
    assert (caught.value.extra or {}).get("code") == code
    assert caught.value.extra["rule_index"] == 0


def test_metadata_core_retains_only_the_server_supplied_old_pair():
    from app.services.setting_category_validation import validate_category_spec_metadata

    graph = _graph()
    graph["definitions"][0]["archived"] = True
    graph["options"][0]["archived"] = True
    spec = CategorySpec(
        name="同行",
        applicable_types=["character"],
        match_mode="all",
        rules=[
            {
                "kind": "dropdown",
                "definition_id": 31,
                "option_id": 41,
            }
        ],
    )
    validate_category_spec_metadata(7, spec, graph["definitions"], graph["options"], retained_rules=spec.rules)
    changed = spec.model_copy(deep=True)
    graph["options"].append({"id": 42, "definition_id": 31, "archived": True})
    changed.rules[0].option_id = 42
    with pytest.raises(AppError, match="停用"):
        validate_category_spec_metadata(7, changed, graph["definitions"], graph["options"], retained_rules=spec.rules)
