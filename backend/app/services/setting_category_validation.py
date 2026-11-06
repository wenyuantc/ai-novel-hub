"""分类图的同步纯校验；只供服务端已存/拟恢复标量快照使用，不接收HTTP元数据覆盖。"""

from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.schemas import setting_categories as s
from app.schemas.progression import PositiveDbInt
from app.schemas.setting_fields import EntryType, FieldType
from app.services.setting_field_values import is_positive_db_int

SnapshotRow = Mapping[str, object]


def _invalid_graph() -> ConflictError:
    return ConflictError("分类规则资料不完整或引用归属不合法", extra={"code": "invalid_category_graph"})


class _Identity(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: PositiveDbInt


class _ProjectIdentity(_Identity):
    project_id: PositiveDbInt


class _RuleIdentity(_Identity):
    category_id: PositiveDbInt


class _Definition(_ProjectIdentity):
    field_type: FieldType
    applicable_types: list[EntryType] = Field(min_length=1, max_length=9)
    archived: StrictBool = False


class _Option(_Identity):
    definition_id: PositiveDbInt
    archived: StrictBool = False


def _index[Snapshot: _Identity](rows: Sequence[SnapshotRow], model: type[Snapshot]) -> dict[int, Snapshot]:
    result = {}
    for row in rows:
        try:
            item = model.model_validate(row)
        except ValidationError as exc:
            raise _invalid_graph() from exc
        if item.id in result:
            raise _invalid_graph()
        result[item.id] = item
    return result


def assert_canonical_setting_tags(value: object) -> None:
    """只验证已存标签的规范形状，不将规范化结果写回或静默丢弃。"""
    try:
        normalized = s.normalize_setting_tags(value)
    except ValueError as exc:
        raise ConflictError("整理标签资料不合法", extra={"code": "invalid_category_graph"}) from exc
    if value != normalized:
        raise ConflictError("整理标签必须已去空白、去空并去重", extra={"code": "invalid_category_graph"})


def _validate_rules(
    project_id: int,
    spec: s.CategorySpec,
    definitions: Mapping[int, _Definition],
    options: Mapping[int, _Option],
    retained_rules: Sequence[s.CategoryRule],
) -> None:
    retained = {(rule.definition_id, rule.option_id) for rule in retained_rules if rule.kind == "dropdown"}
    for index, rule in enumerate(spec.rules):
        if rule.kind == "tag":
            continue
        extra = {"rule_index": index}
        definition = definitions.get(rule.definition_id)
        if definition is None or definition.project_id != project_id:
            raise NotFoundError("分类条件的字段不存在或不属于此作品", extra=extra)
        option = options.get(rule.option_id)
        if definition.field_type != "dropdown" or option is None or option.definition_id != definition.id:
            raise BadRequestError(
                "分类条件必须选择该下拉字段自己的选项", extra={**extra, "code": "invalid_category_rule"}
            )
        if not set(spec.applicable_types).intersection(definition.applicable_types):
            raise BadRequestError(
                "分类与条件字段没有共同适用类型", extra={**extra, "code": "incompatible_category_rule"}
            )
        if (definition.archived or option.archived) and (definition.id, option.id) not in retained:
            raise ConflictError("新条件不能选择已停用的字段或选项", extra={**extra, "code": "category_rule_archived"})


def validate_category_spec_metadata(
    project_id: int,
    spec: s.CategorySpec,
    definitions: Sequence[SnapshotRow],
    options: Sequence[SnapshotRow],
    *,
    retained_rules: Sequence[s.CategoryRule] = (),
) -> None:
    """查询/写入共用纯内核；metadata和retained只由服务端数据库快照提供。"""
    _validate_rules(project_id, spec, _index(definitions, _Definition), _index(options, _Option), retained_rules)


def validate_category_graph_snapshot(
    project_id: int,
    *,
    categories: Sequence[SnapshotRow],
    rules: Sequence[SnapshotRow],
    definitions: Sequence[SnapshotRow],
    options: Sequence[SnapshotRow],
) -> None:
    """同步检查整轮拟恢复图；无DB、flush、commit或输入修改。

    可传完整table标量行，多余列忽略。categories最少id/project_id/name/applicable_types/match_mode；
    rules最少id/category_id/kind及对应tag或definition_id/option_id，另一载荷必须空；sort缺省0。
    definitions最少id/project_id/field_type/applicable_types；options最少id/definition_id；archived缺省False。
    类别/定义均须属于project_id，规则/选项必须有给定父身份；当前图的历史停用身份可以保留。
    """
    if not is_positive_db_int(project_id):
        raise _invalid_graph()
    category_ids = _index(categories, _ProjectIdentity)
    rule_ids = _index(rules, _RuleIdentity)
    definition_ids = _index(definitions, _Definition)
    option_ids = _index(options, _Option)
    if any(row.project_id != project_id for row in (*category_ids.values(), *definition_ids.values())):
        raise _invalid_graph()
    if any(row.definition_id not in definition_ids for row in option_ids.values()):
        raise _invalid_graph()
    grouped = {category_id: [] for category_id in category_ids}
    for row in rules:
        identity = rule_ids[row["id"]]
        if identity.category_id not in grouped:
            raise _invalid_graph()
        kind = row.get("kind")
        payload = {"id": identity.id, "category_id": identity.category_id, "kind": kind, "sort": row.get("sort", 0)}
        if kind == "tag":
            if row.get("definition_id") is not None or row.get("option_id") is not None:
                raise _invalid_graph()
            assert_canonical_setting_tags([row.get("tag")])
            payload["tag"] = row.get("tag")
        elif kind == "dropdown":
            if row.get("tag") is not None:
                raise _invalid_graph()
            payload.update(definition_id=row.get("definition_id"), option_id=row.get("option_id"))
        else:
            raise _invalid_graph()
        grouped[identity.category_id].append(payload)
    for row in categories:
        try:
            category = s.CategoryOut.model_validate(
                {
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "name": row.get("name"),
                    "applicable_types": row.get("applicable_types"),
                    "match_mode": row.get("match_mode"),
                    "sort": row.get("sort", 0),
                    "rules": grouped[row["id"]],
                }
            )
        except ValidationError as exc:
            raise _invalid_graph() from exc
        _validate_rules(project_id, category.to_spec(), definition_ids, option_ids, category.rules)
