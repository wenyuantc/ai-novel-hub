"""EPUB 3 导出（附 NCX，兼容 EPUB 2 阅读器）。只用标准库 zipfile 生成，不引入第三方依赖。

文件结构：
- mimetype：第一个条目，ZIP_STORED，内容 application/epub+zip
- META-INF/container.xml → OEBPS/content.opf
- OEBPS/content.opf（EPUB 3：dc:identifier = urn:uuid:uuid5(项目 id + 导出时间)、title、creator、language、
  dcterms:modified、每个类型标签一个 dc:subject、dc:description；manifest 含全部 xhtml / nav / ncx / css；
  spine toc="ncx"）
- OEBPS/nav.xhtml（EPUB 3 目录）与 OEBPS/toc.ncx（EPUB 2 目录），都是「卷 → 章」两级，没有分卷时一级
- OEBPS/title.xhtml（扉页：书名、作者、类型标签）、synopsis.xhtml（简介页，可选）、vol-N.xhtml（卷标题页）、
  ch-N.xhtml、style.css

分卷：章节经 outline_id 取所属大纲的 volume_no / volume_title；没有关联大纲的章节归入前一个有卷的章节所在的卷
（排在所有有卷章节之前的，不归卷，放在第一卷之前）。只有一卷且没有卷名时视为不分卷（不生成卷标题页）。
正文：按换行切段，每段一个 <p>；全部文本做 XML 转义，并去掉 XML 1.0 不允许的控制字符。
"""

import io
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from xml.sax.saxutils import escape, quoteattr

from app.ai.images import IMAGE_EXTENSIONS
from app.models import Chapter, Outline, Project
from app.schemas.reader import ReaderBlock
from app.services.export_service import chapter_heading

MIMETYPE = b"application/epub+zip"
# XML 1.0 不允许的字符：C0 控制字符（制表 / 换行 / 回车除外）、代理区、U+FFFE / U+FFFF
_INVALID_XML_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_VOLUME_PREFIX_RE = re.compile(r"^第\s*[0-9一二三四五六七八九十百千零〇两]+\s*卷")
_CN_DIGITS = "零一二三四五六七八九"

CSS = """body { margin: 0 5%; }
h1, h2, h3 { text-align: center; font-weight: bold; }
h1 { font-size: 1.8em; }
.title-page h1 { margin: 30% 0 1em; }
h2 { margin: 1.5em 0 1.2em; font-size: 1.4em; }
p { text-indent: 2em; line-height: 1.6; margin: 0 0 .4em; }
.title-page p, .volume-page p { text-indent: 0; text-align: center; }
.author { margin-top: 2em; font-size: 1.1em; }
.tags { margin-top: 1em; color: #666; }
.volume-page h1 { margin-top: 40%; }
"""


def clean(text: str | None) -> str:
    return _INVALID_XML_RE.sub("", text or "")


def x(text: str | None) -> str:
    """转义为 XML 文本。"""
    return escape(clean(text))


def xa(text: str | None) -> str:
    """转义为带引号的 XML 属性值。"""
    return quoteattr(clean(text))


def cn_number(n: int) -> str:
    """1–99 转中文数字（第一卷、第十二卷）；其余用阿拉伯数字。"""
    if n <= 0 or n >= 100:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    tens, ones = divmod(n, 10)
    return ("十" if tens == 1 else f"{_CN_DIGITS[tens]}十") + (_CN_DIGITS[ones] if ones else "")


def volume_label(volume_no: int, volume_title: str) -> str:
    title = (volume_title or "").strip()
    if title and _VOLUME_PREFIX_RE.match(title):
        return title
    head = f"第{cn_number(volume_no)}卷"
    return f"{head} {title}" if title else head


@dataclass
class EpubChapter:
    file: str
    heading: str
    paragraphs: list[str]
    body_xhtml: str | None = None


@dataclass
class EpubVolume:
    file: str | None  # None = 不归卷的章节组（没有卷标题页）
    label: str
    chapters: list[EpubChapter] = field(default_factory=list)


@dataclass
class EpubBook:
    title: str
    author: str
    tags: list[str]
    synopsis: str
    include_synopsis: bool
    volumes: list[EpubVolume]
    identifier: str
    modified: str
    cover: tuple[bytes, str] | None = None

    @property
    def cover_file(self) -> str | None:
        return f"cover.{IMAGE_EXTENSIONS[self.cover[1]]}" if self.cover is not None else None


def paragraphs(content: str) -> list[str]:
    return [line.strip() for line in clean(content).splitlines() if line.strip()]


def group_volumes(chapters: list[Chapter], outlines: dict[int, Outline]) -> list[tuple[int | None, str, list[Chapter]]]:
    """按卷分组（保持章节顺序）：[(volume_no | None, volume_title, chapters)]。"""
    groups: list[tuple[int | None, str, list[Chapter]]] = []
    current: int | None = None
    for ch in chapters:
        outline = outlines.get(ch.outline_id) if ch.outline_id is not None else None
        vol = outline.volume_no if outline is not None else current
        title = outline.volume_title if outline is not None else ""
        current = vol
        if groups and groups[-1][0] == vol:
            if title and not groups[-1][1]:
                groups[-1] = (vol, title, groups[-1][2])
            groups[-1][2].append(ch)
        else:
            groups.append((vol, title, [ch]))
    vols = {g[0] for g in groups if g[0] is not None}
    if len(vols) <= 1 and not any(g[1].strip() for g in groups if g[0] is not None):
        # 只有一卷且没有卷名：视为不分卷
        return [(None, "", [ch for g in groups for ch in g[2]])] if groups else []
    return groups


def build_book(
    project: Project,
    chapters: list[Chapter],
    outlines: dict[int, Outline],
    *,
    author: str,
    include_synopsis: bool,
    now: datetime | None = None,
    formatting: dict[int, list[ReaderBlock]] | None = None,
) -> EpubBook:
    from app.services.rich_document_render import render_xhtml

    now = now or datetime.now(UTC)
    volumes: list[EpubVolume] = []
    ch_index = 0
    vol_index = 0
    for vol_no, vol_title, items in group_volumes(chapters, outlines):
        if vol_no is None:
            volume = EpubVolume(file=None, label="")
        else:
            vol_index += 1
            volume = EpubVolume(file=f"vol-{vol_index}.xhtml", label=volume_label(vol_no, vol_title))
        for ch in items:
            ch_index += 1
            rich_body = render_xhtml(ch.content, formatting[ch.id], x) if formatting and ch.id in formatting else None
            volume.chapters.append(EpubChapter(f"ch-{ch_index}.xhtml", chapter_heading(ch), paragraphs(ch.content),
                                               body_xhtml=rich_body))
        volumes.append(volume)
    synopsis = (project.synopsis or "").strip() or (project.premise or "").strip()
    tags = [t for t in (project.tags or []) if isinstance(t, str) and t.strip()]
    if not tags and (project.genre or "").strip():
        tags = [project.genre.strip()]
    return EpubBook(
        title=project.title,
        author=author,
        tags=tags,
        synopsis=synopsis,
        include_synopsis=include_synopsis and bool(synopsis),
        volumes=volumes,
        identifier=f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'ainovel:project:{project.id}:{now.isoformat()}')}",
        modified=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


# ---------------- 渲染 ----------------


def _xhtml(title: str, body: str, *, css_class: str = "") -> str:
    cls = f" class={xa(css_class)}" if css_class else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        'xml:lang="zh-CN" lang="zh-CN">\n'
        f'<head>\n<meta charset="utf-8"/>\n<title>{x(title)}</title>\n'
        '<link rel="stylesheet" type="text/css" href="style.css"/>\n</head>\n'
        f"<body{cls}>\n{body}\n</body>\n</html>\n"
    )


def _title_page(book: EpubBook) -> str:
    parts = [f"<h1>{x(book.title)}</h1>"]
    if book.author.strip():
        parts.append(f'<p class="author">{x(book.author)} 著</p>')
    if book.tags:
        parts.append(f'<p class="tags">{x(" · ".join(book.tags))}</p>')
    return _xhtml(book.title, "\n".join(parts), css_class="title-page")


def _synopsis_page(book: EpubBook) -> str:
    body = "<h2>简介</h2>\n" + "\n".join(f"<p>{x(p)}</p>" for p in paragraphs(book.synopsis))
    return _xhtml("简介", body)


def _volume_page(volume: EpubVolume) -> str:
    return _xhtml(volume.label, f"<h1>{x(volume.label)}</h1>", css_class="volume-page")


def _chapter_page(ch: EpubChapter) -> str:
    body = f"<h2>{x(ch.heading)}</h2>\n" + (
        ch.body_xhtml if ch.body_xhtml is not None else "\n".join(f"<p>{x(p)}</p>" for p in ch.paragraphs)
    )
    return _xhtml(ch.heading, body)


def _toc_entries(book: EpubBook) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """[(文件, 标题, [(章文件, 章标题)])]；不归卷的章节作为顶层条目（子列表为空）。"""
    entries: list[tuple[str, str, list[tuple[str, str]]]] = []
    for vol in book.volumes:
        if vol.file is None:
            entries += [(ch.file, ch.heading, []) for ch in vol.chapters]
        else:
            entries.append((vol.file, vol.label, [(ch.file, ch.heading) for ch in vol.chapters]))
    return entries


def _nav(book: EpubBook) -> str:
    def item(href: str, text: str, children: list[tuple[str, str]]) -> str:
        sub = ""
        if children:
            sub = "\n<ol>\n" + "\n".join(f"<li><a href={xa(h)}>{x(t)}</a></li>" for h, t in children) + "\n</ol>\n"
        return f"<li><a href={xa(href)}>{x(text)}</a>{sub}</li>"

    items = "\n".join(item(*e) for e in _toc_entries(book))
    body = f'<nav epub:type="toc" id="toc">\n<h1>目录</h1>\n<ol>\n{items}\n</ol>\n</nav>'
    return _xhtml("目录", body)


def _ncx(book: EpubBook) -> str:
    entries = _toc_entries(book)
    depth = 2 if any(children for _, _, children in entries) else 1
    order = 0
    points: list[str] = []
    for href, text, children in entries:
        order += 1
        parent = order
        inner = []
        for c_href, c_text in children:
            order += 1
            inner.append(
                f'<navPoint id="np-{order}" playOrder="{order}"><navLabel><text>{x(c_text)}</text></navLabel>'
                f"<content src={xa(c_href)}/></navPoint>"
            )
        points.append(
            f'<navPoint id="np-{parent}" playOrder="{parent}">'
            f"<navLabel><text>{x(text)}</text></navLabel><content src={xa(href)}/>" + "".join(inner) + "</navPoint>"
        )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="zh-CN">\n'
        "<head>\n"
        f'<meta name="dtb:uid" content={xa(book.identifier)}/>\n'
        f'<meta name="dtb:depth" content="{depth}"/>\n'
        '<meta name="dtb:totalPageCount" content="0"/>\n<meta name="dtb:maxPageNumber" content="0"/>\n'
        "</head>\n"
        f"<docTitle><text>{x(book.title)}</text></docTitle>\n"
        f"<docAuthor><text>{x(book.author)}</text></docAuthor>\n"
        "<navMap>\n" + "\n".join(points) + "\n</navMap>\n</ncx>\n"
    )


def _spine_files(book: EpubBook) -> list[str]:
    files = (["cover.xhtml"] if book.cover is not None else []) + ["title.xhtml"]
    if book.include_synopsis:
        files.append("synopsis.xhtml")
    for vol in book.volumes:
        if vol.file is not None:
            files.append(vol.file)
        files += [ch.file for ch in vol.chapters]
    return files


def _opf(book: EpubBook) -> str:
    spine = _spine_files(book)
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    manifest += [f'<item id="{_item_id(f)}" href="{f}" media-type="application/xhtml+xml"/>' for f in spine]
    if book.cover is not None:
        manifest.append(
            f'<item id="cover-image" href="{book.cover_file}" media-type="{book.cover[1]}" properties="cover-image"/>'
        )
    meta = [
        f'<dc:identifier id="bookid">{x(book.identifier)}</dc:identifier>',
        f"<dc:title>{x(book.title)}</dc:title>",
        "<dc:language>zh-CN</dc:language>",
        f'<meta property="dcterms:modified">{book.modified}</meta>',
    ]
    if book.author.strip():
        meta.append(f"<dc:creator>{x(book.author)}</dc:creator>")
    meta += [f"<dc:subject>{x(t)}</dc:subject>" for t in book.tags]
    if book.synopsis:
        meta.append(f"<dc:description>{x(book.synopsis)}</dc:description>")
    if book.cover is not None:
        meta.append('<meta name="cover" content="cover-image"/>')
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="zh-CN">\n'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n' + "\n".join(meta) + "\n</metadata>\n"
        "<manifest>\n" + "\n".join(manifest) + "\n</manifest>\n"
        '<spine toc="ncx">\n' + "\n".join(f'<itemref idref="{_item_id(f)}"/>' for f in spine) + "\n</spine>\n"
        "</package>\n"
    )


def _item_id(filename: str) -> str:
    return "x-" + filename.rsplit(".", 1)[0]


CONTAINER = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    "<rootfiles>\n"
    '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
    "</rootfiles>\n</container>\n"
)


def render(book: EpubBook) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # mimetype 必须是第一个条目且不压缩
        zf.writestr(zipfile.ZipInfo("mimetype"), MIMETYPE, compress_type=zipfile.ZIP_STORED)

        def put(name: str, text: str) -> None:
            zf.writestr(name, text.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

        put("META-INF/container.xml", CONTAINER)
        put("OEBPS/content.opf", _opf(book))
        put("OEBPS/nav.xhtml", _nav(book))
        put("OEBPS/toc.ncx", _ncx(book))
        put("OEBPS/style.css", CSS)
        put("OEBPS/title.xhtml", _title_page(book))
        if book.cover is not None:
            zf.writestr(f"OEBPS/{book.cover_file}", book.cover[0], compress_type=zipfile.ZIP_DEFLATED)
            body = (
                f'<div style="text-align:center"><img src="{book.cover_file}" '
                'alt="封面" style="max-width:100%;max-height:100vh"/></div>'
            )
            put("OEBPS/cover.xhtml", _xhtml("封面", body))
        if book.include_synopsis:
            put("OEBPS/synopsis.xhtml", _synopsis_page(book))
        for vol in book.volumes:
            if vol.file is not None:
                put(f"OEBPS/{vol.file}", _volume_page(vol))
            for ch in vol.chapters:
                put(f"OEBPS/{ch.file}", _chapter_page(ch))
    return buf.getvalue()


def build_epub(
    project: Project,
    chapters: list[Chapter],
    outlines: dict[int, Outline],
    *,
    author: str,
    include_synopsis: bool,
    now: datetime | None = None,
    cover: tuple[bytes, str] | None = None,
    formatting: dict[int, list[ReaderBlock]] | None = None,
) -> bytes:
    book = build_book(project, chapters, outlines, author=author, include_synopsis=include_synopsis, now=now,
                      formatting=formatting)
    book.cover = cover
    return render(book)
