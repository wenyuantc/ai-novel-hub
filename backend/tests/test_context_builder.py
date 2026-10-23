from app.services.context_builder import PriorChapter, assemble_context
from tests.helpers import create_ai_config, create_project, register, run_task


def _chapters(n: int, size: int) -> list[PriorChapter]:
    return [PriorChapter(chapter_no=i, title=f"标题{i}", content=f"第{i}章内容" + "字" * size) for i in range(1, n + 1)]


def _build(budget: int, chapters: list[PriorChapter], world: str = "世界观设定", chars: str = "角色卡") -> str:
    return assemble_context(
        project_text="书名：《测试》",
        world_text=world,
        characters_text=chars,
        outline_text="第1卷\n  第1章 a：b",
        prior_chapters=chapters,
        budget=budget,
    )


def test_full_context_within_budget():
    ctx = _build(100_000, _chapters(5, 300))
    assert "【世界观】" in ctx and "【主要角色】" in ctx and "【当前卷大纲】" in ctx
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
