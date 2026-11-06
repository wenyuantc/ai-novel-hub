"""类型化字段的纯文本展示；调用方负责提供同一时点的公共投影。"""

from typing import assert_never, cast

from app.schemas.setting_fields import EntryFieldsOut, FieldSlot

REFERENCE_TYPE_LABELS = {
    "character": "角色",
    "world.place": "世界观·地点",
    "world.faction": "世界观·势力",
    "world.item": "世界观·物品",
    "world.rule": "世界观·规则",
    "world.history": "世界观·历史",
    "world.other": "世界观·其他",
    "organization": "组织",
    "career": "职业",
}


def _display_value(field: FieldSlot) -> str:
    if field.value is None:
        return ""
    match field.field_type:
        case "text" | "line":
            return cast(str, field.value).strip()
        case "dropdown":
            option_id = cast(dict[str, int], field.value)["option_id"]
            return next(option.label for option in field.options if option.id == option_id)
        case "reference":
            if field.reference is None:
                raise ValueError("引用字段缺少目标名称投影")
            return f"{field.reference.name}（{REFERENCE_TYPE_LABELS[field.reference.type]}）"
        case _:
            assert_never(field.field_type)


def format_entry_fields(result: EntryFieldsOut) -> str:
    """仅输出非空有效值；停用旧值仍展示，引用不展开目标正文或内部身份。"""
    lines = []
    for field in [*result.fields, *result.legacy_fields]:
        value = _display_value(field)
        if value:
            lines.append(f"{field.label}：{value}")
    return "\n".join(lines)
