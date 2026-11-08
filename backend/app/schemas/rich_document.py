"""场景富格式、作者区块与正文标记的类型及 Unicode 坐标契约。

正式正文仍是 Scene.content；格式、区块和标记不会被拼入章正文。这里不访问数据库，
所属项目、版本及持久身份由外层服务在同一事务中校验。
"""

import json
from bisect import bisect_right
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

TextOffset = Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)]
SortOrder = Annotated[int, Field(strict=True, ge=0, le=2_147_483_647)]
MAX_SECTION_CONTENT = 2_000_000
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_MARKER_EXCERPT = 5_000


class StrictDocumentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def is_text_boundary(text: str, offset: int) -> bool:
    """可见文本边界：码点在字符串范围内且不将 CRLF 拆为两部分。"""
    return 0 <= offset <= len(text) and not (0 < offset < len(text) and text[offset - 1:offset + 1] == "\r\n")


def require_range(text: str, start: int, end: int, *, allow_empty: bool = False) -> None:
    if not is_text_boundary(text, start) or not is_text_boundary(text, end):
        raise ValueError("文本范围超出正文或位于 CRLF 内部")
    if start > end or (start == end and not allow_empty):
        raise ValueError("文本范围无效")


def marker_excerpt(text: str, start: int, end: int) -> tuple[str, bool]:
    """有界原文摘录；截断时不拆 CRLF，并明确告诉调用方省略了后续内容。"""
    limit = min(end, start + MAX_MARKER_EXCERPT)
    truncated = limit < end
    if truncated and text[limit - 1:limit + 1] == "\r\n":
        limit -= 1
    return text[start:limit], truncated


def validate_chapter_document_boundaries(parts: list[tuple[str, "SceneDocument"]]) -> None:
    """对拼接后的章投影复核元数据落点，包括跨景或分隔形成的 CRLF。"""
    content = "".join(separator + document.content for separator, document in parts)
    cursor = 0
    for separator, document in parts:
        cursor += len(separator)
        offsets = list(document.formatting.boundaries)
        for style in (*document.formatting.inline, *document.formatting.blocks):
            offsets.extend((style.start, style.end))
        offsets.extend(section.anchor for section in document.sections)
        for marker in document.markers:
            if marker.section_key is None and marker.status == "active":
                assert marker.start is not None and marker.end is not None
                offsets.extend((marker.start, marker.end))
        if any(not is_text_boundary(content, cursor + offset) for offset in offsets):
            raise ValueError("文档元数据落在章节 CRLF 中间")
        cursor += len(document.content)


class InlineStyle(StrictDocumentModel):
    kind: Literal["bold", "italic"]
    start: TextOffset
    end: TextOffset


class BlockStyle(StrictDocumentModel):
    kind: Literal["heading", "bullet_list", "ordered_list", "blockquote"]
    start: TextOffset
    end: TextOffset
    level: Annotated[int, Field(strict=True, ge=1, le=6)] | None = None

    @model_validator(mode="after")
    def validate_level(self):
        if (self.kind == "heading") != (self.level is not None):
            raise ValueError("只有标题需要指定层级")
        return self


class TextFormatting(StrictDocumentModel):
    version: Literal[1] = 1
    # 区块等零字符节点可将一个自然段拆为多个可独立格式化的编辑器块。
    boundaries: list[TextOffset] = Field(default_factory=list, max_length=20_000)
    inline: list[InlineStyle] = Field(default_factory=list, max_length=20_000)
    blocks: list[BlockStyle] = Field(default_factory=list, max_length=20_000)

    @model_validator(mode="before")
    @classmethod
    def strict_version(cls, value):
        if isinstance(value, dict) and "version" in value and type(value["version"]) is not int:
            raise ValueError("格式版本必须为整数")
        return value

    def validate_for_text(self, text: str) -> None:
        if self.boundaries != sorted(set(self.boundaries)):
            raise ValueError("零宽段落边界必须唯一且有序")
        if any(not is_text_boundary(text, offset) for offset in self.boundaries):
            raise ValueError("零宽段落边界超出正文或位于 CRLF 内部")
        boundaries = set(self.boundaries)
        by_kind: dict[str, list[InlineStyle]] = {}
        for item in self.inline:
            require_range(text, item.start, item.end)
            by_kind.setdefault(item.kind, []).append(item)
        for items in by_kind.values():
            ordered = sorted(items, key=lambda item: (item.start, item.end))
            if any(left.end > right.start for left, right in zip(ordered, ordered[1:], strict=False)):
                raise ValueError("相同文字样式范围不能重叠")
        blocks = sorted(self.blocks, key=lambda item: (item.start, item.end))
        for left, right in zip(blocks, blocks[1:], strict=False):
            if left.end > right.start or left.start == left.end == right.start == right.end:
                raise ValueError("段落格式不能重叠")
        for item in blocks:
            require_range(text, item.start, item.end, allow_empty=True)
            starts_block = item.start == 0 or item.start in boundaries or text[item.start - 1] in "\r\n"
            ends_block = item.end == len(text) or item.end in boundaries or text[item.end] in "\r\n"
            if not starts_block or not ends_block:
                raise ValueError("段落格式必须覆盖完整段落或显式结构边界")
            next_boundary = bisect_right(self.boundaries, item.start)
            if item.kind == "heading" and (
                "\r" in text[item.start:item.end] or "\n" in text[item.start:item.end]
                or next_boundary < len(self.boundaries) and self.boundaries[next_boundary] < item.end
            ):
                raise ValueError("标题不能跨越段落边界")


class DocumentSection(StrictDocumentModel):
    key: UUID
    anchor: TextOffset
    order: SortOrder
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=500_000)
    formatting: TextFormatting = Field(default_factory=TextFormatting)
    include_in_ai: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_content(self):
        if not self.title.strip():
            raise ValueError("区块名称不能为空")
        try:
            self.content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("区块正文包含无效 Unicode") from exc
        self.formatting.validate_for_text(self.content)
        return self


class DocumentMarker(StrictDocumentModel):
    key: UUID
    # None 指场景正文；有效标记须引用同景活动区块。失效标记可保留退场区块身份，
    # 但外层服务仍须验证它确实曾属于同一场景。
    section_key: UUID | None = None
    status: Literal["active", "stale"]
    start: TextOffset | None = None
    end: TextOffset | None = None
    color: Literal["yellow", "green", "blue", "purple", "red"]
    excerpt: str = Field(default="", max_length=MAX_MARKER_EXCERPT)
    excerpt_truncated: bool = Field(default=False, strict=True)
    last_start: TextOffset | None = None
    # 独立保存的草稿摘录不声称经过服务端重放或验证。
    draft_excerpt: str | None = Field(default=None, min_length=1, max_length=MAX_MARKER_EXCERPT)
    draft_excerpt_truncated: bool = Field(default=False, strict=True)
    draft_last_start: TextOffset | None = None

    @model_validator(mode="after")
    def validate_status(self):
        if self.status == "active":
            if self.start is None or self.end is None:
                raise ValueError("有效标记必须有文字范围")
        elif self.start is not None or self.end is not None or self.last_start is None:
            raise ValueError("失效标记必须保留旧位置且不能伪装为有效范围")
        if self.draft_excerpt is None:
            if self.draft_excerpt_truncated or self.draft_last_start is not None:
                raise ValueError("没有草稿片段时不能提供截断状态或草稿位置")
        elif self.status != "stale" or self.draft_last_start is None:
            raise ValueError("草稿片段仅可随失效标记保存，且必须保留草稿位置")
        return self


class SceneDocument(StrictDocumentModel):
    content: str
    formatting: TextFormatting = Field(default_factory=TextFormatting)
    sections: list[DocumentSection] = Field(default_factory=list, max_length=1_000)
    # 由服务端提供／校验的同景退场区块身份；不携带已删除区块的正文。
    retired_section_keys: list[UUID] = Field(default_factory=list, max_length=20_000)
    markers: list[DocumentMarker] = Field(default_factory=list, max_length=20_000)

    @model_validator(mode="after")
    def validate_document(self):
        try:
            self.content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("场景正文包含无效 Unicode") from exc
        self.formatting.validate_for_text(self.content)
        sections: dict[UUID, DocumentSection] = {}
        retired = set(self.retired_section_keys)
        if len(retired) != len(self.retired_section_keys):
            raise ValueError("退场区块身份重复")
        orders: set[tuple[int, int]] = set()
        if sum(len(item.content) for item in self.sections) > MAX_SECTION_CONTENT:
            raise ValueError("区块正文总量超过上限")
        for item in self.sections:
            if item.key in sections or item.key in retired:
                raise ValueError("区块身份重复")
            if not is_text_boundary(self.content, item.anchor):
                raise ValueError("区块锚点超出正文或位于 CRLF 内部")
            order = item.anchor, item.order
            if order in orders:
                raise ValueError("同一锚点的区块顺序重复")
            orders.add(order)
            sections[item.key] = item
        markers: set[UUID] = set()
        for item in self.markers:
            if item.key in markers:
                raise ValueError("标记身份重复")
            markers.add(item.key)
            if item.section_key is not None and item.section_key not in sections:
                if item.status == "active" or item.section_key not in retired:
                    raise ValueError("标记的区块不属于当前场景")
            if item.status == "active":
                target = self.content if item.section_key is None else sections[item.section_key].content
                assert item.start is not None and item.end is not None
                require_range(target, item.start, item.end)
        metadata = {
            "formatting": self.formatting.model_dump(mode="json"),
            "sections": [item.model_dump(mode="json", exclude={"content"}) for item in self.sections],
            "retired_section_keys": [str(key) for key in self.retired_section_keys],
            "markers": [item.model_dump(mode="json") for item in self.markers],
        }
        try:
            metadata_size = len(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("元数据包含无效 Unicode") from exc
        if metadata_size > MAX_METADATA_BYTES:
            raise ValueError("文档元数据总量超过上限")
        return self


class SceneDocumentSave(StrictDocumentModel):
    expected_content_revision: Annotated[int, Field(strict=True, ge=0)]
    document: SceneDocument
    # 不通过遗漏字段或身份推断删除；这些集合描述本次作者操作。
    add_section_keys: list[UUID] = Field(max_length=1_000)
    delete_section_keys: list[UUID] = Field(max_length=1_000)
    # 跨保存撤销可显式复活同景墓碑，不把旧身份伪装成新建。
    restore_section_keys: list[UUID] = Field(default_factory=list, max_length=1_000)
    add_marker_keys: list[UUID] = Field(max_length=20_000)
    delete_marker_keys: list[UUID] = Field(max_length=20_000)
    # 显式恢复同景已删除的身份，不能通过 add 冒充新标记。
    restore_marker_keys: list[UUID] = Field(default_factory=list, max_length=20_000)
    mapped_section_keys: list[UUID] = Field(max_length=1_000)
    mapped_marker_keys: list[UUID] = Field(max_length=20_000)

    @model_validator(mode="after")
    def explicit_document_and_unique_intents(self):
        required = {"content", "formatting", "sections", "retired_section_keys", "markers"}
        if not required <= self.document.model_fields_set:
            raise ValueError("完整文档的正文、格式、区块、退场身份和标记都必须显式提供")
        if not {"version", "boundaries", "inline", "blocks"} <= self.document.formatting.model_fields_set:
            raise ValueError("完整文档必须显式提供全部正文格式字段")
        for field in ("add_section_keys", "delete_section_keys", "restore_section_keys", "add_marker_keys",
                      "delete_marker_keys", "restore_marker_keys", "mapped_section_keys", "mapped_marker_keys"):
            keys = getattr(self, field)
            if len(set(keys)) != len(keys):
                raise ValueError("文档操作意图不能包含重复身份")
        return self


class SceneDocumentOut(StrictDocumentModel):
    content_revision: int
    planning_revision: int
    document: SceneDocument


class SceneSectionConvert(StrictDocumentModel):
    expected_content_revision: Annotated[int, Field(strict=True, ge=0)]


class MarkerTimelineItem(StrictDocumentModel):
    key: UUID
    chapter_id: int
    chapter_no: int
    chapter_title: str
    scene_id: int
    scene_title: str
    scene_active: bool
    section_key: UUID | None
    section_title: str | None
    status: Literal["active", "stale"]
    color: Literal["yellow", "green", "blue", "purple", "red"]
    start: int | None
    end: int | None
    last_start: int | None
    excerpt: str
    excerpt_truncated: bool
    draft_excerpt: str | None = None
    draft_excerpt_truncated: bool = False
    draft_last_start: int | None = None
    content_revision: int


class MarkerTimelinePage(StrictDocumentModel):
    items: list[MarkerTimelineItem]
    total: int
    offset: int
    limit: int
    planning_revision: int
    snapshot: str


class MarkerRelink(StrictDocumentModel):
    expected_source_revision: Annotated[int, Field(strict=True, ge=0)]
    target_chapter_id: Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]
    target_scene_id: Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]
    expected_target_revision: Annotated[int, Field(strict=True, ge=0)]
    target_section_key: UUID | None = None
    start: TextOffset
    end: TextOffset
    selected_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class MarkerRelinkOut(StrictDocumentModel):
    source_chapter_id: int
    source_revision: int
    target_chapter_id: int
    target_revision: int
    target_scene_id: int
    key: UUID


class MarkerDeleteOut(StrictDocumentModel):
    chapter_id: int
    content_revision: int
