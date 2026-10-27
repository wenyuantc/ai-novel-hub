"""知识库测试用的样例文件：全部用代码现场生成（docx / epub 用 zipfile 拼，pdf 用 pypdf 写一页带文字层的文件）。"""

import io
import zipfile
from xml.sax.saxutils import escape

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(paragraphs: list[tuple[str | None, str]], *, styles: dict[str, str] | None = None) -> bytes:
    """paragraphs：(样式 id 或 None, 文本)。styles：styleId → 样式名（写入 styles.xml）。"""
    body = []
    for style, text in paragraphs:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body.append(f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W_NS}"><w:body>'
        + "".join(body)
        + "</w:body></w:document>"
    )
    styles_xml = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="{W_NS}">'
        + "".join(
            f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{escape(name)}"/></w:style>'
            for sid, name in (styles or {}).items()
        )
        + "</w:styles>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", styles_xml)
    return buf.getvalue()


def make_epub(chapters: list[tuple[str, str]]) -> bytes:
    """chapters：(文件名, xhtml body 内容)；spine 顺序与列表相反地写入 manifest，验证按 spine 读取。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        items = "".join(
            f'<item id="c{i}" href="text/{name}" media-type="application/xhtml+xml"/>'
            for i, (name, _) in reversed(list(enumerate(chapters)))
        )
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            f"<manifest>{items}</manifest><spine>{spine}</spine></package>",
        )
        for name, body in chapters:
            zf.writestr(
                f"OEBPS/text/{name}",
                '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                f"<head><title>忽略</title><style>p{{color:red}}</style></head><body>{body}</body></html>",
            )
    return buf.getvalue()


def make_pdf(lines: list[str]) -> bytes:
    """一页 PDF，文字层为 lines（ASCII，Helvetica）。lines 为空 = 没有文字层（模拟扫描件）。"""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    ops = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({safe}) Tj T*")
    ops.append("ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(ops).encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
