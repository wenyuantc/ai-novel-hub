from typing import Any

from app.services.card_service import rename_for
from tests.helpers import create_project, register


def _id_keys(obj: Any) -> list[str]:
    """递归找出所有 id / *_id 键（卡片不得含内部 ID）。"""
    if isinstance(obj, dict):
        own = [k for k in obj if k == "id" or k.endswith("_id")]
        return own + [k for v in obj.values() for k in _id_keys(v)]
    if isinstance(obj, list):
        return [k for v in obj for k in _id_keys(v)]
    return []


async def _char(client, base, name, **kw):
    resp = await client.post(f"{base}/characters", json={"name": name, **kw})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _source_project(client):
    """项目甲：3 个角色、1 个组织（2 名成员）、2 条关系 + 1 个未导出的角色。"""
    p = await create_project(client, title="甲")
    base = f"/api/v1/projects/{p['id']}"
    a = await _char(client, base, "林远", role_type="主角", aliases=["小远"], personality="坚韧")
    b = await _char(client, base, "张三", role_type="重要配角")
    c = await _char(client, base, "李四", role_type="反派")
    outsider = await _char(client, base, "路人")
    org = (await client.post(f"{base}/organizations", json={"name": "青云宗", "org_type": "宗门"})).json()
    for cid, pos in ((a["id"], "弟子"), (b["id"], "长老"), (outsider["id"], "杂役")):
        await client.post(f"{base}/organizations/{org['id']}/members", json={"character_id": cid, "position": pos})
    rels = [
        {"a_id": b["id"], "b_id": a["id"], "role_a": "师父", "role_b": "徒弟", "intimacy": 80},
        {"a_id": a["id"], "b_id": c["id"], "role_a": "宿命之敌", "role_b": "宿命之敌", "category": "hostile"},
        {"a_id": a["id"], "b_id": outsider["id"], "role_a": "朋友"},
    ]
    for r in rels:
        assert (await client.post(f"{base}/relationships", json=r)).status_code == 201
    return base, [a, b, c], org


async def test_cards_roundtrip_with_rename(client):
    """AC7：甲导出 3 角色 + 1 组织，导入乙；冲突重命名后角色 / 组织 / 成员 / 关系全部重建，与甲的 id 无关。"""
    await register(client)
    base, chars, org = await _source_project(client)
    resp = await client.post(
        f"{base}/cards/export", json={"character_ids": [c["id"] for c in chars], "organization_ids": [org["id"]]}
    )
    assert resp.status_code == 200, resp.text
    card = resp.json()
    assert (card["format"], card["version"]) == ("ainovel.cards", 1)
    assert [c["name"] for c in card["characters"]] == ["林远", "张三", "李四"]
    assert card["characters"][0]["aliases"] == ["小远"]
    # 成员 / 关系只含导出集合内的
    assert [m["character_name"] for m in card["organizations"][0]["members"]] == ["林远", "张三"]
    assert {(r["a_name"], r["b_name"], r["role_a"]) for r in card["relationships"]} == {
        ("张三", "林远", "师父"),
        ("林远", "李四", "宿命之敌"),
    }
    assert not _id_keys(card)

    # 项目乙：已有同名「林远」与「青云宗」，以及「林远（导入）」
    p2 = await create_project(client, title="乙")
    base2 = f"/api/v1/projects/{p2['id']}"
    await _char(client, base2, "林远")
    await _char(client, base2, "林远（导入）")
    await client.post(f"{base2}/organizations", json={"name": "青云宗"})

    resp = await client.post(f"{base2}/cards/import/preview", json={"data": card})
    assert resp.status_code == 200, resp.text
    pv = resp.json()
    assert [(c["name"], c["status"], c["rename_to"]) for c in pv["characters"]] == [
        ("林远", "conflict", "林远（导入2）"),
        ("张三", "new", None),
        ("李四", "new", None),
    ]
    assert [(o["name"], o["status"]) for o in pv["organizations"]] == [("青云宗", "conflict")]
    assert all(r["status"] == "ok" for r in pv["relationships"])

    resp = await client.post(
        f"{base2}/cards/import",
        json={"data": card, "resolutions": {"characters": {"林远": "rename"}, "organizations": {"青云宗": "rename"}}},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert [(c["name"], c["imported_name"], c["action"]) for c in result["characters"]] == [
        ("林远", "林远（导入2）", "renamed"),
        ("张三", "张三", "created"),
        ("李四", "李四", "created"),
    ]
    assert result["organizations"][0]["imported_name"] == "青云宗（导入）"
    assert (result["relationships_created"], result["members_created"]) == (2, 2)

    imported = {c["name"]: c for c in (await client.get(f"{base2}/characters")).json()}
    assert imported["林远（导入2）"]["personality"] == "坚韧"
    assert imported["林远（导入2）"]["always_in_context"] is True
    new_ids = {imported[n]["id"] for n in ("林远（导入2）", "张三", "李四")}
    assert not new_ids & {c["id"] for c in chars}
    rels = (await client.get(f"{base2}/relationships", params={"character_id": imported["林远（导入2）"]["id"]})).json()
    assert sorted((r["other_name"], r["other_role"]) for r in rels) == [("张三", "师父"), ("李四", "宿命之敌")]
    assert all(r["a_id"] in new_ids and r["b_id"] in new_ids for r in rels)
    # 自定义类型在乙中新建
    assert any(
        t["role_a"] == "宿命之敌" and not t["builtin"] for t in (await client.get(f"{base2}/relationship-types")).json()
    )
    hist = (await client.get(f"{base2}/relationships/{rels[0]['id']}/history")).json()
    assert hist[0]["source"] == "import"
    orgs = {o["name"]: o for o in (await client.get(f"{base2}/organizations")).json()}
    members = (await client.get(f"{base2}/organizations/{orgs['青云宗（导入）']['id']}/members")).json()
    assert [(m["character_name"], m["position"]) for m in members] == [("林远（导入2）", "弟子"), ("张三", "长老")]


async def test_import_skip_links_to_existing(client):
    """跳过同名项时，关系 / 成员关系映射到目标项目已有的同名对象；已有关系的角色对跳过并记入 warnings。"""
    await register(client)
    base, chars, org = await _source_project(client)
    card = (
        await client.post(
            f"{base}/cards/export", json={"character_ids": [c["id"] for c in chars], "organization_ids": [org["id"]]}
        )
    ).json()
    p2 = await create_project(client, title="乙")
    base2 = f"/api/v1/projects/{p2['id']}"
    lin = await _char(client, base2, "林远")
    zhang = await _char(client, base2, "张三")
    await client.post(f"{base2}/relationships", json={"a_id": lin["id"], "b_id": zhang["id"], "role_a": "朋友"})
    qy = (await client.post(f"{base2}/organizations", json={"name": "青云宗"})).json()

    result = (await client.post(f"{base2}/cards/import", json={"data": card})).json()
    assert [c["action"] for c in result["characters"]] == ["skipped", "skipped", "created"]
    assert result["characters"][0]["id"] == lin["id"]
    assert result["relationships_created"] == 1
    assert any("之间已有关系" in w for w in result["warnings"])
    li = next(c for c in (await client.get(f"{base2}/characters")).json() if c["name"] == "李四")
    rels = (await client.get(f"{base2}/relationships", params={"character_id": li["id"]})).json()
    assert [(r["other_id"], r["other_role"]) for r in rels] == [(lin["id"], "宿命之敌")]
    members = (await client.get(f"{base2}/organizations/{qy['id']}/members")).json()
    assert {m["character_id"] for m in members} == {lin["id"], zhang["id"]}


async def test_import_unresolved_and_parent_cycle(client):
    await register(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    card = {
        "format": "ainovel.cards",
        "version": 1,
        "characters": [{"name": "甲", "id": 1, "life_status": "unknown"}, {"name": "甲"}, {"name": "丙"}],
        "organizations": [
            {"name": "A", "parent_name": "B", "members": [{"character_name": "无名氏"}]},
            {"name": "B", "parent_name": "A"},
            {"name": "C", "parent_name": "不存在"},
        ],
        "relationships": [
            {"a_name": "甲", "b_name": "乙", "role_a": "朋友", "role_b": "朋友"},
            {"a_name": "甲", "b_name": "甲", "role_a": "朋友", "role_b": "朋友"},
            # 只有空白的称呼：跳过并记 warning，不中断整个导入
            {"a_name": "甲", "b_name": "丙", "role_a": "  ", "role_b": " "},
        ],
    }
    pv = (await client.post(f"{base}/cards/import/preview", json={"data": card})).json()
    assert [r["status"] for r in pv["relationships"]] == ["unresolved", "unresolved", "unresolved"]
    assert any("重复的角色「甲」" in w for w in pv["warnings"])
    assert any("无名氏" in w for w in pv["warnings"])

    result = (await client.post(f"{base}/cards/import", json={"data": card})).json()
    assert len(result["characters"]) == 2 and result["relationships_created"] == 0
    assert any("循环引用" in w for w in result["warnings"])
    assert any("缺少称呼" in w for w in result["warnings"])
    orgs = {o["name"]: o for o in (await client.get(f"{base}/organizations")).json()}
    ids = {o["id"] for o in orgs.values()}
    # A 挂到 B 下，B → A 会成环被忽略；C 的上级不存在
    assert orgs["A"]["parent_id"] == orgs["B"]["id"] and orgs["B"]["parent_id"] is None
    assert orgs["C"]["parent_id"] is None and orgs["A"]["parent_id"] in ids
    chars = (await client.get(f"{base}/characters")).json()
    assert [(c["name"], c["life_status"]) for c in chars] == [("甲", "alive"), ("丙", "alive")]


async def test_import_rejects_bad_files(client):
    """AC10：格式错误 / 超过大小上限返回 400 中文提示。"""
    await register(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    cases = [
        ([1, 2], "应为 JSON 对象"),
        ({"format": "other", "version": 1}, "不是有效的卡片文件"),
        ({"format": "ainovel.cards", "version": 2}, "不支持的卡片版本"),
        ({"format": "ainovel.cards", "version": 1, "characters": [{"name": ""}]}, "卡片文件格式不正确"),
        ({"format": "ainovel.cards", "version": 1, "characters": "x"}, "卡片文件格式不正确"),
        (
            {"format": "ainovel.cards", "version": 1, "characters": [{"name": "大", "profile": "字" * (1024 * 1024)}]},
            "卡片文件过大",
        ),
    ]
    for data, msg in cases:
        for url in (f"{base}/cards/import/preview", f"{base}/cards/import"):
            resp = await client.post(url, json={"data": data})
            assert resp.status_code == 400, (data if len(str(data)) < 200 else url, resp.text[:200])
            assert msg in resp.json()["detail"]
    assert (await client.get(f"{base}/characters")).json() == []
    resp = await client.post(f"{base}/cards/export", json={"character_ids": [99999]})
    assert resp.status_code == 400 and resp.json()["detail"] == "部分角色不存在或不属于该项目"


def test_rename_for():
    assert rename_for("甲", {"甲"}) == "甲（导入）"
    assert rename_for("甲", {"甲", "甲（导入）", "甲（导入2）"}) == "甲（导入3）"
    long = "长" * 100
    renamed = rename_for(long, {long})
    assert len(renamed) == 100 and renamed.endswith("（导入）")
