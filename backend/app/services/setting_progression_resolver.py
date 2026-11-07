"""设定时间线的纯求值：不访问数据库，也不修改调用方传入的数据。"""

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal, cast

from pydantic import JsonValue

from app.schemas.progression import (
    AnchorKind,
    BaselineStatus,
    Boundary,
    ProgressionOperation,
    ProgressionProtection,
    StoryPosition,
    SubjectKind,
)
from app.services.setting_field_values import CanonicalFieldValue, FieldSpec, validate_field_value
from app.services.setting_field_values import is_positive_db_int as _positive_integer


@dataclass(frozen=True, slots=True)
class ProgressionEvent:
    """一条已解码事件；绑定章节的当前章号由读取方通过 JOIN 提供。"""

    id: int
    field_key: str | None
    operation: ProgressionOperation
    value: JsonValue
    anchor_kind: AnchorKind
    chapter_id: int | None = None
    chapter_no: int | None = None
    scene_id: int | None = None
    resolved_chapter_no: int | None = None
    resolved_chapter_id: int | None = None
    resolved_scene_order: int | None = None
    boundary: Boundary = "start"
    sort_order: int = 0
    protection: ProgressionProtection = "manual"
    active: bool = True
    source_change_id: int | None = None
    custom_field_id: int | None = None


@dataclass(frozen=True, slots=True)
class EffectiveSnapshot:
    """一次求值的独立快照，保留被采用事件及无法定位的孤儿事件。"""

    fields: dict[str, JsonValue]
    custom_fields: dict[int, str]
    applied_event_ids: tuple[int, ...]
    orphaned_event_ids: tuple[int, ...]
    baseline_status: BaselineStatus
    field_values: dict[int, CanonicalFieldValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _FieldRule:
    kind: Literal["text", "life_status", "relationship_status", "member_status", "intimacy", "stage_id", "identity"]
    append: bool = False
    max_length: int | None = None


# 一个主体字段只在这里声明一次；外层还须校验被引用记录的项目归属。
_RULES: dict[SubjectKind, dict[str, _FieldRule]] = {
    "character": {
        "profile": _FieldRule("text", append=True),
        "appearance": _FieldRule("text", append=True),
        "personality": _FieldRule("text", append=True),
        "background": _FieldRule("text", append=True),
        "current_state": _FieldRule("text", append=True),
        "age": _FieldRule("text", max_length=50),
        "gender": _FieldRule("text", max_length=20),
        "life_status": _FieldRule("life_status"),
    },
    "world": {"content": _FieldRule("text", append=True)},
    "relationship": {
        "identity": _FieldRule("identity"),
        "intimacy": _FieldRule("intimacy"),
        "status": _FieldRule("relationship_status"),
        "description": _FieldRule("text", append=True),
    },
    "career": {"stage_id": _FieldRule("stage_id"), "note": _FieldRule("text", max_length=200)},
    "membership": {
        "position": _FieldRule("text", max_length=50),
        "status": _FieldRule("member_status"),
        "note": _FieldRule("text", append=True),
    },
    "organization": {},
    "career_entry": {},
}

_LIFECYCLE_FIELDS: dict[str, frozenset[str]] = {
    "relationship": frozenset({"identity", "intimacy", "status", "description"}),
    "career": frozenset({"role", "stage_id", "note"}),
    "membership": frozenset({"position", "status", "note"}),
}


def _validate_identity(value: object, subject_identity: tuple[int, int] | None) -> None:
    if not isinstance(value, dict) or set(value) != {"a_id", "b_id", "type_id"}:
        raise ValueError("关系身份必须包含 a_id、b_id 和 type_id")
    a_id, b_id, type_id = value["a_id"], value["b_id"], value["type_id"]
    if not all(_positive_integer(item) for item in (a_id, b_id, type_id)) or a_id == b_id:
        raise ValueError("关系身份包含无效引用")
    if (
        subject_identity is None
        or len(subject_identity) != 2
        or not all(_positive_integer(item) for item in subject_identity)
        or subject_identity[0] == subject_identity[1]
        or {a_id, b_id} != set(subject_identity)
    ):
        raise ValueError("关系身份必须使用时间线主体的两个人物")


def _validate_rule(
    rule: _FieldRule, value: object, operation: ProgressionOperation, subject_identity: tuple[int, int] | None
) -> None:
    if operation not in ("append", "replace"):
        raise ValueError("不支持的变化模式")
    if operation == "append" and not rule.append:
        raise ValueError("该字段不支持追加")

    if rule.kind == "text":
        if not isinstance(value, str):
            raise ValueError("字段值必须是文本")
        if rule.max_length is not None and len(value) > rule.max_length:
            raise ValueError(f"字段内容不能超过 {rule.max_length} 字")
        if operation == "append" and not value.strip():
            raise ValueError("追加内容不能为空")
    elif rule.kind == "life_status":
        if value not in ("alive", "dead", "missing", "other"):
            raise ValueError("存活状态不合法")
    elif rule.kind == "relationship_status":
        if value not in ("normal", "broken", "past", "complicated"):
            raise ValueError("关系状态不合法")
    elif rule.kind == "member_status":
        if value not in ("active", "left", "expelled", "dead"):
            raise ValueError("成员状态不合法")
    elif rule.kind == "intimacy":
        if type(value) is not int or not -100 <= value <= 100:
            raise ValueError("亲密度必须是 -100 至 100 的整数")
    elif rule.kind == "stage_id":
        if not _positive_integer(value):
            raise ValueError("职业阶段引用不合法")
    elif rule.kind == "identity":
        _validate_identity(value, subject_identity)
    else:  # 防止新增规则类型时静默漏掉校验分支。
        raise ValueError("不支持的字段值类型")


def _validate_present_state(
    subject_kind: SubjectKind, value: object, subject_identity: tuple[int, int] | None
) -> None:
    required = _LIFECYCLE_FIELDS.get(subject_kind)
    if required is None or not isinstance(value, dict) or set(value) != required:
        raise ValueError("创建关系、职业或成员时必须提供完整状态")
    for key, field_value in value.items():
        if subject_kind == "career" and key == "role":
            if field_value not in ("main", "sub"):
                raise ValueError("职业类别不合法")
            continue
        _validate_rule(_RULES[subject_kind][key], field_value, "replace", subject_identity)


def validate_progression_value(
    subject_kind: SubjectKind,
    field_key: str | None,
    operation: ProgressionOperation,
    value: JsonValue,
    *,
    custom_field_id: int | None = None,
    subject_identity: tuple[int, int] | None = None,
    field_spec: FieldSpec | None = None,
) -> None:
    """校验单次事件的字段、模式和值；不替代主体及外键的归属检查。

    涉及关系身份或关系建立时调用方必须传入时间线的稳定人物对。
    自定义字段的存在性及其属于此时间线由调用方校验。
    """
    if subject_kind not in _RULES:
        raise ValueError("不支持的设定主体")
    if (field_key is None) == (custom_field_id is None):
        raise ValueError("内置字段与自定义字段必须且只能指定一个")
    if custom_field_id is not None:
        if subject_kind not in ("character", "world", "organization", "career_entry") or not _positive_integer(
            custom_field_id
        ):
            raise ValueError("该主体不支持指定的自定义字段")
        validate_field_value(field_spec or FieldSpec("text"), value, operation=operation)
        return

    if field_key == "presence":
        if operation != "replace" or subject_kind not in _LIFECYCLE_FIELDS:
            raise ValueError("该主体不支持此存在性变化")
        if not isinstance(value, dict) or type(value.get("exists")) is not bool:
            raise ValueError("存在性变化必须指定布尔 exists")
        if value["exists"]:
            if set(value) != {"exists", "state"}:
                raise ValueError("建立时必须且只能提供完整状态")
            _validate_present_state(subject_kind, value["state"], subject_identity)
        elif set(value) != {"exists"}:
            raise ValueError("解除时不能附带状态")
        return

    rule = _RULES[subject_kind].get(cast(str, field_key))
    if rule is None:
        raise ValueError("该设定字段不支持剧情变化")
    _validate_rule(rule, value, operation, subject_identity)


def validate_baseline(
    subject_kind: SubjectKind,
    baseline: Mapping[str, JsonValue],
    *,
    subject_identity: tuple[int, int] | None = None,
    require_complete: bool = True,
) -> None:
    """校验持久化基础快照；关系身份必须对应调用方提供的稳定人物对。"""
    if subject_kind not in _RULES or not isinstance(baseline, Mapping):
        raise ValueError("基础设定格式不正确")
    if subject_kind in _LIFECYCLE_FIELDS:
        exists = baseline.get("exists")
        if type(exists) is not bool:
            raise ValueError("关系、职业或成员基础设定必须指定布尔 exists")
        expected = _LIFECYCLE_FIELDS[subject_kind] | {"exists"} if exists else frozenset({"exists"})
    else:
        expected = frozenset(_RULES[subject_kind])

    keys = frozenset(baseline)
    if not keys <= expected or (require_complete and keys != expected):
        raise ValueError("基础设定字段与主体类型不匹配")
    for field_key, value in baseline.items():
        if field_key == "exists":
            continue
        if subject_kind == "career" and field_key == "role":
            if value not in ("main", "sub"):
                raise ValueError("职业类别不合法")
            continue
        validate_progression_value(subject_kind, field_key, "replace", value, subject_identity=subject_identity)


def _event_chapter_no(event: ProgressionEvent) -> int | None:
    if event.anchor_kind == "chapter":
        if event.chapter_id is None or event.resolved_chapter_no is None:
            return None
        if event.chapter_no is not None or event.scene_id is not None or not _positive_integer(event.chapter_id):
            raise ValueError("关联章节锚点格式不正确")
        chapter_no = event.resolved_chapter_no
    elif event.anchor_kind == "planned":
        if event.chapter_id is not None or event.scene_id is not None:
            raise ValueError("计划章节不能携带章节 id")
        chapter_no = event.chapter_no
    elif event.anchor_kind == "scene":
        if event.chapter_id is not None or event.chapter_no is not None:
            raise ValueError("场景锚点不能携带章节 id 或计划章号")
        if event.scene_id is None or event.resolved_chapter_no is None:
            return None
        if (not _positive_integer(event.scene_id) or not _positive_integer(event.resolved_chapter_id)
                or type(event.resolved_scene_order) is not int
                or not 0 <= event.resolved_scene_order <= 2_147_483_647):
            raise ValueError("场景锚点缺少真实章节或顺序")
        chapter_no = event.resolved_chapter_no
    else:
        raise ValueError("不支持的章节锚点")
    if not _positive_integer(chapter_no):
        raise ValueError("故事章节号必须是正整数")
    return cast(int, chapter_no)


def story_position_key(position: StoryPosition) -> tuple[int, int, int, int, int]:
    """保留同章号旧边界，在其间插入按实际章身份与当前场景顺序定位的窗口。"""
    if position.scene_id is not None:
        return position.chapter_no, 1, position.chapter_id, position.scene_order, int(position.boundary == "end")
    return position.chapter_no, 0 if position.boundary == "start" else 2, 0, 0, 0


def event_position(event: ProgressionEvent) -> StoryPosition | None:
    chapter_no = _event_chapter_no(event)
    if chapter_no is None:
        return None
    return StoryPosition(
        chapter_id=event.resolved_chapter_id if event.anchor_kind == "scene" else event.chapter_id,
        chapter_no=chapter_no, boundary=event.boundary,
        scene_id=event.scene_id if event.anchor_kind == "scene" else None,
        scene_order=event.resolved_scene_order if event.anchor_kind == "scene" else None,
    )


def _sort_key(event: ProgressionEvent, chapter_no: int) -> tuple[int, ...]:
    boundary_order = {"start": 0, "end": 1}
    protection_order = {"automatic": 0, "confirmed": 1, "manual": 1}
    if event.boundary not in boundary_order or event.protection not in protection_order:
        raise ValueError("变化的章节边界或人工保护状态不合法")
    position = event_position(event)
    assert position is not None and position.chapter_no == chapter_no
    return (*story_position_key(position), protection_order[event.protection], event.sort_order, event.id)


def _append_text(previous: str, value: str) -> str:
    return f"{previous}\n{value}" if previous else value


def resolve_progressions(
    subject_kind: SubjectKind,
    baseline: Mapping[str, JsonValue],
    custom_fields: Mapping[int, CanonicalFieldValue],
    events: Iterable[ProgressionEvent],
    position: StoryPosition | None,
    *,
    baseline_status: BaselineStatus = "confirmed",
    exclude_event_ids: Iterable[int] = (),
    subject_identity: tuple[int, int] | None = None,
    field_specs: Mapping[int, FieldSpec] | None = None,
) -> EffectiveSnapshot:
    """求给定位置的有效设定；position=None 表示全部事件之后的最新投影。

    绑定章节已删除或查不到时该事件列入孤儿提示并忽略；planned 总按其
    数字章号排序。调用方负责将 chapter_id JOIN 得到 resolved_chapter_no。
    exclude_event_ids 可用于预测撤销或分析替代，绝不改变原事件对象。
    """
    if subject_kind not in _RULES or baseline_status not in ("confirmed", "legacy_unverified"):
        raise ValueError("设定主体或基础状态不合法")
    if not isinstance(baseline, Mapping) or not isinstance(custom_fields, Mapping):
        raise ValueError("基础设定格式不正确")
    if any(not _positive_integer(key) for key in custom_fields):
        raise ValueError("自定义字段基础值不合法")
    if subject_kind in ("organization", "career_entry"):
        validate_baseline(subject_kind, baseline)

    fields = deepcopy(dict(baseline))
    # 未提供元数据的旧调用保持 text 语义；对象绝不能被当作文本隐式转换。
    specs = {key: (field_specs or {}).get(key, FieldSpec("text")) for key in custom_fields}
    values = {key: validate_field_value(specs[key], value) for key, value in custom_fields.items()}
    excluded = set(exclude_event_ids)
    ordered: list[tuple[tuple[int, ...], ProgressionEvent]] = []
    orphaned: list[int] = []
    target = story_position_key(position) if position else None

    for event in events:
        if not event.active or event.id in excluded:
            continue
        chapter_no = _event_chapter_no(event)
        if chapter_no is None:
            orphaned.append(event.id)
            continue
        key = _sort_key(event, chapter_no)
        if target is None or key[:5] <= target:
            ordered.append((key, event))

    applied: list[int] = []
    for _, event in sorted(ordered, key=lambda item: item[0]):
        validate_progression_value(
            subject_kind,
            event.field_key,
            event.operation,
            event.value,
            custom_field_id=event.custom_field_id,
            subject_identity=subject_identity,
            field_spec=specs.get(event.custom_field_id) if event.custom_field_id is not None else None,
        )
        if event.custom_field_id is not None:
            if event.custom_field_id not in values:
                raise ValueError("自定义字段不存在或不属于此设定")
            if event.operation == "append":
                values[event.custom_field_id] = _append_text(
                    cast(str, values[event.custom_field_id]), cast(str, event.value)
                )
            else:
                values[event.custom_field_id] = deepcopy(cast(CanonicalFieldValue, event.value))
        elif event.field_key == "presence":
            change = cast(dict[str, JsonValue], event.value)
            fields.clear()
            if change["exists"]:
                fields.update(deepcopy(cast(dict[str, JsonValue], change["state"])))
                fields["exists"] = True
            else:
                fields["exists"] = False
        else:
            if subject_kind in _LIFECYCLE_FIELDS and fields.get("exists") is not True:
                raise ValueError("主体不存在时不能修改其设定字段")
            field_key = cast(str, event.field_key)
            if event.operation == "append":
                previous = fields.get(field_key, "")
                if not isinstance(previous, str):
                    raise ValueError("只能向文本基础值追加内容")
                fields[field_key] = _append_text(previous, cast(str, event.value))
            else:
                fields[field_key] = deepcopy(event.value)
        applied.append(event.id)

    custom = {key: cast(str, value) for key, value in values.items() if specs[key].field_type == "text"}
    return EffectiveSnapshot(fields, custom, tuple(applied), tuple(sorted(orphaned)), baseline_status, values)
