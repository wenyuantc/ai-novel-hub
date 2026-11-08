"""正式正文投影、排版导出和作者资料隔离。"""

import io
import random
import re
import zipfile
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import event, update

from app.models import Scene
from app.schemas.rich_document import TextFormatting
from app.services.rich_document_render import chapter_projection, prose_blocks, render_markdown
from tests.helpers import register
from tests.test_rich_document_api import MARKER, add_section, save_payload
from tests.test_scenes import setup_scene

X = "{http://www.w3.org/1999/xhtml}"


def _range(content: str, value: str) -> tuple[int, int]:
    start = content.index(value)
    return start, start + len(value)


def _style(content: str, value: str, kind: str, **extra):
    start, end = _range(content, value)
    return {"kind": kind, "start": start, "end": end, **extra}


async def test_reader_markdown_epub_share_format_without_author_material(client, make_client):
    content = " 章题\r\n甲 <script>alert(1)</script> 粗🙂斜\n列表一\n列表二\n序一\n序二\n引用\n尾 *原始*  "
    project, chapter, url, scene = await setup_scene(client, content)
    document_url = f"{url}/scenes/{scene['id']}/document"
    draft = save_payload((await client.get(document_url)).json())
    add_section(draft)
    draft["document"]["sections"][0].update(title="不可展示的标题", content="不可展示的作者区块")
    draft["document"]["formatting"] = {
        "version": 1, "boundaries": [],
        "inline": [_style(content, "<script>alert(1)</script>", "bold"),
                   _style(content, "粗🙂", "bold"), _style(content, "🙂斜", "italic")],
        "blocks": [_style(content, " 章题", "heading", level=3),
                   _style(content, "列表一\n列表二", "bullet_list", level=None),
                   _style(content, "序一\n序二", "ordered_list", level=None),
                   _style(content, "引用", "blockquote", level=None)],
    }
    marker_start, marker_end = _range(content, "粗🙂")
    draft["document"]["markers"].append({"key": MARKER, "section_key": None, "status": "active", "color": "red",
                                          "start": marker_start, "end": marker_end, "excerpt": "", "last_start": None})
    draft["add_marker_keys"].append(MARKER)
    saved = await client.put(document_url, json=draft)
    assert saved.status_code == 200, saved.text
    base = f"/api/v1/projects/{project['id']}"

    reader = await client.get(f"{base}/reader/chapters/{chapter['id']}")
    assert reader.status_code == 200, reader.text
    body = reader.json()
    assert body["chapter"]["content"] == content
    assert [block["kind"] for block in body["blocks"]] == [
        "heading", "paragraph", "bullet_list", "bullet_list", "ordered_list", "ordered_list", "blockquote",
        "paragraph",
    ]
    assert any(run["bold"] and run["italic"] for block in body["blocks"] for run in block["runs"])
    assert "不可展示的作者区块" not in reader.text and "不可展示的标题" not in reader.text and MARKER not in reader.text

    txt = await client.get(f"{base}/export?format=txt")
    assert txt.status_code == 200 and content in txt.text
    assert "不可展示的作者区块" not in txt.text and "<strong>" not in txt.text
    md = await client.get(f"{base}/export?format=md")
    assert md.status_code == 200
    assert "###  章题\r\n" in md.text
    assert "- 列表一\n- 列表二\n\n1. 序一\n1. 序二\n\n> 引用\n\n尾 *原始*" in md.text
    assert "<strong>&lt;script&gt;alert(1)&lt;/script&gt;</strong>" in md.text
    assert "*原始*" in md.text and "不可展示的作者区块" not in md.text

    epub = await client.get(f"{base}/export?format=epub")
    assert epub.status_code == 200, epub.text
    with zipfile.ZipFile(io.BytesIO(epub.content)) as archive:
        source = archive.read("OEBPS/ch-1.xhtml")
        page = ET.fromstring(source)
    assert not page.findall(f".//{X}script")
    assert "<script>alert(1)</script>" in "".join(page.itertext())
    assert page.find(f".//{X}h3") is not None
    assert len(page.findall(f".//{X}ul/{X}li")) == 2
    assert len(page.findall(f".//{X}ol/{X}li")) == 2
    assert page.find(f".//{X}blockquote") is not None
    assert page.find(f".//{X}strong/{X}em") is not None
    assert "不可展示的作者区块" not in source.decode() and MARKER not in source.decode()

    outsider = make_client()
    await register(outsider, "reader-outsider")
    assert (await outsider.get(f"{base}/reader/chapters/{chapter['id']}")).status_code == 404


async def test_multiscene_projection_keeps_legacy_crlf_seam_and_formats_only_real_text(client):
    content = "甲\r\n乙🙂"
    project, chapter, url, scene = await setup_scene(client, content)
    response = await client.post(f"{url}/scenes/{scene['id']}/split", json={
        "expected_content_revision": 0, "offset": 2,
    })
    assert response.status_code == 200, response.text
    _left, right = response.json()["items"]
    endpoint = f"{url}/scenes/{right['id']}/document"
    payload = save_payload((await client.get(endpoint)).json())
    payload["document"]["formatting"]["inline"] = [{"kind": "bold", "start": 1, "end": 3}]
    assert (await client.put(endpoint, json=payload)).status_code == 200
    base = f"/api/v1/projects/{project['id']}"
    projection = await client.get(f"{base}/reader/chapters/{chapter['id']}")
    assert projection.status_code == 200, projection.text
    data = projection.json()
    assert data["chapter"]["content"] == content
    assert [(block["start"], block["end"]) for block in data["blocks"]] == [(0, 1), (3, 5)]
    assert data["blocks"][1]["runs"][0] == {"start": 3, "end": 5, "bold": True, "italic": False}
    md = await client.get(f"{base}/export?format=md")
    assert "甲\r\n<strong>乙🙂</strong>" in md.text
    assert content in (await client.get(f"{base}/export?format=txt")).text


async def test_reader_takes_body_and_format_in_one_statement_without_author_tables(app, client):
    project, chapter, _url, _scene = await setup_scene(client, "旧稿🙂")
    queries = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement.lower())

    engine = app.state.engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = await client.get(f"/api/v1/projects/{project['id']}/reader/chapters/{chapter['id']}")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200, response.text
    assert response.json()["blocks"] is None
    document_queries = [query for query in queries if "scenes" in query or "scene_documents" in query]
    assert len(document_queries) == 1
    assert all(name in document_queries[0] for name in ("chapters", "scenes", "scene_documents"))
    assert "row_number() over" in document_queries[0]
    assert len(re.findall(r"\bchapters\.content\b", document_queries[0])) == 1
    assert all("scene_sections" not in query and "scene_markers" not in query for query in queries)


async def test_txt_export_does_not_fetch_rich_metadata(app, client):
    project, _chapter, url, scene = await setup_scene(client, "长旧稿\r\n结尾")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    draft = save_payload((await client.get(endpoint)).json())
    add_section(draft)
    assert (await client.put(endpoint, json=draft)).status_code == 200
    queries = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement.lower())

    engine = app.state.engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = await client.get(f"/api/v1/projects/{project['id']}/export?format=txt")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200 and "长旧稿\r\n结尾" in response.text
    assert all(not any(table in query for table in ("scene_documents", "scene_sections", "scene_markers"))
               for query in queries)


async def test_large_plain_chapter_does_not_expand_to_per_line_reader_metadata(client):
    content = "\n".join(["旧稿🙂"] * 10_000)
    project, chapter, _url, _scene = await setup_scene(client, content)
    response = await client.get(f"/api/v1/projects/{project['id']}/reader/chapters/{chapter['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["chapter"]["content"] == content and body["blocks"] is None
    # JSON 对每个换行额外添加一个转义字符；不应再附加逐行对象。
    assert len(response.content) < len(content.encode("utf-8")) + content.count("\n") + 2_000


async def test_exports_refuse_a_corrupt_scene_projection_instead_of_using_stale_chapter(app, client):
    project, _chapter, _url, scene = await setup_scene(client, "章投影")
    async with app.state.sessionmaker() as db:
        await db.execute(update(Scene).where(Scene.id == scene["id"]).values(content="场景另一版"))
        await db.commit()
    base = f"/api/v1/projects/{project['id']}"
    for fmt in ("txt", "md", "epub"):
        response = await client.get(f"{base}/export", params={"format": fmt})
        assert response.status_code == 409 and response.json()["code"] == "scene_invariant_error"


def test_markdown_keeps_unformatted_source_and_encodes_delimiters_inside_styled_text():
    content = "原样 *保留*\r\n**<&> _ ` \\**\n结束"
    start, end = _range(content, "**<&> _ ` \\**")
    formatting = TextFormatting.model_validate({"version": 1, "boundaries": [], "blocks": [],
                                                "inline": [{"kind": "bold", "start": start, "end": end}]})
    blocks = prose_blocks(content, formatting)
    result = render_markdown(content, blocks)
    assert result.startswith("原样 *保留*\r\n<strong>&#42;&#42;&lt;&amp;&gt;")
    assert "&#95;" in result and "&#96;" in result and "&#92;" in result
    assert result.endswith("</strong>\n结束")


def test_hiding_section_does_not_invent_paragraph_break_at_zero_width_boundary():
    content = "甲乙"
    formatting = TextFormatting.model_validate({"version": 1, "boundaries": [1], "blocks": [],
                                                "inline": [{"kind": "bold", "start": 1, "end": 2}]})
    blocks = prose_blocks(content, formatting)
    assert len(blocks) == 1 and (blocks[0].start, blocks[0].end) == (0, 2)
    assert render_markdown(content, blocks) == "甲<strong>乙</strong>"


@pytest.mark.parametrize("kind,prefix", [("bullet_list", "- "), ("ordered_list", "1. "), ("blockquote", "> ")])
def test_hidden_section_does_not_split_a_single_styled_block(kind, prefix):
    content = "甲乙"
    formatting = TextFormatting.model_validate({"version": 1, "boundaries": [1], "inline": [],
        "blocks": [{"kind": kind, "start": 0, "end": 2}]})
    blocks = prose_blocks(content, formatting)
    assert len(blocks) == 1 and (blocks[0].start, blocks[0].end) == (0, 2)
    assert render_markdown(content, blocks) == f"{prefix}甲乙"


def test_distinct_adjacent_list_blocks_remain_distinct():
    content = "甲乙"
    formatting = TextFormatting.model_validate({"version": 1, "boundaries": [1], "inline": [],
        "blocks": [{"kind": "bullet_list", "start": 0, "end": 1},
                   {"kind": "bullet_list", "start": 1, "end": 2}]})
    blocks = prose_blocks(content, formatting)
    assert [(block.start, block.end) for block in blocks] == [(0, 1), (1, 2)]
    assert render_markdown(content, blocks) == "- 甲\n- 乙"


@pytest.mark.parametrize("kind,prefix", [("blockquote", "> "), ("bullet_list", "- "), ("ordered_list", "1. ")])
@pytest.mark.parametrize("newline", ["\r", "\n", "\r\n"])
def test_markdown_ends_container_before_plain_paragraph_for_all_newlines(kind, prefix, newline):
    content = f"引用{newline}尾"
    formatting = TextFormatting.model_validate({"blocks": [{"kind": kind, "start": 0, "end": 2}]})
    result = render_markdown(content, prose_blocks(content, formatting))
    assert result.startswith(f"{prefix}引用{newline}")
    separators = result[len(f"{prefix}引用"):-len("尾")]
    assert len(re.findall(r"\r\n|\r|\n", separators)) >= 2


def test_projection_preserves_codepoints_around_random_newlines_and_boundaries():
    rng = random.Random(20261108)
    alphabet = ["甲", "乙", "🙂", "e", "\u0301", " ", "\r", "\n"]
    for _ in range(500):
        content = "".join(rng.choices(alphabet, k=rng.randrange(1, 80)))
        valid = [offset for offset in range(len(content) + 1)
                 if not (0 < offset < len(content) and content[offset - 1:offset + 1] == "\r\n")]
        boundaries = sorted(rng.sample(valid, k=min(len(valid), rng.randrange(0, 8))))
        formatting = TextFormatting(boundaries=boundaries)
        blocks = prose_blocks(content, formatting)
        assert render_markdown(content, blocks) == content
        for block in blocks:
            assert "".join(content[run.start:run.end] for run in block.runs) == content[block.start:block.end]
            assert all(a.end == b.start for a, b in zip(block.runs, block.runs[1:], strict=False))


def test_multiscene_render_does_not_apply_single_scene_metadata_limit_to_chapter():
    text = "甲" * 10_003
    meta = {"version": 1, "boundaries": list(range(1, 10_002)),
            "inline": [{"kind": "bold", "start": 0, "end": 1}], "blocks": []}
    chapter = SimpleNamespace(content=text * 2)
    projected = chapter_projection(chapter, [(1, "", text, meta), (2, "", text, meta)])
    assert len(projected.formatting.boundaries) == 20_002
    assert projected.blocks is not None and len(projected.blocks) == 1
    assert projected.blocks[0].end == len(chapter.content)
    # 不得将不可见的零宽结构逐一暴露为读者排版边界或导致响应线性膨胀。
    assert [(run.start, run.end, run.bold) for run in projected.blocks[0].runs] == [
        (0, 1, True), (1, len(text), False),
        (len(text), len(text) + 1, True), (len(text) + 1, len(text) * 2, False),
    ]
