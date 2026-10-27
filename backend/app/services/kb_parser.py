"""知识库文档解析与分块（纯函数，CPU 密集：调用方用 anyio.to_thread.run_sync 运行）。

解析结果统一为「类 Markdown」纯文本：标题行写成 `#… 标题`，段落之间空一行。只保存这份文本（不保存原文件），
重建索引时 chunk_text 从它重新分块。

- txt：依次尝试 utf-8-sig / utf-8 / gb18030（有 UTF-16 BOM 时按 UTF-16）解码，不识别标题。
- md：同上，`#` 开头的行（代码块外）是标题。
- json：键路径作为标题，标量写成「键：值」，列表项写成「- 值」。
- docx：标准库 zipfile + xml.etree 读 word/document.xml；段落样式名为 Heading n / 标题 n（或带大纲级别）的是标题。
- epub：按 OPF spine 顺序读 xhtml，h1–h3 为标题，其余块级元素为段落。
- pdf：pypdf 逐页取文字层；全文有效字数 < 50 判定为扫描件，拒绝（不做 OCR）。
docx / epub 防压缩炸弹（条目数、总解压大小、单条目解压大小上限），XML 拒绝 DTD（防实体膨胀）。
类型按扩展名 + 文件头魔数双重校验；单文件 ≤ 20MB（main 的 UploadLimitMiddleware 在 multipart 解析前拦截超大请求体）；
解析出的文本超过 300 万字截断并给 warning。

分块（chunk_text）：先按标题切段（heading_path「第一卷 > 门派 > 天剑宗」）→ 段内按空行分段，累积到约 500 字为一块，
超长段落按句末标点再切（单句仍超长则硬切）→ 同一段内相邻块重叠约 80 字（从上一块末尾按句子边界截取）。
每块是原文的一个连续子串：text[start_offset:end_offset] == content。
"""

import io
import json
import posixpath
import pyexpat
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from app.core.errors import BadRequestError

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 3_000_000
# docx / epub 解压后读取的内容上限（防压缩炸弹）
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
# 单个被读取的条目（document.xml、xhtml 等）解压后的上限、压缩包条目数上限
MAX_ENTRY_BYTES = 64 * 1024 * 1024
MAX_ZIP_ENTRIES = 10000
PDF_MIN_CHARS = 50
CHUNK_CHARS = 500
OVERLAP_CHARS = 80
HEADING_PATH_CHARS = 500

EXTENSIONS = {
    ".txt": "txt",
    ".text": "txt",
    ".md": "md",
    ".markdown": "md",
    ".json": "json",
    ".docx": "docx",
    ".epub": "epub",
    ".pdf": "pdf",
}
MIME_TYPES = {
    "txt": "text/plain",
    "md": "text/markdown",
    "json": "application/json",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "epub": "application/epub+zip",
    "pdf": "application/pdf",
}
UNSUPPORTED_TYPE = "不支持的文件类型，仅支持 txt、md、json、docx、epub、pdf"
TOO_LARGE = "文件过大，单个文件上限为 20MB"
SCANNED_PDF = "PDF 没有文字层（扫描件不支持）"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_SENTENCE_END = set("。！？!?；;…\n")
_WS_RE = re.compile(r"[ \t　]+")
_BLANK_RE = re.compile(r"\n{3,}")


@dataclass
class ParsedDoc:
    kind: str
    text: str
    warnings: list[str] = field(default_factory=list)

    @property
    def mime(self) -> str:
        return MIME_TYPES[self.kind]


@dataclass
class Chunk:
    seq: int
    heading_path: str
    content: str
    start_offset: int
    end_offset: int


# ---------------- 类型校验 ----------------


def detect_kind(filename: str, data: bytes) -> str:
    """扩展名 + 魔数双重校验；不符一律 400。"""
    ext = posixpath.splitext((filename or "").strip().lower())[1]
    kind = EXTENSIONS.get(ext)
    if kind is None:
        raise BadRequestError(UNSUPPORTED_TYPE)
    head = data[:8]
    if kind in ("docx", "epub"):
        ok = head.startswith(b"PK\x03\x04")
    elif kind == "pdf":
        ok = data[:1024].lstrip().startswith(b"%PDF-")
    else:
        # 文本类：不得是其他已知二进制格式，前 8KB 不得含 NUL（UTF-16 BOM 除外）
        sample = data[:8192]
        is_utf16 = sample.startswith((b"\xff\xfe", b"\xfe\xff"))
        ok = not head.startswith((b"PK\x03\x04", b"%PDF-")) and (is_utf16 or b"\x00" not in sample)
    if not ok:
        raise BadRequestError(f"文件内容与扩展名不符（{ext}），请确认文件类型")
    return kind


# ---------------- 解析 ----------------


def parse(filename: str, data: bytes, *, max_chars: int | None = None, truncate: bool = True) -> ParsedDoc:
    """解析为类 Markdown 文本。超过 max_chars（默认 MAX_TEXT_CHARS）：truncate=True 截断并给 warning（知识库），
    否则 400（拆书导入）。"""
    max_chars = MAX_TEXT_CHARS if max_chars is None else max_chars
    if len(data) > MAX_FILE_BYTES:
        raise BadRequestError(TOO_LARGE)
    if not data:
        raise BadRequestError("文件为空")
    kind = detect_kind(filename, data)
    try:
        text = _PARSERS[kind](data)
    except BadRequestError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BadRequestError(f"文件解析失败：{_describe(exc)}") from exc
    text = clean_text(text)
    warnings: list[str] = []
    if len(text) > max_chars:
        if not truncate:
            raise BadRequestError(text_too_long(max_chars))
        text = text[:max_chars]
        warnings.append(f"解析出的文本超过 {max_chars // 10000} 万字，已截断")
    if not text.strip():
        raise BadRequestError("文件中没有可用的文字内容")
    return ParsedDoc(kind=kind, text=text, warnings=warnings)


def text_too_long(max_chars: int) -> str:
    return f"解析出的文本超过 {max_chars // 10000} 万字，无法导入"


def _describe(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:200]


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [ln.rstrip() for ln in text.split("\n")]
    return _BLANK_RE.sub("\n\n", "\n".join(lines)).strip()


# 常用汉字（简繁两种写法都列入）：GB18030 与 Big5 都能解码同一段字节时，按命中率判断哪种是对的
_COMMON_HAN = set(
    "的一是不了人我在有他这這个個们們中来來上大为為和国國地到以说說时時要就出会會可也你对對生能而子那得于於"
    "着著下自之年过過发發后後作里裡用道行所然家种種事成方多经經么麼去法学學如都同现現当當没沒动動面起看定天分"
    "还還进進好小部其些主样樣理心她本前开開但因只从從想实實日军軍者意无無力它与與长長把机機十民第公此已工使"
)


def _han_score(text: str) -> float:
    han = [c for c in text[:20000] if "\u4e00" <= c <= "\u9fff"]
    return sum(c in _COMMON_HAN for c in han) / len(han) if han else 0.0


def decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    candidates: list[str] = []
    # Big5 的双字节编码区几乎都落在 GB18030 的合法范围内（解码成功但是乱码），所以两种都能解时比较常用字命中率
    for enc in ("gb18030", "big5"):
        try:
            candidates.append(data.decode(enc))
        except UnicodeDecodeError:
            continue
    if not candidates:
        raise BadRequestError("无法识别文本编码（支持 UTF-8、GB18030 与 Big5）")
    return max(candidates, key=_han_score) if len(candidates) > 1 else candidates[0]


def _parse_txt(data: bytes) -> str:
    return decode_text(data)


def _parse_md(data: bytes) -> str:
    return decode_text(data)


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parse_json(data: bytes) -> str:
    try:
        obj = json.loads(decode_text(data))
    except json.JSONDecodeError as exc:
        raise BadRequestError(f"JSON 格式不正确：第 {exc.lineno} 行第 {exc.colno} 列") from exc
    out: list[str] = []

    def walk(value: Any, depth: int, title: str | None) -> None:
        if title is not None:
            out.append(f"{'#' * min(depth, 6)} {title}")
        if isinstance(value, dict):
            scalars = [(k, v) for k, v in value.items() if not isinstance(v, dict | list)]
            if scalars:
                out.append("\n".join(f"{k}：{_scalar(v)}" for k, v in scalars))
            for k, v in value.items():
                if isinstance(v, dict | list):
                    walk(v, depth + 1, str(k))
        elif isinstance(value, list):
            items = [v for v in value if not isinstance(v, dict | list)]
            if items:
                out.append("\n".join(f"- {_scalar(v)}" for v in items))
            nested = [v for v in value if isinstance(v, dict | list)]
            for i, v in enumerate(nested, start=1):
                name = _item_title(v) or f"第 {i} 项"
                walk(v, depth + 1, name)
        else:
            out.append(_scalar(value))

    walk(obj, 0, None)
    return "\n\n".join(p for p in out if p.strip())


def _item_title(value: Any) -> str:
    """列表里的对象优先用 name / title 字段作为标题。"""
    if isinstance(value, dict):
        for key in ("name", "title", "名称", "标题"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()[:100]
    return ""


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BadRequestError("文件已损坏，无法解压") from exc
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
        raise BadRequestError("文件解压后过大，无法处理")
    return zf


def _read(zf: zipfile.ZipFile, name: str) -> bytes:
    """读取一个条目；声明的解压大小超限 → 400（zipfile 按声明大小读取，实际更大时 CRC 校验失败）。"""
    try:
        info = zf.getinfo(name)
    except KeyError as exc:
        raise BadRequestError(f"文件结构不完整：缺少 {name}") from exc
    if info.file_size > MAX_ENTRY_BYTES:
        raise BadRequestError("文件解压后过大，无法处理")
    return zf.read(info)


class _RootReached(Exception):
    pass


def _xml(data: bytes) -> ET.Element:
    """解析 docx / epub 中的 XML；拒绝 DTD（实体声明可用于实体膨胀攻击，正常的 docx / OPF 不会带）。"""
    probe = pyexpat.ParserCreate()

    def reject_doctype(*_args: Any) -> None:
        raise BadRequestError("文件包含不支持的 XML 文档类型声明（DTD），无法解析")

    def stop(*_args: Any) -> None:
        # DTD 只能出现在根元素之前：读到根元素即可停止预检
        raise _RootReached

    probe.StartDoctypeDeclHandler = reject_doctype
    probe.StartElementHandler = stop
    try:
        probe.Parse(data, True)
    except _RootReached:
        pass
    except pyexpat.ExpatError:
        pass  # 格式错误交给下面的正式解析报错
    return ET.fromstring(data)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_HEADING = re.compile(r"^(?:heading|标题)\s*(\d)$", re.IGNORECASE)


def _docx_styles(zf: zipfile.ZipFile) -> dict[str, int]:
    """styleId → 标题级别（1 起）。样式名为 Heading n / 标题 n / Title，或样式带大纲级别。"""
    if "word/styles.xml" not in zf.namelist():
        return {}
    levels: dict[str, int] = {}
    root = _xml(_read(zf, "word/styles.xml"))
    for style in root.iter(f"{_W}style"):
        sid = style.get(f"{_W}styleId") or ""
        name_el = style.find(f"{_W}name")
        name = (name_el.get(f"{_W}val") if name_el is not None else "") or ""
        m = _DOCX_HEADING.match(name.strip()) or _DOCX_HEADING.match(sid.strip())
        if m:
            levels[sid] = int(m.group(1))
            continue
        if name.strip().lower() == "title":
            levels[sid] = 1
            continue
        lvl = style.find(f"{_W}pPr/{_W}outlineLvl")
        if lvl is not None and (lvl.get(f"{_W}val") or "").isdigit() and int(lvl.get(f"{_W}val")) < 9:
            levels[sid] = int(lvl.get(f"{_W}val")) + 1
    return levels


def _docx_paragraph_text(p: ET.Element) -> str:
    parts: list[str] = []
    for el in p.iter():
        if el.tag == f"{_W}t":
            parts.append(el.text or "")
        elif el.tag == f"{_W}tab":
            parts.append("\t")
        elif el.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return "".join(parts).strip()


def _parse_docx(data: bytes) -> str:
    zf = _open_zip(data)
    styles = _docx_styles(zf)
    root = _xml(_read(zf, "word/document.xml"))
    body = root.find(f"{_W}body")
    if body is None:
        return ""
    out: list[str] = []
    for p in body.iter(f"{_W}p"):
        text = _docx_paragraph_text(p)
        if not text:
            continue
        level = 0
        style = p.find(f"{_W}pPr/{_W}pStyle")
        if style is not None:
            level = styles.get(style.get(f"{_W}val") or "", 0)
            if not level:
                m = _DOCX_HEADING.match((style.get(f"{_W}val") or "").strip())
                level = int(m.group(1)) if m else 0
        outline = p.find(f"{_W}pPr/{_W}outlineLvl")
        if not level and outline is not None and (outline.get(f"{_W}val") or "").isdigit():
            val = int(outline.get(f"{_W}val") or 9)
            level = val + 1 if val < 9 else 0
        if level:
            out.append(f"{'#' * min(level, 6)} {' '.join(text.split())}")
        else:
            out.append(text)
    return "\n\n".join(out)


class _XhtmlText(HTMLParser):
    """h1–h3 → 标题行；p / li / div / blockquote / h4–h6 等块级元素 → 段落；忽略 script / style / head。"""

    BLOCKS = {"p", "li", "div", "blockquote", "h4", "h5", "h6", "tr", "dd", "dt", "pre", "section", "article"}
    HEADINGS = {"h1": 1, "h2": 2, "h3": 3}
    SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.buf: list[str] = []
        self.heading: int = 0
        self.skip = 0

    def _flush(self) -> None:
        text = " ".join("".join(self.buf).split()) if self.heading else "".join(self.buf).strip()
        self.buf = []
        if text:
            self.out.append(f"{'#' * self.heading} {text}" if self.heading else text)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.HEADINGS:
            self._flush()
            self.heading = self.HEADINGS[tag]
        elif tag in self.BLOCKS:
            self._flush()
        elif tag == "br":
            self.buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.HEADINGS:
            self._flush()
            self.heading = 0
        elif tag in self.BLOCKS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.buf.append(data)

    def text(self) -> str:
        self._flush()
        return "\n\n".join(self.out)


def epub_documents(data: bytes) -> tuple[str | None, list[str]]:
    """按 OPF spine 顺序读出每个文档的文字（类 Markdown，可能为空串），以及 OPF 里的 dc:title（没有为 None）。"""
    zf = _open_zip(data)
    container = _xml(_read(zf, "META-INF/container.xml"))
    rootfile = next((el for el in container.iter() if el.tag.endswith("rootfile")), None)
    opf_path = rootfile.get("full-path") if rootfile is not None else None
    if not opf_path:
        raise BadRequestError("EPUB 结构不完整：找不到 OPF 文件")
    opf = _xml(_read(zf, opf_path))
    title_el = next((el for el in opf.iter() if el.tag.endswith("}title") or el.tag == "title"), None)
    title = " ".join((title_el.text or "").split()) if title_el is not None else ""
    base = posixpath.dirname(opf_path)
    manifest = {
        el.get("id"): el.get("href") for el in opf.iter() if el.tag.endswith("item") and el.get("id") and el.get("href")
    }
    spine = [el.get("idref") for el in opf.iter() if el.tag.endswith("itemref") and el.get("idref")]
    docs: list[str] = []
    names = set(zf.namelist())
    for idref in spine:
        href = manifest.get(idref)
        if not href:
            continue
        path = posixpath.normpath(posixpath.join(base, unquote(href.split("#", 1)[0])))
        if path not in names:
            continue
        parser = _XhtmlText()
        parser.feed(decode_text(_read(zf, path)))
        docs.append(parser.text())
    return title or None, docs


def _parse_epub(data: bytes) -> str:
    return "\n\n".join(t for t in epub_documents(data)[1] if t.strip())


def _parse_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise BadRequestError("PDF 已加密，无法读取")
        pages: list[str] = []
        total = 0
        for page in reader.pages:
            text = (page.extract_text() or "").strip()
            pages.append(text)
            total += len(text)
            # 已超过文本上限（之后由 parse 截断并给 warning），不再继续提取剩余页
            if total > MAX_TEXT_CHARS:
                break
    except PdfReadError as exc:
        raise BadRequestError(f"PDF 解析失败：{_describe(exc)}") from exc
    text = "\n\n".join(p for p in pages if p)
    if len(re.sub(r"\s", "", text)) < PDF_MIN_CHARS:
        raise BadRequestError(SCANNED_PDF)
    return text


_PARSERS = {
    "txt": _parse_txt,
    "md": _parse_md,
    "json": _parse_json,
    "docx": _parse_docx,
    "epub": _parse_epub,
    "pdf": _parse_pdf,
}


# ---------------- 分块 ----------------


@dataclass
class _Section:
    path: str
    spans: list[tuple[int, int]] = field(default_factory=list)


def _sections(text: str) -> list[_Section]:
    """按标题切段；每段的段落（空行分隔）以原文偏移表示。代码块内的 # 不算标题。"""
    sections: list[_Section] = [_Section(path="")]
    stack: list[tuple[int, str]] = []
    pos = 0
    in_fence = False
    para_start: int | None = None
    para_end = 0

    def close_para() -> None:
        nonlocal para_start
        if para_start is not None:
            sections[-1].spans.append((para_start, para_end))
            para_start = None

    for line in text.split("\n"):
        start, end = pos, pos + len(line)
        pos = end + 1
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING_RE.match(line)
        if m:
            close_para()
            level, title = len(m.group(1)), " ".join(m.group(2).split())
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            sections.append(_Section(path=" > ".join(t for _, t in stack)[:HEADING_PATH_CHARS]))
            continue
        if not line.strip():
            if not in_fence:
                close_para()
            continue
        if para_start is None:
            para_start = start + (len(line) - len(line.lstrip()))
        para_end = end
    close_para()
    return [s for s in sections if s.spans]


def _sentence_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """把超长段落按句末标点切成 ≤ CHUNK_CHARS 的片段（单句超长时硬切）。"""
    out: list[tuple[int, int]] = []
    s = start
    for i in range(start, end):
        if text[i] in _SENTENCE_END:
            out.append((s, i + 1))
            s = i + 1
    if s < end:
        out.append((s, end))
    pieces: list[tuple[int, int]] = []
    for a, b in out:
        while b - a > CHUNK_CHARS:
            pieces.append((a, a + CHUNK_CHARS))
            a += CHUNK_CHARS
        if b > a:
            pieces.append((a, b))
    return pieces


def _overlap_start(text: str, prev_start: int, prev_end: int) -> int:
    """从上一块末尾截取约 OVERLAP_CHARS 字：取距末尾 ≤ 80 字的最早句子边界；末句超过 80 字时硬取最后 80 字。"""
    lower = max(prev_start, prev_end - OVERLAP_CHARS)
    for i in range(lower, prev_end - 1):
        if i > prev_start and text[i - 1] in _SENTENCE_END:
            return i
    return lower


def _trim(text: str, a: int, b: int) -> tuple[int, int]:
    while a < b and text[a].isspace():
        a += 1
    while b > a and text[b - 1].isspace():
        b -= 1
    return a, b


def chunk_text(text: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section in _sections(text):
        units: list[tuple[int, int]] = []
        for a, b in section.spans:
            units.extend(_sentence_spans(text, a, b) if b - a > CHUNK_CHARS else [(a, b)])
        groups: list[tuple[int, int]] = []
        cur: tuple[int, int] | None = None
        for a, b in units:
            if cur is None:
                cur = (a, b)
            elif b - cur[0] <= CHUNK_CHARS:
                cur = (cur[0], b)
            else:
                groups.append(cur)
                cur = (a, b)
        if cur is not None:
            groups.append(cur)
        prev: tuple[int, int] | None = None
        for a, b in groups:
            start = _overlap_start(text, *prev) if prev is not None else a
            s, e = _trim(text, start, b)
            if e > s:
                chunks.append(
                    Chunk(
                        seq=len(chunks) + 1, heading_path=section.path, content=text[s:e], start_offset=s, end_offset=e
                    )
                )
            prev = (a, b)
    return chunks


def retrieval_text(heading_path: str, content: str) -> str:
    """检索文本：标题路径参与检索。"""
    return f"{heading_path}\n{content}" if heading_path else content
