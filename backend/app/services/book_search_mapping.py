"""批次写入及备份验证共用同一套明确选区映射。"""

from dataclasses import dataclass, field
from uuid import UUID

from app.schemas.rich_document import SceneDocument, is_text_boundary, marker_excerpt
from app.services.rich_document_mapping import UnsafeMapping, map_explicit_replacement


def lost_marker_keys(before: SceneDocument, after: SceneDocument) -> set[UUID]:
    active = {marker.key for marker in before.markers if marker.status == "active"}
    return {marker.key for marker in after.markers if marker.key in active and marker.status == "stale"}


@dataclass
class VerifiedPreviews:
    """同一写事务、同一场景的逐项预检。只保留倒序回放起点的文档，其他项只保留标记身份。"""

    rightmost_start: int = -1
    rightmost_document: SceneDocument | None = None
    lost_markers: dict[int, set[UUID]] = field(default_factory=dict)

    def record(self, start: int, candidate: SceneDocument, lost: set[UUID]) -> None:
        self.lost_markers[start] = lost
        if start > self.rightmost_start:
            self.rightmost_start = start
            self.rightmost_document = candidate


def replacement_warning(before: SceneDocument, start: int, end: int, lost: int) -> str | None:
    touched = sum(1 for marker in before.markers if marker.section_key is None and marker.status == "active"
                  and marker.start < end and marker.end > start)
    warnings = []
    if lost:
        warnings.append(f"{lost} 个正文标记需要重新定位")
    if touched > lost:
        warnings.append(f"{touched - lost} 个正文标记将随选区调整")
    return "替换后" + "，".join(warnings) if warnings else None


def replay_replacements(
    document: SceneDocument, query: str, replacement: str, start_offsets: list[int],
    *, verified: VerifiedPreviews | None = None,
) -> SceneDocument:
    """输入均基于原稿，位置局限于本场景；组合不得产生逐处预览未提示的标记失效。"""
    if not query or query == replacement or not start_offsets:
        raise UnsafeMapping("未选择有效改动")
    previous_end = 0
    for start in start_offsets:
        end = start + len(query)
        if (start < previous_end or not is_text_boundary(document.content, start)
                or not is_text_boundary(document.content, end) or document.content[start:end] != query):
            raise UnsafeMapping("选区重叠、文本边界无效或原文已变化")
        previous_end = end

    # 纯正文不需要对整场景重复拼接或验证；偏移始终相对原稿。
    fmt = document.formatting
    if not (fmt.boundaries or fmt.inline or fmt.blocks or document.sections or document.markers):
        parts = []
        cursor = 0
        for start in start_offsets:
            parts.extend((document.content[cursor:start], replacement))
            cursor = start + len(query)
        parts.append(document.content[cursor:])
        return document.model_copy(update={"content": "".join(parts)})

    result = document
    for start in reversed(start_offsets):
        end = start + len(query)
        if verified is not None and start == verified.rightmost_start and verified.rightmost_document is not None:
            mapped = verified.rightmost_document
        else:
            mapped = map_explicit_replacement(result, start, end, replacement)
        if result is not document and document.markers:
            actual = lost_marker_keys(result, mapped)
            if verified is not None:
                expected = verified.lost_markers[start]
            elif actual:
                original_view = map_explicit_replacement(document, start, end, replacement)
                expected = lost_marker_keys(document, original_view)
            else:
                # 若单独替换本会使标记失效，标记原范围必然完全位于本选区内。
                # 右侧不重叠的先行替换不会触及该范围，组合到这里仍必然观察到失效。
                expected = set()
            if expected != actual:
                raise UnsafeMapping("组合替换会造成预览之外的标记失效，请分批核对")
        result = mapped

    # 持久层以本次写入前的整份原文核对失效标记的摘录，而非中途的临时文本。
    stale = lost_marker_keys(document, result)
    if stale:
        originals = {m.key: m for m in document.markers if m.key in stale}
        for marker in result.markers:
            if marker.key in originals:
                old = originals[marker.key]
                marker.last_start = old.start
                marker.excerpt, marker.excerpt_truncated = marker_excerpt(document.content, old.start, old.end)
    return result
