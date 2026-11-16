"""不同格式共享选择，Word 保留项目内的可见正文格式。"""

import io
import zipfile
from xml.etree import ElementTree

import pytest
from docx import Document

from app.core.errors import BadRequestError
from app.schemas.rich_document import TextFormatting
from app.services.document_export.model import DocumentModel, DocumentNode
from app.services.document_export.renderers import render_docx, render_text
from app.services.rich_document_render import prose_blocks


def test_word_renders_formatted_unicode_and_plain_content_without_losing_text():
    content = "标题\r\n粗🙂斜\n\n列表一\n列表二\n引用\n尾  "
    bold = content.index("粗")
    italic = content.index("🙂")
    bullet = content.index("列表一")
    quote = content.index("引用")
    formatting = TextFormatting.model_validate({
        "version": 1, "boundaries": [],
        "inline": [{"kind": "bold", "start": bold, "end": italic + 1},
                   {"kind": "italic", "start": italic, "end": italic + 2}],
        "blocks": [{"kind": "heading", "start": 0, "end": 2, "level": 3},
                   {"kind": "bullet_list", "start": bullet, "end": quote - 1, "level": None},
                   {"kind": "blockquote", "start": quote, "end": quote + 2, "level": None}],
    })
    model = DocumentModel(nodes=[
        DocumentNode("heading", "测试书名", level=0),
        DocumentNode("heading", "第一章", level=1),
        DocumentNode("prose", content, blocks=prose_blocks(content, formatting), source="章节"),
    ])
    data = render_docx(model)
    opened = Document(io.BytesIO(data))
    assert [p.text for p in opened.paragraphs] == [
        "测试书名", "第一章", "标题", "粗🙂斜", "", "列表一", "列表二", "引用", "尾  ",
    ]
    assert any(run.bold and run.italic and run.text == "🙂" for run in opened.paragraphs[3].runs)
    assert opened.paragraphs[5].style.name == "List Bullet"
    assert opened.paragraphs[7].style.name == "Quote"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    assert root.findall(".//w:b", ns) and root.findall(".//w:i", ns)
    assert "粗🙂斜" in render_text(model, markdown=False)
    markdown = render_text(model, markdown=True)
    assert markdown.startswith("# 测试书名\n\n## 第一章") and "<strong>" in markdown


def test_word_rejects_xml_controls_without_silently_discarding_content():
    model = DocumentModel(nodes=[DocumentNode("text", "前\x00后", source="笔记")])
    with pytest.raises(BadRequestError, match="笔记包含 Word 不支持的控制字符"):
        render_docx(model)
