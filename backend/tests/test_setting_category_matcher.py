"""分类规范化与纯规则语义，不依赖数据库或名称。"""

import pytest
from pydantic import ValidationError

from app.schemas.setting_categories import CategorySpec, SettingTagsValue, normalize_setting_tags
from app.services.setting_category_matcher import match_category


def _spec(mode="all", **changes):
    return CategorySpec.model_validate(
        {
            "name": "同行",
            "applicable_types": ["character", "organization"],
            "match_mode": mode,
            "rules": [{"kind": "tag", "tag": "同盟"}, {"kind": "dropdown", "definition_id": 7, "option_id": 11}],
            **changes,
        }
    )


def test_tags_are_strict_normalized_preserve_order_and_case():
    original = ["  同盟 ", "", "同盟", "A", "a", "\t\n", "😀" * 50]
    expected = ["同盟", "A", "a", "😀" * 50]
    assert normalize_setting_tags(original) == expected
    assert SettingTagsValue(setting_tags=original).setting_tags == expected
    assert original[0] == "  同盟 "
    assert normalize_setting_tags(["相同"] * 51) == ["相同"]
    assert normalize_setting_tags([]) == []


@pytest.mark.parametrize(
    "raw", [None, "同盟", ("同盟",), [1], [True], [None], ["长" * 51], [str(i) for i in range(51)]]
)
def test_invalid_tags_are_rejected(raw):
    with pytest.raises(ValueError):
        normalize_setting_tags(raw)
    with pytest.raises(ValidationError):
        SettingTagsValue(setting_tags=raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "  "},
        {"match_mode": "none"},
        {"applicable_types": []},
        {"applicable_types": ["world"]},
        {"applicable_types": ["character", "character"]},
        {"rules": []},
        {"rules": [{"kind": "tag", "tag": " "}]},
        {"rules": [{"kind": "tag", "tag": 1}]},
        {"rules": [{"kind": "tag", "tag": "同盟", "option_id": 1}]},
        {"rules": [{"kind": "dropdown", "definition_id": True, "option_id": 1}]},
        {"rules": [{"kind": "dropdown", "definition_id": "1", "option_id": 1}]},
        {"rules": [{"kind": "dropdown", "definition_id": 1, "option_id": 2_147_483_648}]},
        {"rules": [{"kind": "tag", "tag": "同盟"}] * 21},
    ],
)
def test_category_schema_rejects_invalid_rule_shapes(changes):
    with pytest.raises(ValidationError):
        _spec(**changes)


@pytest.mark.parametrize(
    "mode,tags,values,expected,indexes",
    [
        ("all", ["同盟"], {7: 11}, True, (0, 1)),
        ("all", [], {7: 11}, False, (1,)),
        ("any", [], {7: 11}, True, (1,)),
        ("any", ["同盟"], {}, True, (0,)),
        ("all", ["同盟"], {7: None}, False, (0,)),
        ("any", [], {8: 11}, False, ()),
        ("any", [], {7: 12}, False, ()),
        ("any", ["同盟 "], {}, False, ()),
    ],
)
def test_any_all_compare_stable_option_identity_and_missing_is_false(mode, tags, values, expected, indexes):
    spec = _spec(mode)
    actual = match_category(spec, "character", tags, values)
    assert actual.matched is expected
    assert actual.matched_rule_indexes == indexes
    assert not match_category(spec, "career", tags, values).matched
