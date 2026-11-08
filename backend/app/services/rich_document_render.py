"""读取、Markdown 与 EPUB 共用的正式正文排版投影。

只读取活动场景的正文和格式，不读取作者区块或标记。章节与所有场景在同一条 SQL
中取得，避免并发编辑时用一版正文套用另一版坐标。偏移始终是 Unicode 码点。
"""

import re
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from typing import Any

from sqlalchemy import and_, case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import set_committed_value

from app.core.errors import ConflictError
from app.models import Chapter, Scene, SceneDocumentMeta
from app.schemas.reader import ReaderBlock, ReaderRun
from app.schemas.rich_document import BlockStyle, InlineStyle, TextFormatting, is_text_boundary

_NEWLINE = re.compile(r"\r\n|\r|\n")


@dataclass(frozen=True)
class ChapterProjection:
    chapter: Chapter
    formatting: TextFormatting
    blocks: list[ReaderBlock] | None


def _runs(start: int, end: int, inline: dict[str, tuple[list[InlineStyle], list[int]]]) -> list[ReaderRun]:
    """裁剪交叠样式；每种样式自身不重叠。"""
    cuts = {start, end}
    relevant: dict[str, list[InlineStyle]] = {}
    for kind, (styles, ends) in inline.items():
        # 一种样式的范围按起点排序且不能重叠，其终点也必然有序。
        index = bisect_right(ends, start)
        selected = []
        while index < len(styles) and styles[index].start < end:
            style = styles[index]
            selected.append(style)
            cuts.add(max(start, style.start))
            cuts.add(min(end, style.end))
            index += 1
        relevant[kind] = selected
    points = sorted(cuts)
    starts = {kind: [style.start for style in styles] for kind, styles in relevant.items()}

    def active(kind: str, pos: int) -> bool:
        index = bisect_right(starts[kind], pos) - 1
        return index >= 0 and pos < relevant[kind][index].end

    return [ReaderRun(start=a, end=b, bold=active("bold", a), italic=active("italic", a))
            for a, b in zip(points, points[1:], strict=False) if a < b]


def prose_blocks(content: str, formatting: TextFormatting, *, validate: bool = True) -> list[ReaderBlock]:
    """将自然换行与零宽结构边界切为可安全渲染的块，不修改原文。"""
    if validate:
        formatting.validate_for_text(content)
    inline = {}
    for kind in ("bold", "italic"):
        items = sorted((item for item in formatting.inline if item.kind == kind),
                       key=lambda item: (item.start, item.end))
        inline[kind] = (items, [item.end for item in items])
    styles = sorted(formatting.blocks, key=lambda item: (item.start, item.end))
    starts = [item.start for item in styles]
    cuts = {0, len(content), *formatting.boundaries}
    for style in styles:
        cuts.update((style.start, style.end))
    newlines = list(_NEWLINE.finditer(content))
    for match in newlines:
        cuts.update((match.start(), match.end()))
    points = sorted(cuts)
    newline_starts = {match.start(): match.end() for match in newlines}
    blocks = []
    for start, end in zip(points, points[1:], strict=False):
        if start == end or newline_starts.get(start) == end:
            continue
        index = bisect_right(starts, start) - 1
        style = styles[index] if index >= 0 and styles[index].end >= end else None
        blocks.append((ReaderBlock(kind=style.kind if style else "paragraph", start=start, end=end,
                                   level=style.level if style else None, runs=_runs(start, end, inline)),
                       index if style else None))
    # 空白标题等没有字符，但仍是一个真实的格式节点。
    for index, style in enumerate(styles):
        if style.start == style.end:
            blocks.append((ReaderBlock(kind=style.kind, start=style.start, end=style.end, level=style.level, runs=[]),
                           index))
    blocks.sort(key=lambda item: (item[0].start, item[0].end))
    merged: list[ReaderBlock] = []
    last_style: int | None = None
    for block, style_index in blocks:
        # 隐藏作者区块时，两个无换行的正文段必须重新连起来，不能凭零宽锚点凭空换段。
        # 同一列表/引用块被零宽锚点切开时亦应连回；不同格式块仍各自保留语义。
        if (merged and merged[-1].end == block.start and merged[-1].kind == block.kind
                and (block.kind == "paragraph" or style_index is not None and style_index == last_style)):
            merged[-1].end = block.end
            for run in block.runs:
                previous_run = merged[-1].runs[-1] if merged[-1].runs else None
                if (previous_run and previous_run.end == run.start
                        and previous_run.bold == run.bold and previous_run.italic == run.italic):
                    # 合并相同排版，避免泄露不可见零宽锚点并防止长章响应膨胀。
                    previous_run.end = run.end
                else:
                    merged[-1].runs.append(run)
        else:
            merged.append(block)
        last_style = style_index
    return merged


def chapter_projection(chapter: Chapter, rows: list[tuple[int, str, str, dict[str, Any] | None]]) -> ChapterProjection:
    parts: list[str] = []
    boundaries: list[int] = []
    inline: list[InlineStyle] = []
    blocks: list[BlockStyle] = []
    offset = 0
    for _scene_id, separator, content, raw in rows:
        parts.extend((separator, content))
        offset += len(separator)
        formatting = TextFormatting.model_validate(raw or {})
        formatting.validate_for_text(content)
        boundaries.extend(offset + value for value in formatting.boundaries)
        inline.extend(item.model_copy(update={"start": offset + item.start, "end": offset + item.end})
                      for item in formatting.inline)
        blocks.extend(item.model_copy(update={"start": offset + item.start, "end": offset + item.end})
                      for item in formatting.blocks)
        offset += len(content)
    if not rows or "".join(parts) != chapter.content:
        raise ConflictError("章节正文与场景投影不一致，暂时无法阅读或导出", extra={"code": "scene_invariant_error"})
    # 每景的格式与单景容量已独立校验；跨景合并后的只读投影不受单景条目上限约束。
    formatting = TextFormatting.model_construct(version=1, boundaries=sorted(set(boundaries)), inline=inline,
                                                blocks=blocks)
    # 场景边界不一定是自然换行；只在真实格式块的端点处切开可见段落。
    # 全章验证允许这些由场景独立定义的块边界。
    for item in blocks:
        if item.start not in (0, len(chapter.content)) and chapter.content[item.start - 1] not in "\r\n":
            boundaries.append(item.start)
        if item.end not in (0, len(chapter.content)) and chapter.content[item.end] not in "\r\n":
            boundaries.append(item.end)
    formatting.boundaries = sorted(set(boundaries))
    offsets = [*formatting.boundaries, *(p for item in (*inline, *blocks) for p in (item.start, item.end))]
    if any(not is_text_boundary(chapter.content, offset) for offset in offsets):
        raise ConflictError("排版位置无法映射章节接缝，请先检查正文", extra={"code": "scene_invariant_error"})
    # 纯文本章节不创建逐行元数据，避免长旧稿在读取/导出时被重复膨胀。
    # 每景已独立验证；两个空场景可在同一章坐标各有空格式节点，不应被全章去重。
    rendered = prose_blocks(chapter.content, formatting, validate=False) if inline or blocks else None
    return ChapterProjection(chapter, formatting, rendered)


async def load_chapter_projections(
    db: AsyncSession, project_id: int, *, chapter_ids: list[int] | None = None, exclude_generating: bool = False,
    include_formatting: bool = True,
) -> list[ChapterProjection]:
    # 若直接 SELECT Chapter 再与多场景联接，整章正文会被每个场景重复传输一次，
    # 大章会产生二次方级的数据量。窗口函数只在每章首行携带一次完整正文；
    # 其余场景仅携带各自的正文与元数据，仍由一条 SQL 保证同一快照。
    fields = [getattr(Chapter, column.key) for column in Chapter.__mapper__.column_attrs if column.key != "content"]
    first_content = case((func.row_number().over(partition_by=Chapter.id, order_by=(Scene.sort, Scene.id)) == 1,
                          Chapter.content), else_=None)
    formatting = SceneDocumentMeta.formatting if include_formatting else literal(None)
    statement = (select(Chapter, first_content, Scene.id, Scene.separator_before, Scene.content, formatting)
                 .options(load_only(*fields))
                 .outerjoin(Scene, and_(Scene.chapter_id == Chapter.id, Scene.active.is_(True))))
    if include_formatting:
        statement = statement.outerjoin(SceneDocumentMeta, SceneDocumentMeta.scene_id == Scene.id)
    statement = (statement.where(Chapter.project_id == project_id)
                 .order_by(Chapter.chapter_no, Chapter.id, Scene.sort, Scene.id)
                 .execution_options(populate_existing=True))
    if chapter_ids is not None:
        statement = statement.where(Chapter.id.in_(chapter_ids))
    if exclude_generating:
        statement = statement.where(Chapter.status != "generating")
    result = await db.execute(statement)
    grouped: dict[int, tuple[Chapter, list[tuple[int, str, str, dict[str, Any] | None]]]] = {}
    for chapter, chapter_content, scene_id, separator, content, formatting in result:
        if chapter.id not in grouped:
            if chapter_content is None:
                raise ConflictError("章节正文快照不完整，暂时无法阅读或导出", extra={"code": "scene_invariant_error"})
            set_committed_value(chapter, "content", chapter_content)
            grouped[chapter.id] = (chapter, [])
        if scene_id is not None:
            grouped[chapter.id][1].append((scene_id, separator, content, formatting))
    return [chapter_projection(chapter, rows) for chapter, rows in grouped.values()]


def _inline_markup(content: str, runs: list[ReaderRun], encode: Callable[[str, bool], str]) -> str:
    parts = []
    for run in runs:
        text = encode(content[run.start:run.end], run.bold or run.italic)
        if run.italic:
            text = f"<em>{text}</em>"
        if run.bold:
            text = f"<strong>{text}</strong>"
        parts.append(text)
    return "".join(parts)


def render_markdown(content: str, blocks: list[ReaderBlock]) -> str:
    """未格式化的原文逐字透传；新增样式用受限标签，不把用户文本当标签拼接。"""
    def encode(text: str, styled: bool) -> str:
        if not styled:
            return text
        # 标签内还可能被 Markdown 解释的字符一并实体化，显示时仍是作者原文。
        return "".join(f"&#{ord(char)};" if char in r"\\`*_[]!" else escape(char, quote=False) for char in text)

    parts: list[str] = []
    cursor = 0
    previous: ReaderBlock | None = None
    for block in blocks:
        between = content[cursor:block.start]
        parts.append(between)
        newline_count = len(_NEWLINE.findall(between))
        # 引用/列表后的普通行可能被 Markdown 当作惰性延续；必须用空行结束该容器。
        if (previous and previous.kind in {"blockquote", "bullet_list", "ordered_list"}
                and block.kind != previous.kind and newline_count < 2):
            # 单独的 CR 后面直接追加 LF 会合并成一个 CRLF，仍不足以结束容器。
            newline = "\r" if between.endswith("\r") else "\n"
            parts.append(newline * (2 - newline_count))
        # 零宽编辑器块边界没有原始换行；Markdown 的块格式需要一个布局换行。
        elif previous and not between and (previous.kind != "paragraph" or block.kind != "paragraph"):
            parts.append("\n")
        prefix = {"paragraph": "", "heading": "#" * (block.level or 1) + " ", "bullet_list": "- ",
                  "ordered_list": "1. ", "blockquote": "> "}[block.kind]
        parts.append(prefix + _inline_markup(content, block.runs, encode))
        previous, cursor = block, block.end
    parts.append(content[cursor:])
    return "".join(parts)


def render_xhtml(content: str, blocks: list[ReaderBlock], escape_text: Callable[[str], str]) -> str:
    """只生成固定的 XHTML 标签；所有作者文字始终经 XML 转义。"""
    parts: list[str] = []
    list_kind: str | None = None
    previous: ReaderBlock | None = None
    tags = {"bullet_list": "ul", "ordered_list": "ol"}
    for block in blocks:
        kind = block.kind if block.kind in tags else None
        gap = content[previous.end:block.start] if previous else ""
        if list_kind and (kind != list_kind or gap not in ("", "\n", "\r", "\r\n")):
            parts.append(f"</{tags[list_kind]}>")
            list_kind = None
        if kind and not list_kind:
            parts.append(f"<{tags[kind]}>")
            list_kind = kind
        text = _inline_markup(content, block.runs, lambda value, _styled: escape_text(value))
        if kind:
            parts.append(f"<li>{text}</li>")
        elif block.kind == "heading":
            level = block.level or 1
            parts.append(f"<h{level}>{text}</h{level}>")
        elif block.kind == "blockquote":
            parts.append(f"<blockquote><p>{text}</p></blockquote>")
        else:
            parts.append(f"<p>{text}</p>")
        previous = block
    if list_kind:
        parts.append(f"</{tags[list_kind]}>")
    return "\n".join(parts)
