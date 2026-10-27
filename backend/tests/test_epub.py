"""EPUB 导出：容器结构、OPF / nav / NCX、卷与章、扉页 / 简介页、控制字符、选章与空章提示。"""

import io
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

from app.services import epub_service
from tests.helpers import create_project, register

NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "x": "http://www.w3.org/1999/xhtml",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
}


def _open(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def _check_structure(zf: zipfile.ZipFile) -> tuple[ET.Element, list[str]]:
    """校验 mimetype / container / OPF manifest 与 spine / 全部 XHTML 格式良好。返回 (opf 根, spine 文件名)。"""
    first = zf.infolist()[0]
    assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
    assert zf.read("mimetype") == b"application/epub+zip"
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    rootfile = container.find(".//c:rootfile", NS)
    assert rootfile is not None and rootfile.get("full-path") == "OEBPS/content.opf"
    opf = ET.fromstring(zf.read("OEBPS/content.opf"))
    assert opf.get("version") == "3.0"
    items = {i.get("id"): i for i in opf.find("opf:manifest", NS)}
    hrefs = {i.get("href") for i in items.values()}
    names = {n.removeprefix("OEBPS/") for n in zf.namelist() if n.startswith("OEBPS/") and n != "OEBPS/content.opf"}
    # manifest 与包内文件一一对应
    assert hrefs == names
    assert any(i.get("properties") == "nav" and i.get("href") == "nav.xhtml" for i in items.values())
    assert items["ncx"].get("href") == "toc.ncx"
    spine = opf.find("opf:spine", NS)
    assert spine.get("toc") == "ncx"
    spine_files = [items[ref.get("idref")].get("href") for ref in spine]
    # 所有 XHTML 都是格式良好的 XML
    for name in zf.namelist():
        if name.endswith((".xhtml", ".ncx", ".opf", ".xml")):
            ET.fromstring(zf.read(name))
    return opf, spine_files


def _nav_tree(zf: zipfile.ZipFile) -> list[tuple[str, list[str]]]:
    nav = ET.fromstring(zf.read("OEBPS/nav.xhtml"))
    top = nav.find(".//x:nav/x:ol", NS)
    out = []
    for li in top.findall("x:li", NS):
        a = li.find("x:a", NS)
        sub = li.find("x:ol", NS)
        out.append((a.text, [s.find("x:a", NS).text for s in sub.findall("x:li", NS)] if sub is not None else []))
    return out


def _ncx_tree(zf: zipfile.ZipFile) -> tuple[list[tuple[str, list[str]]], str]:
    ncx = ET.fromstring(zf.read("OEBPS/toc.ncx"))
    out = []
    for point in ncx.find("ncx:navMap", NS).findall("ncx:navPoint", NS):
        label = point.find("ncx:navLabel/ncx:text", NS).text
        children = [p.find("ncx:navLabel/ncx:text", NS).text for p in point.findall("ncx:navPoint", NS)]
        out.append((label, children))
    orders = [int(p.get("playOrder")) for p in ncx.iter(f"{{{NS['ncx']}}}navPoint")]
    assert orders == list(range(1, len(orders) + 1))
    depth = ncx.find("ncx:head/ncx:meta[@name='dtb:depth']", NS).get("content")
    return out, depth


async def _setup(client, *, volumes: bool = True):
    await register(client)
    p = await create_project(client, title="星海<纪元>", tags=["科幻", "太空"], synopsis="人类首次接触\n外星文明。")
    base = f"/api/v1/projects/{p['id']}"
    chapters = []
    if volumes:
        o1 = (await client.post(f"{base}/outlines", json={"volume_no": 1, "volume_title": "启程", "title": "a"})).json()
        o2 = (
            await client.post(
                f"{base}/outlines", json={"volume_no": 2, "volume_title": "第二卷 远航", "chapter_no": 3, "title": "b"}
            )
        ).json()
        specs = [
            {"title": "启航", "content": "第一段\n\n第二段", "outline_id": o1["id"]},
            {"title": "暗流", "content": "第二章正文", "outline_id": None},
            {"title": "远航", "content": "第三章\x01正文 & <b>", "outline_id": o2["id"]},
            {"title": "空章", "content": "", "outline_id": o2["id"]},
        ]
    else:
        specs = [{"title": "启航", "content": "第一章正文"}, {"title": "暗流", "content": "第二章正文"}]
    for spec in specs:
        resp = await client.post(f"{base}/chapters", json=spec)
        assert resp.status_code == 201, resp.text
        chapters.append(resp.json())
    return p, base, chapters


async def test_epub_with_volumes(client):
    p, base, chapters = await _setup(client)
    resp = await client.get(f"{base}/export", params={"format": "epub", "author": "测试作者"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/epub+zip"
    assert "filename*=UTF-8''" + quote("星海_纪元_.epub") in resp.headers["content-disposition"]
    # 空章被跳过，并在响应头注明
    assert "空章" in unquote(resp.headers["x-export-warnings"])
    zf = _open(resp.content)
    opf, spine = _check_structure(zf)
    assert spine == [
        "title.xhtml",
        "synopsis.xhtml",
        "vol-1.xhtml",
        "ch-1.xhtml",
        "ch-2.xhtml",
        "vol-2.xhtml",
        "ch-3.xhtml",
    ]
    meta = opf.find("opf:metadata", NS)
    assert meta.find("dc:title", NS).text == "星海<纪元>"
    assert meta.find("dc:creator", NS).text == "测试作者"
    assert meta.find("dc:language", NS).text == "zh-CN"
    assert [s.text for s in meta.findall("dc:subject", NS)] == ["科幻", "太空"]
    assert meta.find("dc:description", NS).text == "人类首次接触\n外星文明。"
    assert meta.find("dc:identifier", NS).text.startswith("urn:uuid:")
    assert meta.find("opf:meta[@property='dcterms:modified']", NS).text.endswith("Z")
    # 目录：卷 → 章；没有关联大纲的章节归入前一章所在的卷
    expected = [("第一卷 启程", ["第1章 启航", "第2章 暗流"]), ("第二卷 远航", ["第3章 远航"])]
    assert _nav_tree(zf) == expected
    ncx, depth = _ncx_tree(zf)
    assert ncx == expected and depth == "2"
    title = ET.fromstring(zf.read("OEBPS/title.xhtml"))
    texts = [e.text for e in title.iter() if e.text and e.text.strip()]
    assert "星海<纪元>" in texts and "测试作者 著" in texts and "科幻 · 太空" in texts
    vol = ET.fromstring(zf.read("OEBPS/vol-2.xhtml"))
    assert vol.find(".//x:h1", NS).text == "第二卷 远航"
    ch1 = ET.fromstring(zf.read("OEBPS/ch-1.xhtml"))
    assert ch1.find(".//x:h2", NS).text == "第1章 启航"
    assert [e.text for e in ch1.findall(".//x:p", NS)] == ["第一段", "第二段"]
    # 控制字符被去掉，特殊字符被转义
    ch3 = ET.fromstring(zf.read("OEBPS/ch-3.xhtml"))
    assert [e.text for e in ch3.findall(".//x:p", NS)] == ["第三章正文 & <b>"]
    css = zf.read("OEBPS/style.css").decode()
    assert "text-indent: 2em" in css and "line-height: 1.6" in css


async def test_epub_flat_selected_and_options(client):
    p, base, chapters = await _setup(client, volumes=False)
    resp = await client.get(
        f"{base}/export", params={"format": "epub", "include_synopsis": "false", "chapter_ids": chapters[1]["id"]}
    )
    assert resp.status_code == 200, resp.text
    assert "x-export-warnings" not in resp.headers
    zf = _open(resp.content)
    opf, spine = _check_structure(zf)
    assert spine == ["title.xhtml", "ch-1.xhtml"]
    assert "OEBPS/synopsis.xhtml" not in zf.namelist()
    assert _nav_tree(zf) == [("第2章 暗流", [])]
    ncx, depth = _ncx_tree(zf)
    assert ncx == [("第2章 暗流", [])] and depth == "1"
    # 作者默认取用户名
    assert opf.find("opf:metadata/dc:creator", NS).text == "alice"
    assert "第一章正文" not in b"".join(zf.read(n) for n in zf.namelist()).decode("utf-8", "ignore")
    # 作者名过长 → 422；他人项目 → 404
    assert (await client.get(f"{base}/export", params={"format": "epub", "author": "a" * 101})).status_code == 422


async def test_epub_cross_user(client, make_client):
    p, base, _ = await _setup(client, volumes=False)
    bob = make_client()
    await register(bob, "bob")
    assert (await bob.get(f"{base}/export", params={"format": "epub"})).status_code == 404


def test_group_volumes_rules():
    def ch(no, outline_id):
        return SimpleNamespace(chapter_no=no, outline_id=outline_id, title=f"c{no}", content="x")

    outlines = {
        1: SimpleNamespace(volume_no=1, volume_title=""),
        2: SimpleNamespace(volume_no=2, volume_title=""),
    }
    # 排在所有有卷章节之前的不归卷；之后没有大纲的归入前一卷
    groups = epub_service.group_volumes([ch(1, None), ch(2, 1), ch(3, None), ch(4, 2)], outlines)
    assert [(g[0], [c.chapter_no for c in g[2]]) for g in groups] == [(None, [1]), (1, [2, 3]), (2, [4])]
    # 只有一卷且没有卷名：不分卷
    groups = epub_service.group_volumes([ch(1, 1), ch(2, None)], outlines)
    assert [(g[0], [c.chapter_no for c in g[2]]) for g in groups] == [(None, [1, 2])]
    assert epub_service.volume_label(12, "") == "第十二卷"
    assert epub_service.volume_label(3, "第三卷 归来") == "第三卷 归来"
    assert epub_service.cn_number(20) == "二十" and epub_service.cn_number(105) == "105"


def test_control_characters_and_identifier_stable():
    project = SimpleNamespace(id=7, title="书\x00名\x1f", synopsis="简\x0b介", premise="", tags=["标\x08签"], genre="")
    chapters = [SimpleNamespace(chapter_no=1, outline_id=None, title="章\x02", content="正文\x0c\ud800\n第二段")]
    now = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
    data = epub_service.build_epub(project, chapters, {}, author="作\x03者", include_synopsis=True, now=now)
    zf = _open(data)
    _check_structure(zf)
    again = epub_service.build_book(project, chapters, {}, author="", include_synopsis=True, now=now)
    first = epub_service.build_book(project, chapters, {}, author="", include_synopsis=True, now=now)
    assert again.identifier == first.identifier and again.modified == "2026-10-27T10:00:00Z"
    ch = ET.fromstring(zf.read("OEBPS/ch-1.xhtml"))
    assert [e.text for e in ch.findall(".//x:p", NS)] == ["正文", "第二段"]
