from sqlalchemy import func, select

from app.models import (
    ChapterCharacter,
    Organization,
    OrganizationMember,
    Relationship,
    RelationshipHistory,
    RelationshipType,
)
from tests.helpers import create_project, register


async def _setup(client):
    await register(client)
    p = await create_project(client)
    return f"/api/v1/projects/{p['id']}"


async def _char(client, base, name, **kw):
    resp = await client.post(f"{base}/characters", json={"name": name, **kw})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _type_id(client, base, role_a, role_b):
    types = (await client.get(f"{base}/relationship-types")).json()
    return next(t["id"] for t in types if (t["role_a"], t["role_b"]) == (role_a, role_b))


async def _count(app, model, *where):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(func.count()).select_from(model).where(*where))


async def test_character_structured_fields(client):
    base = await _setup(client)
    hero = await _char(client, base, "林远", role_type="主角")
    assert hero["always_in_context"] is True and hero["aliases"] == [] and hero["life_status"] == "alive"
    side = await _char(client, base, "苏晚", role_type="配角")
    assert side["always_in_context"] is False
    # 自由文本定位照常保留
    legacy = await _char(client, base, "白夜", role_type="神秘人", always_in_context=True)
    assert legacy["role_type"] == "神秘人" and legacy["always_in_context"] is True

    resp = await client.patch(
        f"{base}/characters/{side['id']}",
        json={
            "aliases": [" 晚晚 ", "苏姑娘", "晚晚", ""],
            "age": "18",
            "gender": "女",
            "appearance": "白衣",
            "personality": "冷静",
            "background": "药王谷",
            "current_state": "受伤",
            "life_status": "missing",
        },
    )
    assert resp.status_code == 200, resp.text
    c = resp.json()
    assert c["aliases"] == ["晚晚", "苏姑娘"]
    assert (c["age"], c["gender"], c["life_status"], c["current_state"]) == ("18", "女", "missing", "受伤")
    assert (await client.patch(f"{base}/characters/{side['id']}", json={"life_status": "zombie"})).status_code == 422
    too_many = [f"别名{i}" for i in range(11)]
    resp = await client.patch(f"{base}/characters/{side['id']}", json={"aliases": too_many})
    assert resp.status_code == 422 and "别名最多 10 个" in resp.json()["detail"]


async def test_relationship_perspective_unique_history_and_cascade(app, client):
    """AC3：视角称呼、角色对唯一、修改写历史、删除角色级联清理。"""
    base = await _setup(client)
    a = await _char(client, base, "A")
    b = await _char(client, base, "B")
    master = await _type_id(client, base, "师父", "徒弟")

    resp = await client.post(
        f"{base}/relationships", json={"a_id": a["id"], "b_id": b["id"], "type_id": master, "intimacy": 50}
    )
    assert resp.status_code == 201, resp.text
    rel = resp.json()
    assert (rel["a_name"], rel["b_name"], rel["role_a"], rel["role_b"]) == ("A", "B", "师父", "徒弟")

    from_a = (await client.get(f"{base}/relationships", params={"character_id": a["id"]})).json()
    assert [(r["other_role"], r["other_name"], r["self_role"]) for r in from_a] == [("徒弟", "B", "师父")]
    from_b = (await client.get(f"{base}/relationships", params={"character_id": b["id"]})).json()
    assert [(r["other_role"], r["other_name"], r["self_role"]) for r in from_b] == [("师父", "A", "徒弟")]

    # 同一对角色（反向）再建 → 409
    resp = await client.post(f"{base}/relationships", json={"a_id": b["id"], "b_id": a["id"], "type_id": master})
    assert resp.status_code == 409 and resp.json()["detail"] == "这两个角色之间已有关系"
    # 与自己 → 400
    resp = await client.post(f"{base}/relationships", json={"a_id": a["id"], "b_id": a["id"], "type_id": master})
    assert resp.status_code == 400

    history = (await client.get(f"{base}/relationships/{rel['id']}/history")).json()
    assert len(history) == 1 and history[0]["source"] == "manual"
    assert history[0]["changes"]["type"] == [None, "师父/徒弟"]
    assert history[0]["changes"]["direction"] == [None, "A→B"]

    resp = await client.patch(f"{base}/relationships/{rel['id']}", json={"intimacy": 80, "chapter_no": 3, "note": "x"})
    assert resp.status_code == 200 and resp.json()["intimacy"] == 80
    history = (await client.get(f"{base}/relationships/{rel['id']}/history")).json()
    assert len(history) == 2
    assert history[0]["changes"] == {"intimacy": [50, 80]} and history[0]["chapter_no"] == 3
    # 无变化不写历史
    await client.patch(f"{base}/relationships/{rel['id']}", json={"intimacy": 80})
    assert len((await client.get(f"{base}/relationships/{rel['id']}/history")).json()) == 2
    # 调换方向：B 成为师父
    resp = await client.patch(f"{base}/relationships/{rel['id']}", json={"a_id": b["id"]})
    assert (resp.json()["a_id"], resp.json()["b_id"]) == (b["id"], a["id"])
    assert (await client.get(f"{base}/relationships/{rel['id']}/history")).json()[0]["changes"] == {
        "direction": ["A→B", "B→A"]
    }
    assert (await client.patch(f"{base}/relationships/{rel['id']}", json={"a_id": 99999})).status_code == 400

    # 成员与出场记录也随角色删除
    org = (await client.post(f"{base}/organizations", json={"name": "青云宗"})).json()
    await client.post(f"{base}/organizations/{org['id']}/members", json={"character_id": a["id"]})
    ch = (await client.post(f"{base}/chapters", json={"title": "一"})).json()
    await client.put(f"{base}/chapters/{ch['id']}/cast", json={"character_ids": [a["id"]]})

    assert (await client.delete(f"{base}/characters/{a['id']}")).status_code == 204
    assert await _count(app, Relationship) == 0
    assert await _count(app, RelationshipHistory) == 0
    assert await _count(app, OrganizationMember) == 0
    assert await _count(app, ChapterCharacter) == 0


async def test_relationship_types(app, client):
    base = await _setup(client)
    types = (await client.get(f"{base}/relationship-types")).json()
    assert len(types) == 20 and all(t["builtin"] for t in types)
    preset = types[0]["id"]

    resp = await client.post(
        f"{base}/relationship-types", json={"category": "work", "role_a": "掌门", "role_b": "弟子"}
    )
    assert resp.status_code == 201 and resp.json()["builtin"] is False
    custom = resp.json()["id"]
    # 重复（含反向、与预置相同）→ 409
    for body in (
        {"role_a": "掌门", "role_b": "弟子"},
        {"role_a": "弟子", "role_b": "掌门"},
        {"role_a": "师父", "role_b": "徒弟"},
    ):
        assert (await client.post(f"{base}/relationship-types", json=body)).status_code == 409

    resp = await client.delete(f"{base}/relationship-types/{preset}")
    assert resp.status_code == 400 and resp.json()["detail"] == "预置关系类型不能修改或删除"

    a = await _char(client, base, "甲")
    b = await _char(client, base, "乙")
    await client.post(f"{base}/relationships", json={"a_id": a["id"], "b_id": b["id"], "type_id": custom})
    resp = await client.delete(f"{base}/relationship-types/{custom}")
    assert resp.status_code == 409 and resp.json()["detail"] == "该关系类型仍被关系使用，无法删除"

    # 其他项目的自定义类型不可见、不可用
    other = (await client.post("/api/v1/projects", json={"title": "另一本"})).json()
    obase = f"/api/v1/projects/{other['id']}"
    assert len((await client.get(f"{obase}/relationship-types")).json()) == 20
    x = await _char(client, obase, "X")
    y = await _char(client, obase, "Y")
    resp = await client.post(f"{obase}/relationships", json={"a_id": x["id"], "b_id": y["id"], "type_id": custom})
    assert resp.status_code == 400
    assert (await client.delete(f"{obase}/relationship-types/{custom}")).status_code == 404
    # 跨项目角色 → 400
    resp = await client.post(f"{obase}/relationships", json={"a_id": x["id"], "b_id": a["id"], "type_id": preset})
    assert resp.status_code == 400

    # 使用中的自定义类型不妨碍删除整个项目；项目下的类型、关系、历史、组织、成员、出场记录一并清理
    org = (await client.post(f"{base}/organizations", json={"name": "宗门"})).json()
    await client.post(f"{base}/organizations/{org['id']}/members", json={"character_id": a["id"]})
    ch = (await client.post(f"{base}/chapters", json={"title": "一"})).json()
    await client.put(f"{base}/chapters/{ch['id']}/cast", json={"character_ids": [a["id"], b["id"]]})
    assert (await client.delete(base)).status_code == 204
    assert await _count(app, RelationshipType, RelationshipType.project_id.is_not(None)) == 0
    assert await _count(app, RelationshipType) == 20
    assert await _count(app, Relationship) == 0
    assert await _count(app, RelationshipHistory) == 0
    assert await _count(app, Organization) == 0
    assert await _count(app, OrganizationMember) == 0
    assert await _count(app, ChapterCharacter) == 0


async def test_relationship_by_roles_and_batch(client):
    base = await _setup(client)
    a = await _char(client, base, "甲")
    b = await _char(client, base, "乙")
    c = await _char(client, base, "丙")
    # 按称呼反向匹配预置「师父/徒弟」：甲是乙的徒弟 → 存为 乙 是 甲 的师父
    resp = await client.post(
        f"{base}/relationships", json={"a_id": a["id"], "b_id": b["id"], "role_a": "徒弟", "role_b": "师父"}
    )
    rel = resp.json()
    assert (rel["a_id"], rel["role_a"], rel["role_b"]) == (b["id"], "师父", "徒弟")

    resp = await client.post(
        f"{base}/relationships/batch",
        json={
            "items": [
                {
                    "a_id": a["id"],
                    "b_id": c["id"],
                    "role_a": "盟主",
                    "role_b": "盟友",
                    "category": "work",
                    "source": "ai_parse",
                },
                {"a_id": b["id"], "b_id": c["id"], "role_a": "朋友", "role_b": "朋友"},
            ]
        },
    )
    assert resp.status_code == 201, resp.text
    assert [(r["role_a"], r["category"]) for r in resp.json()] == [("盟主", "work"), ("朋友", "social")]
    hist = (await client.get(f"{base}/relationships/{resp.json()[0]['id']}/history")).json()
    assert hist[0]["source"] == "ai_parse"
    assert any(t["role_a"] == "盟主" for t in (await client.get(f"{base}/relationship-types")).json())

    # 批量中任一条失败 → 全部不写入
    d = await _char(client, base, "丁")
    resp = await client.post(
        f"{base}/relationships/batch",
        json={
            "items": [
                {"a_id": a["id"], "b_id": d["id"], "role_a": "朋友"},
                {"a_id": a["id"], "b_id": b["id"], "role_a": "朋友"},
            ]
        },
    )
    assert resp.status_code == 409
    assert (await client.get(f"{base}/relationships", params={"character_id": d["id"]})).json() == []


async def test_character_with_relations_and_graph(client):
    base = await _setup(client)
    hero = await _char(client, base, "林远", role_type="主角")
    master = await _type_id(client, base, "师父", "徒弟")
    resp = await client.post(
        f"{base}/characters/with-relations",
        json={
            "character": {"name": "张三", "role_type": "重要配角", "aliases": ["张师"]},
            "relations": [
                {
                    "target_id": hero["id"],
                    "type_id": master,
                    "subject_is_a": True,
                    "intimacy": 70,
                    "description": "授业",
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["character"]["name"] == "张三"
    assert [(r["other_name"], r["other_role"]) for r in data["relationships"]] == [("林远", "徒弟")]

    # 任一关系非法 → 角色也不写入
    resp = await client.post(
        f"{base}/characters/with-relations",
        json={"character": {"name": "幽灵"}, "relations": [{"target_id": 99999, "type_id": master}]},
    )
    assert resp.status_code == 400
    assert [c["name"] for c in (await client.get(f"{base}/characters")).json()] == ["林远", "张三"]

    org = (await client.post(f"{base}/organizations", json={"name": "青云宗"})).json()
    await client.post(f"{base}/organizations/{org['id']}/members", json={"character_id": hero["id"]})
    g = (await client.get(f"{base}/relationship-graph")).json()
    nodes = {n["name"]: n for n in g["nodes"]}
    assert nodes["林远"]["is_protagonist"] and nodes["林远"]["organization_ids"] == [org["id"]]
    assert nodes["张三"]["aliases"] == ["张师"]
    assert [(e["source"], e["target"], e["label"], e["intimacy"]) for e in g["edges"]] == [
        (data["character"]["id"], hero["id"], "师父/徒弟", 70)
    ]
    assert [o["name"] for o in g["organizations"]] == ["青云宗"]
