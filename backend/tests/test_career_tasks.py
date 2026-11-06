import json

from sqlalchemy import func, select

from app.models import Career, CareerHistory, CharacterCareer, GenerationLog
from app.services.career_service import normalize_career_drafts
from app.services.context_builder import CastSection, assemble_context
from tests.helpers import create_ai_config, create_project, mark_existing_settings, register, run_task


async def _count(app, model) -> int:
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(func.count()).select_from(model))


async def _setup(client):
    await register(client)
    await create_ai_config(client)
    p = await create_project(client, title="剑道", genre="仙侠")
    return f"/api/v1/projects/{p['id']}"


async def _career(client, base, name, stages, kind="main", **kw):
    resp = await client.post(
        f"{base}/careers", json={"name": name, "kind": kind, "stages": [{"name": s} for s in stages], **kw}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


GENERATED = {
    "careers": [
        {
            "name": "剑修",
            "kind": "主修",
            "description": "以剑入道",
            "stages": [{"name": "练气", "description": "引气入体"}, "筑基", {"name": "筑基"}, {"name": "金丹"}],
        },
        {"name": "丹师", "kind": "sub", "rules": "丹火", "stages": ["一品", "二品"]},
        {"name": "阵师", "kind": "副职业", "stages": ["一阶"]},
        {"name": "空壳", "stages": []},
    ]
}


async def test_career_generate_draft_only(app, client, fake_llm):
    """AC4：生成职业以任务运行，只产出草稿；与已有职业同名的标记 conflict；确认后批量保存勾选项。"""
    base = await _setup(client)
    await _career(
        client,
        base,
        "丹师",
        ["一品"],
        kind="sub",
        description="炼丹为业",
        abilities="控火" + "炼" * 120,
        rules="丹毒累积",
        requirements="需火灵根",
    )
    await client.post(f"{base}/world", json={"title": "灵气", "content": "天地灵气复苏", "category": "rule"})
    fake_llm.push("```json\n" + json.dumps(GENERATED, ensure_ascii=False) + "\n```")

    resp = await client.post(f"{base}/careers/generate", json={"count": 3, "instruction": "偏东方，参考丹师和灵气"})
    assert resp.status_code == 202, resp.text
    assert (resp.json()["kind"], resp.json()["title"]) == ("career.generate", "AI 生成职业")
    task, events = await run_task(client, resp)
    assert task["status"] == "succeeded"
    result = events[-1][1]["result"]
    drafts = result["drafts"]
    assert [(d["name"], d["kind"], d["conflict"]) for d in drafts] == [
        ("剑修", "main", False),
        ("丹师", "sub", True),
        ("阵师", "sub", False),
    ]
    assert drafts[0]["stages"] == [
        {"name": "练气", "description": "引气入体"},
        {"name": "筑基", "description": ""},
        {"name": "金丹", "description": ""},
    ]
    assert any("空壳" in w for w in result["warnings"]) and any("丹师" in w for w in result["warnings"])

    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "3 个职业" in prompt and "丹师（副修）：一品" in prompt and "天地灵气复苏" in prompt and "偏东方" in prompt
    assert "- 丹师（副修）：一品\n  描述：炼丹为业\n  特殊能力：控火" in prompt and "……\n  体系规则：丹毒累积" in prompt
    assert "需火灵根" not in prompt  # 获得条件不进提示词
    # 不写库、不写生成记录
    assert await _count(app, Career) == 1
    assert await _count(app, GenerationLog) == 0

    # 预览：取消勾选冲突项，只保存其余两个
    chosen = [d for d in drafts if not d["conflict"]]
    resp = await client.post(
        f"{base}/careers/batch",
        json={"careers": [{k: v for k, v in d.items() if k != "conflict"} for d in chosen]},
    )
    assert resp.status_code == 201, resp.text
    assert [c["name"] for c in (await client.get(f"{base}/careers")).json()] == ["丹师", "剑修", "阵师"]


async def test_career_generate_forced_kind(client, fake_llm):
    base = await _setup(client)
    fake_llm.push(json.dumps(GENERATED, ensure_ascii=False))
    _, events = await run_task(client, await client.post(f"{base}/careers/generate", json={"count": 2, "kind": "sub"}))
    assert {d["kind"] for d in events[-1][1]["result"]["drafts"]} == {"sub"}
    assert "全部为副修职业" in fake_llm.calls[-1]["messages"][-1]["content"]
    resp = await client.post(f"{base}/careers/generate", json={"count": 6})
    assert resp.status_code == 422
    fake_llm.push('{"careers": []}')
    task, _ = await run_task(client, await client.post(f"{base}/careers/generate"))
    assert task["status"] == "failed" and "未解析到任何职业" in task["error"]


async def test_character_generate_with_career_suggestions(app, client, fake_llm):
    """生成角色时参考已有职业；草稿带 career_suggestions；with-relations 一并保存挂接（来源 ai）。"""
    base = await _setup(client)
    sword = await _career(client, base, "剑修", ["练气", "筑基", "金丹"])
    dan = await _career(client, base, "丹师", ["一品", "二品"], kind="sub")
    magic = await _career(client, base, "法修", ["学徒"])
    fake_llm.push(
        json.dumps(
            {
                "character": {"name": "张三", "role_type": "配角"},
                "relationships": [],
                "careers": [
                    {"name": "剑修", "stage": "筑基", "role": "main", "note": "瓶颈期"},
                    {"name": "丹师", "stage": "九品", "role": "main"},
                    {"name": "法修", "stage": "学徒"},
                    {"name": "不存在", "stage": "一"},
                ],
            },
            ensure_ascii=False,
        )
    )
    _, events = await run_task(client, await client.post(
        f"{base}/characters/generate", json={"instruction": "参考剑修、丹师和法修"},
    ))
    result = events[-1][1]["result"]
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "剑修（主修）：练气 → 筑基 → 金丹" in prompt and '"careers"' in prompt
    rows = [
        (s["career_name"], s["career_id"], s["role"], s["stage_name"], s["resolved"])
        for s in result["career_suggestions"]
    ]
    assert rows == [
        ("剑修", sword["id"], "main", "筑基", True),
        # 角色给错了主副：按职业类别；阶段对不上取第 1 阶并给警告
        ("丹师", dan["id"], "sub", "一品", True),
        ("法修", magic["id"], "main", "学徒", False),
        ("不存在", None, None, "一", False),
    ]
    assert result["career_suggestions"][2]["reason"] == "该角色已有主职业"
    assert result["career_suggestions"][3]["reason"] == "未找到名为「不存在」的职业"
    assert any("九品" in w for w in result["warnings"])
    assert await _count(app, CharacterCareer) == 0

    picked = [s for s in result["career_suggestions"] if s["resolved"]]
    resp = await client.post(
        f"{base}/characters/with-relations",
        json={
            "character": result["draft"],
            "careers": [{"career_id": s["career_id"], "stage_id": s["stage_id"], "note": s["note"]} for s in picked],
        },
    )
    assert resp.status_code == 201, resp.text
    saved = resp.json()
    assert [(a["career_name"], a["stage_name"], a["note"]) for a in saved["careers"]] == [
        ("剑修", "筑基", "瓶颈期"),
        ("丹师", "一品", ""),
    ]
    hist = (await client.get(f"{base}/characters/{saved['character']['id']}/career-history")).json()
    assert {h["source"] for h in hist} == {"ai"}


async def test_character_generate_without_careers_unchanged(client, fake_llm):
    base = await _setup(client)
    fake_llm.push('{"character": {"name": "甲"}, "careers": [{"name": "剑修"}]}')
    _, events = await run_task(client, await client.post(f"{base}/characters/generate"))
    assert events[-1][1]["result"]["career_suggestions"] == []
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "职业" not in prompt and '"careers"' not in prompt


CAREERS_WIZARD = {
    "careers": [
        {"name": "剑修", "kind": "main", "rules": "剑心通明", "stages": [{"name": "练气"}, {"name": "筑基"}]},
        {"name": "丹师", "kind": "sub", "stages": [{"name": "一品"}]},
    ]
}


async def test_wizard_careers_then_characters(app, client, fake_llm):
    """AC6：向导职业步骤整体覆盖；之后角色向导为角色分配职业（来源 wizard），匹配不上 / 超限给 warnings。"""
    base = await _setup(client)
    old = await _career(client, base, "旧职业", ["一"])
    old_char = (await client.post(f"{base}/characters", json={"name": "旧人"})).json()
    await client.post(f"{base}/characters/{old_char['id']}/careers", json={"career_id": old["id"]})
    await client.post(f"{base}/world", json={"title": "灵气", "content": "灵气复苏"})

    fake_llm.push(json.dumps(CAREERS_WIZARD, ensure_ascii=False))
    resp = await client.post(f"{base}/wizard/careers", json={"career_count": 2, "instruction": "参考灵气体系"})
    assert resp.status_code == 202, resp.text
    assert (resp.json()["kind"], resp.json()["title"]) == ("wizard.careers", "职业体系向导")
    task, events = await run_task(client, resp)
    assert task["status"] == "succeeded", task
    result = events[-1][1]["result"]
    assert result["kind"] == "careers"
    assert [(c["name"], c["kind"], [s["name"] for s in c["stages"]]) for c in result["items"]] == [
        ("剑修", "main", ["练气", "筑基"]),
        ("丹师", "sub", ["一品"]),
    ]
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "灵气复苏" in prompt and "约 2 个职业" in prompt
    # 旧职业与旧挂接、挂接历史被清掉
    assert [c["name"] for c in (await client.get(f"{base}/careers")).json()] == ["剑修", "丹师"]
    assert (await client.get(f"{base}/characters/{old_char['id']}/careers")).json() == []
    assert (await client.get(f"{base}/characters/{old_char['id']}/career-history")).json() == []

    fake_llm.push(
        json.dumps(
            {
                "characters": [
                    {
                        "name": "林远",
                        "role_type": "主角",
                        "careers": [
                            {"name": "剑修", "stage": "筑基", "role": "main", "note": "半步金丹"},
                            {"name": "丹师", "stage": "九品", "role": "sub"},
                            {"name": "符师", "stage": "一品", "role": "sub"},
                        ],
                    },
                    {"name": "张三", "careers": [{"name": "丹师"}, {"name": "丹师", "stage": "一品"}]},
                    {"name": "李四"},
                ]
            },
            ensure_ascii=False,
        )
    )
    _, events = await run_task(client, await client.post(
        f"{base}/wizard/characters", json={"character_count": 3, "instruction": "可参考剑修、丹师"},
    ))
    result = events[-1][1]["result"]
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "剑修（主修）：练气 → 筑基" in prompt and '"careers"' in prompt
    assert [(a["career_name"], a["stage_name"], a["note"]) for a in result["assignments"]] == [
        ("剑修", "筑基", "半步金丹"),
        ("丹师", "一品", ""),
        ("丹师", "一品", ""),
    ]
    warnings = result["warnings"]
    assert any("符师" in w and "不存在" in w for w in warnings)
    assert any("九品" in w for w in warnings)
    assert any("张三" in w and "该角色已挂该职业" in w for w in warnings)
    hero = next(i for i in result["items"] if i["name"] == "林远")
    hist = (await client.get(f"{base}/characters/{hero['id']}/career-history")).json()
    assert {h["source"] for h in hist} == {"wizard"} and len(hist) == 2

    # 再跑一次职业向导：挂接与历史随职业一起被清掉，角色保留
    fake_llm.push(json.dumps(CAREERS_WIZARD, ensure_ascii=False))
    await run_task(client, await client.post(f"{base}/wizard/careers"))
    assert await _count(app, CharacterCareer) == 0 and await _count(app, CareerHistory) == 0
    assert len((await client.get(f"{base}/characters")).json()) == 3


async def test_wizard_characters_without_careers_unchanged(client, fake_llm):
    """AC6：没有职业（跳过职业步骤）时角色向导的提示词与写入不变；输出里多余的 careers 被忽略。"""
    base = await _setup(client)
    fake_llm.push(json.dumps({"characters": [{"name": "甲", "careers": [{"name": "剑修"}]}, {"name": "乙"}]}))
    _, events = await run_task(client, await client.post(f"{base}/wizard/characters"))
    result = events[-1][1]["result"]
    assert (result["assignments"], result["warnings"]) == ([], [])
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "职业" not in prompt and '"careers"' not in prompt


async def test_wizard_careers_parse_error_keeps_data(client, fake_llm):
    base = await _setup(client)
    await _career(client, base, "剑修", ["练气"])
    fake_llm.push('{"careers": [{"name": "无阶", "stages": []}]}')
    task, _ = await run_task(client, await client.post(f"{base}/wizard/careers"))
    assert task["status"] == "failed" and "未解析到任何职业" in task["error"]
    assert [c["name"] for c in (await client.get(f"{base}/careers")).json()] == ["剑修"]


async def test_chapter_context_includes_careers(app, client, fake_llm):
    """AC5：涉及角色卡含职业行，【职业体系】列出所挂职业的完整阶梯；未涉及的职业不出现。"""
    base = await _setup(client)
    stages = ["练气", "筑基", "金丹", "元婴", "化神", "炼虚", "合体", "大乘", "渡劫"]
    sword = await _career(client, base, "剑修", stages, rules="以剑入道，" + "规" * 300)
    dan = await _career(client, base, "丹师", ["一品"] + [f"{i}品" for i in range(2, 10)], kind="sub")
    await _career(client, base, "魔修", ["炼魔", "化魔"])
    hero = (await client.post(f"{base}/characters", json={"name": "林远", "role_type": "主角"})).json()
    villain = (await client.post(f"{base}/characters", json={"name": "白夜", "role_type": "反派"})).json()
    url = f"{base}/characters/{hero['id']}/careers"
    zhuji = next(s["id"] for s in sword["stages"] if s["name"] == "筑基")
    await client.post(url, json={"career_id": sword["id"], "stage_id": zhuji, "note": "瓶颈期"})
    await client.post(url, json={"career_id": dan["id"]})
    magic = next(c for c in (await client.get(f"{base}/careers")).json() if c["name"] == "魔修")
    # 白夜不在本章：魔修不应出现
    await client.post(f"{base}/characters/{villain['id']}/careers", json={"career_id": magic["id"]})
    ch = (await client.post(f"{base}/chapters", json={"title": "下山"})).json()
    # 旧档案的主角常驻与名单行为保留；新默认另由可见性专项测试覆盖。
    await mark_existing_settings(app, ch["project_id"])

    fake_llm.push("正文")
    await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/generate"))
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    cards = prompt.split("【本章涉及角色】")[1].split("【")[0]
    assert "职业：剑修·筑基（2/9，瓶颈期）；副职业：丹师·一品（1/9）" in cards
    ladder = prompt.split("【职业体系】")[1].split("【")[0].strip().splitlines()
    assert ladder[0] == "■ 剑修（主修）：" + " → ".join(stages)
    assert ladder[1] == "规则：" + ("以剑入道，" + "规" * 300)[:200] + "……"
    assert ladder[2] == "■ 丹师（副修）：一品 → " + " → ".join(f"{i}品" for i in range(2, 10))
    assert "魔修" not in prompt and "白夜" in prompt
    # 生成记录里的提示词与实际发送一致
    logs = (await client.get(f"{base}/chapters/{ch['id']}/generation-logs")).json()
    detail = (await client.get(f"{base}/chapters/{ch['id']}/generation-logs/{logs[0]['id']}")).json()
    assert "职业：剑修·筑基（2/9，瓶颈期）" in detail["messages"][-1]["content"]


def test_careers_trimmed_before_organizations():
    cast = CastSection(
        cards=["■ 主角\n" + "主" * 300],
        organizations=["■ 青云宗：" + "宗" * 100],
        careers=["■ 剑修（主修）：" + "阶" * 200],
    )

    def ctx(budget: int) -> str:
        return assemble_context(
            project_text="书名：《测试》", world_text="", cast=cast, outline_text="", prior_chapters=[], budget=budget
        )

    full = ctx(100_000)
    assert full.index("【相关组织】") < full.index("【职业体系】")
    trimmed = ctx(len(full) - 150)
    assert "宗" * 100 in trimmed and "阶" * 200 not in trimmed and "【职业体系】" in trimmed
    trimmed = ctx(len(full) - 260)
    assert "【职业体系】" not in trimmed and "主" * 300 in trimmed


def test_normalize_career_drafts():
    drafts, warnings = normalize_career_drafts(
        [{"name": "甲", "type": "副修", "levels": ["一", "二", "一"]}, {"name": "甲", "stages": ["x"]}, "bad"]
    )
    assert drafts == [
        {
            "name": "甲",
            "kind": "sub",
            "description": "",
            "requirements": "",
            "abilities": "",
            "rules": "",
            "stages": [{"name": "一", "description": ""}, {"name": "二", "description": ""}],
        }
    ]
    assert warnings == ["重复的职业「甲」，只保留第一个"]
    long, _ = normalize_career_drafts({"name": "长" * 200, "stages": ["阶" * 80]})
    assert len(long[0]["name"]) == 100 and len(long[0]["stages"][0]["name"]) == 50


def test_no_career_prompts_keep_legacy_layout():
    """AC6：没有职业时两个提示词的要求编号与输出格式保持旧版原样（careers_text 为空 = 旧版逐字输出）。"""
    from app.ai.prompts import build_character_generate_messages, build_characters_messages
    from app.ai.prompts.common import JSON_ONLY_RULE, ProjectInfo

    info = ProjectInfo(title="测试")
    wizard = build_characters_messages(info, character_count=5)[-1]["content"]
    assert f"\n5. {JSON_ONLY_RULE}\n\n【输出格式】" in wizard and "\n6. " not in wizard
    assert '"profile": "简介"}\n  ],\n  "relationships"' in wizard
    assert wizard == build_characters_messages(info, character_count=5, careers_text="  ")[-1]["content"]
    single = build_character_generate_messages(info)[-1]["content"]
    assert f"\n5. {JSON_ONLY_RULE}\n\n【输出格式】" in single and "\n6. " not in single
    assert single.endswith('"description": "关系描述"}\n  ]\n}')
    with_careers = build_characters_messages(info, character_count=5, careers_text="剑修（主修）：练气")[-1]["content"]
    assert f"\n6. {JSON_ONLY_RULE}" in with_careers and '"careers": [' in with_careers
