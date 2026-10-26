import json

from sqlalchemy import func, select

from app.models import Character, GenerationLog, Organization, Relationship
from app.services.entity_ai_service import normalize_character_draft, normalize_relation_items
from tests.helpers import Gate, create_ai_config, create_project, register, run_task, wait_task


async def _setup(client):
    await register(client)
    await create_ai_config(client)
    p = await create_project(client)
    return f"/api/v1/projects/{p['id']}"


async def _count(app, model):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(func.count()).select_from(model))


CHARACTER_JSON = {
    "character": {
        "name": "张三",
        "aliases": ["张师", "张三"],
        "role_type": "重要配角",
        "age": "五十",
        "gender": "男",
        "appearance": "白发",
        "personality": "严厉",
        "background": "青云宗长老",
        "profile": "林远的师父",
    },
    "relationships": [
        {
            "target": "小远",
            "self_role": "师父",
            "target_role": "徒弟",
            "category": "social",
            "intimacy": 70,
            "description": "授业",
        },
        {"target": "王五", "self_role": "朋友", "target_role": "朋友", "intimacy": 999},
    ],
}


async def test_character_generate_preview_then_save(app, client, fake_llm):
    """AC5：生成角色作为任务运行；草稿不落库；勾选关系保存后才写入。"""
    base = await _setup(client)
    hero = (
        await client.post(f"{base}/characters", json={"name": "林远", "role_type": "主角", "aliases": ["小远"]})
    ).json()
    await client.post(f"{base}/organizations", json={"name": "青云宗"})
    fake_llm.push("```json\n" + json.dumps(CHARACTER_JSON, ensure_ascii=False) + "\n```")

    resp = await client.post(f"{base}/characters/generate", json={"instruction": "要一位师父", "role_type": "重要配角"})
    assert resp.status_code == 202, resp.text
    assert (resp.json()["kind"], resp.json()["title"]) == ("character.generate", "AI 生成角色")
    assert resp.json()["character_id"] is None
    task, events = await run_task(client, resp)
    assert task["status"] == "succeeded"
    listed = (await client.get("/api/v1/tasks", params={"kind": "character.generate"})).json()
    assert [t["id"] for t in listed] == [task["id"]]

    result = events[-1][1]["result"]
    draft = result["draft"]
    assert (draft["name"], draft["aliases"], draft["role_type"], draft["always_in_context"]) == (
        "张三",
        ["张师"],
        "重要配角",
        False,
    )
    ok, bad = result["relation_suggestions"]
    assert (ok["target_id"], ok["resolved"], ok["subject_is_a"]) == (hero["id"], True, True)
    assert ok["type"]["role_a"] == "师父" and ok["type"]["type_id"] is not None
    assert (bad["target_id"], bad["resolved"], bad["reason"]) == (None, False, "未找到名为「王五」的角色")
    assert bad["intimacy"] == 100

    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "林远（主角） 别名：小远" in prompt and "青云宗" in prompt and "要一位师父" in prompt
    # 不写库、不写生成记录
    assert await _count(app, Character) == 1
    assert await _count(app, Relationship) == 0
    assert await _count(app, GenerationLog) == 0

    draft["name"] = "张长老"  # 预览中编辑
    resp = await client.post(
        f"{base}/characters/with-relations",
        json={
            "character": draft,
            "relations": [
                {
                    "target_id": ok["target_id"],
                    "subject_is_a": ok["subject_is_a"],
                    **ok["type"],
                    "intimacy": ok["intimacy"],
                    "description": ok["description"],
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    saved = resp.json()
    assert saved["character"]["name"] == "张长老"
    rels = (await client.get(f"{base}/relationships", params={"character_id": hero["id"]})).json()
    assert [(r["other_name"], r["other_role"]) for r in rels] == [("张长老", "师父")]


async def test_organization_generate(app, client, fake_llm):
    base = await _setup(client)
    a = (await client.post(f"{base}/characters", json={"name": "甲"})).json()
    parent = (
        await client.post(
            f"{base}/organizations",
            json={
                "name": "天道盟",
                "org_type": "联盟",
                "purpose": "维护正道",
                "location": "中州",
                "power_level": 90,
                "description": "正道魁首",
            },
        )
    ).json()
    await client.post(
        f"{base}/organizations/{parent['id']}/members", json={"character_id": a["id"], "position": "盟主"}
    )
    fake_llm.push(
        json.dumps(
            {
                "organization": {"name": "黑风寨", "org_type": "山寨", "power_level": "60", "parent": "天道盟"},
                "members": [{"name": "甲", "position": "寨主"}, {"name": "无名", "position": "喽啰"}, "甲"],
            },
            ensure_ascii=False,
        )
    )
    task, events = await run_task(client, await client.post(f"{base}/organizations/generate", json={}))
    assert task["status"] == "succeeded" and task["title"] == "AI 生成组织"
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "- 天道盟（联盟）｜势力 90｜所在地：中州" in prompt
    assert "宗旨：维护正道" in prompt and "描述：正道魁首" in prompt and "成员：甲（盟主）" in prompt
    result = events[-1][1]["result"]
    assert result["draft"]["power_level"] == 60 and result["draft"]["parent_id"] == parent["id"]
    assert [(m["character_name"], m["character_id"], m["resolved"]) for m in result["member_suggestions"]] == [
        ("甲", a["id"], True),
        ("无名", None, False),
    ]
    assert await _count(app, Organization) == 1

    d = result["draft"]
    body = {k: d[k] for k in ("name", "org_type", "purpose", "location", "power_level", "description", "parent_id")}
    resp = await client.post(
        f"{base}/organizations/with-members",
        json={"organization": body, "members": [{"character_id": a["id"], "position": "寨主"}]},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["organization"]["parent_id"] == parent["id"]


async def test_relations_parse(app, client, fake_llm):
    """AC6：解析「师父是张三，宿敌李四」→ 两条建议，对不上的标记；确认后写入，旧备注不变。"""
    base = await _setup(client)
    lin = (
        await client.post(f"{base}/characters", json={"name": "林远", "relations_text": "师父是张三，宿敌李四"})
    ).json()
    zhang = (await client.post(f"{base}/characters", json={"name": "张三"})).json()
    fake_llm.push(
        json.dumps(
            {
                "relationships": [
                    {"target": "张三", "self_role": "徒弟", "target_role": "师父", "category": "师徒", "intimacy": 60},
                    {
                        "target": "李四",
                        "self_role": "宿敌",
                        "target_role": "宿敌",
                        "category": "hostile",
                        "intimacy": -80,
                    },
                ]
            },
            ensure_ascii=False,
        )
    )
    resp = await client.post(f"{base}/characters/{lin['id']}/parse-relations")
    assert resp.status_code == 202, resp.text
    assert resp.json()["title"] == "解析旧关系 · 林远"
    task, events = await run_task(client, resp)
    result = events[-1][1]["result"]
    assert result["character_id"] == lin["id"]
    first, second = result["suggestions"]
    # 「林远是张三的徒弟」反向匹配预置「师父/徒弟」：主体是 b 一方
    assert (first["target_id"], first["resolved"], first["subject_is_a"], first["type"]["role_a"]) == (
        zhang["id"],
        True,
        False,
        "师父",
    )
    assert first["type"]["category"] == "social"
    assert (second["resolved"], second["reason"]) == (False, "未找到名为「李四」的角色")
    assert "师父是张三，宿敌李四" in fake_llm.calls[-1]["messages"][-1]["content"]
    assert await _count(app, Relationship) == 0

    a_id, b_id = (lin["id"], zhang["id"]) if first["subject_is_a"] else (zhang["id"], lin["id"])
    resp = await client.post(
        f"{base}/relationships/batch",
        json={"items": [{"a_id": a_id, "b_id": b_id, **first["type"], "intimacy": 60, "source": "ai_parse"}]},
    )
    assert resp.status_code == 201, resp.text
    view = (await client.get(f"{base}/relationships", params={"character_id": lin["id"]})).json()
    assert [(r["other_name"], r["other_role"]) for r in view] == [("张三", "师父")]
    assert (await client.get(f"{base}/characters/{lin['id']}")).json()["relations_text"] == "师父是张三，宿敌李四"

    # 再次解析：已有关系的条目不可勾选
    fake_llm.push('{"relationships": [{"target": "张三", "self_role": "徒弟", "target_role": "师父"}]}')
    _, events = await run_task(client, await client.post(f"{base}/characters/{lin['id']}/parse-relations"))
    again = events[-1][1]["result"]["suggestions"][0]
    assert (again["resolved"], again["reason"], again["existing_relationship_id"]) == (
        False,
        "两人之间已有关系",
        view[0]["id"],
    )


async def test_relations_parse_validation_and_conflict(client, fake_llm):
    base = await _setup(client)
    empty = (await client.post(f"{base}/characters", json={"name": "空"})).json()
    resp = await client.post(f"{base}/characters/{empty['id']}/parse-relations")
    assert resp.status_code == 400 and resp.json()["detail"] == "该角色没有旧关系备注，无需解析"
    assert (await client.post(f"{base}/characters/99999/parse-relations")).status_code == 404

    lin = (await client.post(f"{base}/characters", json={"name": "林远", "relations_text": "朋友张三"})).json()
    gate = Gate()
    fake_llm.push([gate, "不是 JSON"])
    first = await client.post(f"{base}/characters/{lin['id']}/parse-relations")
    assert first.status_code == 202
    assert first.json()["character_id"] == lin["id"]
    await gate.wait_reached()
    # 弹窗重新打开时按 project + kind + active 查询，再按 character_id 接上同一任务
    active = (await client.get("/api/v1/tasks", params={"kind": "relations.parse", "active": "true"})).json()
    assert [(t["id"], t["character_id"]) for t in active] == [(first.json()["id"], lin["id"])]
    resp = await client.post(f"{base}/characters/{lin['id']}/parse-relations")
    assert resp.status_code == 409 and resp.json()["detail"] == "已有任务在进行中，请等待其完成后再试"
    # 生成角色允许并发
    assert (await client.post(f"{base}/characters/generate")).status_code == 202
    gate.release()
    task = await wait_task(client, first.json()["id"])
    assert task["status"] == "failed" and "解析失败" in task["error"]


async def test_entity_tasks_require_ai_config(client):
    await register(client)
    p = await create_project(client)
    resp = await client.post(f"/api/v1/projects/{p['id']}/characters/generate")
    assert resp.status_code == 400 and "尚未配置文本模型" in resp.json()["detail"]
    assert (await client.get("/api/v1/tasks")).json() == []


def test_normalize_tolerates_formats():
    draft, rels = normalize_character_draft([{"name": "甲", "aliases": "阿甲、小甲", "role": "反派"}])
    assert (draft["name"], draft["aliases"], draft["role_type"], rels) == ("甲", ["阿甲", "小甲"], "反派", [])
    rows = normalize_relation_items([{"name": "乙", "relation": "师父"}, {"target": "丙"}, "x"])
    assert rows == [
        {
            "target": "乙",
            "self_role": "师父",
            "target_role": "师父",
            "category": "other",
            "intimacy": 0,
            "description": "",
        }
    ]
