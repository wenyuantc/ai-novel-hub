from urllib.parse import quote

from tests.helpers import create_project, register


async def _setup(client):
    await register(client)
    p = await create_project(client, title="星海纪元")
    base = f"/api/v1/projects/{p['id']}"
    c1 = (await client.post(f"{base}/chapters", json={"title": "启航", "content": "第一章正文"})).json()
    c2 = (await client.post(f"{base}/chapters", json={"title": "第二章 异象", "content": "第二章正文"})).json()
    c3 = (await client.post(f"{base}/chapters", json={"title": "空章"})).json()
    return p, base, c1, c2, c3


async def test_export_txt(client):
    p, base, *_ = await _setup(client)
    resp = await client.get(f"{base}/export", params={"format": "txt"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    cd = resp.headers["content-disposition"]
    assert "filename*=UTF-8''" + quote("星海纪元.txt") in cd
    assert 'filename="novel.txt"' in cd
    text = resp.content.decode("utf-8")
    assert text.startswith("《星海纪元》")
    assert "第1章 启航\n\n第一章正文" in text
    assert "第二章 异象\n\n第二章正文" in text  # 已带章节前缀的标题不重复加
    assert "空章" not in text  # 空章节不导出
    assert text.index("第一章正文") < text.index("第二章正文")


async def test_export_md_selected(client):
    p, base, c1, c2, _ = await _setup(client)
    resp = await client.get(f"{base}/export?format=md&chapter_ids={c2['id']}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    text = resp.text
    assert text.startswith("# 星海纪元")
    assert "## 第二章 异象" in text
    assert "第一章正文" not in text

    resp = await client.get(
        f"{base}/export", params=[("format", "md"), ("chapter_ids", c1["id"]), ("chapter_ids", c2["id"])]
    )
    assert "第一章正文" in resp.text and "第二章正文" in resp.text


async def test_export_errors(client):
    p, base, *_ = await _setup(client)
    assert (await client.get(f"{base}/export?format=pdf")).status_code == 422
    assert (await client.get(f"{base}/export?chapter_ids=abc")).status_code == 400
    empty = await create_project(client, title="空书")
    resp = await client.get(f"/api/v1/projects/{empty['id']}/export")
    assert resp.status_code == 400
    assert "没有可导出的章节" in resp.json()["detail"]
