"""将同一份可阅读节点渲染为 TXT、Markdown 或 Word。"""

import io
import re

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt

from app.core.errors import BadRequestError
from app.services.document_export.model import DocumentModel, DocumentNode
from app.services.rich_document_render import render_markdown

_NEWLINE = re.compile(r"\r\n|\r|\n")
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def render_text(model: DocumentModel, *, markdown: bool) -> str:
    parts = []
    for node in model.nodes:
        if node.kind == "heading":
            parts.append(f"{'#' * (node.level + 1)} {node.text}" if markdown else node.text)
        elif node.kind == "prose" and markdown and node.blocks:
            parts.append(render_markdown(node.text, node.blocks))
        else:
            parts.append(node.text)
    return "\n\n".join(parts) + "\n"


def _docx_paragraphs(document, node: DocumentNode) -> None:
    if node.blocks is None:
        for line in _NEWLINE.split(node.text):
            document.add_paragraph(line)
        return
    cursor = 0
    previous = False
    for block in node.blocks:
        gap = node.text[cursor:block.start]
        count = len(_NEWLINE.findall(gap))
        for _ in range(max(0, count - int(previous))):
            document.add_paragraph()
        style = {
            "paragraph": None,
            "heading": f"Heading {block.level or 1}",
            "bullet_list": "List Bullet",
            "ordered_list": "List Number",
            "blockquote": "Quote",
        }[block.kind]
        paragraph = document.add_paragraph(style=style)
        for run in block.runs:
            text = node.text[run.start:run.end]
            output = paragraph.add_run(text)
            output.bold = run.bold
            output.italic = run.italic
        cursor = block.end
        previous = True
    for _ in _NEWLINE.findall(node.text[cursor:]):
        document.add_paragraph()


def render_docx(model: DocumentModel) -> bytes:
    for node in model.nodes:
        if _INVALID_XML.search(node.text):
            raise BadRequestError(f"{node.source}包含 Word 不支持的控制字符，请检查后重试")
    document = Document()
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "宋体")
    for node in model.nodes:
        if node.kind == "heading":
            paragraph = document.add_heading(node.text, level=node.level)
            if node.new_page:
                paragraph.paragraph_format.page_break_before = True
        elif node.kind == "prose":
            _docx_paragraphs(document, node)
        else:
            for line in _NEWLINE.split(node.text):
                document.add_paragraph(line)
    data = io.BytesIO()
    document.save(data)
    return data.getvalue()
