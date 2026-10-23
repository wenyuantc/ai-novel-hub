from tests.helpers import create_project, register


async def test_project_crud(client):
    await register(client)
    p = await create_project(client)
    assert p["status"] == "planning"
    assert p["chapter_count"] == 0 and p["total_words"] == 0

    resp = await client.patch(f"/api/v1/projects/{p['id']}", json={"title": "新书名", "status": "writing"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "新书名"
    assert resp.json()["status"] == "writing"

    listed = (await client.get("/api/v1/projects")).json()
    assert [x["id"] for x in listed] == [p["id"]]

    assert (await client.delete(f"/api/v1/projects/{p['id']}")).status_code == 204
    assert (await client.get(f"/api/v1/projects/{p['id']}")).status_code == 404


async def test_requires_login(client):
    assert (await client.get("/api/v1/projects")).status_code == 401


async def test_world_character_outline_crud(client):
    await register(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"

    w = (await client.post(f"{base}/world", json={"title": "星际联邦", "content": "由十二个星系组成"})).json()
    assert w["sort"] == 1
    w2 = (await client.post(f"{base}/world", json={"title": "跃迁技术"})).json()
    assert w2["sort"] == 2
    resp = await client.patch(f"{base}/world/{w['id']}", json={"content": "改"})
    assert resp.json()["content"] == "改"
    assert len((await client.get(f"{base}/world")).json()) == 2
    assert (await client.delete(f"{base}/world/{w2['id']}")).status_code == 204

    c = (
        await client.post(
            f"{base}/characters",
            json={"name": "林舟", "role_type": "主角", "profile": "舰长", "relations_text": "与苏晚是搭档"},
        )
    ).json()
    assert c["name"] == "林舟"
    assert (await client.get(f"{base}/characters/{c['id']}")).json()["relations_text"] == "与苏晚是搭档"

    o = (await client.post(f"{base}/outlines", json={"volume_no": 1, "chapter_no": 1, "title": "启航"})).json()
    assert o["volume_title"] == ""
    resp = await client.patch(f"{base}/outlines/{o['id']}", json={"summary": "飞船出发"})
    assert resp.json()["summary"] == "飞船出发"

    # 删除项目级联删除子数据
    assert (await client.delete(base)).status_code == 204
    assert (await client.get(f"{base}/world")).status_code == 404


async def test_cross_user_isolation(client, make_client):
    await register(client, "alice")
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    w = (await client.post(f"{base}/world", json={"title": "设定"})).json()
    ch = (await client.post(f"{base}/chapters", json={"title": "第一章", "content": "正文"})).json()

    bob = make_client()
    await register(bob, "bob")
    assert (await bob.get("/api/v1/projects")).json() == []

    checks = [
        ("GET", base),
        ("PATCH", base),
        ("DELETE", base),
        ("GET", f"{base}/world"),
        ("POST", f"{base}/world"),
        ("GET", f"{base}/world/{w['id']}"),
        ("PATCH", f"{base}/world/{w['id']}"),
        ("DELETE", f"{base}/world/{w['id']}"),
        ("GET", f"{base}/characters"),
        ("GET", f"{base}/outlines"),
        ("GET", f"{base}/chapters"),
        ("POST", f"{base}/chapters"),
        ("POST", f"{base}/chapters/from-outlines"),
        ("GET", f"{base}/chapters/{ch['id']}"),
        ("PATCH", f"{base}/chapters/{ch['id']}"),
        ("DELETE", f"{base}/chapters/{ch['id']}"),
        ("POST", f"/api/v1/chapters/{ch['id']}/generate"),
        ("POST", f"/api/v1/chapters/{ch['id']}/polish"),
        ("POST", f"{base}/wizard/outline"),
        ("GET", f"{base}/export?format=txt"),
    ]
    for method, url in checks:
        body = None
        if method in ("POST", "PATCH"):
            body = {"title": "x"} if url.endswith("/world") else {}
        resp = await bob.request(method, url, json=body)
        assert resp.status_code == 404, (method, url, resp.text)

    # bob 不能把 alice 的项目挂到自己名下的章节里，alice 数据保持不变
    assert (await client.get(f"{base}/chapters/{ch['id']}")).json()["content"] == "正文"
    assert len((await client.get(f"{base}/world")).json()) == 1
