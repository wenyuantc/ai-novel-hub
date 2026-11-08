"""明确选区替换的保守结构映射；不根据重复文本寻找新位置。"""

from bisect import bisect_left, bisect_right

from pydantic import ValidationError

from app.schemas.rich_document import SceneDocument, TextFormatting, is_text_boundary, marker_excerpt


class UnsafeMapping(ValueError):
    """需要作者明确核对的区块边界或格式。"""


class FormatLossRequired(UnsafeMapping):
    """选区格式不能全部可靠映射；允许作者明确确认后保留可证明的部分。"""


def _confirmed_formatting(formatting: dict, text: str, start: int, end: int, replacement: str) -> dict:
    """仅保留替换前后可证明且仍可表示的格式，不给新文字猜测字符样式。"""
    result = {"version": formatting["version"], "boundaries": [], "inline": [], "blocks": []}
    new_text = text[:start] + replacement + text[end:]
    delta = len(replacement) - (end - start)
    insertion = start == end
    for boundary in formatting["boundaries"]:
        if start < boundary < end or insertion and boundary == start:
            continue
        mapped = boundary if boundary <= start else boundary + delta
        if is_text_boundary(new_text, mapped):
            result["boundaries"].append(mapped)
    result["boundaries"] = sorted(set(result["boundaries"]))

    def pieces(style: dict) -> list[tuple[int, int]]:
        left, right = style["start"], style["end"]
        if right <= start:
            return [(left, right)]
        if left >= end:
            return [(left + delta, right + delta)]
        return [(left, min(right, start)), (max(left, end) + delta, right + delta)]

    for style in formatting["inline"]:
        for left, right in pieces(style):
            # 接缝形成 CRLF 时向内收缩，绝不制造半个 CRLF 的格式范围。
            if not is_text_boundary(new_text, left):
                left += 1
            if not is_text_boundary(new_text, right):
                right -= 1
            if left < right:
                result["inline"].append({**style, "start": left, "end": right})

    starts = {0, *result["boundaries"]}
    ends = {len(new_text), *result["boundaries"]}
    for index, char in enumerate(new_text):
        if char in "\r\n":
            if is_text_boundary(new_text, index):
                ends.add(index)
            if is_text_boundary(new_text, index + 1):
                starts.add(index + 1)
    starts, ends = sorted(starts), sorted(ends)

    def valid_block(style: dict, left: int, right: int) -> bool:
        if left not in starts_set or right not in ends_set or left > right:
            return False
        boundary = bisect_right(result["boundaries"], left)
        return style["kind"] != "heading" or (
            "\r" not in new_text[left:right] and "\n" not in new_text[left:right]
            and (boundary == len(result["boundaries"]) or result["boundaries"][boundary] >= right)
        )

    starts_set, ends_set = set(starts), set(ends)
    for style in formatting["blocks"]:
        left, right = style["start"], style["end"]
        # 同一格式段内的单行替换不改变段落身份；可保留其整段排版。
        if (left <= start <= end <= right and not any(char in "\r\n" for char in text[start:end] + replacement)
                and valid_block(style, left, right + delta)):
            result["blocks"].append({**style, "start": left, "end": right + delta})
            continue
        for first, last in pieces(style):
            first_index, last_index = bisect_left(starts, first), bisect_right(ends, last) - 1
            if first_index >= len(starts) or last_index < 0:
                continue
            clipped_start, clipped_end = starts[first_index], ends[last_index]
            if clipped_start > clipped_end or clipped_start == clipped_end and left != right:
                continue
            if valid_block(style, clipped_start, clipped_end):
                result["blocks"].append({**style, "start": clipped_start, "end": clipped_end})
    return result


def map_explicit_replacement(
    document: SceneDocument, start: int, end: int, replacement: str, *, confirm_format_loss: bool = False,
) -> SceneDocument:
    """只映射相对选区能直接证明的范围；不可证明的结构由调用方保留原稿并提示。"""
    text = document.content
    if start > end or not is_text_boundary(text, start) or not is_text_boundary(text, end):
        raise UnsafeMapping("选区超出正文或位于 CRLF 中间")
    if text[start:end] == replacement:
        return document.model_copy(deep=True)
    delta = len(replacement) - (end - start)
    insertion = start == end
    result = document.model_dump(mode="json")
    result["content"] = text[:start] + replacement + text[end:]

    # 旧 API 只传码点，选区端点与零宽区块重合时缺少左右偏向与身份，不能猜。
    for section in result["sections"]:
        anchor = section["anchor"]
        if start <= anchor <= end:
            raise UnsafeMapping("选区触及区块位置，需要作者确认锚点")
        if anchor > end:
            section["anchor"] += delta

    if confirm_format_loss:
        result["formatting"] = _confirmed_formatting(result["formatting"], text, start, end, replacement)
    else:
        boundaries = []
        for boundary in result["formatting"]["boundaries"]:
            if start < boundary < end or insertion and boundary == start:
                raise FormatLossRequired("选区触及结构化段落边界，需要作者确认格式")
            mapped = boundary if boundary <= start else boundary + delta
            if not is_text_boundary(result["content"], mapped):
                raise FormatLossRequired("替换接缝无法保留原段落边界，需要作者确认格式")
            boundaries.append(mapped)
        result["formatting"]["boundaries"] = boundaries

        for kind in ("inline", "blocks"):
            for style in result["formatting"][kind]:
                left, right = style["start"], style["end"]
                if insertion:
                    intersects = left < start < right or left == right == start
                else:
                    intersects = left < end and right > start
                if intersects:
                    raise FormatLossRequired("选区包含富格式，需要作者核对并确认格式变化")
                if left >= end:
                    style["start"], style["end"] = left + delta, right + delta
                if not is_text_boundary(result["content"], style["start"]) or not is_text_boundary(
                    result["content"], style["end"],
                ):
                    raise FormatLossRequired("替换接缝无法保留原格式，需要作者确认格式")

    try:
        TextFormatting.model_validate(result["formatting"]).validate_for_text(result["content"])
    except (ValidationError, ValueError) as exc:
        if not confirm_format_loss:
            raise FormatLossRequired("替换后原段落格式无法完整保留，需要作者确认格式") from exc
        raise UnsafeMapping("替换后段落格式仍无法可靠映射") from exc

    for marker in result["markers"]:
        if marker["section_key"] is not None or marker["status"] != "active":
            continue
        left, right = marker["start"], marker["end"]
        if right <= start:
            continue
        if left >= end:
            marker["start"], marker["end"] = left + delta, right + delta
        elif insertion:
            # 范围内部插入：两端原字符均可定位，扩大标记，不按新词面搜索。
            marker["end"] = right + delta
        elif left < start and right > end:
            # 标记两端仍有原字符，可靠跟随内部替换。
            marker["end"] = right + delta
        elif left < start:
            # 右半边被覆盖，只保留可证明存在的左侧原范围。
            marker["end"] = start
        elif right > end:
            # 左半边被覆盖，只保留可证明存在的右侧原范围。
            marker["start"], marker["end"] = start + len(replacement), right + delta
        else:
            # 全部原字符被覆盖，不能根据新文本的相同词面猜测关联。
            excerpt, truncated = marker_excerpt(text, left, right)
            marker.update(status="stale", start=None, end=None, last_start=left, excerpt=excerpt,
                          excerpt_truncated=truncated)
    try:
        return SceneDocument.model_validate(result)
    except (ValidationError, ValueError) as exc:
        raise UnsafeMapping("替换后的结构或 CRLF 边界无法可靠映射") from exc
