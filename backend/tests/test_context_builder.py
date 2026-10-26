from app.models import Chapter
from app.services.context_builder import (
    CastSection,
    ForeshadowSection,
    PriorChapter,
    assemble_context,
    chapter_task,
)
from tests.helpers import create_ai_config, create_project, register, run_task


def _chapters(n: int, size: int) -> list[PriorChapter]:
    return [PriorChapter(chapter_no=i, title=f"标题{i}", content=f"第{i}章内容" + "字" * size) for i in range(1, n + 1)]


def _build(budget: int, chapters: list[PriorChapter], world: str = "世界观设定", chars: str = "角色卡") -> str:
    return assemble_context(
        project_text="书名：《测试》",
        world_text=world,
        cast=CastSection(cards=[chars]),
        outline_text="第1卷\n  第1章 a：b",
        prior_chapters=chapters,
        budget=budget,
    )


def test_full_context_within_budget():
    ctx = _build(100_000, _chapters(5, 300))
    assert "【世界观】" in ctx and "【本章涉及角色】\n角色卡" in ctx and "【当前卷大纲】" in ctx
    # 最近 2 章全文
    assert "第5章 标题5\n第5章内容" + "字" * 300 in ctx
    assert "第4章 标题4\n第4章内容" in ctx
    # 更早章节只有标题 + 前 200 字摘要
    summary_section = ctx.split("【前情提要（更早章节）】")[1].split("【最近章节正文】")[0]
    assert "第1章 标题1：" in summary_section
    line1 = next(line for line in summary_section.splitlines() if line.startswith("第1章"))
    assert len(line1.split("：", 1)[1]) == 200 + len("……")


def test_trims_earliest_summaries_first():
    chapters = _chapters(10, 300)
    full = _build(1_000_000, chapters)
    budget = len(full) - 300
    ctx = _build(budget, chapters)
    assert len(ctx) <= budget
    assert "第1章 标题1：" not in ctx  # 最早摘要被丢弃
    assert "第8章 标题8：" in ctx  # 较近摘要保留
    assert "第10章内容" + "字" * 300 in ctx  # 最近章节正文完整
    assert "世界观设定" in ctx


def test_trims_recent_chapter_head_after_summaries():
    chapters = _chapters(4, 2000)
    ctx = _build(3000, chapters)
    assert len(ctx) <= 3000
    assert "【前情提要（更早章节）】" not in ctx
    # 最后一章的结尾必须保留（利于衔接）
    assert ctx.endswith("字" * 50)
    assert "世界观设定" in ctx


def test_hard_budget_respected_even_with_huge_settings():
    ctx = _build(1500, _chapters(3, 5000), world="设" * 10_000, chars="角" * 10_000)
    assert len(ctx) <= 1500
    assert "【作品信息】" in ctx


def test_no_prior_chapters():
    ctx = _build(5000, [])
    assert "【最近章节正文】" not in ctx
    assert "【前情提要" not in ctx


async def test_generate_uses_budgeted_context(app, client, fake_llm):
    """端到端：生成第 N 章时上下文只含此前章节，且最早章节被裁剪。"""
    # 2 章全文约 1220 字 + 3 条摘要约 630 字，预算 1600 迫使最早摘要被裁剪
    app.state.settings.context_char_budget = 1600
    await register(client)
    await create_ai_config(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    for i in range(1, 6):
        await client.post(f"{base}/chapters", json={"title": f"章{i}", "content": f"内容{i}" + "文" * 600})
    target = (await client.post(f"{base}/chapters", json={"title": "新章"})).json()
    fake_llm.push("新章节正文")
    await run_task(client, await client.post(f"/api/v1/chapters/{target['id']}/generate"))
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "内容5" in prompt
    assert "第1章 章1" not in prompt
    assert "第6章 新章" in prompt


def test_summary_prefers_analysis_summary():
    """更早章节有分析摘要时用摘要（不截断为 200 字），没有时退回正文开头。"""
    chapters = _chapters(4, 300)
    chapters[0].summary = "第一章的分析摘要" + "要" * 250
    ctx = _build(100_000, chapters)
    summary_section = ctx.split("【前情提要（更早章节）】")[1].split("【最近章节正文】")[0]
    assert "第1章 标题1：第一章的分析摘要" + "要" * 250 in summary_section
    line2 = next(line for line in summary_section.splitlines() if line.startswith("第2章"))
    assert line2.split("：", 1)[1].startswith("第2章内容") and len(line2.split("：", 1)[1]) == 200 + len("……")


def _foreshadows() -> ForeshadowSection:
    return ForeshadowSection(
        to_plant=["◆ 本章计划埋设：#1 玉佩 —— 埋法：雨夜"],
        must_resolve=["◆ 本章必须回收：#2 旧伤 —— 回收：揭底"],
        overdue=["◆ 超期未回收：#3 信件（已超期 2 章）"],
        upcoming=["◆ 即将到期（可适当铺垫）：#4 钥匙（第 10 章）"],
        long_term=["◆ 长线伏笔：#5 血脉" + "长" * 40],
    )


def _build_fs(budget: int, foreshadows: ForeshadowSection | None, chapters=None) -> str:
    return assemble_context(
        project_text="书名：《测试》",
        world_text="",
        cast=CastSection(cards=["角色卡"], roster=["甲（配角）", "乙（配角）"]),
        outline_text="第1卷\n  第1章 a：b",
        prior_chapters=chapters or [],
        budget=budget,
        foreshadows=foreshadows,
    )


def test_foreshadow_section_position_and_order():
    """AC4：【伏笔提醒】在当前卷大纲之后、前情提要之前，五档依次排列。"""
    ctx = _build_fs(100_000, _foreshadows(), _chapters(4, 100))
    assert ctx.index("【当前卷大纲】") < ctx.index("【伏笔提醒】") < ctx.index("【前情提要（更早章节）】")
    section = ctx.split("【伏笔提醒】\n")[1].split("\n\n")[0].splitlines()
    assert [line.split("：")[0] for line in section] == [
        "◆ 本章计划埋设",
        "◆ 本章必须回收",
        "◆ 超期未回收",
        "◆ 即将到期（可适当铺垫）",
        "◆ 长线伏笔",
    ]


def test_no_foreshadows_context_unchanged():
    """没有伏笔时上下文与原来一致（不出现该段）。"""
    plain = _build_fs(100_000, None, _chapters(4, 100))
    assert plain == _build_fs(100_000, ForeshadowSection(), _chapters(4, 100))
    assert "【伏笔提醒】" not in plain


def test_foreshadow_trim_order():
    """AC4：预算紧张时先整档去掉长线，再去掉即将到期；都在截断名单之前，其余三档保留。"""
    section = _foreshadows()
    full = _build_fs(100_000, section)
    long_line = section.long_term[0]
    ctx = _build_fs(len(full) - 5, section)
    assert long_line not in ctx and "#4 钥匙" in ctx and "乙（配角）" in ctx
    ctx = _build_fs(len(full) - len(long_line) - 5, section)
    assert "#4 钥匙" not in ctx and "#5" not in ctx and "乙（配角）" in ctx
    assert "#1 玉佩" in ctx and "#2 旧伤" in ctx and "#3 信件" in ctx
    # 再紧才轮到名单
    ctx = _build_fs(len(full) - len(long_line) - len(section.upcoming[0]) - 3, section)
    assert "乙（配角）" not in ctx and "#3 信件" in ctx
    # 调用方的对象不被裁剪改动
    assert section.long_term == [long_line]


async def test_generate_prompt_has_foreshadow_reminders(app, client, fake_llm):
    """AC4 端到端：生成第 8 章时提示词含五档提醒，关闭注入 / 已回收 / 已废弃的不出现。"""
    await register(client)
    await create_ai_config(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    lin = (await client.post(f"{base}/characters", json={"name": "林远"})).json()

    async def fs(**body):
        resp = await client.post(f"{base}/foreshadows", json=body)
        assert resp.status_code == 201, resp.text
        return resp.json()

    planted = {"status": "planted", "actual_plant_chapter": 1}
    to_plant = await fs(
        title="玉佩来历", planned_plant_chapter=8, plant_hint="打斗间隙\n瞥见", character_ids=[lin["id"]]
    )
    must = await fs(title="师父的旧伤", planned_resolve_chapter=8, reveal_hint="旧伤来自魔教", **planted)
    overdue = await fs(title="神秘信件", planned_resolve_chapter=6, **planted)
    upcoming = await fs(title="钥匙", planned_resolve_chapter=10, **planted)
    long_term = await fs(title="血脉之秘", content="主角身负上古血脉", importance=5, **planted)
    hidden = await fs(title="关闭的", planned_resolve_chapter=8, include_in_context=False, **planted)
    done = await fs(
        title="已回收的", planned_resolve_chapter=8, actual_resolve_chapter=7, **{**planted, "status": "resolved"}
    )
    gone = await fs(title="已废弃的", planned_plant_chapter=8, status="abandoned")
    target = (await client.post(f"{base}/chapters", json={"title": "新章", "chapter_no": 8})).json()
    fake_llm.push("新章节正文")
    await run_task(client, await client.post(f"/api/v1/chapters/{target['id']}/generate"))
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    section = prompt.split("【伏笔提醒】\n")[1].split("\n\n")[0].splitlines()
    assert section == [
        f"◆ 本章计划埋设：#{to_plant['id']} 玉佩来历 —— 埋法：打斗间隙 瞥见（关联：林远）",
        f"◆ 本章必须回收：#{must['id']} 师父的旧伤 —— 回收：旧伤来自魔教",
        f"◆ 超期未回收：#{overdue['id']} 神秘信件（已超期 2 章）",
        f"◆ 即将到期（可适当铺垫）：#{upcoming['id']} 钥匙（第 10 章）",
        f"◆ 长线伏笔：#{long_term['id']} 血脉之秘：主角身负上古血脉",
    ]
    for item in (hidden, done, gone):
        assert item["title"] not in prompt


async def test_chapter_task_prefers_plan(app, client):
    """本章任务：有细纲时用细纲（梗概为空退回大纲梗概），没有细纲用大纲，都没有为空。"""
    await register(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    o = (await client.post(f"{base}/outlines", json={"title": "大纲标题", "summary": "大纲梗概"})).json()
    await client.put(
        f"{base}/outlines/{o['id']}/plans",
        json={"plans": [{"title": "细一", "key_events": ["甲"], "goal": "目标", "est_words": 5000}, {"title": "细二"}]},
    )
    built = (await client.post(f"{base}/outlines/{o['id']}/plans/build")).json()
    plain = (await client.post(f"{base}/chapters", json={"title": "散章"})).json()
    linked = (await client.post(f"{base}/chapters", json={"outline_id": o["id"]})).json()
    async with app.state.sessionmaker() as db:
        first = await db.get(Chapter, built["chapters"][0]["id"])
        task = await chapter_task(db, first)
        assert (task.source, task.title, task.summary, task.key_events, task.goal, task.est_words) == (
            "plan",
            "细一",
            "大纲梗概",
            ["甲"],
            "目标",
            5000,
        )
        assert task.match_text() == "大纲梗概\n甲"
        task = await chapter_task(db, await db.get(Chapter, linked["id"]))
        assert (task.source, task.title, task.summary, task.key_events) == ("outline", "大纲标题", "大纲梗概", [])
        task = await chapter_task(db, await db.get(Chapter, plain["id"]))
        assert (task.source, task.summary) == ("", "")
