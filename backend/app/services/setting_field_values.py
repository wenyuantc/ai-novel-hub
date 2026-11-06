"""字段值的纯 codec：定义决定类型，归属与当前可选状态留给事务服务。"""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, cast

from app.schemas.setting_field_value import EntityKind, EntityRef, FieldType, LineFieldValue

CanonicalFieldValue = str | dict[str, int] | None
PublicFieldValue = str | dict[str, int | str] | None


def has_field_content(value: object, *, has_events: bool = False) -> bool:
    """空绑定无资料；任何保留事件（包括空值、停用和孤儿）仍属于历史。"""
    return has_events or (value is not None and value != "")


def is_positive_db_int(value: object) -> bool:
    return type(value) is int and 1 <= value <= 2_147_483_647


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """一次读取预取的类型和历史合法身份；集合包括停用但仍存在的旧选项。"""

    field_type: FieldType
    definition_id: int | None = None
    option_ids: frozenset[int] = frozenset()
    reference_handle_ids: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        if self.field_type not in ("text", "line", "dropdown", "reference"):
            raise ValueError("不支持的字段类型")
        if self.definition_id is not None and not is_positive_db_int(self.definition_id):
            raise ValueError("字段定义身份不合法")
        for name in ("option_ids", "reference_handle_ids"):
            values = frozenset(getattr(self, name))
            if any(not is_positive_db_int(value) for value in values):
                raise ValueError("字段预取身份不合法")
            object.__setattr__(self, name, values)


def validate_field_value(
    spec: FieldSpec, value: object, *, operation: Literal["append", "replace"] = "replace",
) -> CanonicalFieldValue:
    """校验 canonical 值并返回独立副本；绝不将对象转换成文本。"""
    if operation not in ("append", "replace"):
        raise ValueError("不支持的变化模式")
    if operation == "append" and spec.field_type != "text":
        raise ValueError("只有多行文本字段支持追加")
    if spec.field_type == "text":
        if not isinstance(value, str):
            raise ValueError("字段值必须是文本")
        if operation == "append" and not value.strip():
            raise ValueError("追加内容不能为空")
        return value
    if spec.field_type == "line":
        return LineFieldValue(value=value).value
    if value is None:
        return None
    if spec.field_type == "dropdown":
        key, identities = "option_id", spec.option_ids
    elif spec.field_type == "reference":
        key, identities = "reference_handle_id", spec.reference_handle_ids
    else:
        raise ValueError("不支持的字段类型")
    if not isinstance(value, dict) or set(value) != {key} or not is_positive_db_int(value[key]):
        raise ValueError("字段选值格式或身份不合法")
    if value[key] not in identities:
        raise ValueError("字段选值不存在或不属于此字段")
    return deepcopy(cast(dict[str, int], value))


def encode_public_value(
    spec: FieldSpec,
    value: object,
    *,
    reference_handles: Mapping[tuple[EntityKind, int], int] | None = None,
    operation: Literal["append", "replace"] = "replace",
) -> CanonicalFieldValue:
    """公共 EntityRef 只在写值时编码一次；不能接收已编码的 handle 对象。"""
    if spec.field_type != "reference" or value is None:
        return validate_field_value(spec, value, operation=operation)
    target = EntityRef.model_validate(value)
    handle_id = (reference_handles or {}).get((target.kind, target.id))
    if handle_id is None:
        raise ValueError("引用目标没有此作品的稳定身份")
    return validate_field_value(spec, {"reference_handle_id": handle_id}, operation=operation)


def decode_public_value(
    spec: FieldSpec,
    value: object,
    *,
    reference_targets: Mapping[int, EntityRef | Mapping[str, object]] | None = None,
) -> PublicFieldValue:
    """读取／事件回填共用的公共投影；缺目标必须报错，不能静默清空。"""
    canonical = validate_field_value(spec, value)
    if spec.field_type != "reference" or canonical is None:
        return cast(PublicFieldValue, canonical)
    handle_id = cast(dict[str, int], canonical)["reference_handle_id"]
    target = (reference_targets or {}).get(handle_id)
    if target is None:
        raise ValueError("引用身份缺少目标，无法读取字段")
    return EntityRef.model_validate(target).model_dump()
