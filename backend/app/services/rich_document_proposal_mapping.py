"""整景候选的线性、保守映射；有歧义的位置和格式必须保留为待核对。"""

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass

from pydantic import ValidationError

from app.schemas.rich_document import SceneDocument, is_text_boundary, marker_excerpt


class ProposalMappingError(ValueError):
    """提案尚未按原身份、顺序和格式风险获得完整确认。"""


@dataclass(frozen=True)
class SceneProposalMapping:
    original: SceneDocument
    candidate: str
    template: dict
    unresolved_sections: list[str]
    format_loss: bool

    @property
    def requires_review(self) -> bool:
        return bool(self.unresolved_sections or self.format_loss)


def _unique(text: str, fragment: str) -> bool:
    first = text.find(fragment)
    return bool(fragment) and first >= 0 and text.find(fragment, first + 1) < 0


def _stable_edges(old: str, new: str) -> tuple[int, int]:
    """仅使用两个文本中都唯一出现的相同前后缀，不让重复文字决定映射。"""
    common = min(len(old), len(new))
    prefix = 0
    while prefix < common and old[prefix] == new[prefix]:
        prefix += 1
    while prefix and (not is_text_boundary(old, prefix) or not is_text_boundary(new, prefix)):
        prefix -= 1
    suffix = 0
    while suffix < common - prefix and old[-suffix - 1] == new[-suffix - 1]:
        suffix += 1
    while suffix and (not is_text_boundary(old, len(old) - suffix)
                      or not is_text_boundary(new, len(new) - suffix)):
        suffix -= 1
    beginning, ending = old[:prefix], old[len(old) - suffix:] if suffix else ""
    if not (_unique(old, beginning) and _unique(new, beginning)):
        prefix = 0
    if not (_unique(old, ending) and _unique(new, ending)):
        suffix = 0
    return prefix, suffix


def _whole_prefix_end(text: str, start: int, prefix: int, boundaries: list[int]) -> int | None:
    """只留下改动前完整段落，绝不制造半个块格式。"""
    possible = [point for point in boundaries if start <= point < prefix]
    for char in ("\r", "\n"):
        point = text.rfind(char, start, prefix)
        if point >= start:
            if point and text[point - 1:point + 1] == "\r\n":
                point -= 1
            if is_text_boundary(text, point):
                possible.append(point)
    return max(possible) if possible else None


def _whole_suffix_start(text: str, suffix: int, end: int, boundaries: list[int]) -> int | None:
    """只留下改动后完整段落，CRLF 必须作为整体跨过。"""
    possible = [point for point in boundaries if suffix < point <= end]
    indices = [point for char in ("\r", "\n") if (point := text.find(char, suffix, end)) >= 0]
    if indices:
        point = min(indices)
        after = point + (2 if text[point:point + 2] == "\r\n" else 1)
        if after <= end and is_text_boundary(text, after):
            possible.append(after)
    return min(possible) if possible else None


def build_scene_proposal(original: SceneDocument, candidate: str) -> SceneProposalMapping:
    raw = original.model_dump(mode="json")
    if candidate == original.content:
        return SceneProposalMapping(original.model_copy(deep=True), candidate, raw, [], False)
    old = original.content
    prefix, suffix = _stable_edges(old, candidate)
    old_suffix = len(old) - suffix
    delta = len(candidate) - len(old)
    raw["content"] = candidate
    unresolved = []
    for section in raw["sections"]:
        anchor = section["anchor"]
        if prefix and anchor < prefix:
            continue
        if suffix and anchor > old_suffix:
            section["anchor"] = anchor + delta
            continue
        section["anchor"] = None
        unresolved.append(section["key"])

    loss = False
    old_boundaries = raw["formatting"]["boundaries"]
    boundaries = []
    for position in old_boundaries:
        if prefix and position < prefix:
            boundaries.append(position)
        elif suffix and position > old_suffix:
            boundaries.append(position + delta)
        else:
            loss = True
    raw["formatting"]["boundaries"] = boundaries
    styles = []
    for item in raw["formatting"]["inline"]:
        start, end = item["start"], item["end"]
        if prefix and end <= prefix:
            styles.append(item)
        elif suffix and start >= old_suffix:
            styles.append({**item, "start": start + delta, "end": end + delta})
        else:
            loss = True
            if prefix and start < prefix:
                styles.append({**item, "end": min(end, prefix)})
            if suffix and end > old_suffix:
                styles.append({**item, "start": max(start, old_suffix) + delta, "end": end + delta})
    raw["formatting"]["inline"] = styles
    blocks = []
    for item in raw["formatting"]["blocks"]:
        start, end = item["start"], item["end"]
        if prefix and end <= prefix:
            blocks.append(item)
        elif suffix and start >= old_suffix:
            blocks.append({**item, "start": start + delta, "end": end + delta})
        else:
            # 没有完整保留的块只按自然换行/显式边界拆出可证明未变的完整段落。
            loss = True
            if prefix and start < prefix:
                left_end = _whole_prefix_end(old, start, min(end, prefix), old_boundaries)
                if left_end is not None and left_end > start:
                    blocks.append({**item, "end": left_end})
            if suffix and end > old_suffix:
                right_start = _whole_suffix_start(old, max(start, old_suffix), end, old_boundaries)
                if right_start is not None and right_start < end:
                    blocks.append({**item, "start": right_start + delta, "end": end + delta})
    raw["formatting"]["blocks"] = blocks

    for marker in raw["markers"]:
        if marker["status"] != "active" or marker["section_key"] is not None:
            continue
        start, end = marker["start"], marker["end"]
        left = (start, min(end, prefix)) if prefix and start < prefix else None
        right = ((max(start, old_suffix) + delta, end + delta)
                 if suffix and end > old_suffix else None)
        if left and right:
            marker["start"], marker["end"] = left[0], right[1]
        elif left:
            marker["start"], marker["end"] = left
        elif right:
            marker["start"], marker["end"] = right
        else:
            excerpt, truncated = marker_excerpt(old, start, end)
            marker.update(status="stale", start=None, end=None, last_start=start, excerpt=excerpt,
                          excerpt_truncated=truncated)
    return SceneProposalMapping(original.model_copy(deep=True), candidate, raw, unresolved, loss)


def resolve_scene_proposal(
    plan: SceneProposalMapping, positions: dict[str, int], *, confirm_format_loss: bool,
) -> SceneDocument:
    if type(confirm_format_loss) is not bool or plan.format_loss and not confirm_format_loss:
        raise ProposalMappingError("请明确确认格式变化")
    if set(positions) != set(plan.unresolved_sections):
        raise ProposalMappingError("每个待核对区块都必须指定一个位置，不能改动其他身份")
    raw = deepcopy(plan.template)
    previous = -1
    per_anchor: dict[int, int] = defaultdict(int)
    order = sorted(plan.original.sections, key=lambda item: (item.anchor, item.order))
    by_key = {item["key"]: item for item in raw["sections"]}
    for section in order:
        item = by_key[str(section.key)]
        anchor = positions.get(item["key"], item["anchor"])
        if type(anchor) is not int or not is_text_boundary(plan.candidate, anchor) or anchor < previous:
            raise ProposalMappingError("区块位置无效或改变了原有相对顺序")
        item["anchor"], item["order"] = anchor, per_anchor[anchor]
        per_anchor[anchor] += 1
        previous = anchor
    try:
        return SceneDocument.model_validate(raw)
    except (ValidationError, ValueError) as exc:
        raise ProposalMappingError("候选格式、标记或区块位置无法无损验证") from exc
