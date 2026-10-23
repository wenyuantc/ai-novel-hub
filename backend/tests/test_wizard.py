import json

import pytest

from app.ai.json_extract import JSONExtractError, extract_json
from app.services.wizard_service import normalize_outline
from tests.helpers import Gate, create_ai_config, create_project, parse_sse, register, run_task, stream_with_gate

OUTLINE_JSON = {
    "volumes": [
        {
            "volume_no": 1,
            "volume_title": "启程",
            "chapters": [
                {"chapter_no": 1, "title": "出发", "summary": "离港"},
                {"chapter_no": 2, "title": "异象", "summary": "遭遇异常信号"},
            ],
        },
        {
            "volume_no": 2,
            "volume_title": "深空",
            # 模型按卷重新编号，落库时统一为全书连续编号
            "chapters": [{"chapter_no": 1, "title": "接触", "summary": "首次接触"}],
        },
    ]
}

CHARACTERS_JSON = [
    {"name": "林舟", "role_type": "主角", "profile": "舰长", "relations_text": "与苏晚是搭档"},
    {"name": "苏晚", "role_type": "女主", "profile": "科学家", "relations_text": ["林舟的搭档", "白夜的旧识"]},
    {"name": "白夜", "role_type": "反派", "profile": "神秘人", "relations_text": "林舟的宿敌"},
]


@pytest.mark.parametrize(
    "text",
    [
        '[{"a": 1}]',
        '```json\n[{"a": 1}]\n```',
        '好的，以下是结果：\n```json\n[{"a": 1}]\n```\n希望对你有帮助',
        '前言 [{"a": 1}] 结尾说明',
        '```json\n[{"a": 1}]',  # 未闭合围栏
    ],
)
def test_extract_json_tolerant(text):
    assert extract_json(text) == [{"a": 1}]


def test_extract_json_object_first():
    assert extract_json('结果：{"volumes": [1]} 完') == {"volumes": [1]}


def test_extract_json_failure():
    with pytest.raises(JSONExtractError):
        extract_json("抱歉，我无法完成")


def test_normalize_flat_outline():
    rows = normalize_outline([{"title": "甲"}, {"title": "乙", "volume_no": 2}])
    assert [(r["volume_no"], r["chapter_no"]) for r in rows] == [(1, 1), (2, 2)]


async def _setup(client):
    await register(client)
    await create_ai_config(client)
    return await create_project(client)


async def test_wizard_outline_with_fence(app, client, fake_llm):
    p = await _setup(client)
    raw = "好的，这是大纲：\n```json\n" + json.dumps(OUTLINE_JSON, ensure_ascii=False) + "\n```"
    gate = Gate()
    fake_llm.push([gate] + [raw[i : i + 7] for i in range(0, len(raw), 7)])
    resp = await client.post(f"/api/v1/projects/{p['id']}/wizard/outline", json={"volume_count": 2})
    assert resp.status_code == 202
    task = resp.json()
    assert (task["kind"], task["project_id"], task["chapter_id"], task["title"]) == (
        "wizard.outline",
        p["id"],
        None,
        "大纲向导",
    )
    events = parse_sse((await stream_with_gate(app, client, task["id"], gate)).text)
    assert "".join(d["text"] for e, d in events if e == "delta") == raw
    kind, payload = events[-1]
    assert kind == "done" and payload["task"]["status"] == "succeeded"
    data = payload["result"]
    assert data["kind"] == "outline"
    assert [(i["volume_no"], i["chapter_no"], i["title"]) for i in data["items"]] == [
        (1, 1, "出发"),
        (1, 2, "异象"),
        (2, 3, "接触"),
    ]
    assert data["items"][2]["volume_title"] == "深空"

    saved = (await client.get(f"/api/v1/projects/{p['id']}/outlines")).json()
    assert len(saved) == 3
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "星海纪元" in prompt and "共 2 卷" in prompt


async def test_wizard_overwrites_and_keeps_chapters(client, fake_llm):
    p = await _setup(client)
    base = f"/api/v1/projects/{p['id']}"
    fake_llm.push(json.dumps(OUTLINE_JSON, ensure_ascii=False))
    await run_task(client, await client.post(f"{base}/wizard/outline"))
    created = (await client.post(f"{base}/chapters/from-outlines", json={})).json()
    ch_id = created[0]["id"]
    await client.patch(f"{base}/chapters/{ch_id}", json={"content": "已写正文"})

    fake_llm.push('[{"title": "唯一一章", "summary": "重写"}]')
    _, events = await run_task(client, await client.post(f"{base}/wizard/outline"))
    assert events[-1][0] == "done"
    outlines = (await client.get(f"{base}/outlines")).json()
    assert [o["title"] for o in outlines] == ["唯一一章"]

    ch = (await client.get(f"{base}/chapters/{ch_id}")).json()
    assert ch["content"] == "已写正文"
    assert ch["outline_id"] is None  # 旧大纲删除后解除关联而非删除章节


async def test_wizard_characters_and_world(client, fake_llm):
    p = await _setup(client)
    base = f"/api/v1/projects/{p['id']}"
    fake_llm.push(json.dumps({"characters": CHARACTERS_JSON}, ensure_ascii=False))
    _, events = await run_task(client, await client.post(f"{base}/wizard/characters", json={"character_count": 3}))
    assert events[-1][0] == "done"
    assert events[-1][1]["result"]["kind"] == "characters"
    items = events[-1][1]["result"]["items"]
    assert len(items) == 3
    assert items[1]["relations_text"] == "林舟的搭档；白夜的旧识"
    assert len((await client.get(f"{base}/characters")).json()) == 3

    fake_llm.push(
        '```json\n[{"title": "星际联邦", "content": "十二星系"}, {"title": "跃迁", "content": "超光速"}]\n```'
    )
    _, events = await run_task(client, await client.post(f"{base}/wizard/world"))
    assert events[-1][0] == "done"
    world = (await client.get(f"{base}/world")).json()
    assert [w["title"] for w in world] == ["星际联邦", "跃迁"]
    # 世界观提示词带入已有角色
    assert "林舟" in fake_llm.calls[-1]["messages"][-1]["content"]


async def test_wizard_parse_error(client, fake_llm):
    p = await _setup(client)
    base = f"/api/v1/projects/{p['id']}"
    await client.post(f"{base}/world", json={"title": "原设定"})
    fake_llm.push("抱歉，我无法生成。")
    task, events = await run_task(client, await client.post(f"{base}/wizard/world"))
    assert events[-1][0] == "error"
    assert "解析失败" in events[-1][1]["detail"]
    assert task["status"] == "failed" and "解析失败" in task["error"]
    # 解析失败不覆盖旧数据
    assert [w["title"] for w in (await client.get(f"{base}/world")).json()] == ["原设定"]


async def test_wizard_invalid_kind(client):
    p = await _setup(client)
    resp = await client.post(f"/api/v1/projects/{p['id']}/wizard/unknown")
    assert resp.status_code == 422


STRUCTURED_CHARACTERS = {
    "characters": [
        {
            "name": "林远",
            "aliases": ["小远"],
            "role_type": "主角",
            "age": "18",
            "gender": "男",
            "appearance": "清瘦",
            "personality": "坚韧",
            "background": "孤儿",
            "profile": "少年剑客",
        },
        {"name": "张三", "role_type": "重要配角", "profile": "师父"},
        {"name": "李四", "role_type": "反派"},
    ],
    "relationships": [
        {
            "a": "张三",
            "b": "小远",
            "role_a": "师父",
            "role_b": "徒弟",
            "category": "social",
            "intimacy": 80,
            "description": "授业",
        },
        {"a": "林远", "b": "李四", "role_a": "宿敌", "role_b": "宿敌", "category": "敌对", "intimacy": -90},
        {"a": "李四", "b": "林远", "role_a": "朋友", "role_b": "朋友"},  # 同一对已有关系 → 忽略
        {"a": "林远", "b": "王五", "role_a": "朋友"},  # 名字对不上 → 忽略
        {"a": "林远", "b": "林远", "role_a": "朋友"},
    ],
}


async def test_wizard_characters_structured_with_relations(client, fake_llm):
    """AC9：角色向导输出结构化字段与关系并写入关系表；再次生成整体替换（旧关系随角色级联删除）。"""
    p = await _setup(client)
    base = f"/api/v1/projects/{p['id']}"
    fake_llm.push(json.dumps(STRUCTURED_CHARACTERS, ensure_ascii=False))
    _, events = await run_task(client, await client.post(f"{base}/wizard/characters"))
    result = events[-1][1]["result"]
    hero = result["items"][0]
    assert (hero["aliases"], hero["age"], hero["personality"], hero["always_in_context"]) == (
        ["小远"],
        "18",
        "坚韧",
        True,
    )
    assert result["items"][1]["always_in_context"] is False
    assert [(r["a_name"], r["b_name"], r["role_a"], r["category"], r["intimacy"]) for r in result["relationships"]] == [
        ("张三", "林远", "师父", "social", 80),
        ("林远", "李四", "宿敌", "hostile", -90),
    ]
    view = (await client.get(f"{base}/relationships", params={"character_id": hero["id"]})).json()
    assert sorted((r["other_name"], r["other_role"]) for r in view) == [("张三", "师父"), ("李四", "宿敌")]
    hist = (await client.get(f"{base}/relationships/{view[0]['id']}/history")).json()
    assert hist[0]["source"] == "wizard"
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert '"relationships"' in prompt and "role_a" in prompt

    fake_llm.push(json.dumps({"characters": CHARACTERS_JSON}, ensure_ascii=False))
    _, events = await run_task(client, await client.post(f"{base}/wizard/characters"))
    assert events[-1][1]["result"]["relationships"] == []
    assert (await client.get(f"{base}/relationships")).json() == []
    assert [c["name"] for c in (await client.get(f"{base}/characters")).json()] == ["林舟", "苏晚", "白夜"]


async def test_wizard_world_categories(client, fake_llm):
    p = await _setup(client)
    base = f"/api/v1/projects/{p['id']}"
    fake_llm.push(
        json.dumps(
            [
                {"title": "青云山", "category": "地点", "content": "仙山"},
                {"title": "灵力", "category": "rule", "content": "修炼体系"},
                {"title": "魔教", "category": "门派势力", "content": "反派"},
                {"title": "上古之战", "category": "历史事件", "content": "千年前"},
                {"title": "杂项", "category": "???", "content": ""},
                {"title": "旧格式", "content": "无分类"},
            ],
            ensure_ascii=False,
        )
    )
    _, events = await run_task(client, await client.post(f"{base}/wizard/world"))
    cats = [(i["title"], i["category"]) for i in events[-1][1]["result"]["items"]]
    assert cats == [
        ("青云山", "place"),
        ("灵力", "rule"),
        ("魔教", "faction"),
        ("上古之战", "history"),
        ("杂项", "other"),
        ("旧格式", "other"),
    ]
    places = (await client.get(f"{base}/world", params={"category": "place"})).json()
    assert [w["title"] for w in places] == ["青云山"]
    assert (await client.get(f"{base}/world", params={"category": "bad"})).status_code == 422
