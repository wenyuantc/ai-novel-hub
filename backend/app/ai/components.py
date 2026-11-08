"""提示词组件引用：`{{> 标识 }}` 的解析与展开（纯函数）。

- 展开是单次文本替换，不递归；组件内容里再出现引用视为错误（不支持嵌套）。
- 没有引用时原样返回（`{{>` 在 Jinja 中本是语法错误，旧的覆盖与 Skill 正文不可能包含它），保证渲染结果逐字不变。
- 展开结果记录每段文本在原文中的位置，供把展开后校验问题的行号映射回原文，并标出来自哪个组件。
"""

import re
from dataclasses import dataclass, field

REF_RE = re.compile(r"\{\{>\s*([a-z0-9][a-z0-9-]{0,47})\s*\}\}")
# 粗匹配：任何 `{{>` 开头的写法（用于发现标识不合法的引用）
_LOOSE_RE = re.compile(r"\{\{>")
NESTED = "组件内不能引用其他组件"


def has_refs(text: str) -> bool:
    return bool(text) and "{{>" in text


def _line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def find_refs(text: str) -> list[tuple[str, int]]:
    """按出现顺序返回 (标识, 行号)。"""
    if not has_refs(text):
        return []
    return [(m.group(1), _line_at(text, m.start())) for m in REF_RE.finditer(text)]


def ref_slugs(text: str) -> list[str]:
    """去重后的引用标识（保持首次出现顺序）。"""
    return list(dict.fromkeys(slug for slug, _ in find_refs(text)))


def bad_ref_lines(text: str) -> list[int]:
    """`{{>` 开头但不是合法引用的位置（行号）。"""
    if not has_refs(text):
        return []
    valid = {m.start() for m in REF_RE.finditer(text)}
    return [_line_at(text, m.start()) for m in _LOOSE_RE.finditer(text) if m.start() not in valid]


@dataclass(frozen=True)
class _Piece:
    exp_start: int  # 在展开结果中的起止偏移
    exp_end: int
    orig_start: int  # 在原文中的起始偏移（组件片段为引用所在位置）
    slug: str | None  # None = 原文片段


@dataclass
class Expansion:
    text: str
    source: str
    pieces: list[_Piece] = field(default_factory=list)
    # (原文行号, 标识, 说明)：引用了不存在的组件、组件内含引用等
    errors: list[tuple[int, str, str]] = field(default_factory=list)
    used: list[str] = field(default_factory=list)

    def map_line(self, line: int | None) -> tuple[int | None, str | None]:
        """展开结果的行号 → (原文行号, 来自的组件标识或 None)。"""
        if line is None or not self.pieces:
            return line, None
        # 定位展开结果中该行的起止偏移
        start = 0
        for _ in range(line - 1):
            nxt = self.text.find("\n", start)
            if nxt < 0:
                return line, None
            start = nxt + 1
        end = self.text.find("\n", start)
        end = len(self.text) if end < 0 else end
        for piece in self.pieces:
            if piece.slug is None or piece.exp_end == piece.exp_start:
                continue
            hit = piece.exp_start < end and piece.exp_end > start if end > start else (
                piece.exp_start <= start < piece.exp_end
            )
            if hit:
                return _line_at(self.source, piece.orig_start), piece.slug
        for piece in self.pieces:
            at_end = piece.exp_end == start == len(self.text)
            if piece.slug is None and (piece.exp_start <= start < piece.exp_end or at_end):
                return _line_at(self.source, piece.orig_start + (start - piece.exp_start)), None
        return line, None


def expand(text: str, contents: dict[str, str]) -> Expansion:
    """按引用处原样展开；不存在的组件替换为空并记错误，组件内容中的引用不再展开并记错误。"""
    if not has_refs(text):
        return Expansion(text=text, source=text)
    out: list[str] = []
    pieces: list[_Piece] = []
    errors: list[tuple[int, str, str]] = []
    used: list[str] = []
    pos = 0  # 原文游标
    size = 0  # 展开结果长度

    def add(chunk: str, orig_start: int, slug: str | None) -> None:
        nonlocal size
        pieces.append(_Piece(size, size + len(chunk), orig_start, slug))
        out.append(chunk)
        size += len(chunk)

    for line in bad_ref_lines(text):
        errors.append((line, "", "组件引用格式不正确，应为 {{> 标识 }}（标识为小写字母、数字和连字符）"))
    for m in REF_RE.finditer(text):
        if m.start() > pos:
            add(text[pos : m.start()], pos, None)
        slug = m.group(1)
        line = _line_at(text, m.start())
        if slug not in used:
            used.append(slug)
        content = contents.get(slug)
        if content is None:
            errors.append((line, slug, f"引用的组件不存在：{slug}"))
            content = ""
        elif has_refs(content):
            errors.append((line, slug, f"组件 {slug}：{NESTED}"))
        add(content, m.start(), slug)
        pos = m.end()
    if pos < len(text):
        add(text[pos:], pos, None)
    return Expansion(text="".join(out), source=text, pieces=pieces, errors=errors, used=used)


class ComponentRefError(ValueError):
    """严格展开失败：引用了不存在的组件或组件内含引用。"""

    def __init__(self, slug: str, line: int, message: str):
        super().__init__(message)
        self.slug = slug
        self.line = line
        self.message = message


def expand_lenient(text: str, contents: dict[str, str]) -> str:
    """只替换能解析到的引用，其余（不存在的标识、格式不对的写法）原样保留。

    用于 Skill 运行时：迁移前的旧正文里可能本来就有 `{{> x }}` 字面文本（例如介绍 Handlebars 的写法），
    不能因此让 Skill 整体不可用；保存时仍走严格校验。
    """
    if not has_refs(text):
        return text
    return REF_RE.sub(lambda m: contents.get(m.group(1), m.group(0)), text)


def expand_strict(text: str, contents: dict[str, str]) -> str:
    result = expand(text, contents)
    if result.errors:
        line, slug, message = result.errors[0]
        raise ComponentRefError(slug, line, message)
    return result.text
