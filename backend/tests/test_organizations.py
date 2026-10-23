from tests.helpers import create_project, register


async def _setup(client):
    await register(client)
    p = await create_project(client)
    return f"/api/v1/projects/{p['id']}"


async def _org(client, base, name, **kw):
    resp = await client.post(f"{base}/organizations", json={"name": name, **kw})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_org_crud_and_cycle(client):
    """AC2：嵌套组织，设置循环上级被拒绝。"""
    base = await _setup(client)
    root = await _org(client, base, "天道盟", org_type="联盟", power_level=90)
    assert root["sort"] == 1 and root["parent_id"] is None and root["member_count"] == 0
    mid = await _org(client, base, "青云宗", parent_id=root["id"])
    leaf = await _org(client, base, "内门", parent_id=mid["id"])

    resp = await client.post(f"{base}/organizations", json={"name": "青云宗"})
    assert resp.status_code == 409 and resp.json()["detail"] == "同名组织已存在"
    assert (await client.post(f"{base}/organizations", json={"name": "x", "power_level": 101})).status_code == 422

    for parent in (leaf["id"], mid["id"], root["id"]):
        resp = await client.patch(f"{base}/organizations/{root['id']}", json={"parent_id": parent})
        assert resp.status_code == 400, parent
        assert resp.json()["detail"] == "不能把组织自身或其下级组织设为上级"
    resp = await client.patch(f"{base}/organizations/{leaf['id']}", json={"parent_id": 99999})
    assert resp.status_code == 400 and resp.json()["detail"] == "上级组织不存在或不属于该项目"

    # 显式 null 清空上级与势力等级；省略不变
    resp = await client.patch(f"{base}/organizations/{mid['id']}", json={"parent_id": None, "purpose": "修仙"})
    assert resp.json()["parent_id"] is None and resp.json()["purpose"] == "修仙"
    resp = await client.patch(f"{base}/organizations/{root['id']}", json={"power_level": None})
    assert resp.json()["power_level"] is None and resp.json()["org_type"] == "联盟"
    # 现在 root 可以挂到 leaf 下（不再成环）
    assert (await client.patch(f"{base}/organizations/{root['id']}", json={"parent_id": leaf["id"]})).status_code == 200
    resp = await client.patch(f"{base}/organizations/{leaf['id']}", json={"name": "天道盟"})
    assert resp.status_code == 409

    # 删除上级：下级变为顶级
    assert (await client.delete(f"{base}/organizations/{mid['id']}")).status_code == 204
    assert (await client.get(f"{base}/organizations/{leaf['id']}")).json()["parent_id"] is None
    assert [o["name"] for o in (await client.get(f"{base}/organizations")).json()] == ["天道盟", "内门"]


async def test_members(client):
    base = await _setup(client)
    org = await _org(client, base, "青云宗")
    hero = (await client.post(f"{base}/characters", json={"name": "林远"})).json()
    resp = await client.post(
        f"{base}/organizations/{org['id']}/members", json={"character_id": hero["id"], "position": "内门弟子"}
    )
    assert resp.status_code == 201, resp.text
    m = resp.json()
    assert (m["character_name"], m["organization_name"], m["status"]) == ("林远", "青云宗", "active")

    resp = await client.post(f"{base}/organizations/{org['id']}/members", json={"character_id": hero["id"]})
    assert resp.status_code == 409 and resp.json()["detail"] == "该角色已是该组织成员"
    resp = await client.post(f"{base}/organizations/{org['id']}/members", json={"character_id": 99999})
    assert resp.status_code == 400 and resp.json()["detail"] == "角色不存在或不属于该项目"

    resp = await client.patch(f"{base}/organizations/{org['id']}/members/{m['id']}", json={"status": "left"})
    assert resp.json()["status"] == "left" and resp.json()["position"] == "内门弟子"
    assert (await client.get(f"{base}/organizations/{org['id']}")).json()["member_count"] == 1
    mine = (await client.get(f"{base}/characters/{hero['id']}/memberships")).json()
    assert [x["organization_name"] for x in mine] == ["青云宗"]

    assert (await client.delete(f"{base}/organizations/{org['id']}/members/{m['id']}")).status_code == 204
    assert (await client.get(f"{base}/organizations/{org['id']}/members")).json() == []
    assert (await client.delete(f"{base}/organizations/{org['id']}/members/{m['id']}")).status_code == 404

    # 删除组织：成员记录随之清理，角色本身保留
    await client.post(f"{base}/organizations/{org['id']}/members", json={"character_id": hero["id"]})
    assert (await client.delete(f"{base}/organizations/{org['id']}")).status_code == 204
    assert (await client.get(f"{base}/characters/{hero['id']}/memberships")).json() == []
    assert (await client.get(f"{base}/characters/{hero['id']}")).status_code == 200


async def test_create_with_members(client):
    base = await _setup(client)
    a = (await client.post(f"{base}/characters", json={"name": "甲"})).json()
    b = (await client.post(f"{base}/characters", json={"name": "乙"})).json()
    resp = await client.post(
        f"{base}/organizations/with-members",
        json={
            "organization": {"name": "黑风寨", "org_type": "山寨"},
            "members": [{"character_id": a["id"], "position": "寨主"}, {"character_id": b["id"]}],
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["organization"]["member_count"] == 2
    assert [(m["character_name"], m["position"]) for m in data["members"]] == [("甲", "寨主"), ("乙", "")]

    # 成员重复 → 组织也不写入
    resp = await client.post(
        f"{base}/organizations/with-members",
        json={"organization": {"name": "白云观"}, "members": [{"character_id": a["id"]}, {"character_id": a["id"]}]},
    )
    assert resp.status_code == 409
    assert [o["name"] for o in (await client.get(f"{base}/organizations")).json()] == ["黑风寨"]
