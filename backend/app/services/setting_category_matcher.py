"""纯分类匹配：只比较当前标签与稳定下拉身份，不读取数据库。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.schemas.setting_categories import CategorySpec
from app.schemas.setting_fields import EntryType


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    matched_rule_indexes: tuple[int, ...]


def match_category(
    spec: CategorySpec,
    entry_type: EntryType,
    tags: Sequence[str],
    dropdown_values: Mapping[int, int | None],
) -> MatchResult:
    if entry_type not in spec.applicable_types:
        return MatchResult(False, ())
    tag_set = set(tags)
    matched = []
    for index, rule in enumerate(spec.rules):
        if rule.kind == "tag":
            hit = rule.tag in tag_set
        else:
            hit = dropdown_values.get(rule.definition_id) == rule.option_id
        if hit:
            matched.append(index)
    return MatchResult(bool(matched) if spec.match_mode == "any" else len(matched) == len(spec.rules), tuple(matched))
