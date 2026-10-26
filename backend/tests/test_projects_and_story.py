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
    assert w["sort"] == 1 and w["category"] == "other"
    w2 = (await client.post(f"{base}/world", json={"title": "跃迁技术"})).json()
    assert w2["sort"] == 2
    resp = await client.patch(f"{base}/world/{w['id']}", json={"content": "改", "category": "faction"})
    assert resp.json()["content"] == "改" and resp.json()["category"] == "faction"
    assert (await client.patch(f"{base}/world/{w['id']}", json={"category": "planet"})).status_code == 422
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
    c1 = (await client.post(f"{base}/characters", json={"name": "甲", "relations_text": "乙的朋友"})).json()
    c2 = (await client.post(f"{base}/characters", json={"name": "乙"})).json()
    org = (await client.post(f"{base}/organizations", json={"name": "宗门"})).json()
    member = (await client.post(f"{base}/organizations/{org['id']}/members", json={"character_id": c1["id"]})).json()
    rel = (
        await client.post(f"{base}/relationships", json={"a_id": c1["id"], "b_id": c2["id"], "role_a": "朋友"})
    ).json()
    custom_type = (await client.post(f"{base}/relationship-types", json={"role_a": "掌门", "role_b": "弟子"})).json()
    career = (await client.post(f"{base}/careers", json={"name": "剑修", "stages": [{"name": "练气"}]})).json()
    assignment = (await client.post(f"{base}/characters/{c1['id']}/careers", json={"career_id": career["id"]})).json()

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
        ("GET", f"{base}/world?category=place"),
        ("PATCH", f"{base}/characters/{c1['id']}"),
        ("DELETE", f"{base}/characters/{c1['id']}"),
        ("POST", f"{base}/characters/with-relations"),
        ("POST", f"{base}/characters/generate"),
        ("POST", f"{base}/characters/{c1['id']}/parse-relations"),
        ("GET", f"{base}/characters/{c1['id']}/memberships"),
        ("GET", f"{base}/organizations"),
        ("POST", f"{base}/organizations"),
        ("POST", f"{base}/organizations/with-members"),
        ("POST", f"{base}/organizations/generate"),
        ("GET", f"{base}/organizations/{org['id']}"),
        ("PATCH", f"{base}/organizations/{org['id']}"),
        ("DELETE", f"{base}/organizations/{org['id']}"),
        ("GET", f"{base}/organizations/{org['id']}/members"),
        ("POST", f"{base}/organizations/{org['id']}/members"),
        ("PATCH", f"{base}/organizations/{org['id']}/members/{member['id']}"),
        ("DELETE", f"{base}/organizations/{org['id']}/members/{member['id']}"),
        ("GET", f"{base}/relationship-types"),
        ("POST", f"{base}/relationship-types"),
        ("DELETE", f"{base}/relationship-types/{custom_type['id']}"),
        ("GET", f"{base}/relationships"),
        ("POST", f"{base}/relationships"),
        ("POST", f"{base}/relationships/batch"),
        ("PATCH", f"{base}/relationships/{rel['id']}"),
        ("DELETE", f"{base}/relationships/{rel['id']}"),
        ("GET", f"{base}/relationships/{rel['id']}/history"),
        ("GET", f"{base}/relationship-graph"),
        ("GET", f"{base}/chapters/{ch['id']}/cast"),
        ("PUT", f"{base}/chapters/{ch['id']}/cast"),
        ("POST", f"{base}/cards/export"),
        ("POST", f"{base}/cards/import/preview"),
        ("POST", f"{base}/cards/import"),
        ("GET", f"{base}/careers"),
        ("POST", f"{base}/careers"),
        ("POST", f"{base}/careers/batch"),
        ("POST", f"{base}/careers/generate"),
        ("GET", f"{base}/careers/{career['id']}"),
        ("PATCH", f"{base}/careers/{career['id']}"),
        ("DELETE", f"{base}/careers/{career['id']}"),
        ("GET", f"{base}/careers/{career['id']}/holders"),
        ("GET", f"{base}/character-careers"),
        ("GET", f"{base}/characters/{c1['id']}/careers"),
        ("POST", f"{base}/characters/{c1['id']}/careers"),
        ("PATCH", f"{base}/characters/{c1['id']}/careers/{assignment['id']}"),
        ("DELETE", f"{base}/characters/{c1['id']}/careers/{assignment['id']}"),
        ("GET", f"{base}/characters/{c1['id']}/career-history"),
        ("POST", f"{base}/wizard/careers"),
    ]
    # 请求体在路由层通过 422 校验，确保 404 来自归属校验
    bodies = {
        f"{base}/world": {"title": "x"},
        f"{base}/characters/with-relations": {"character": {"name": "x"}},
        f"{base}/organizations": {"name": "x"},
        f"{base}/organizations/with-members": {"organization": {"name": "x"}},
        f"{base}/organizations/{org['id']}/members": {"character_id": c1["id"]},
        f"{base}/relationship-types": {"role_a": "x", "role_b": "y"},
        f"{base}/relationships": {"a_id": c1["id"], "b_id": c2["id"], "role_a": "x"},
        f"{base}/relationships/batch": {"items": [{"a_id": c1["id"], "b_id": c2["id"], "role_a": "x"}]},
        f"{base}/chapters/{ch['id']}/cast": {"character_ids": []},
        f"{base}/cards/import/preview": {"data": {}},
        f"{base}/cards/import": {"data": {}},
        f"{base}/careers": {"name": "x", "stages": [{"name": "一"}]},
        f"{base}/careers/batch": {"careers": [{"name": "x", "stages": [{"name": "一"}]}]},
        f"{base}/characters/{c1['id']}/careers": {"career_id": career["id"]},
    }
    for method, url in checks:
        body = None
        if method in ("POST", "PATCH", "PUT"):
            body = bodies.get(url, {})
        resp = await bob.request(method, url, json=body)
        assert resp.status_code == 404, (method, url, resp.text)

    # bob 不能把 alice 的项目挂到自己名下的章节里，alice 数据保持不变
    assert (await client.get(f"{base}/chapters/{ch['id']}")).json()["content"] == "正文"
    assert len((await client.get(f"{base}/world")).json()) == 1
    assert len((await client.get(f"{base}/characters")).json()) == 2
    assert len((await client.get(f"{base}/relationships")).json()) == 1
    assert len((await client.get(f"{base}/organizations/{org['id']}/members")).json()) == 1
    assert len((await client.get(f"{base}/characters/{c1['id']}/careers")).json()) == 1
    assert len((await client.get(f"{base}/characters/{c1['id']}/career-history")).json()) == 1
