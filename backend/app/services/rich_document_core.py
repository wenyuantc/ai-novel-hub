"""场景富文档的纯投影原语。

这些函数不代表已完成模型出站门禁；调用方仍须持有并核验项目 AI 可见性发送 lease、
作品授权及目标快照，且不得将未经确认的选区拼接进任何模型消息。
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from app.schemas.rich_document import (
    MAX_METADATA_BYTES,
    MAX_SECTION_CONTENT,
    DocumentSection,
    is_text_boundary,
    require_range,
)

MAX_STORED_SECTIONS = 21_000
MAX_STORED_MARKERS = 20_000


@dataclass(frozen=True)
class AIContentPiece:
    kind: Literal["prose", "section"]
    text: str
    section_key: UUID | None = None
    title: str = ""


def ai_content_pieces(content: str, sections: Sequence[DocumentSection]) -> list[AIContentPiece]:
    """按正文码点顺序投影，只包含当前明确开启的区块。

    结果为带来源的结构，不隐式加入提示词或渲染模型控制指令；调用方按已有的预算及
    不可信数据边界组装。关闭区块连名称和身份也不会出现在结果中。
    """
    visible = sorted(
        (item for item in sections if item.include_in_ai),
        key=lambda item: (item.anchor, item.order, str(item.key)),
    )
    if not visible:
        return [AIContentPiece(kind="prose", text=content)]
    pieces: list[AIContentPiece] = []
    cursor = 0
    for item in visible:
        if not is_text_boundary(content, item.anchor):
            raise ValueError("区块锚点超出正文或位于 CRLF 内部")
        if item.anchor > cursor:
            pieces.append(AIContentPiece(kind="prose", text=content[cursor:item.anchor]))
        pieces.append(AIContentPiece(kind="section", text=item.content, section_key=item.key, title=item.title))
        cursor = item.anchor
    if cursor < len(content):
        pieces.append(AIContentPiece(kind="prose", text=content[cursor:]))
    return pieces


def selected_section_text(section: DocumentSection, start: int, end: int, *, confirmed: bool) -> str:
    """显式选择关闭区块时仅取得所选正文，不返回标题、前后文或同景其他区块。"""
    if type(confirmed) is not bool:
        raise ValueError("选区发送确认必须为布尔值")
    require_range(section.content, start, end)
    if not section.include_in_ai and not confirmed:
        raise ValueError("发送关闭 AI 的区块选区前需要确认")
    selected = section.content[start:end]
    if not selected.strip():
        raise ValueError("请先选择需要处理的文字")
    return selected


def validate_storage_bounds(formatting: dict[str, Any], sections: list[dict], markers: list[dict]) -> None:
    """统一限制当前与退场身份，持久化和备份预检须使用同一口径。"""
    if len(sections) > MAX_STORED_SECTIONS or len(markers) > MAX_STORED_MARKERS:
        raise ValueError("区块或标记总数超过上限")
    if sum(len(row.get("content", "")) for row in sections) > MAX_SECTION_CONTENT:
        raise ValueError("区块正文总量超过上限")
    section_ids = {row["id"]: row["logical_key"] for row in sections}
    section_meta = [{
        "key": row["logical_key"], "anchor": row["anchor"], "order": row["sort"], "title": row["title"],
        "formatting": row.get("formatting", {}), "include_in_ai": row.get("include_in_ai", False),
        "active": row.get("active", True),
    } for row in sections]
    marker_meta = []
    for row in markers:
        section_id = row.get("section_id")
        if section_id is not None and section_id not in section_ids:
            raise ValueError("标记的区块不属于当前场景")
        marker_meta.append({
            "key": row["logical_key"], "section_key": section_ids.get(section_id), "status": row["status"],
            "color": row["color"], "start": row.get("start_offset"), "end": row.get("end_offset"),
            "last_start": row.get("last_start"), "excerpt": row.get("excerpt", ""),
            "excerpt_truncated": row.get("excerpt_truncated", False),
            "draft_excerpt": row.get("draft_excerpt"),
            "draft_excerpt_truncated": row.get("draft_excerpt_truncated", False),
            "draft_last_start": row.get("draft_last_start"),
        })
    metadata = {
        "formatting": formatting,
        "sections": section_meta,
        "markers": marker_meta,
    }
    try:
        for row in sections:
            row.get("content", "").encode("utf-8")
        metadata_size = len(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("富文档包含无效 Unicode") from exc
    if metadata_size > MAX_METADATA_BYTES:
        raise ValueError("富文档元数据总量超过上限")


def new_analysis_hasher(content: str):
    hasher = hashlib.sha256()
    hasher.update(b"ainovel.rich-analysis.v1\0")
    encoded = content.encode("utf-8")
    hasher.update(len(encoded).to_bytes(8, "big"))
    hasher.update(encoded)
    return hasher


def add_analysis_section(
    hasher, scene_order: int, anchor: int, order: int, key: UUID, title: str, content: str,
) -> None:
    payload = json.dumps([scene_order, anchor, order, str(key), title, content], ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    hasher.update(len(payload).to_bytes(8, "big"))
    hasher.update(payload)


def analysis_content_fingerprint(sources: Iterable[tuple[str, Sequence[DocumentSection]]]) -> str:
    """分析输入指纹；无启用区块时严格兼容原章纯文本 SHA-256。

    调用方按章投影顺序逐景提供 `(separator_before + 正文, 区块)`，区块锚点仍以本景正文为基准。
    关闭区块、作者高亮和纯排版不影响分析来源；已启用区块的位置、顺序、标题或正文
    改变会改变新指纹。该函数本身不修改现有分析／剧情记忆，也不执行写回校验。
    """
    chunks = list(sources)
    content = "".join(text for text, _ in chunks)
    visible = [
        (index, item)
        for index, (_text, sections) in enumerate(chunks)
        for item in sorted(sections, key=lambda section: (section.anchor, section.order, str(section.key)))
        if item.include_in_ai
    ]
    if not visible:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    hasher = new_analysis_hasher(content)
    for scene_order, item in visible:
        add_analysis_section(hasher, scene_order, item.anchor, item.order, item.key, item.title, item.content)
    return hasher.hexdigest()
