"""拆书切章与校正（纯函数，不依赖数据库；CPU 密集，调用方放到线程里执行）。

章节条目 {title, segments: [[start, end], ...], volume_no, volume_title, source_no}：segments 是指向全文的半开区间，
按顺序排列，拼接处在正文里插入一个空行（"\\n\\n"）；source_no 是从标题解析出的原始序号（只用于「序号断档」提示）。

切章规则（只用强规则，不用「短行 + 前后空行」这类弱规则；标题行去掉首尾空白后 ≤ 50 字）：
- 卷：第X卷 / 部 / 集（X 为阿拉伯或中文数字，后面须有分隔符或直接结束）；同一行「第一卷 第一章 雨夜」先开卷再开章。
- 章：第X章 / 回 / 节、Chapter N / Chap. N，以及序章、序言、楔子、引子、前言、尾声、后记、终章、番外。
  「第X节」后面没有分隔符而直接跟带逗号句号的文字（如「第一节课下课后，……」）不算标题。
- 序章：第一个标题之前的正文去掉空白后 ≥ 200 字时保留为「序章」（不归属任何卷），否则丢弃。
- 目录区：从第一个标题起连续 ≥ 5 个章标题、标题之间去掉空白后的正文都 < 50 字，整段去掉并记入提示。
- 兜底：一个章标题都没有时按 3000–5000 字一块、在句末断开，标题为「第 N 段」。
- md / docx：`#` 标题行去掉 `#` 再参与匹配；标题行里没有强规则章标题时，取最常见的标题层级为章、高一级为卷。
- epub：按书脊文档切，每个文档一章；文档内有 ≥ 2 个强规则章标题时再细分；去掉空白后 < 50 字的文档跳过；
  只有一行且匹配卷规则的文档是卷标记。
- 自定义正则：≤ 100 字符、拒绝嵌套量词，只匹配 ≤ 60 字的行，在 spawn 子进程中执行并有超时保护；
  匹配到的行作为章标题（不再识别卷），序章与目录区规则照常适用。

分卷的表示：volume_no 连续相同的章节属于同一卷；有卷名的卷从 1 起编号，排在第一个卷之前、没有卷名的章节
volume_no 为 0（无分卷）；一个有卷名的卷都没有时全部为 volume_no = 1、volume_title = ""。
"""

import multiprocessing
import posixpath
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import BadRequestError
from app.services import kb_parser
from app.services.book_import import regex_worker

MAX_TEXT_CHARS = 5_000_000
MAX_IMPORT_CHAPTERS = 3000
HEADING_MAX = 50
REGEX_LINE_MAX = 60
REGEX_MAX_LEN = 100
# 自定义正则在子进程中的执行上限（秒）；测试里改小
REGEX_TIMEOUT_SECONDS = 5.0
# 同时执行自定义正则的子进程上限（run_regex 在线程池里调用）
REGEX_MAX_WORKERS = 2
_REGEX_SLOTS = threading.BoundedSemaphore(REGEX_MAX_WORKERS)
PROLOGUE_MIN = 200
TOC_MIN_HEADINGS = 5
TOC_GAP_MAX = 50
CHUNK_MIN = 3000
CHUNK_MAX = 5000
EPUB_DOC_MIN = 50
SHORT_CHARS = 300
LONG_CHARS = 20000
TITLE_LIMIT = 200
FIRST_LINE_LIMIT = 60

EXTENSIONS = {".txt": "txt", ".text": "txt", ".md": "md", ".markdown": "md", ".docx": "docx", ".epub": "epub"}
UNSUPPORTED_TYPE = "不支持的文件类型，拆书导入仅支持 txt、md、docx、epub"
WARNING_KINDS = ("short", "long", "dup_title", "gap")

_N = "0-9０-９零〇一二两三四五六七八九十百千万"
_SEP = r"\s:：·、.\-—"
_VOLUME_RE = re.compile(rf"^第([{_N}]+)[卷部集](?:[{_SEP}]+(.*))?$")
_CHAPTER_RE = re.compile(rf"^第([{_N}]+)[章回节]([{_SEP}]*)(.*)$")
_ENGLISH_RE = re.compile(r"^(?:chapter|chap\.?)\s*(\d+)\b.*$", re.IGNORECASE)
_SPECIAL_RE = re.compile(rf"^(?:序章|序言|楔子|引子|前言|尾声|后记|终章)(?:[{_SEP}].*)?$")
_EXTRA_RE = re.compile(r"^番外[^，。！？!?；;]*$")
# 标题前缀（比较「标题重复」时去掉）
_NO_PREFIX_RE = re.compile(rf"^(?:第[{_N}]+[章回节卷部集]|chapter\s*\d+|chap\.?\s*\d+)[{_SEP}]*", re.IGNORECASE)
_MARK_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SENTENCE_PUNCT = re.compile(r"[，。！？；,;!?]")
_SENTENCE_END = set("。！？!?…\n")
_CN_DIGITS = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9), strict=True))
_WS_RE = re.compile(r"\s+")
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def count_words(text: str) -> int:
    """字数：按非空白字符计（与 chapter_service.count_words 同一规则；这里不导入它，保持本模块不依赖数据库层）。"""
    return len(_WS_RE.sub("", text or ""))


# ---------------- 数字与标题识别 ----------------


def cn_to_int(value: str) -> int | None:
    """阿拉伯 / 全角 / 中文数字 → 整数（「一〇三」按位读，「一千二百三十四」按单位读）；无法解析返回 None。"""
    value = value.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if value.isdigit():
        return int(value)
    if not any(c in _CN_UNITS or c == "万" for c in value):
        digits = [_CN_DIGITS.get(c) for c in value]
        return int("".join(str(d) for d in digits)) if digits and None not in digits else None
    total = section = num = 0
    for c in value:
        if c in _CN_DIGITS:
            num = _CN_DIGITS[c]
        elif c in _CN_UNITS:
            section += (num or 1) * _CN_UNITS[c]
            num = 0
        elif c == "万":
            total += (section + num) * 10000
            section = num = 0
        else:
            return None
    return total + section + num


def _chapter(line: str) -> tuple[str, int | None] | None:
    m = _CHAPTER_RE.match(line)
    if m:
        sep, rest = m.group(2), m.group(3).strip()
        if line.endswith(("。", "，", "；", ",", ";")):
            return None
        # 「第一节课下课后，……」：序号后没有分隔符、直接接带标点的句子，是正文不是标题
        if not sep and rest and _SENTENCE_PUNCT.search(rest):
            return None
        return line, cn_to_int(m.group(1))
    m = _ENGLISH_RE.match(line)
    if m:
        return line, int(m.group(1))
    if _SPECIAL_RE.match(line) or _EXTRA_RE.match(line):
        return line, None
    return None


def classify(raw: str) -> tuple[str | None, str | None, int | None] | None:
    """一行是否为标题：(章标题 | None, 卷标题 | None, 原始序号)；不是标题返回 None。"""
    line = raw.strip()
    if not line or len(line) > HEADING_MAX:
        return None
    m = _VOLUME_RE.match(line)
    if m:
        if line.endswith(("。", "，")):
            return None
        rest = (m.group(2) or "").strip()
        if rest:
            ch = _chapter(rest)
            if ch is not None:
                return ch[0], line[: m.start(2)].strip(), ch[1]
        return None, line, None
    ch = _chapter(line)
    return (ch[0], None, ch[1]) if ch is not None else None


def source_no(title: str) -> int | None:
    result = classify(title)
    return result[2] if result is not None else None


def strip_number(title: str) -> str:
    """去掉「第N章」「Chapter N」等序号前缀（比较标题是否重复用）。"""
    return _NO_PREFIX_RE.sub("", title.strip()).strip()


def _strip_mark(line: str) -> tuple[str, int]:
    """去掉 Markdown 标题标记：(文本, 标题级别；不是标题为 0)。"""
    m = _MARK_RE.match(line.strip())
    return (m.group(2).strip(), len(m.group(1))) if m else (line.strip(), 0)


# ---------------- 文本工具 ----------------


def _lines(text: str, start: int = 0, end: int | None = None) -> list[tuple[int, int, str]]:
    """[start, end) 内每一行的 (行首偏移, 行尾偏移, 行文本)。"""
    end = len(text) if end is None else end
    out: list[tuple[int, int, str]] = []
    pos = start
    for line in text[start:end].split("\n"):
        out.append((pos, pos + len(line), line))
        pos += len(line) + 1
    return out


def _span(text: str, start: int, end: int) -> list[list[int]]:
    """去掉首尾空行后的区间；为空返回 []。"""
    while start < end and text[start] in "\n":
        start += 1
    while end > start and text[end - 1] in "\n ":
        end -= 1
    return [[start, end]] if end > start else []


def body_text(text: str, segments: list[list[int]]) -> str:
    return "\n\n".join(text[s:e] for s, e in segments)


def _chars(text: str, segments: list[list[int]]) -> int:
    return sum(count_words(text[s:e]) for s, e in segments)


def _entry(title: str, segments: list[list[int]], volume_no: int, volume_title: str, no: int | None) -> dict[str, Any]:
    return {
        "title": title.strip()[:TITLE_LIMIT],
        "segments": segments,
        "volume_no": volume_no,
        "volume_title": volume_title.strip()[:TITLE_LIMIT],
        "source_no": no,
    }


# ---------------- 切章 ----------------


@dataclass
class Heading:
    start: int
    end: int
    chapter: str | None
    volume: str | None
    source_no: int | None
    # 来自 Markdown 标题行（`#`）
    marked: bool = False


def _strong_headings(text: str, start: int = 0, end: int | None = None, *, strip_marks: bool) -> list[Heading]:
    out: list[Heading] = []
    for s, e, line in _lines(text, start, end):
        content, level = _strip_mark(line) if strip_marks else (line, 0)
        result = classify(content)
        if result is not None:
            out.append(Heading(s, e, result[0], result[1], result[2], marked=level > 0))
    return out


def _level_headings(text: str) -> list[Heading]:
    """docx / md 的标题行都不匹配强规则时：最常见的标题层级为章、高一级为卷，更高的忽略。不足 2 章返回 []。"""
    marks = [(s, e, *_strip_mark(line)) for s, e, line in _lines(text)]
    marks = [m for m in marks if m[3] > 0]
    if not marks:
        return []
    counts = Counter(m[3] for m in marks)
    # 次数相同时取更深的层级（章节粒度更细）
    level = max(counts, key=lambda lv: (counts[lv], lv))
    if counts[level] < 2:
        return []
    out: list[Heading] = []
    for s, e, title, lv in marks:
        if lv == level:
            out.append(Heading(s, e, title, None, source_no(title), marked=True))
        elif lv == level - 1:
            out.append(Heading(s, e, None, title, None, marked=True))
    return out


def _gap(text: str, headings: list[Heading], i: int, end: int) -> int:
    nxt = headings[i + 1].start if i + 1 < len(headings) else end
    return count_words(text[headings[i].end : nxt])


def _drop_toc(
    text: str, headings: list[Heading], end: int, notices: list[str]
) -> tuple[list[Heading], list[list[int]]]:
    """从第一个标题起连续 ≥ 5 个章标题、之间几乎没有正文 → 目录区，去掉。紧挨正文第一章的卷标题属于正文。

    目录最后一项后面常紧跟引言（正文不短），它的标题会在后文再次出现：这一项也属于目录，
    其后的文字作为序章候选返回（第二个返回值，未去目录或没有引言时为 []）。
    """
    run = 0
    while run < len(headings) and _gap(text, headings, run, end) < TOC_GAP_MAX:
        run += 1
    if run >= len(headings):
        # 全部标题都几乎没有正文：不是目录，保持原样（之后按章节过短提示）
        return headings, []
    intro: list[list[int]] = []
    last = headings[run]
    if (
        run >= TOC_MIN_HEADINGS - 1
        and last.chapter
        and run + 1 < len(headings)
        and any(h.chapter and _same_title(h.chapter, last.chapter) for h in headings[run + 1 :])
    ):
        intro = _span(text, last.end + 1, headings[run + 1].start)
        run += 1
    real = run
    while real > 0 and headings[real - 1].chapter is None:
        real -= 1
    toc = headings[:real]
    if sum(1 for h in toc if h.chapter) < TOC_MIN_HEADINGS:
        return headings, []
    lines = text[toc[0].start : (toc[-1].end if intro else headings[real].start)].count("\n") + (1 if intro else 0)
    notices.append(f"已去除目录区 {lines} 行")
    return headings[real:], intro


def _same_title(a: str, b: str) -> bool:
    return "".join(a.split()) == "".join(b.split())


def _assemble(
    text: str,
    headings: list[Heading],
    start: int,
    end: int,
    volume: tuple[int, str],
    notices: list[str],
    *,
    pre_title: str = "序章",
) -> tuple[list[dict[str, Any]], tuple[int, str]]:
    """按标题把 [start, end) 切成章节；volume 为进入时的 (卷号, 卷名)，返回 (章节, 结束时的卷)。"""
    pre_end = headings[0].start
    headings, intro = _drop_toc(text, headings, end, notices)
    vol_no, vol_title = volume
    chapters: list[dict[str, Any]] = []
    # 目录之后的引言优先作为序章；没有引言时取第一个标题之前的文字
    pre = intro or _span(text, start, pre_end)
    if pre and _chars(text, pre) >= PROLOGUE_MIN:
        # 第一个标题之前的正文：位于第一个卷标题之前时不归属任何卷
        chapters.append(_entry(pre_title, pre, vol_no, vol_title, None))
    for i, h in enumerate(headings):
        nxt = headings[i + 1].start if i + 1 < len(headings) else end
        body = _span(text, min(h.end + 1, nxt), nxt)
        if h.volume is not None:
            vol_no, vol_title = vol_no + 1, h.volume
            if h.chapter is None:
                # 卷标题与本卷第一章之间的文字（卷首语）够长时单独成章
                if body and _chars(text, body) >= PROLOGUE_MIN:
                    chapters.append(_entry(vol_title, body, vol_no, vol_title, None))
                continue
        chapters.append(_entry(h.chapter or "", body, vol_no, vol_title, h.source_no))
    return chapters, (vol_no, vol_title)


def chunk_text(text: str, start: int = 0, end: int | None = None) -> list[dict[str, Any]]:
    """兜底切块：累计到 3000 字后在 5000 字以内的第一个句末断开，找不到句末时在 5000 字处硬切。"""
    end = len(text) if end is None else end
    chapters: list[dict[str, Any]] = []
    pos = start
    while pos < end:
        cut = end
        if end - pos > CHUNK_MAX:
            cut = pos + CHUNK_MAX
            for i in range(pos + CHUNK_MIN, pos + CHUNK_MAX):
                if text[i] in _SENTENCE_END:
                    cut = i + 1
                    break
        seg = _span(text, pos, cut)
        if seg:
            chapters.append(_entry(f"第 {len(chapters) + 1} 段", seg, 1, "", None))
        pos = cut
    return chapters


def _split_epub(text: str, doc_spans: list[list[int]], notices: list[str]) -> list[dict[str, Any]]:
    chapters: list[dict[str, Any]] = []
    volume = (0, "")
    short_docs = toc_docs = 0
    for start, end in doc_spans:
        lines = [(s, e, *_strip_mark(line)) for s, e, line in _lines(text, start, end)]
        lines = [x for x in lines if x[2]]
        if not lines:
            continue
        first = classify(lines[0][2])
        if len(lines) == 1 and first is not None and first[0] is None:
            # 只有一行卷标题的文档：卷标记
            volume = (volume[0] + 1, first[1] or "")
            continue
        if count_words(text[start:end]) < EPUB_DOC_MIN:
            short_docs += 1
            continue
        heads = _strong_headings(text, start, end, strip_marks=True)
        if sum(1 for h in heads if h.chapter) >= 2:
            sub, vol = _assemble(text, heads, start, end, volume, notices, pre_title=lines[0][2])
            if sub and all(_chars(text, c["segments"]) < EPUB_DOC_MIN for c in sub):
                # 每个「章」都几乎没有正文：目录文档
                toc_docs += 1
                continue
            chapters.extend(sub)
            volume = vol
            continue
        # 整个文档为一章；标题依次取：第一个 # 标题、第一个强规则标题行、第一个 ≤ 50 字的行
        body_start = start
        if first is not None and first[0] is None:
            volume = (volume[0] + 1, first[1] or "")
            body_start = lines[0][1] + 1
            lines = lines[1:]
        pick = next((x for x in lines if x[3] > 0), None)
        pick = pick or next((x for x in lines if (r := classify(x[2])) is not None and r[0]), None)
        pick = pick or next((x for x in lines if len(x[2]) <= HEADING_MAX), None)
        title = pick[2] if pick is not None else f"第 {len(chapters) + 1} 章"
        if pick is not None and lines and pick is lines[0]:
            body_start = pick[1] + 1
        body = _span(text, min(body_start, end), end)
        chapters.append(_entry(title, body, volume[0], volume[1], source_no(title)))
    if short_docs:
        notices.append(f"跳过 {short_docs} 个过短的文档（封面、版权页等）")
    if toc_docs:
        notices.append(f"跳过 {toc_docs} 个目录文档")
    return chapters


def _fallback(text: str, notices: list[str]) -> list[dict[str, Any]]:
    notices.append("没有识别到章节标题，已按 3000–5000 字分段")
    return chunk_text(text)


def split_auto(
    kind: str, text: str, doc_spans: list[list[int]] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """自动切章：返回 (章节, 提示)。"""
    notices: list[str] = []
    if kind == "epub" and doc_spans:
        chapters = _split_epub(text, doc_spans, notices)
        return (normalize_volumes(chapters) if chapters else _fallback(text, notices)), notices
    headings = _strong_headings(text, strip_marks=kind != "txt")
    # 标题行（#）里没有强规则章标题：按标题层级切（只匹配到卷规则的也算没有）
    if kind in ("docx", "md") and not any(h.marked and h.chapter for h in headings):
        levels = _level_headings(text)
        if levels:
            chapters, _ = _assemble(text, levels, 0, len(text), (0, ""), notices)
            return normalize_volumes(chapters), notices
    if any(h.chapter for h in headings):
        chapters, _ = _assemble(text, headings, 0, len(text), (0, ""), notices)
        return normalize_volumes(chapters), notices
    return _fallback(text, notices), notices


# ---------------- 自定义正则 ----------------


def has_nested_quantifier(pattern: str) -> bool:
    """静态检查形如 (…+…)+ / (…*…){n} 的嵌套量词（灾难性回溯的常见来源）。"""
    stack = [False]
    i, n = 0, len(pattern)
    in_class = False
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
            # 紧跟的 ] 或 ^] 是字面量
            if pattern[i + 1 : i + 2] == "^":
                i += 1
            if pattern[i + 1 : i + 2] == "]":
                i += 1
        elif c == "(":
            stack.append(False)
        elif c == ")":
            inner = stack.pop() if len(stack) > 1 else False
            if inner and pattern[i + 1 : i + 2] in ("+", "*", "{"):
                return True
            if inner:
                stack[-1] = True
        elif c in "+*" or (c == "{" and re.match(r"\{\d*,?\d*\}", pattern[i:])):
            stack[-1] = True
        i += 1
    return False


def validate_pattern(pattern: str) -> str:
    pattern = (pattern or "").strip()
    if not pattern:
        raise BadRequestError("请填写用于切章的正则表达式")
    if len(pattern) > REGEX_MAX_LEN:
        raise BadRequestError(f"正则表达式不能超过 {REGEX_MAX_LEN} 个字符")
    if has_nested_quantifier(pattern):
        raise BadRequestError("正则表达式包含嵌套量词（如 (a+)+），可能执行极慢，请简化")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise BadRequestError(f"正则表达式不合法：{exc}") from exc
    return pattern


def _reap(proc: Any) -> None:
    """回收子进程：仍在运行则 SIGTERM，1 秒内没退出再 SIGKILL；join 之后释放进程对象的资源（不留僵尸进程）。"""
    if proc.is_alive():
        proc.terminate()
    proc.join(1)
    if proc.is_alive():
        proc.kill()
        proc.join(1)
    if not proc.is_alive():
        proc.close()


def run_regex(pattern: str, lines: list[tuple[int, str]], *, timeout_seconds: float | None = None) -> list[int]:
    """在 spawn 子进程中执行正则（阻塞，调用方放到线程里）；超时终止子进程 → 400。"""
    timeout = REGEX_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    ctx = multiprocessing.get_context("spawn")
    # 同时执行的子进程数有上限：并发重切时排队，不会一次拉起大量进程
    with _REGEX_SLOTS:
        receiver, sender = ctx.Pipe(duplex=False)
        proc = ctx.Process(target=regex_worker.run, args=(pattern, lines, sender), daemon=True)
        try:
            proc.start()
        except Exception as exc:
            receiver.close()
            sender.close()
            raise BadRequestError("正则执行失败，请稍后重试") from exc
        sender.close()
        try:
            if not receiver.poll(timeout):
                raise BadRequestError("正则执行超时，请简化表达式后重试")
            status, payload = receiver.recv()
        except EOFError as exc:
            # 子进程没能启动或异常退出
            raise BadRequestError("正则执行失败，请稍后重试") from exc
        finally:
            _reap(proc)
            receiver.close()
    if status != "ok":
        raise BadRequestError(f"正则执行失败：{payload}")
    return list(payload)


def split_regex(
    kind: str, text: str, pattern: str, *, timeout_seconds: float | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    pattern = validate_pattern(pattern)
    candidates: list[tuple[int, str]] = []
    rows = _lines(text)
    for i, (_, _, line) in enumerate(rows):
        content = _strip_mark(line)[0] if kind != "txt" else line.strip()
        if content and len(content) <= REGEX_LINE_MAX:
            candidates.append((i, content))
    matched = run_regex(pattern, candidates, timeout_seconds=timeout_seconds)
    if not matched:
        raise BadRequestError("没有匹配到任何标题，请检查正则表达式")
    contents = dict(candidates)
    headings = [
        Heading(rows[i][0], rows[i][1], contents[i], None, source_no(contents[i])) for i in sorted(set(matched))
    ]
    notices: list[str] = []
    chapters, _ = _assemble(text, headings, 0, len(text), (0, ""), notices)
    return normalize_volumes(chapters), notices


# ---------------- 解析上传文件 ----------------


@dataclass
class ParsedBook:
    kind: str
    text: str
    book_title: str
    chapters: list[dict[str, Any]]
    notices: list[str] = field(default_factory=list)
    doc_spans: list[list[int]] = field(default_factory=list)


def detect_kind(filename: str) -> str:
    ext = posixpath.splitext((filename or "").strip().lower())[1]
    kind = EXTENSIONS.get(ext)
    if kind is None:
        raise BadRequestError(UNSUPPORTED_TYPE)
    return kind


def default_title(filename: str, meta_title: str | None) -> str:
    stem = posixpath.splitext(posixpath.basename((filename or "").replace("\\", "/")))[0]
    return ((meta_title or "").strip() or stem.strip() or "未命名")[:TITLE_LIMIT]


def parse_book(filename: str, data: bytes) -> ParsedBook:
    """解码、清洗并自动切章。清洗后正文超过 500 万字 → 400。"""
    kind = detect_kind(filename)
    if len(data) > kb_parser.MAX_FILE_BYTES:
        raise BadRequestError(kb_parser.TOO_LARGE)
    if not data:
        raise BadRequestError("文件为空")
    meta_title: str | None = None
    doc_spans: list[list[int]] = []
    if kind == "epub":
        kb_parser.detect_kind(filename, data)
        try:
            meta_title, docs = kb_parser.epub_documents(data)
        except BadRequestError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BadRequestError(f"文件解析失败：{str(exc)[:200] or exc.__class__.__name__}") from exc
        parts: list[str] = []
        pos = 0
        for doc in docs:
            doc = kb_parser.clean_text(doc)
            if not doc:
                continue
            if parts:
                pos += 2
            doc_spans.append([pos, pos + len(doc)])
            parts.append(doc)
            pos += len(doc)
        text = "\n\n".join(parts)
        if len(text) > MAX_TEXT_CHARS:
            raise BadRequestError(kb_parser.text_too_long(MAX_TEXT_CHARS))
        if not text.strip():
            raise BadRequestError("文件中没有可用的文字内容")
    else:
        text = kb_parser.parse(filename, data, max_chars=MAX_TEXT_CHARS, truncate=False).text
    chapters, notices = split_auto(kind, text, doc_spans)
    return ParsedBook(
        kind=kind,
        text=text,
        book_title=default_title(filename, meta_title),
        chapters=chapters,
        notices=notices,
        doc_spans=doc_spans,
    )


# ---------------- 分卷规整 ----------------


def normalize_volumes(chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 volume_no 的连续段分组后重新编号（见模块说明）；返回新列表。"""
    groups: list[list[dict[str, Any]]] = []
    prev: Any = object()
    for c in chapters:
        if not groups or c["volume_no"] != prev:
            groups.append([])
            prev = c["volume_no"]
        groups[-1].append(c)
    titles = [next((c["volume_title"] for c in g if c["volume_title"]), "") for g in groups]
    out: list[dict[str, Any]] = []
    if not any(titles):
        return [{**c, "volume_no": 1, "volume_title": ""} for c in chapters]
    number, current = 0, ""
    for group, title in zip(groups, titles, strict=True):
        if title:
            number, current = number + 1, title
        # 没有卷名的段：在开头 → 无分卷（0）；在中间 → 并入上一卷
        for c in group:
            out.append({**c, "volume_no": number, "volume_title": current})
    return out


# ---------------- 预览与提示 ----------------


def chapter_lines(text: str, segments: list[list[int]]) -> list[tuple[str, int | None]]:
    """章内每一行 (文本, 在全文中的行首偏移)；segments 拼接处的空行偏移为 None。行号从 1 起即列表下标 + 1。"""
    out: list[tuple[str, int | None]] = []
    for k, (s, e) in enumerate(segments):
        if k:
            out.append(("", None))
        out.extend((line, ls) for ls, _, line in _lines(text, s, e))
    return out


def chapter_views(text: str, chapters: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """每章的展示信息（index 从 1 起、字数、开头一行、提示）与各类提示的数量。"""
    stripped = [strip_number(c["title"]) for c in chapters]
    dup = Counter(t for t in stripped if t)
    views: list[dict[str, Any]] = []
    counts = dict.fromkeys(WARNING_KINDS, 0)
    for i, c in enumerate(chapters):
        chars = _chars(text, c["segments"])
        first = next((ln.strip() for ln, _ in chapter_lines(text, c["segments"]) if ln.strip()), "")
        warnings: list[str] = []
        if chars < SHORT_CHARS:
            warnings.append("short")
        if chars > LONG_CHARS:
            warnings.append("long")
        if stripped[i] and dup[stripped[i]] > 1:
            warnings.append("dup_title")
        if i:
            prev, cur = chapters[i - 1].get("source_no"), c.get("source_no")
            new_volume = chapters[i - 1]["volume_no"] != c["volume_no"]
            # 分卷后序号从 1 重新开始不算断档
            if prev is not None and cur is not None and cur != prev + 1 and not (new_volume and cur == 1):
                warnings.append("gap")
        for w in warnings:
            counts[w] += 1
        views.append(
            {
                "index": i + 1,
                "title": c["title"],
                "volume_no": c["volume_no"],
                "volume_title": c["volume_title"],
                "source_no": c.get("source_no"),
                "chars": chars,
                "first_line": first[:FIRST_LINE_LIMIT],
                "warnings": warnings,
            }
        )
    return views, counts


# ---------------- 校正操作 ----------------

OPS = ("rename", "merge_next", "split", "delete", "start_volume", "rename_volume", "clear_volume")


def _title(title: str | None) -> str:
    title = (title or "").strip()
    if not title:
        raise BadRequestError("标题不能为空")
    if len(title) > TITLE_LIMIT:
        raise BadRequestError(f"标题不能超过 {TITLE_LIMIT} 字")
    return title


def _volume_range(chapters: list[dict[str, Any]], i: int) -> tuple[int, int]:
    """第 i 章所在卷（volume_no 连续段）的 [起, 止)。"""
    no = chapters[i]["volume_no"]
    a = i
    while a > 0 and chapters[a - 1]["volume_no"] == no:
        a -= 1
    b = i + 1
    while b < len(chapters) and chapters[b]["volume_no"] == no:
        b += 1
    return a, b


def apply_op(
    text: str,
    chapters: list[dict[str, Any]],
    op: str,
    index: int,
    *,
    title: str | None = None,
    line: int | None = None,
) -> list[dict[str, Any]]:
    """执行一次校正（index 从 1 起），返回新的章节列表；越界或非法时 400。"""
    if not 1 <= index <= len(chapters):
        raise BadRequestError("章节序号超出范围")
    i = index - 1
    out = [dict(c, segments=[list(s) for s in c["segments"]]) for c in chapters]
    cur = out[i]
    if op == "rename":
        cur["title"] = _title(title)
        cur["source_no"] = source_no(cur["title"])
    elif op == "merge_next":
        if i + 1 >= len(out):
            raise BadRequestError("最后一章没有下一章可以合并")
        cur["segments"] = cur["segments"] + out[i + 1]["segments"]
        del out[i + 1]
    elif op == "split":
        lines = chapter_lines(text, cur["segments"])
        if line is None or not 2 <= line <= len(lines):
            raise BadRequestError("拆分行号超出范围（不能是第 1 行）")
        content, pos = lines[line - 1]
        if pos is None or not content.strip():
            raise BadRequestError("不能在空行处拆分")
        segs = cur["segments"]
        k = next(j for j, (s, e) in enumerate(segs) if s <= pos <= e)
        s_k, e_k = segs[k]
        head = segs[:k]
        tail_end = pos
        while tail_end > s_k and text[tail_end - 1] == "\n":
            tail_end -= 1
        if tail_end > s_k:
            head = [*head, [s_k, tail_end]]
        new_title = content.strip()[:TITLE_LIMIT]
        cur["segments"] = head
        new = _entry(new_title, [[pos, e_k], *segs[k + 1 :]], cur["volume_no"], cur["volume_title"], None)
        new["source_no"] = source_no(new_title)
        out.insert(i + 1, new)
    elif op == "delete":
        if len(out) <= 1:
            raise BadRequestError("至少需要保留 1 章")
        del out[i]
    elif op == "start_volume":
        name = _title(title)
        _, b = _volume_range(out, i)
        # 新卷号取一个不会与相邻卷相同的临时值，规整时重新编号
        temp = max(c["volume_no"] for c in out) + 1
        for c in out[i:b]:
            c["volume_no"], c["volume_title"] = temp, name
    elif op == "rename_volume":
        name = _title(title)
        if not cur["volume_title"]:
            raise BadRequestError("本章不在任何分卷中")
        a, b = _volume_range(out, i)
        for c in out[a:b]:
            c["volume_title"] = name
    elif op == "clear_volume":
        if not cur["volume_title"]:
            raise BadRequestError("本章不在任何分卷中")
        a, b = _volume_range(out, i)
        prev = (out[a - 1]["volume_no"], out[a - 1]["volume_title"]) if a > 0 else (0, "")
        for c in out[a:b]:
            c["volume_no"], c["volume_title"] = prev
    else:
        raise BadRequestError(f"不支持的操作：{op}")
    return normalize_volumes(out)
