import json

import pytest

from app.ai.json_extract import JSONExtractError, extract_json
from app.services import setting_progression_service
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
    _, events = await run_task(client, await client.post(
        f"{base}/wizard/world", json={"instruction": "为林舟设计世界"},
    ))
    assert events[-1][0] == "done"
    world = (await client.get(f"{base}/world")).json()
    assert [w["title"] for w in world] == ["星际联邦", "跃迁"]
    # 世界观提示词带入已有角色
    assert "■ 林舟" in fake_llm.calls[-1]["messages"][-1]["content"]


async def _setting_timeline(client, base, **params):
    response = await client.get(f"{base}/setting-timelines", params=params)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    return response.json()["items"][0]


async def _record_change(client, url, revision, field_key, value):
    response = await client.post(
        f"{url}/progressions",
        json={
            "expected_revision": revision,
            "field_key": field_key,
            "operation": "replace",
            "value": value,
            "anchor_kind": "planned",
            "chapter_no": 5,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("kind", ["characters", "world"])
async def test_wizard_initializes_confirmed_baselines_in_own_project(client, fake_llm, kind):
    p = await _setup(client)
    other = await create_project(client, title="另一部作品")
    base = f"/api/v1/projects/{p['id']}"
    other_base = f"/api/v1/projects/{other['id']}"
    foreign = await client.post(f"{other_base}/world", json={"title": "外部", "content": "外部基础"})
    assert foreign.status_code == 201, foreign.text
    foreign_timeline = await _setting_timeline(client, other_base, world_setting_id=foreign.json()["id"])
    await _record_change(client, f"{other_base}/setting-timelines/{foreign_timeline['id']}", 1, "content", "外部变化")

    raw = {"characters": CHARACTERS_JSON[:2]} if kind == "characters" else [
        {"title": "青云山", "content": "仙山"}, {"title": "北境", "content": "雪原"}
    ]
    fake_llm.push(json.dumps(raw, ensure_ascii=False))
    task, events = await run_task(client, await client.post(f"{base}/wizard/{kind}"))
    assert task["status"] == "succeeded", task
    for item in events[-1][1]["result"]["items"]:
        param = {"character_id": item["id"], "subject_kind": "character"} if kind == "characters" else {
            "world_setting_id": item["id"], "subject_kind": "world"
        }
        timeline = await _setting_timeline(client, base, **param)
        assert timeline["baseline_status"] == "confirmed"
        assert timeline["legacy_compat_allowed"] is False
        baseline = (await client.get(f"{base}/setting-timelines/{timeline['id']}/baseline")).json()["baseline"]
        field = "profile" if kind == "characters" else "content"
        assert baseline[field] == item[field]
    assert (await client.get(f"{other_base}/world/{foreign.json()['id']}")).json()["content"] == "外部变化"


@pytest.mark.parametrize("kind", ["characters", "world"])
@pytest.mark.parametrize("history", ["progression", "custom_field"])
async def test_wizard_rejects_replace_before_task_when_history_would_be_deleted(client, kind, history):
    p = await _setup(client)
    base = f"/api/v1/projects/{p['id']}"
    endpoint = "characters" if kind == "characters" else "world"
    key, field = ("name", "profile") if kind == "characters" else ("title", "content")
    first = await client.post(f"{base}/{endpoint}", json={key: "先保存", field: "原始甲"})
    second = await client.post(f"{base}/{endpoint}", json={key: "需保留", field: "原始乙"})
    assert first.status_code == second.status_code == 201
    subject = "character_id" if kind == "characters" else "world_setting_id"
    timeline = await _setting_timeline(client, base, **{subject: second.json()["id"]})
    timeline_url = f"{base}/setting-timelines/{timeline['id']}"
    if history == "progression":
        created = await _record_change(client, timeline_url, timeline["revision"], field, "剧情变化后")
        expected_detail = "剧情变化后"
        event_id = created["item"]["id"]
    else:
        created = await client.post(
            f"{timeline_url}/text-fields",
            json={"expected_revision": timeline["revision"], "label": "作者补充", "base_value": "原补充"},
        )
        assert created.status_code == 201, created.text
        expected_detail = "原始乙"

    before = (await client.get(f"{base}/{endpoint}")).json()
    response = await client.post(f"{base}/wizard/{kind}")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "wizard_history_conflict"
    assert "变化" in response.json()["detail"] or "字段" in response.json()["detail"]
    assert (await client.get(f"{base}/{endpoint}")).json() == before
    assert (await client.get(f"{base}/{endpoint}/{second.json()['id']}")).json()[field] == expected_detail
    if history == "progression":
        assert (await client.get(f"{timeline_url}/progressions")).json()["items"][0]["id"] == event_id
    else:
        assert (await client.get(f"{timeline_url}/text-fields")).json()["items"][0]["base_value"] == "原补充"


async def test_wizard_rechecks_history_added_during_generation_before_replacing(app, client, fake_llm):
    p = await _setup(client)
    base = f"/api/v1/projects/{p['id']}"
    world = (await client.post(f"{base}/world", json={"title": "旧山门", "content": "完好"})).json()
    timeline = await _setting_timeline(client, base, world_setting_id=world["id"])
    gate = Gate()
    fake_llm.push([gate, '[{"title":"新山门","content":"替换"}]'])
    response = await client.post(f"{base}/wizard/world")
    assert response.status_code == 202, response.text
    await gate.wait_reached()
    url = f"{base}/setting-timelines/{timeline['id']}"
    created = await _record_change(client, url, timeline["revision"], "content", "新剧情")
    gate.release()
    events = parse_sse((await client.get(f"/api/v1/tasks/{response.json()['id']}/stream")).text)
    assert events[-1][0] == "error"
    assert "变化" in events[-1][1]["detail"]
    assert events[-1][1]["task"]["status"] == "failed"
    assert (await client.get(f"{base}/world/{world['id']}")).json()["content"] == "新剧情"
    assert (await client.get(f"{url}/progressions")).json()["items"][0]["id"] == created["item"]["id"]
    assert [w["title"] for w in (await client.get(f"{base}/world")).json()] == ["旧山门"]


@pytest.mark.parametrize(
    ("subject_kind", "wizard_kind", "field"),
    [("relationship", "characters", "description"), ("career", "characters", "note"),
     ("membership", "characters", "position"), ("career", "careers", "note")],
)
async def test_wizard_rejects_when_replacing_would_cascade_other_subject_history(
    client, subject_kind, wizard_kind, field
):
    p = await _setup(client)
    base = f"/api/v1/projects/{p['id']}"
    char = await client.post(f"{base}/characters", json={"name": "甲"})
    assert char.status_code == 201, char.text
    character_id = char.json()["id"]
    if subject_kind == "relationship":
        other = await client.post(f"{base}/characters", json={"name": "乙"})
        assert other.status_code == 201, other.text
        response = await client.post(
            f"{base}/relationships", json={"a_id": character_id, "b_id": other.json()["id"], "role_a": "朋友"}
        )
    elif subject_kind == "career":
        career = await client.post(f"{base}/careers", json={"name": "剑修", "stages": [{"name": "练气"}]})
        assert career.status_code == 201, career.text
        response = await client.post(
            f"{base}/characters/{character_id}/careers", json={"career_id": career.json()["id"]}
        )
    else:
        org = await client.post(f"{base}/organizations", json={"name": "剑宗"})
        assert org.status_code == 201, org.text
        response = await client.post(
            f"{base}/organizations/{org.json()['id']}/members", json={"character_id": character_id}
        )
    assert response.status_code == 201, response.text
    timeline = await _setting_timeline(client, base, subject_kind=subject_kind, character_id=character_id)
    url = f"{base}/setting-timelines/{timeline['id']}"
    event = await _record_change(client, url, timeline["revision"], field, "应保留的变化")
    before = (await client.get(f"{url}/effective", params={"chapter_no": 10})).json()["fields"]
    blocked = await client.post(f"{base}/wizard/{wizard_kind}")
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "wizard_history_conflict"
    assert (await client.get(f"{url}/progressions")).json()["items"][0]["id"] == event["item"]["id"]
    assert (await client.get(f"{url}/effective", params={"chapter_no": 10})).json()["fields"] == before
    assert (await client.get(f"{base}/characters/{character_id}")).status_code == 200


async def test_wizard_replacement_ignores_inactive_events_and_unaffected_subjects(client, fake_llm):
    p = await _setup(client)
    base = f"/api/v1/projects/{p['id']}"
    world = (await client.post(f"{base}/world", json={"title": "旧世界", "content": "旧内容"})).json()
    character = (await client.post(f"{base}/characters", json={"name": "保留的角色"})).json()
    world_timeline = await _setting_timeline(client, base, world_setting_id=world["id"])
    char_timeline = await _setting_timeline(client, base, character_id=character["id"])
    char_url = f"{base}/setting-timelines/{char_timeline['id']}"
    await _record_change(client, char_url, char_timeline["revision"], "profile", "角色剧情")
    world_url = f"{base}/setting-timelines/{world_timeline['id']}"
    created = await _record_change(client, world_url, world_timeline["revision"], "content", "已撤销")
    removed = await client.delete(
        f"{world_url}/progressions/{created['item']['id']}",
        params={"expected_revision": created["timeline_revision"]},
    )
    assert removed.status_code == 200, removed.text
    fake_llm.push('[{"title":"新世界","content":"新内容"}]')
    task, _ = await run_task(client, await client.post(f"{base}/wizard/world"))
    assert task["status"] == "succeeded", task
    assert [item["title"] for item in (await client.get(f"{base}/world")).json()] == ["新世界"]
    assert (await client.get(f"{base}/characters/{character['id']}")).json()["profile"] == "角色剧情"
    assert (await client.get(f"{char_url}/progressions")).json()["total"] == 1


async def test_wizard_rolls_back_old_rows_and_new_timelines_if_initialization_fails(client, fake_llm, monkeypatch):
    p = await _setup(client)
    base = f"/api/v1/projects/{p['id']}"
    old = (await client.post(f"{base}/world", json={"title": "旧设定", "content": "保留"})).json()
    old_timeline = await _setting_timeline(client, base, world_setting_id=old["id"])
    original = setting_progression_service.create_world_timeline

    async def fail_after_initialize(db, world, **kwargs):
        await original(db, world, **kwargs)
        raise RuntimeError("测试初始化失败")

    monkeypatch.setattr(setting_progression_service, "create_world_timeline", fail_after_initialize)
    fake_llm.push('[{"title":"新设定","content":"新值"}]')
    task, _ = await run_task(client, await client.post(f"{base}/wizard/world"))
    assert task["status"] == "failed"
    assert (await client.get(f"{base}/world")).json() == [old]
    assert (await _setting_timeline(client, base, world_setting_id=old["id"]))["id"] == old_timeline["id"]


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


async def test_wizard_outline_append_mode(client, fake_llm):
    """追加模式：原有大纲保留，新大纲接在末尾（卷号接最后一卷、章节号接全书最后一章），不存快照。"""
    p = await _setup(client)
    base = f"/api/v1/projects/{p['id']}"
    fake_llm.push(json.dumps(OUTLINE_JSON, ensure_ascii=False))
    await run_task(client, await client.post(f"{base}/wizard/outline"))
    old = (await client.get(f"{base}/outlines")).json()

    fake_llm.push(json.dumps(OUTLINE_JSON, ensure_ascii=False))
    _, events = await run_task(
        client, await client.post(f"{base}/wizard/outline", json={"outline_mode": "append", "volume_count": 2})
    )
    result = events[-1][1]["result"]
    assert result["mode"] == "append"
    assert [(i["volume_no"], i["chapter_no"], i["title"]) for i in result["items"]] == [
        (3, 4, "出发"),
        (3, 5, "异象"),
        (4, 6, "接触"),
    ]
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "【已有大纲（末尾部分）】" in prompt and "第 3 卷、第 4 章开始" in prompt and "第3章 接触" in prompt
    outlines = (await client.get(f"{base}/outlines")).json()
    assert [o["id"] for o in outlines[:3]] == [o["id"] for o in old] and len(outlines) == 6
    assert (await client.get(f"{base}/outline-snapshots")).json() == []


async def test_wizard_outline_replace_saves_snapshot(client, fake_llm):
    p = await _setup(client)
    base = f"/api/v1/projects/{p['id']}"
    fake_llm.push(json.dumps(OUTLINE_JSON, ensure_ascii=False))
    _, events = await run_task(client, await client.post(f"{base}/wizard/outline"))
    assert events[-1][1]["result"]["mode"] == "replace"
    # 首次生成（没有旧大纲）：提示词不含已有大纲，也不存快照
    assert "已有大纲" not in fake_llm.calls[-1]["messages"][-1]["content"]
    fake_llm.push('[{"title": "唯一一章"}]')
    await run_task(client, await client.post(f"{base}/wizard/outline", json={"outline_mode": "replace"}))
    snaps = (await client.get(f"{base}/outline-snapshots")).json()
    assert [(s["reason"], s["outline_count"]) for s in snaps] == [("wizard_replace", 3)]
