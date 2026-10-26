"""灵感模式：会话持久化与隔离、步骤决定 / 跳过 / stale、补充想法、生成选项（重试 / 断开 / 并发）、规模建议、
转为项目、应用到已有项目、从项目发起、会话上限；项目结构化字段的校验与【作品信息】输出。"""

import json

import pytest

from app.ai.prompts import ProjectInfo, format_project_info
from app.models import InspirationSession
from app.schemas.inspiration import MAX_SESSIONS, GenerateRequest
from app.services import inspiration_service
from tests.helpers import Gate, create_ai_config, create_project, parse_sse, register, run_task

API = "/api/v1/inspiration"
TITLES = ["星海余烬", "听见城市的心跳", "末日前夜", "哥哥的地图", "回声少女"]
TAGS = ["都市", "奇幻", "末日", "亲情", "成长", "悬疑", "治愈"]


def options_json(options: list[str]) -> str:
    return json.dumps({"options": options}, ensure_ascii=False)


async def _setup(client, idea="一个能听见城市心跳的少女，在末日前夜寻找失踪的哥哥"):
    await register(client)
    await create_ai_config(client)
    resp = await client.post(f"{API}/sessions", json={"idea": idea})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _generate(client, sid, step, feedback=""):
    resp = await client.post(f"{API}/sessions/{sid}/steps/{step}/generate", json={"feedback": feedback})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    return parse_sse(resp.text)


async def _decide(client, sid, step, value, source="ai"):
    resp = await client.patch(
        f"{API}/sessions/{sid}/steps/{step}", json={"action": "decide", "value": value, "source": source}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------- 项目结构化字段


def test_format_project_info_each_field():
    base = ProjectInfo(title="书", genre="科幻", premise="设定", style="冷峻")
    legacy = "书名：《书》\n题材：科幻\n一句话设定：设定\n写作风格：冷峻"
    assert format_project_info(base) == legacy
    # 空白也不输出
    blank = ProjectInfo(title="书", genre="科幻", premise="设定", style="冷峻", synopsis="  ", tags=(" ",))
    assert format_project_info(blank) == legacy
    cases = {
        "tags": (("都市", "奇幻"), "类型标签：都市、奇幻"),
        "theme": ("成长", "主题：成长"),
        "synopsis": ("一段简介", "简介：一段简介"),
        "selling_points": ("反差", "核心卖点：反差"),
        "audience": ("学生", "目标读者：学生"),
    }
    for field, (value, line) in cases.items():
        info = ProjectInfo(title="书", genre="科幻", premise="设定", style="冷峻", **{field: value})
        assert format_project_info(info) == legacy + "\n" + line
    full = ProjectInfo(
        title="书",
        tags=("都市",),
        theme="成长",
        synopsis="简介",
        selling_points="卖点",
        audience="读者",
    )
    expected = "书名：《书》\n类型标签：都市\n主题：成长\n简介：简介\n核心卖点：卖点\n目标读者：读者"
    assert format_project_info(full) == expected


async def test_project_structured_fields_validation(client, fake_llm):
    await register(client)
    await create_ai_config(client)
    p = await create_project(
        client,
        synopsis=" 简介 ",
        theme="成长",
        tags=[" 都市 ", "奇幻", "都市", "", "  "],
        selling_points="卖点",
        audience="学生",
        target_words=300000,
    )
    assert p["tags"] == ["都市", "奇幻"] and p["synopsis"] == "简介" and p["target_words"] == 300000
    url = f"/api/v1/projects/{p['id']}"
    bad = [
        {"tags": [f"t{i}" for i in range(9)]},
        {"tags": ["一二三四五六七八九十十"]},
        {"synopsis": "长" * 2001},
        {"theme": "长" * 201},
        {"selling_points": "长" * 501},
        {"audience": "长" * 101},
        {"target_words": 999},
        {"target_words": 10_000_001},
    ]
    for body in bad:
        assert (await client.patch(url, json=body)).status_code == 422, body
    resp = await client.patch(url, json={"tags": ["悬疑"], "theme": " 救赎 "})
    assert resp.json()["tags"] == ["悬疑"] and resp.json()["theme"] == "救赎"
    # target_words 显式 null 清空；其余未给的字段不变
    resp = await client.patch(url, json={"target_words": None})
    assert resp.json()["target_words"] is None and resp.json()["synopsis"] == "简介"

    # 新字段注入【作品信息】（向导提示词）
    fake_llm.push('[{"title": "设定", "category": "place", "content": "内容"}]')
    task, _ = await run_task(client, await client.post(f"{url}/wizard/world", json={}))
    assert task["status"] == "succeeded", task
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "类型标签：悬疑\n主题：救赎\n简介：简介\n核心卖点：卖点\n目标读者：学生" in prompt


# ---------------------------------------------------------------- 会话持久化与隔离


async def test_session_crud_persisted_and_isolated(client, make_client):
    s = await _setup(client)
    assert s["status"] == "active" and s["state"]["current"] == "title"
    assert list(s["state"]["steps"]) == ["title", "synopsis", "theme", "tags", "selling_points", "audience"]
    assert s["title"] == "一个能听见城市心跳的少女，在末日前夜寻找失踪的哥哥"[:30]

    # 换个客户端（模拟换浏览器）登录同一用户也能继续
    other = make_client()
    resp = await other.post("/api/v1/auth/login", json={"username": "alice", "password": "secret123"})
    assert resp.status_code == 200
    got = (await other.get(f"{API}/sessions/{s['id']}")).json()
    assert got["state"]["idea"] == s["state"]["idea"]
    listed = (await other.get(f"{API}/sessions")).json()
    assert [x["id"] for x in listed] == [s["id"]] and listed[0]["done_steps"] == 0

    bob = make_client()
    await register(bob, "bob")
    p = await create_project(client)
    sid = s["id"]
    checks = [
        ("GET", f"{API}/sessions/{sid}", None),
        ("PATCH", f"{API}/sessions/{sid}", {"current": "theme"}),
        ("DELETE", f"{API}/sessions/{sid}", None),
        ("PATCH", f"{API}/sessions/{sid}/steps/title", {"action": "skip"}),
        ("POST", f"{API}/sessions/{sid}/notes", {"text": "x"}),
        ("DELETE", f"{API}/sessions/{sid}/notes/0", None),
        ("POST", f"{API}/sessions/{sid}/steps/title/generate", {}),
        ("POST", f"{API}/sessions/{sid}/convert", {"title_override": "x"}),
        ("GET", f"{API}/sessions/{sid}/apply-preview?project_id={p['id']}", None),
        ("POST", f"{API}/sessions/{sid}/apply", {"project_id": p["id"], "fields": []}),
        ("POST", f"{API}/sessions/from-project/{p['id']}", None),
    ]
    await create_ai_config(bob)
    for method, url, body in checks:
        resp = await bob.request(method, url, json=body)
        assert resp.status_code == 404, (method, url, resp.text)
    assert (await bob.get(f"{API}/sessions")).json() == []
    # alice 的数据不变
    assert (await client.get(f"{API}/sessions/{sid}")).json()["state"] == s["state"]

    assert (await client.delete(f"{API}/sessions/{sid}")).status_code == 204
    assert (await client.get(f"{API}/sessions/{sid}")).status_code == 404


# ---------------------------------------------------------------- 步骤：选择 / 编辑 / 自己填 / 跳过 / stale


async def test_steps_decide_skip_and_stale(client):
    s = await _setup(client)
    sid = s["id"]
    s = await _decide(client, sid, "title", "星海余烬", "ai")
    assert s["title"] == "星海余烬"
    s = await _decide(client, sid, "synopsis", "编辑过的简介", "edited")
    s = await _decide(client, sid, "tags", [" 都市 ", "奇幻", "都市"], "ai")
    assert s["state"]["steps"]["tags"]["value"] == ["都市", "奇幻"]
    s = await _decide(client, sid, "audience", "学生党", "manual")
    resp = await client.patch(f"{API}/sessions/{sid}/steps/theme", json={"action": "skip"})
    assert resp.json()["state"]["steps"]["theme"]["status"] == "skipped"

    # 非法值
    url = f"{API}/sessions/{sid}/steps"
    assert (await client.patch(f"{url}/title", json={"action": "decide", "value": "  "})).status_code == 400
    assert (await client.patch(f"{url}/title", json={"action": "decide", "value": "长" * 201})).status_code == 400
    many = [f"t{i}" for i in range(9)]
    assert (await client.patch(f"{url}/tags", json={"action": "decide", "value": many})).status_code == 400
    assert (await client.patch(f"{url}/nope", json={"action": "skip"})).status_code == 422

    # 再次定下同样的值不触发 stale
    s = await _decide(client, sid, "title", "星海余烬")
    assert not any(st["stale"] for st in s["state"]["steps"].values())

    # 修改书名：后面已定的步骤保留原值并标记 stale；跳过 / 待定的步骤不标记
    s = await _decide(client, sid, "title", "听见城市的心跳")
    steps = s["state"]["steps"]
    assert steps["title"]["stale"] is False
    assert steps["synopsis"]["stale"] and steps["synopsis"]["value"] == "编辑过的简介"
    assert steps["tags"]["stale"] and steps["audience"]["stale"]
    assert not steps["theme"]["stale"] and not steps["selling_points"]["stale"]
    listed = (await client.get(f"{API}/sessions")).json()
    assert listed[0]["stale_steps"] == 3 and listed[0]["done_steps"] == 5

    # 重新决定清除自己的 stale；「保持不变」同样清除
    s = await _decide(client, sid, "synopsis", "新简介")
    assert s["state"]["steps"]["synopsis"]["stale"] is False
    # 简介的值变了 → 后面已定的 tags / audience 仍是 stale
    resp = await client.patch(f"{url}/tags", json={"action": "keep"})
    steps = resp.json()["state"]["steps"]
    assert steps["tags"]["stale"] is False and steps["tags"]["value"] == ["都市", "奇幻"]
    assert steps["audience"]["stale"] is True

    # 修改想法：所有已定步骤标记 stale
    resp = await client.patch(f"{API}/sessions/{sid}", json={"idea": "新的想法", "current": "scale"})
    state = resp.json()["state"]
    assert state["idea"] == "新的想法" and state["current"] == "scale"
    assert all(state["steps"][k]["stale"] for k in ("title", "synopsis", "tags", "audience"))

    # 清除一步：回到待定
    resp = await client.patch(f"{url}/audience", json={"action": "clear"})
    assert resp.json()["state"]["steps"]["audience"] == {
        "value": None,
        "status": "pending",
        "stale": False,
        "source": None,
        "rounds": [],
    }


async def test_notes(client):
    s = await _setup(client)
    sid = s["id"]
    resp = await client.post(f"{API}/sessions/{sid}/notes", json={"text": " 结局温暖 "})
    assert resp.json()["state"]["notes"] == ["结局温暖"]
    await _decide(client, sid, "title", "星海余烬")
    resp = await client.post(f"{API}/sessions/{sid}/notes", json={"text": "主角是女性"})
    state = resp.json()["state"]
    # 补充想法不触发 stale
    assert state["notes"] == ["结局温暖", "主角是女性"] and not state["steps"]["title"]["stale"]
    assert (await client.post(f"{API}/sessions/{sid}/notes", json={"text": "长" * 201})).status_code == 422
    resp = await client.delete(f"{API}/sessions/{sid}/notes/0")
    assert resp.json()["state"]["notes"] == ["主角是女性"]
    assert (await client.delete(f"{API}/sessions/{sid}/notes/5")).status_code == 404
    for i in range(19):
        await client.post(f"{API}/sessions/{sid}/notes", json={"text": f"想法{i}"})
    assert (await client.post(f"{API}/sessions/{sid}/notes", json={"text": "多余"})).status_code == 400


# ---------------------------------------------------------------- 生成选项


async def test_generate_saves_round_and_prompt_contains_context(client, fake_llm):
    s = await _setup(client)
    sid = s["id"]
    await client.post(f"{API}/sessions/{sid}/notes", json={"text": "结局要温暖"})
    await _decide(client, sid, "theme", "在失去中学会告别")
    fake_llm.push(options_json(["《星海余烬》", *TITLES[1:], "星海余烬", "", "长" * 31]))
    events = await _generate(client, sid, "title")
    names = [e for e, _ in events]
    assert names[-1] == "done" and "delta" in names and "retry" not in names
    done = events[-1][1]
    # 去书名号、去重、丢掉空的和超长的
    assert done["round"]["options"] == TITLES and done["round"]["feedback"] == ""
    rounds = done["session"]["state"]["steps"]["title"]["rounds"]
    assert len(rounds) == 1
    got = (await client.get(f"{API}/sessions/{sid}")).json()
    assert got["state"]["steps"]["title"]["rounds"][0]["options"] == TITLES
    assert got["state"]["steps"]["title"]["status"] == "pending"

    # 带反馈换一批：提示词包含本步之前全部轮次与反馈、已定内容、补充想法
    fake_llm.push(options_json(["甲书", "乙书", "丙书", "丁书"]))
    await _generate(client, sid, "title", feedback="更短一点")
    fake_llm.push(options_json(["戊书", "己书", "庚书", "辛书"]))
    events = await _generate(client, sid, "title", feedback="要有画面感")
    assert events[-1][1]["round"]["feedback"] == "要有画面感"
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert fake_llm.calls[-1]["messages"][0]["role"] == "system"
    assert "一个能听见城市心跳的少女" in prompt
    assert "- 结局要温暖" in prompt
    assert "主题：在失去中学会告别" in prompt
    assert "第 1 轮：\n1. 星海余烬" in prompt
    assert "第 2 轮（作者反馈：更短一点）：\n1. 甲书" in prompt
    assert "【这次的反馈（请重点参考）】\n要有画面感" in prompt
    assert '"options"' in prompt
    rounds = (await client.get(f"{API}/sessions/{sid}")).json()["state"]["steps"]["title"]["rounds"]
    assert [r["feedback"] for r in rounds] == ["", "更短一点", "要有画面感"]

    # 类型标签：6~10 个
    fake_llm.push(options_json(TAGS))
    events = await _generate(client, sid, "tags")
    assert events[-1][0] == "done" and events[-1][1]["round"]["options"] == TAGS


async def test_generate_keeps_at_most_10_rounds(client, fake_llm):
    s = await _setup(client)
    for i in range(11):
        fake_llm.push(options_json([f"第{i}批{j}" for j in range(4)]))
        events = await _generate(client, s["id"], "theme")
        assert events[-1][0] == "done"
    rounds = (await client.get(f"{API}/sessions/{s['id']}")).json()["state"]["steps"]["theme"]["rounds"]
    assert len(rounds) == 10 and rounds[0]["options"][0] == "第1批0"


async def test_generate_retries_then_succeeds(client, fake_llm):
    s = await _setup(client)
    fake_llm.push("这不是 JSON")
    fake_llm.push(options_json(TITLES))
    events = await _generate(client, s["id"], "title")
    names = [e for e, _ in events]
    assert names.count("retry") == 1 and names[-1] == "done"
    retry = next(d for e, d in events if e == "retry")
    assert retry["attempt"] == 1 and retry["max"] == 2 and retry["reason"]
    # retry 之后还有 delta（前端据 retry 清空已显示的原文）
    assert "delta" in names[names.index("retry") :]
    rounds = (await client.get(f"{API}/sessions/{s['id']}")).json()["state"]["steps"]["title"]["rounds"]
    assert len(rounds) == 1 and rounds[0]["options"] == TITLES

    # 有效选项少于下限同样重试
    fake_llm.push(options_json(["只有一个"]))
    fake_llm.push(options_json(TAGS))
    events = await _generate(client, s["id"], "tags")
    assert [e for e, _ in events].count("retry") == 1 and events[-1][0] == "done"
    assert "至少需要 6 个" in next(d for e, d in events if e == "retry")["reason"]


async def test_generate_fails_after_two_retries(client, fake_llm):
    s = await _setup(client)
    before = (await client.get(f"{API}/sessions/{s['id']}")).json()
    for _ in range(3):
        fake_llm.push("不合法")
    events = await _generate(client, s["id"], "title")
    names = [e for e, _ in events]
    assert names.count("retry") == 2 and names[-1] == "error"
    assert "已自动重试 2 次" in events[-1][1]["detail"]
    assert len(fake_llm.calls) == 3
    after = (await client.get(f"{API}/sessions/{s['id']}")).json()
    assert after["state"] == before["state"]


async def test_generate_requires_ai_config_and_valid_step(client):
    await register(client)
    s = (await client.post(f"{API}/sessions", json={"idea": "想法"})).json()
    resp = await client.post(f"{API}/sessions/{s['id']}/steps/title/generate", json={})
    assert resp.status_code == 400 and "模型" in resp.json()["detail"]
    await create_ai_config(client)
    assert (await client.post(f"{API}/sessions/{s['id']}/steps/scale/generate", json={})).status_code == 422
    resp = await client.post(f"{API}/sessions/{s['id']}/steps/title/generate", json={"feedback": "长" * 501})
    assert resp.status_code == 422


async def test_generate_disconnect_not_saved_and_concurrent_409(app, client, fake_llm):
    s = await _setup(client)
    sid = s["id"]
    me = (await client.get("/api/v1/auth/me")).json()
    gate = Gate()
    fake_llm.push(['{"options": ["甲', gate, '", "乙", "丙", "丁"]}'])
    async with app.state.sessionmaker() as db:
        job = await inspiration_service.prepare_generate(
            db,
            user_id=me["id"],
            session_id=sid,
            step="title",
            req=GenerateRequest(),
            cipher=app.state.cipher,
            factory=app.state.provider_factory,
            ai_timeout=30,
            allow_private=False,
        )
    gen = inspiration_service.run_generate(job, app.state.sessionmaker)
    assert await gen.__anext__() == ("delta", {"text": '{"options": ["甲'})

    # 同一会话同一步骤正在生成 → 409；其他步骤不受影响
    resp = await client.post(f"{API}/sessions/{sid}/steps/title/generate", json={})
    assert resp.status_code == 409
    fake_llm.push(options_json(["主题一", "主题二", "主题三", "主题四"]))
    assert (await _generate(client, sid, "theme"))[-1][0] == "done"

    # 中途断开：不保存半截选项
    await gen.aclose()
    assert fake_llm.aborted == 1
    got = (await client.get(f"{API}/sessions/{sid}")).json()
    assert got["state"]["steps"]["title"]["rounds"] == []

    # 断开后锁已释放，重试可以正常生成
    fake_llm.push(options_json(TITLES))
    events = await _generate(client, sid, "title")
    assert events[-1][0] == "done"
    got = (await client.get(f"{API}/sessions/{sid}")).json()
    assert len(got["state"]["steps"]["title"]["rounds"]) == 1


def test_normalize_options():
    norm = inspiration_service.normalize_options
    assert norm("title", {"options": [" 甲 ", "甲", "", None, 3, {"text": "乙"}, "《丙》"]}) == ["甲", "3", "乙", "丙"]
    assert norm("title", ["a", "b"]) == ["a", "b"]
    assert len(norm("title", {"options": [f"书{i}" for i in range(9)]})) == 6
    assert len(norm("tags", {"options": [f"标{i}" for i in range(12)]})) == 10
    assert norm("tags", {"options": ["一二三四五六七八九十", "一二三四五六七八九十一"]}) == ["一二三四五六七八九十"]
    with pytest.raises(ValueError):
        norm("title", {"items": []})


# ---------------------------------------------------------------- 规模建议


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (1000, (1, 10, 7)),  # 最少 10 章
        (30000, (1, 10, 7)),
        (90000, (1, 30, 7)),
        (300000, (4, 25, 13)),  # 100 章 → 4 卷
        (1_000_000, (12, 28, 29)),
        (3_000_000, (20, 50, 30)),  # 卷数上限 20，角色数上限 30
        (10_000_000, (20, 100, 30)),  # 每卷章数上限 100
    ],
)
def test_suggest_scale_boundaries(target, expected):
    scale = inspiration_service.suggest_scale(target)
    assert (scale.volume_count, scale.chapters_per_volume, scale.character_count) == expected
    assert scale.target_words == target


async def test_scale_suggest_api_and_save(client):
    s = await _setup(client)
    resp = await client.get(f"{API}/scale-suggest", params={"target_words": 300000})
    assert resp.json() == {"target_words": 300000, "volume_count": 4, "chapters_per_volume": 25, "character_count": 13}
    assert (await client.get(f"{API}/scale-suggest", params={"target_words": 10})).status_code == 422
    scale = {**resp.json(), "chapters_per_volume": 40}
    resp = await client.patch(f"{API}/sessions/{s['id']}", json={"scale": scale})
    assert resp.json()["state"]["scale"] == scale
    bad = {**scale, "volume_count": 21}
    assert (await client.patch(f"{API}/sessions/{s['id']}", json={"scale": bad})).status_code == 422
    resp = await client.patch(f"{API}/sessions/{s['id']}", json={"scale": None})
    assert resp.json()["state"]["scale"] is None


# ---------------------------------------------------------------- 转为项目 / 应用 / 从项目发起


async def test_convert_creates_project_with_wizard_params(client):
    s = await _setup(client)
    sid = s["id"]
    assert (await client.post(f"{API}/sessions/{sid}/convert", json={})).status_code == 400
    await _decide(client, sid, "title", "星海余烬")
    await _decide(client, sid, "synopsis", "简介内容")
    await _decide(client, sid, "theme", "成长")
    await _decide(client, sid, "tags", ["都市", "奇幻"])
    await _decide(client, sid, "selling_points", "卖点")
    await _decide(client, sid, "audience", "学生")
    await client.patch(
        f"{API}/sessions/{sid}",
        json={"scale": {"target_words": 300000, "volume_count": 4, "chapters_per_volume": 25, "character_count": 13}},
    )
    # 确认页调整后的规模以请求为准
    scale = {"target_words": 400000, "volume_count": 5, "chapters_per_volume": 30, "character_count": 12}
    resp = await client.post(f"{API}/sessions/{sid}/convert", json={"scale": scale})
    assert resp.status_code == 201, resp.text
    p = resp.json()
    assert p["title"] == "星海余烬" and p["genre"] == "都市" and p["premise"] == s["state"]["idea"]
    assert (p["synopsis"], p["theme"], p["tags"], p["selling_points"], p["audience"]) == (
        "简介内容",
        "成长",
        ["都市", "奇幻"],
        "卖点",
        "学生",
    )
    assert p["target_words"] == 400000
    assert p["wizard_progress"]["params"] == {"volume_count": 5, "chapters_per_volume": 30, "character_count": 12}
    got = (await client.get(f"/api/v1/projects/{p['id']}")).json()
    assert got["wizard_progress"]["params"]["volume_count"] == 5

    sess = (await client.get(f"{API}/sessions/{sid}")).json()
    assert sess["status"] == "converted" and sess["converted_project_id"] == p["id"]
    assert sess["state"]["scale"] == scale
    assert (await client.post(f"{API}/sessions/{sid}/convert", json={})).status_code == 409

    # 向导进度 PATCH 不丢掉参数
    resp = await client.patch(f"/api/v1/projects/{p['id']}/wizard-progress", json={"current": "world"})
    assert resp.json()["wizard_progress"]["params"]["character_count"] == 12


async def test_convert_with_title_override_without_scale(client):
    s = await _setup(client)
    resp = await client.post(f"{API}/sessions/{s['id']}/convert", json={"title_override": " 临时书名 "})
    assert resp.status_code == 201
    p = resp.json()
    assert p["title"] == "临时书名" and p["tags"] == [] and p["target_words"] is None
    assert p["wizard_progress"]["params"] is None


async def test_apply_preview_and_apply_only_checked(client):
    s = await _setup(client)
    sid = s["id"]
    p = await create_project(client, title="旧书名", synopsis="旧简介")
    await _decide(client, sid, "title", "新书名")
    await _decide(client, sid, "synopsis", "新简介")
    await _decide(client, sid, "theme", "成长")
    await client.patch(
        f"{API}/sessions/{sid}",
        json={"scale": {"target_words": 300000, "volume_count": 4, "chapters_per_volume": 25, "character_count": 13}},
    )
    resp = await client.get(f"{API}/sessions/{sid}/apply-preview", params={"project_id": p["id"]})
    assert resp.status_code == 200
    items = {i["field"]: i for i in resp.json()}
    assert set(items) == {"title", "synopsis", "theme", "premise", "target_words"}
    assert items["title"]["current"] == "旧书名" and items["title"]["proposed"] == "新书名"
    # 默认只勾选项目中为空的字段
    assert {f for f, i in items.items() if i["default_checked"]} == {"theme", "target_words"}
    assert items["premise"]["default_checked"] is False  # 项目已有一句话设定

    resp = await client.post(f"{API}/sessions/{sid}/apply", json={"project_id": p["id"], "fields": ["theme"]})
    assert resp.status_code == 200
    got = resp.json()
    assert got["theme"] == "成长" and got["title"] == "旧书名" and got["synopsis"] == "旧简介"
    assert got["target_words"] is None
    # 勾选后才覆盖已有内容；可以反复应用
    resp = await client.post(
        f"{API}/sessions/{sid}/apply", json={"project_id": p["id"], "fields": ["title", "target_words"]}
    )
    assert resp.json()["title"] == "新书名" and resp.json()["target_words"] == 300000
    # 没有可应用内容的字段 → 400；会话状态不变
    resp = await client.post(f"{API}/sessions/{sid}/apply", json={"project_id": p["id"], "fields": ["audience"]})
    assert resp.status_code == 400
    assert (await client.get(f"{API}/sessions/{sid}")).json()["status"] == "active"
    # 不存在的项目 404
    resp = await client.get(f"{API}/sessions/{sid}/apply-preview", params={"project_id": 9999})
    assert resp.status_code == 404


async def test_from_project_prefills_and_reuses(client, fake_llm):
    await register(client)
    await create_ai_config(client)
    p = await create_project(client, theme="成长", tags=["都市"], target_words=300000)
    resp = await client.post(f"{API}/sessions/from-project/{p['id']}")
    assert resp.status_code == 200, resp.text
    s = resp.json()
    state = s["state"]
    assert s["project_id"] == p["id"] and state["idea"] == p["premise"] and state["genre"] == "科幻"
    steps = state["steps"]
    assert steps["title"] == {**steps["title"], "value": "星海纪元", "status": "decided", "source": "project"}
    assert steps["theme"]["value"] == "成长" and steps["tags"]["value"] == ["都市"]
    assert steps["synopsis"]["status"] == "pending" and steps["audience"]["status"] == "pending"
    # 当前步骤定位到第一个空着的步骤；规模按目标字数预填
    assert state["current"] == "synopsis" and state["scale"]["volume_count"] == 4

    # 只为空着的步骤生成时，已定的项目字段（含题材）作为已定内容
    fake_llm.push(options_json(["简介一", "简介二", "简介三", "简介四"]))
    await _generate(client, s["id"], "synopsis")
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "题材：科幻\n书名：星海纪元\n主题：成长\n类型标签：都市" in prompt

    # 同一项目已有进行中的会话 → 返回同一个
    again = (await client.post(f"{API}/sessions/from-project/{p['id']}")).json()
    assert again["id"] == s["id"]
    assert len((await client.get(f"{API}/sessions")).json()) == 1

    # 填空（pending → decided）不会把后面已定的项目字段标为 stale
    state = (await _decide(client, s["id"], "synopsis", "简介一"))["state"]
    assert not any(st["stale"] for st in state["steps"].values())
    # 已定步骤「换一批」：已定内容不含本步自己的值
    fake_llm.push(options_json(["简介五", "简介六", "简介七", "简介八"]))
    await _generate(client, s["id"], "synopsis")
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "简介：简介一" not in prompt and "书名：星海纪元" in prompt

    # 应用回项目：默认只勾选空字段
    items = (await client.get(f"{API}/sessions/{s['id']}/apply-preview", params={"project_id": p["id"]})).json()
    checked = {i["field"] for i in items if i["default_checked"]}
    assert checked == {"synopsis"}


async def test_session_limit_evicts_converted_then_409(app, client):
    s = await _setup(client)
    me = (await client.get("/api/v1/auth/me")).json()
    async with app.state.sessionmaker() as db:
        for i in range(MAX_SESSIONS - 1):
            db.add(
                InspirationSession(
                    user_id=me["id"], title=f"会话{i}", status="converted" if i < 2 else "active", state={}
                )
            )
        await db.commit()
    assert len((await client.get(f"{API}/sessions")).json()) == MAX_SESSIONS
    # 满了：删掉最旧的已转换会话
    resp = await client.post(f"{API}/sessions", json={"idea": "第 51 个"})
    assert resp.status_code == 201
    titles = {x["title"] for x in (await client.get(f"{API}/sessions")).json()}
    assert len(titles) == MAX_SESSIONS and "会话0" not in titles and "会话1" in titles
    resp = await client.post(f"{API}/sessions", json={"idea": "第 52 个"})
    assert resp.status_code == 201
    # 没有可删的已转换会话 → 409
    resp = await client.post(f"{API}/sessions", json={"idea": "第 53 个"})
    assert resp.status_code == 409 and "上限" in resp.json()["detail"]
    assert (await client.get(f"{API}/sessions/{s['id']}")).status_code == 200


async def test_apply_to_other_users_project_404(client, make_client):
    s = await _setup(client)
    await _decide(client, s["id"], "theme", "成长")
    bob = make_client()
    await register(bob, "bob")
    bob_project = await create_project(bob, title="鲍勃的书")
    url = f"{API}/sessions/{s['id']}"
    resp = await client.get(f"{url}/apply-preview", params={"project_id": bob_project["id"]})
    assert resp.status_code == 404 and resp.json()["detail"] == "项目不存在"
    resp = await client.post(f"{url}/apply", json={"project_id": bob_project["id"], "fields": ["theme"]})
    assert resp.status_code == 404
    assert (await bob.get(f"/api/v1/projects/{bob_project['id']}")).json()["theme"] == ""


async def test_malformed_state_in_db(app, client):
    s = await _setup(client)
    async with app.state.sessionmaker() as db:
        row = await db.get(InspirationSession, s["id"])
        row.state = {"idea": "x", "notes": ["长" * 500], "steps": {"nope": {}}}
        await db.commit()
    # 详情 / 修改 → 409（可读错误，而非 500）；列表与删除仍可用
    resp = await client.get(f"{API}/sessions/{s['id']}")
    assert resp.status_code == 409 and "损坏" in resp.json()["detail"]
    assert (await client.patch(f"{API}/sessions/{s['id']}", json={"current": "theme"})).status_code == 409
    listed = (await client.get(f"{API}/sessions")).json()
    assert [x["id"] for x in listed] == [s["id"]]
    assert (await client.delete(f"{API}/sessions/{s['id']}")).status_code == 204
