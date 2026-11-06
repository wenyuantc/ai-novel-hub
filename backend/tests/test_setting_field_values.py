"""类型化字段的单一 canonical 合同与公共引用边界。"""

from copy import deepcopy

import pytest
from pydantic import TypeAdapter, ValidationError


def _codec():
    from app.services import setting_field_values

    return setting_field_values


@pytest.mark.parametrize(
    ("field_type", "value"),
    [("text", "原文\r\n不裁剪😀"), ("text", ""), ("text", "旧" * 100_000), ("line", "😀" * 500),
     ("line", ""), ("dropdown", {"option_id": 3}), ("dropdown", None),
     ("reference", {"reference_handle_id": 7}), ("reference", None)],
)
def test_valid_canonical_values_roundtrip_without_stringification(field_type, value):
    codec = _codec()
    spec = codec.FieldSpec(field_type, option_ids=frozenset({3}), reference_handle_ids=frozenset({7}))
    original = deepcopy(value)
    result = codec.validate_field_value(spec, value)
    assert result == original
    if isinstance(result, dict):
        result[next(iter(result))] = 99
        assert value == original


@pytest.mark.parametrize(
    "separator", ["\r", "\n", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
def test_single_line_rejects_every_line_separator(separator):
    codec = _codec()
    with pytest.raises(ValueError):
        codec.validate_field_value(codec.FieldSpec("line"), f"前{separator}后")


@pytest.mark.parametrize(
    ("field_type", "value"),
    [("line", "😀" * 501), ("text", None), ("line", None), ("text", {"text": "不能字符串化"}),
     ("dropdown", 3), ("dropdown", "3"), ("dropdown", {"option_id": 4}),
     ("dropdown", {"option_id": 3, "label": "不能靠名称"}),
     ("reference", {"kind": "character", "id": 7}), ("reference", {"reference_handle_id": 8}),
     ("reference", {"reference_handle_id": 7, "id": 7})],
)
def test_invalid_shapes_and_foreign_field_identities_are_rejected(field_type, value):
    codec = _codec()
    spec = codec.FieldSpec(field_type, option_ids=frozenset({3}), reference_handle_ids=frozenset({7}))
    with pytest.raises(ValueError):
        codec.validate_field_value(spec, value)


@pytest.mark.parametrize("field_type,key", [("dropdown", "option_id"), ("reference", "reference_handle_id")])
@pytest.mark.parametrize("bad_id", [False, True, "3", 3.0, 0, -1, 2**31])
def test_nested_identities_are_strict_positive_int32(field_type, key, bad_id):
    codec = _codec()
    spec = codec.FieldSpec(field_type, option_ids=frozenset({3}), reference_handle_ids=frozenset({3}))
    with pytest.raises(ValueError):
        codec.validate_field_value(spec, {key: bad_id})


@pytest.mark.parametrize("field_type,value", [("line", "一行"), ("dropdown", None), ("reference", None)])
def test_only_multiline_text_can_append(field_type, value):
    codec = _codec()
    with pytest.raises(ValueError):
        codec.validate_field_value(codec.FieldSpec(field_type), value, operation="append")
    assert codec.validate_field_value(codec.FieldSpec("text"), " 非空 ", operation="append") == " 非空 "
    with pytest.raises(ValueError):
        codec.validate_field_value(codec.FieldSpec("text"), " \n ", operation="append")


def test_reference_codec_keeps_entity_refs_separate_from_internal_handles():
    codec = _codec()
    from app.schemas.setting_field_value import EntityRef

    spec = codec.FieldSpec("reference", reference_handle_ids=frozenset({7}))
    target = EntityRef(kind="organization", id=11)
    handles = {("organization", 11): 7}
    canonical = codec.encode_public_value(spec, target, reference_handles=handles)
    assert canonical == {"reference_handle_id": 7}
    public = codec.decode_public_value(spec, canonical, reference_targets={7: target})
    assert public == {"kind": "organization", "id": 11}
    assert codec.encode_public_value(spec, public, reference_handles=handles) == canonical
    assert codec.encode_public_value(spec, None, reference_handles={}) is None
    assert codec.decode_public_value(spec, None, reference_targets={}) is None
    with pytest.raises(ValueError):
        codec.encode_public_value(spec, canonical, reference_handles=handles)
    with pytest.raises(ValueError):
        codec.encode_public_value(spec, {"kind": "organization", "id": 11}, reference_handles={})
    with pytest.raises(ValueError):
        codec.decode_public_value(spec, canonical, reference_targets={})


@pytest.mark.parametrize("bad_id", [True, "1", 0, 2**31])
def test_public_entity_refs_reject_coercion_and_extra_keys(bad_id):
    from app.schemas.setting_field_value import EntityRef

    with pytest.raises(ValidationError):
        EntityRef(kind="character", id=bad_id)
    with pytest.raises(ValidationError):
        EntityRef(kind="character", id=1, reference_handle_id=2)


def test_public_value_union_is_discriminated_and_never_accepts_internal_handles():
    from app.schemas.setting_field_value import TypedFieldValue

    adapter = TypeAdapter(TypedFieldValue)
    selected = adapter.validate_python({"field_type": "dropdown", "value": {"option_id": 3}})
    assert selected.model_dump() == {"field_type": "dropdown", "value": {"option_id": 3}}
    assert adapter.validate_python({"field_type": "reference", "value": None}).value is None
    with pytest.raises(ValidationError):
        adapter.validate_python({"field_type": "reference", "value": {"reference_handle_id": 7}})
    with pytest.raises(ValidationError):
        adapter.validate_python({"field_type": "line", "value": {"option_id": 3}})
    with pytest.raises(ValidationError):
        adapter.validate_python({"field_type": "text", "value": None})
