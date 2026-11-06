"""知识库：解析 6 种格式与校验、分块、上传建索引、失败重试、删除、注入开关、检索调试、向量模型与重建、提示词注入。"""

import json

import pytest
from sqlalchemy import func, select

from app.ai.providers import AIProviderError
from app.core.errors import BadRequestError
from app.models import KbChunk, RetrievalChunk
from app.services import kb_parser, kb_service, retrieval_service
from tests.helpers import create_ai_config, create_project, register, run_task, wait_task
from tests.kb_fixtures import make_docx, make_epub, make_pdf

SECT = (
    "# 门派\n\n## 天剑宗\n\n天剑宗位于北境雪山之巅，宗主林远山以剑道闻名天下。门下弟子三千，每年冬至在雪山比剑。\n\n"
    "## 东海龙宫\n\n龙宫掌管四海水族，龙王性情暴躁，与天剑宗素有旧怨。\n\n# 地理\n\n城南集市以售卖灵草闻名。"
)


@pytest.fixture(autouse=True)
def _clear_cache():
    retrieval_service.invalidate()
    yield
    retrieval_service.invalidate()


# ---------------- 解析与分块（纯函数） ----------------


def test_parse_six_formats():
    txt = kb_parser.parse("a.txt", "第一行\r\n\r\n\r\n第二行".encode("gb18030"))
    assert txt.kind == "txt" and txt.text == "第一行\n\n第二行"
    md = kb_parser.parse("a.md", SECT.encode())
    assert md.kind == "md" and md.text.startswith("# 门派")
    js = kb_parser.parse("a.json", json.dumps({"门派": {"天剑宗": {"位置": "北境"}}}, ensure_ascii=False).encode())
    assert js.text == "# 门派\n\n## 天剑宗\n\n位置：北境"
    docx = kb_parser.parse(
        "a.docx",
        make_docx(
            [("1", "第一卷"), (None, "正文一"), ("Heading2", "门派"), (None, "天剑宗。")], styles={"1": "标题 1"}
        ),
    )
    assert docx.text == "# 第一卷\n\n正文一\n\n## 门派\n\n天剑宗。"
    epub = kb_parser.parse(
        "a.epub",
        make_epub(
            [("c1.xhtml", "<h1>第一章</h1><p>甲</p><script>x</script>"), ("c2.xhtml", "<h2>第二节</h2><p>乙</p>")]
        ),
    )
    assert epub.text == "# 第一章\n\n甲\n\n## 第二节\n\n乙"
    pdf = kb_parser.parse("a.pdf", make_pdf(["The Sky Sword Sect sits on the snow mountain.", "Disciples duel."]))
    assert "Sky Sword Sect" in pdf.text and pdf.mime == "application/pdf"


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("a.exe", b"MZ", "不支持的文件类型"),
        ("a.doc", b"x", "不支持的文件类型"),
        ("a.pdf", b"not a pdf", "文件内容与扩展名不符"),
        ("a.docx", b"%PDF-1.4", "文件内容与扩展名不符"),
        ("a.txt", b"PK\x03\x04zip", "文件内容与扩展名不符"),
        ("a.txt", b"bin\x00ary", "文件内容与扩展名不符"),
        ("a.pdf", make_pdf([]), kb_parser.SCANNED_PDF),
        ("a.json", b"{bad", "JSON 格式不正确"),
        ("a.docx", b"PK\x03\x04broken", "文件已损坏"),
        ("a.txt", b"", "文件为空"),
    ],
)
def test_parse_rejects(name, data, message):
    with pytest.raises(BadRequestError) as exc:
        kb_parser.parse(name, data)
    assert message in exc.value.detail


def test_parse_size_limit_and_truncate(monkeypatch):
    with pytest.raises(BadRequestError) as exc:
        kb_parser.parse("a.txt", b"x" * (kb_parser.MAX_FILE_BYTES + 1))
    assert exc.value.detail == kb_parser.TOO_LARGE
    monkeypatch.setattr(kb_parser, "MAX_TEXT_CHARS", 10)
    doc = kb_parser.parse("a.txt", "一二三四五六七八九十十一".encode())
    assert len(doc.text) == 10 and "已截断" in doc.warnings[0]


def _zip(entries: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_parse_rejects_dtd_and_zip_bombs(monkeypatch):
    # 实体膨胀（billion laughs）：带 DTD 的 XML 一律拒绝
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>"
    )
    with pytest.raises(BadRequestError) as exc:
        kb_parser.parse("a.docx", _zip({"word/document.xml": bomb}))
    assert "DTD" in exc.value.detail
    # UTF-16 编码同样被识别
    utf16 = bomb.decode().replace('"1.0"?>', '"1.0" encoding="UTF-16"?>', 1).encode("utf-16")
    with pytest.raises(BadRequestError) as exc:
        kb_parser.parse("a.docx", _zip({"word/document.xml": utf16}))
    assert "DTD" in exc.value.detail
    # 单条目解压大小、条目数上限
    docx = make_docx([(None, "正文" * 100)])
    monkeypatch.setattr(kb_parser, "MAX_ENTRY_BYTES", 100)
    with pytest.raises(BadRequestError) as exc:
        kb_parser.parse("a.docx", docx)
    assert "解压后过大" in exc.value.detail
    monkeypatch.setattr(kb_parser, "MAX_ENTRY_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(kb_parser, "MAX_ZIP_ENTRIES", 2)
    with pytest.raises(BadRequestError) as exc:
        kb_parser.parse("a.docx", docx)
    assert "解压后过大" in exc.value.detail


def test_chunking_size_overlap_heading_path():
    para = "天剑宗弟子每日卯时练剑，午后研读剑谱。" * 4  # 每段约 80 字
    body = "\n\n".join(para for _ in range(20))
    text = f"# 第一卷\n\n## 门派\n\n{body}\n\n## 地理\n\n城南集市以售卖灵草闻名。"
    chunks = kb_parser.chunk_text(text)
    sect = [c for c in chunks if c.heading_path == "第一卷 > 门派"]
    geo = [c for c in chunks if c.heading_path == "第一卷 > 地理"]
    assert len(sect) >= 3 and len(geo) == 1
    assert [c.seq for c in chunks] == list(range(1, len(chunks) + 1))
    for c in chunks:
        assert text[c.start_offset : c.end_offset] == c.content
        assert len(c.content) <= kb_parser.CHUNK_CHARS + kb_parser.OVERLAP_CHARS
    for prev, cur in zip(sect, sect[1:], strict=False):
        # 相邻块重叠：当前块开头落在上一块末尾 80 字以内，且从句子边界开始
        overlap = prev.end_offset - cur.start_offset
        assert 0 < overlap <= kb_parser.OVERLAP_CHARS
        assert text[cur.start_offset - 1] in "。\n"
        # 块正文（去掉重叠）约 500 字
        assert len(cur.content) - overlap <= kb_parser.CHUNK_CHARS
    assert max(len(c.content) for c in sect) > 400
    # 标题之间不重叠
    assert geo[0].content == "城南集市以售卖灵草闻名。"


def test_chunking_long_paragraph_and_code_fence():
    sentence = "林远独自穿过风雪，" * 30 + "终于抵达山门。"
    text = "```\n# 不是标题\n```\n\n" + sentence * 3
    chunks = kb_parser.chunk_text(text)
    assert all(c.heading_path == "" for c in chunks)
    assert all(len(c.content) <= kb_parser.CHUNK_CHARS + kb_parser.OVERLAP_CHARS for c in chunks)
    assert len(chunks) >= 3
    # 单句超过 80 字：硬取上一块最后 80 字作为重叠
    assert chunks[2].start_offset == chunks[1].end_offset - kb_parser.OVERLAP_CHARS


# ---------------- 上传 / 建索引 ----------------


async def _setup(client):
    await register(client)
    p = await create_project(client)
    return p, f"/api/v1/projects/{p['id']}/kb"


async def _upload(client, base, name="设定集.md", data: bytes = SECT.encode(), *, wait=True) -> dict:
    resp = await client.post(f"{base}/documents", files={"file": (name, data, "application/octet-stream")})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    if wait:
        task = await wait_task(client, body["task"]["id"])
        assert task["status"] == "succeeded", task
        assert task["kind"] == "kb.index" and task["project_id"] == body["document"]["project_id"]
    return body


async def _doc(client, base, doc_id) -> dict:
    return next(d for d in (await client.get(f"{base}/documents")).json() if d["id"] == doc_id)


async def test_upload_index_keyword_only(client, app):
    p, base = await _setup(client)
    body = await _upload(client, base)
    assert body["document"]["status"] == "pending" and body["document"]["filename"] == "设定集.md"
    doc = await _doc(client, base, body["document"]["id"])
    assert doc["status"] == "ready_keyword_only" and doc["chunk_count"] == 3
    assert doc["char_count"] == len(SECT) and doc["mime"] == "text/markdown"
    result = (await client.get(f"/api/v1/tasks/{body['task']['id']}")).json()
    assert result["progress"] == 100
    # 分块：带标题路径
    page = (await client.get(f"{base}/documents/{doc['id']}/chunks", params={"page_size": 2})).json()
    assert page["total"] == 3 and len(page["items"]) == 2
    assert [c["heading_path"] for c in page["items"]] == ["门派 > 天剑宗", "门派 > 东海龙宫"]
    # 检索调试：只用关键词，可以命中
    res = (await client.post(f"{base}/search", json={"query": "天剑宗在哪里", "min_score": 0})).json()
    assert res["mode"] == "keyword" and res["hits"]
    top = res["hits"][0]
    assert top["filename"] == "设定集.md" and top["heading_path"] == "门派 > 天剑宗"
    assert top["vector_score"] is None and 0 <= top["score"] <= 1
    status = (await client.get(f"{base}/index-status")).json()
    assert status == {
        "embedding_model": None,
        "needs_rebuild": False,
        "stale_count": 0,
        "stale": {"kb": 0, "memory": 0},
        "indexing": False,
    }


async def test_upload_validation(client, monkeypatch):
    _, base = await _setup(client)
    resp = await client.post(f"{base}/documents", files={"file": ("a.exe", b"MZ", "application/octet-stream")})
    assert resp.status_code == 400 and "不支持的文件类型" in resp.json()["detail"]
    big = b"x" * (kb_parser.MAX_FILE_BYTES + 1)
    resp = await client.post(f"{base}/documents", files={"file": ("a.txt", big, "text/plain")})
    assert resp.status_code == 400 and resp.json()["detail"] == kb_parser.TOO_LARGE
    resp = await client.post(f"{base}/documents", files={"file": ("a.pdf", make_pdf([]), "application/pdf")})
    assert resp.status_code == 400 and resp.json()["detail"] == kb_parser.SCANNED_PDF
    # 重复上传 → 409，并告知已存在的文档
    first = await _upload(client, base)
    resp = await client.post(f"{base}/documents", files={"file": ("另一个名字.md", SECT.encode(), "text/markdown")})
    assert resp.status_code == 409 and "设定集.md" in resp.json()["detail"]
    assert resp.json()["document_id"] == first["document"]["id"]
    # 文档数上限
    monkeypatch.setattr(kb_service, "MAX_DOCUMENTS", 2)
    await _upload(client, base, "b.txt", "第二份资料".encode())
    resp = await client.post(f"{base}/documents", files={"file": ("c.txt", "第三份".encode(), "text/plain")})
    assert resp.status_code == 400 and "最多 2 个文档" in resp.json()["detail"]
    assert len((await client.get(f"{base}/documents")).json()) == 2


async def test_upload_body_limit_before_parsing(client):
    """超大请求体在 multipart 解析前拦截：Content-Length 超限直接 400；分块传输边收边计数。"""
    _, base = await _setup(client)
    url = f"{base}/documents"
    limit = kb_parser.MAX_FILE_BYTES + 1024 * 1024
    resp = await client.post(
        url,
        content=b"x",
        headers={"content-type": "multipart/form-data; boundary=b", "content-length": str(limit + 1)},
    )
    assert resp.status_code == 400 and resp.json()["detail"] == kb_parser.TOO_LARGE

    sent = 0
    total = kb_parser.MAX_FILE_BYTES // (1024 * 1024) + 10

    async def chunks():
        nonlocal sent
        yield b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'
        piece = b"x" * (1024 * 1024)
        for _ in range(total):
            sent += 1
            yield piece

    resp = await client.post(url, content=chunks(), headers={"content-type": "multipart/form-data; boundary=b"})
    assert resp.status_code == 400 and resp.json()["detail"] == kb_parser.TOO_LARGE
    # 超限即停止接收，没有把整个请求体读完
    assert sent < total
    assert (await client.get(f"{base}/documents")).json() == []


async def test_all_formats_upload(client):
    _, base = await _setup(client)
    files = {
        "a.txt": "天剑宗的规矩：弟子不得私自下山。".encode(),
        "b.md": SECT.encode(),
        "c.json": json.dumps({"门派": {"龙宫": "掌管四海"}}, ensure_ascii=False).encode(),
        "d.docx": make_docx([("Heading1", "人物"), (None, "林远是天剑宗弟子。")]),
        "e.epub": make_epub([("c1.xhtml", "<h1>序章</h1><p>雪落山门。</p>")]),
        "f.pdf": make_pdf(["The Sky Sword Sect sits on the northern snow mountain peak.", "Disciples duel at dawn."]),
    }
    for name, data in files.items():
        await _upload(client, base, name, data)
    docs = (await client.get(f"{base}/documents")).json()
    assert {d["filename"] for d in docs} == set(files)
    assert all(d["status"] == "ready_keyword_only" and d["chunk_count"] >= 1 for d in docs)
    res = (await client.post(f"{base}/search", json={"query": "林远 天剑宗弟子", "min_score": 0})).json()
    assert "d.docx" in {h["filename"] for h in res["hits"]}
    docx_hit = next(h for h in res["hits"] if h["filename"] == "d.docx")
    assert docx_hit["heading_path"] == "人物"


async def test_index_failure_retry_and_delete(client, app, monkeypatch):
    _, base = await _setup(client)
    original = kb_parser.chunk_text

    def broken(text):
        raise RuntimeError("boom")

    monkeypatch.setattr(kb_parser, "chunk_text", broken)
    body = await _upload(client, base, wait=False)
    task = await wait_task(client, body["task"]["id"])
    assert task["status"] == "failed" and "建索引失败" in task["error"]
    doc = await _doc(client, base, body["document"]["id"])
    assert doc["status"] == "failed" and doc["error"]

    monkeypatch.setattr(kb_parser, "chunk_text", original)
    retry = await client.post(f"/api/v1/tasks/{task['id']}/retry")
    assert retry.status_code == 202
    assert (await wait_task(client, retry.json()["id"]))["status"] == "succeeded"
    doc = await _doc(client, base, doc["id"])
    assert doc["status"] == "ready_keyword_only" and doc["chunk_count"] == 3

    # 重建该文档：重新分块
    resp = await client.post(f"{base}/documents/{doc['id']}/reindex")
    assert resp.status_code == 202
    assert (await wait_task(client, resp.json()["id"]))["status"] == "succeeded"
    assert (await _doc(client, base, doc["id"]))["chunk_count"] == 3

    # 删除：分块与检索单元一并删除
    assert (await client.delete(f"{base}/documents/{doc['id']}")).status_code == 204
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(KbChunk.id))) == 0
        assert await db.scalar(select(func.count(RetrievalChunk.id))) == 0
    assert (await client.post(f"{base}/search", json={"query": "天剑宗"})).json()["hits"] == []
    assert (await client.delete(f"{base}/documents/{doc['id']}")).status_code == 404


async def test_search_threshold_and_inject_toggle(client):
    _, base = await _setup(client)
    body = await _upload(client, base)
    res = (await client.post(f"{base}/search", json={"query": "天剑宗 龙宫 旧怨", "min_score": 0})).json()
    scores = [h["score"] for h in res["hits"]]
    assert len(scores) >= 2 and all(0 <= s <= 1 for s in scores) and scores[0] == 1
    threshold = (scores[0] + scores[-1]) / 2
    cut = (await client.post(f"{base}/search", json={"query": "天剑宗 龙宫 旧怨", "min_score": threshold})).json()
    assert cut["hits"] and all(h["score"] >= threshold for h in cut["hits"])
    assert len(cut["hits"]) < len(res["hits"])
    # top_k
    one = (await client.post(f"{base}/search", json={"query": "天剑宗 龙宫 旧怨", "top_k": 1, "min_score": 0})).json()
    assert len(one["hits"]) == 1

    # 关闭「参与注入」后不再被召回
    resp = await client.patch(f"{base}/documents/{body['document']['id']}", json={"inject": False})
    assert resp.status_code == 200 and resp.json()["inject"] is False
    assert (await client.post(f"{base}/search", json={"query": "天剑宗", "min_score": 0})).json()["hits"] == []
    await client.patch(f"{base}/documents/{body['document']['id']}", json={"inject": True})
    assert (await client.post(f"{base}/search", json={"query": "天剑宗", "min_score": 0})).json()["hits"]


async def test_settings(client):
    _, base = await _setup(client)
    s = (await client.get(f"{base}/settings")).json()
    assert s == {
        "auto_inject": True,
        "top_k": 6,
        "min_score": 0.35,
        "max_chars": 3000,
        "memory_top_k": 8,
        "memory_max_chars": 1500,
        "memory_min_score": 0.35,
    }
    s = (await client.patch(f"{base}/settings", json={"auto_inject": False, "top_k": 3})).json()
    assert s["auto_inject"] is False and s["top_k"] == 3 and s["max_chars"] == 3000
    assert (await client.patch(f"{base}/settings", json={"top_k": 21})).status_code == 422
    assert (await client.patch(f"{base}/settings", json={"min_score": 1.5})).status_code == 422
    assert (await client.patch(f"{base}/settings", json={"max_chars": 100})).status_code == 422


# ---------------- 向量模型 ----------------


async def test_embedding_index_rebuild_and_model_change(client, fake_llm):
    _, base = await _setup(client)
    # 没有向量模型：只用关键词
    body = await _upload(client, base)
    doc_id = body["document"]["id"]
    assert fake_llm.embed_calls == []

    # 配置向量模型后：提示需要重建，重建前仍只用关键词（不为查询求向量）
    emb = await create_ai_config(client, name="向量A", model_type="embedding", model="emb-a")
    status = (await client.get(f"{base}/index-status")).json()
    assert status["needs_rebuild"] is True and status["stale"] == {"kb": 3, "memory": 0}
    assert status["embedding_model"] == "向量A · emb-a"
    res = (await client.post(f"{base}/search", json={"query": "天剑宗", "min_score": 0})).json()
    assert res["mode"] == "keyword" and retrieval_service.KEYWORD_ONLY_STALE in res["warnings"]
    assert fake_llm.embed_calls == []

    task, _ = await run_task(client, await client.post(f"{base}/rebuild"))
    assert task["status"] == "succeeded" and task["title"] == "重建检索索引"
    assert (await _doc(client, base, doc_id))["status"] == "ready"
    status = (await client.get(f"{base}/index-status")).json()
    assert status["needs_rebuild"] is False and status["stale_count"] == 0
    res = (await client.post(f"{base}/search", json={"query": "天剑宗在北境", "min_score": 0})).json()
    assert res["mode"] == "hybrid" and len(res["hits"]) == 3
    for h in res["hits"]:
        assert 0 <= h["vector_score"] <= 1 and 0 <= h["keyword_score"] <= 1
        assert h["score"] == pytest.approx(0.7 * h["vector_score"] + 0.3 * h["keyword_score"], abs=1e-3)
    assert res["hits"][0]["heading_path"] == "门派 > 天剑宗"
    # 请求格式：模型名、单条截断、批大小
    assert all(c["model"] == "emb-a" and len(c["texts"]) <= 32 for c in fake_llm.embed_calls)

    # 新上传的文档直接生成向量
    body2 = await _upload(client, base, "b.txt", "龙王的三太子性情温和。".encode())
    assert (await _doc(client, base, body2["document"]["id"]))["status"] == "ready"

    # 更换向量模型：提示重建，重建完成前不使用旧向量
    await create_ai_config(client, name="向量B", model_type="embedding", model="emb-b", is_default=True)
    status = (await client.get(f"{base}/index-status")).json()
    assert status["needs_rebuild"] is True and status["stale"]["kb"] == 4
    res = (await client.post(f"{base}/search", json={"query": "天剑宗", "min_score": 0})).json()
    assert res["mode"] == "keyword" and all(h["vector_score"] is None for h in res["hits"])
    await run_task(client, await client.post(f"{base}/rebuild"))
    res = (await client.post(f"{base}/search", json={"query": "天剑宗", "min_score": 0})).json()
    assert res["mode"] == "hybrid"
    assert fake_llm.embed_calls[-1]["model"] == "emb-b"

    # 删除全部向量模型：退回关键词，不提示重建
    for cfg in (await client.get("/api/v1/ai-configs")).json():
        await client.delete(f"/api/v1/ai-configs/{cfg['id']}")
    assert emb["id"]
    status = (await client.get(f"{base}/index-status")).json()
    assert status["embedding_model"] is None and status["needs_rebuild"] is False
    resp = await client.post(f"{base}/rebuild")
    assert resp.status_code == 400 and "尚未配置向量模型" in resp.json()["detail"]


async def test_embedding_failure_keyword_only_with_warning(client, fake_llm):
    _, base = await _setup(client)
    await create_ai_config(client, name="向量", model_type="embedding", model="emb")
    fake_llm.embed_error = AIProviderError("模型调用失败：额度不足")
    body = await _upload(client, base, wait=False)
    task = await wait_task(client, body["task"]["id"])
    assert task["status"] == "succeeded"
    result = (await client.get(f"/api/v1/tasks/{task['id']}")).json()
    assert result["status"] == "succeeded"
    doc = await _doc(client, base, body["document"]["id"])
    assert doc["status"] == "ready_keyword_only" and any("额度不足" in w for w in doc["warnings"])
    # 关键词仍可用
    res = (await client.post(f"{base}/search", json={"query": "天剑宗", "min_score": 0})).json()
    assert res["hits"] and res["mode"] == "keyword"
    assert (await client.get(f"{base}/index-status")).json()["needs_rebuild"] is True
    # 重建（接口恢复）后变为 ready
    await run_task(client, await client.post(f"{base}/rebuild"))
    assert (await _doc(client, base, body["document"]["id"]))["status"] == "ready"


async def test_blocked_embedding_endpoint_reports_reason(client):
    # 向量模型指向内网（默认禁止私网）：提示「不可用」及原因，而不是「未配置」
    _, base = await _setup(client)
    await create_ai_config(
        client, name="内网向量", model_type="embedding", model="emb", base_url="http://127.0.0.1:9/v1"
    )
    body = await _upload(client, base)
    doc = await _doc(client, base, body["document"]["id"])
    assert doc["status"] == "ready_keyword_only"
    assert any("向量模型不可用" in w and "内网" in w and "未配置" not in w for w in doc["warnings"])
    task = await wait_task(client, (await client.post(f"{base}/rebuild")).json()["id"])
    assert task["status"] == "failed" and "向量模型不可用" in task["error"] and "尚未配置" not in task["error"]


async def test_embedding_config_rejects_anthropic(client):
    await register(client)
    resp = await client.post(
        "/api/v1/ai-configs",
        json={"name": "x", "provider": "anthropic", "api_key": "k", "model": "m", "model_type": "embedding"},
    )
    assert resp.status_code == 422 and "该提供方不支持向量模型" in resp.text
    cfg = await create_ai_config(client, provider="anthropic", base_url=None)
    resp = await client.patch(f"/api/v1/ai-configs/{cfg['id']}", json={"model_type": "embedding"})
    assert resp.status_code == 400 and resp.json()["detail"] == "该提供方不支持向量模型"


async def test_embedding_connectivity(client, fake_llm):
    await register(client)
    cfg = await create_ai_config(client, name="向量", model_type="embedding", model="emb")
    body = (await client.post(f"/api/v1/ai-configs/{cfg['id']}/test")).json()
    assert body["ok"] is True and "向量维度 64" in body["message"]
    assert fake_llm.calls == []
    fake_llm.embed_error = AIProviderError("模型调用失败：401")
    body = (await client.post(f"/api/v1/ai-configs/{cfg['id']}/test")).json()
    assert body["ok"] is False and "401" in body["message"]
    # 向量模型不会被当作文本模型使用
    p = await create_project(client)
    resp = await client.post(f"/api/v1/projects/{p['id']}/wizard/world")
    assert resp.status_code == 400 and "尚未配置文本模型" in resp.json()["detail"]


# ---------------- 提示词注入 ----------------


async def test_kb_injected_into_wizard_entity_and_chapter(client, fake_llm):
    p, base = await _setup(client)
    await create_ai_config(client)
    pbase = f"/api/v1/projects/{p['id']}"
    # 没有知识库：不出现参考资料块
    fake_llm.push('{"items": []}')
    await run_task(client, await client.post(f"{pbase}/wizard/world", json={"instruction": "天剑宗"}))
    assert "参考资料" not in fake_llm.calls[-1]["messages"][-1]["content"]

    await _upload(client, base)
    fake_llm.push('{"items": []}')
    await run_task(client, await client.post(f"{pbase}/wizard/world", json={"instruction": "天剑宗的门规"}))
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "【参考资料（来自知识库，仅供参考；与项目设定冲突时以设定为准）】" in prompt
    assert "- 《设定集.md》门派 > 天剑宗：天剑宗位于北境雪山之巅" in prompt

    fake_llm.push('{"name": "龙女"}')
    await run_task(client, await client.post(f"{pbase}/characters/generate", json={"instruction": "龙宫的公主"}))
    assert "《设定集.md》门派 > 东海龙宫" in fake_llm.calls[-1]["messages"][-1]["content"]

    # 章节生成：参考资料在世界观之后（通过上下文注入）
    await client.post(f"{pbase}/world", json={"title": "天剑宗", "content": "北境第一剑宗"})
    ch = (await client.post(f"{pbase}/chapters", json={"title": "天剑宗试炼"})).json()
    fake_llm.push("正文")
    await run_task(client, await client.post(
        f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 500, "instruction": "写天剑宗的试炼"},
    ))
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "【参考资料（来自知识库" in prompt
    assert "北境第一剑宗" in prompt
    assert prompt.index("【世界观】") < prompt.index("【参考资料（来自知识库")

    # 项目总开关关闭：不再注入
    await client.patch(f"{base}/settings", json={"auto_inject": False})
    fake_llm.push("正文")
    await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 500}))
    assert "参考资料（来自知识库" not in fake_llm.calls[-1]["messages"][-1]["content"]
    fake_llm.push('{"items": []}')
    await run_task(client, await client.post(f"{pbase}/wizard/world", json={"instruction": "天剑宗的门规"}))
    assert "参考资料" not in fake_llm.calls[-1]["messages"][-1]["content"]


async def test_kb_max_chars(client, fake_llm):
    p, base = await _setup(client)
    await create_ai_config(client)
    long = "\n\n".join(f"## 条目{i}\n\n" + f"天剑宗门规第{i}条：弟子不得私斗。" * 20 for i in range(10))
    await _upload(client, base, "规矩.md", long.encode())
    await client.patch(f"{base}/settings", json={"max_chars": 500, "min_score": 0, "top_k": 10})
    fake_llm.push('{"items": []}')
    await run_task(
        client, await client.post(f"/api/v1/projects/{p['id']}/wizard/world", json={"instruction": "天剑宗门规"})
    )
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    header = "【参考资料（来自知识库，仅供参考；与项目设定冲突时以设定为准）】\n"
    block = prompt.split(header, 1)[1].split("\n\n", 1)[0]
    assert 0 < len(block) <= 500


async def test_cross_user_kb_404(client, make_client):
    _, base = await _setup(client)
    body = await _upload(client, base)
    doc_id = body["document"]["id"]
    bob = make_client()
    await register(bob, "bob")
    resp = await bob.post(f"{base}/documents", files={"file": ("x.txt", "资料".encode(), "text/plain")})
    assert resp.status_code == 404
    assert (await bob.get(f"/api/v1/tasks/{body['task']['id']}")).status_code == 404
    assert (await bob.post(f"{base}/documents/{doc_id}/reindex")).status_code == 404
    assert len((await client.get(f"{base}/documents")).json()) == 1


# ---------------- provider 请求格式（假服务） ----------------


async def test_openai_compat_embed_request_format():
    import httpx2
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient

    from app.ai.providers import OpenAICompatProvider

    seen: list[tuple[str, dict]] = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((str(request.url), body))
        # 故意打乱顺序：按 index 还原
        data = [{"object": "embedding", "index": i, "embedding": [float(i), 1.0]} for i in range(len(body["input"]))]
        return httpx2.Response(200, json={"object": "list", "model": body["model"], "data": data[::-1]})

    class Provider(OpenAICompatProvider):
        def _client(self):
            return AsyncOpenAI(
                api_key="k",
                base_url=self.base_url,
                http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
            )

    vectors = await Provider("k", "https://api.example.com/v1").embed(["甲", "乙"], "emb-model")
    assert vectors == [[0.0, 1.0], [1.0, 1.0]]
    url, body = seen[0]
    assert url == "https://api.example.com/v1/embeddings"
    assert body == {"model": "emb-model", "input": ["甲", "乙"], "encoding_format": "float"}

    def failing(request):
        return httpx2.Response(401, json={"error": {"message": "bad key"}})

    class Failing(Provider):
        def _client(self):
            return AsyncOpenAI(
                api_key="k",
                base_url=self.base_url,
                max_retries=0,
                http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(failing)),
            )

    with pytest.raises(AIProviderError) as exc:
        await Failing("k", "https://api.example.com/v1").embed(["甲"], "emb-model")
    assert str(exc.value).startswith("模型调用失败")


async def test_gemini_embed_request_format():
    import httpx
    from google import genai
    from google.genai import types

    from app.ai.providers import GeminiProvider

    seen: list[tuple[str, dict]] = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((str(request.url), body))
        return httpx.Response(
            200, json={"embeddings": [{"values": [1.0, float(i)]} for i in range(len(body["requests"]))]}
        )

    class Provider(GeminiProvider):
        def _client(self):
            return genai.Client(
                api_key="k",
                http_options=types.HttpOptions(async_client_args={"transport": httpx.MockTransport(handler)}),
            )

    vectors = await Provider("k", None).embed(["甲", "乙"], "text-embedding-004")
    assert vectors == [[1.0, 0.0], [1.0, 1.0]]
    url, body = seen[0]
    assert url.endswith("/models/text-embedding-004:batchEmbedContents")
    assert [r["content"]["parts"][0]["text"] for r in body["requests"]] == ["甲", "乙"]
    assert all(r["model"] == "models/text-embedding-004" for r in body["requests"])


async def test_anthropic_embed_unsupported():
    from app.ai.providers import AnthropicProvider

    with pytest.raises(AIProviderError) as exc:
        await AnthropicProvider("k").embed(["甲"], "m")
    assert str(exc.value) == "该提供方不支持向量模型"
