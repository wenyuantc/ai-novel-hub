import pytest
from sqlalchemy import event, inspect, select

from app.models import (
    Career,
    CareerStage,
    Chapter,
    Character,
    CharacterCareer,
    Organization,
    OrganizationMember,
    Project,
    Relationship,
    RelationshipType,
    SettingProgression,
    SettingTextField,
    SettingTimeline,
    WorldSetting,
)
from app.services.context_builder import (
    KNOWLEDGE_HEADER,
    MEMORY_HEADER,
    CastSection,
    ForeshadowSection,
    MemoryLine,
    MemorySection,
    PriorChapter,
    assemble_context,
    build_chapter_context,
    build_setting_context,
    chapter_task,
    prior_coverage,
)
from tests.helpers import analysis_json, create_ai_config, create_project, register, run_task


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


async def test_chapter_and_analysis_use_story_time_settings(app, client, fake_llm):
    """后期设定不能泄漏至早章；整章生成和分析均使用相同的章初投影。"""
    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    pid = project["id"]

    async with app.state.sessionmaker() as db:
        hero = Character(
            project_id=pid, name="林远", role_type="主角", current_state="金丹状态", life_status="dead",
            relations_text="后来与苏晚成为盟友",
        )
        partner = Character(project_id=pid, name="苏晚", role_type="主角")
        place = WorldSetting(project_id=pid, title="青云城", category="place", content="城池沦陷")
        org = Organization(project_id=pid, name="青云宗", org_type="宗门")
        old_org = Organization(project_id=pid, name="玄天阁", org_type="书院")
        career = Career(project_id=pid, name="剑修", kind="main")
        old_career = Career(project_id=pid, name="丹师", kind="sub")
        ch5 = Chapter(project_id=pid, chapter_no=5, title="五", content="林远和苏晚走进城池。")
        ch20 = Chapter(project_id=pid, chapter_no=20, title="二十", content="晋升发生在本章结尾。")
        ch21 = Chapter(project_id=pid, chapter_no=21, title="二一", content="")
        db.add_all([hero, partner, place, org, old_org, career, old_career, ch5, ch20, ch21])
        await db.flush()
        first = CareerStage(career_id=career.id, position=1, name="练气")
        second = CareerStage(career_id=career.id, position=2, name="金丹")
        old_stage = CareerStage(career_id=old_career.id, position=1, name="初品")
        db.add_all([first, second, old_stage])
        await db.flush()
        friendship = await db.scalar(select(RelationshipType).where(RelationshipType.role_a == "朋友"))
        assert friendship is not None
        relation = Relationship(
            project_id=pid, a_id=hero.id, b_id=partner.id, pair_low=hero.id, pair_high=partner.id,
            type_id=friendship.id, intimacy=80, description="结为盟友",
        )
        db.add_all([
            relation,
            CharacterCareer(character_id=hero.id, career_id=career.id, role="main", stage_id=second.id),
            OrganizationMember(character_id=hero.id, organization_id=org.id, position="宗主", status="left"),
        ])
        await db.flush()
        hero_base = {key: getattr(hero, key) for key in (
            "profile", "appearance", "personality", "background", "current_state", "age", "gender", "life_status",
        )}
        hero_base.update(current_state="练气状态", life_status="alive")
        timeline_hero = SettingTimeline(
            project_id=pid, subject_kind="character", character_id=hero.id, baseline=hero_base
        )
        timeline_place = SettingTimeline(project_id=pid, subject_kind="world", world_setting_id=place.id,
                                         baseline={"content": "山城完好"})
        timeline_career = SettingTimeline(project_id=pid, subject_kind="career", character_id=hero.id,
                                          career_id=career.id, baseline={"exists": True, "role": "main",
                                                                         "stage_id": first.id, "note": ""})
        timeline_org = SettingTimeline(project_id=pid, subject_kind="membership", character_id=hero.id,
                                       organization_id=org.id, baseline={"exists": True, "position": "弟子",
                                                                          "status": "active", "note": ""})
        # 这两条旧挂接的最新投影不存在，历史读取不能只枚举当前挂接表。
        timeline_old_org = SettingTimeline(project_id=pid, subject_kind="membership", character_id=hero.id,
                                           organization_id=old_org.id, baseline={"exists": True,
                                               "position": "记名弟子", "status": "active", "note": ""})
        timeline_old_career = SettingTimeline(project_id=pid, subject_kind="career", character_id=hero.id,
                                              career_id=old_career.id, baseline={"exists": True, "role": "sub",
                                                                                 "stage_id": old_stage.id, "note": ""})
        timeline_relation = SettingTimeline(project_id=pid, subject_kind="relationship", character_id=hero.id,
                                            related_character_id=partner.id, baseline={"exists": False})
        db.add_all([timeline_hero, timeline_place, timeline_career, timeline_org, timeline_old_org,
                    timeline_old_career, timeline_relation])
        await db.flush()
        field = SettingTextField(timeline_id=timeline_hero.id, label="佩剑", base_value="木剑")
        world_field = SettingTextField(timeline_id=timeline_place.id, label="地标", base_value="古城门")
        db.add_all([field, world_field])
        await db.flush()

        def event(timeline, key, value, *, custom_id=None):
            return SettingProgression(
                timeline_id=timeline.id, field_key=key, custom_field_id=custom_id, operation="replace", value=value,
                anchor_kind="chapter", chapter_id=ch20.id, boundary="end",
            )

        db.add_all([
            event(timeline_hero, "current_state", "金丹状态"),
            event(timeline_hero, "life_status", "dead"),
            event(timeline_hero, None, "神剑", custom_id=field.id),
            event(timeline_place, "content", "城池沦陷"),
            event(timeline_place, None, "门楼废墟", custom_id=world_field.id),
            event(timeline_career, "stage_id", second.id),
            event(timeline_old_career, "presence", {"exists": False}),
            event(timeline_org, "position", "宗主"),
            event(timeline_org, "status", "left"),
            event(timeline_old_org, "presence", {"exists": False}),
            event(timeline_relation, "presence", {
                "exists": True,
                "state": {"identity": {"a_id": hero.id, "b_id": partner.id, "type_id": friendship.id},
                          "intimacy": 80, "status": "normal", "description": "结为盟友"},
            }),
        ])
        await db.commit()
        ids = (ch5.id, ch20.id, ch21.id)
        for chapter_id in ids:
            ch = await db.get(Chapter, chapter_id)
            result = await build_chapter_context(db, await db.get(Project, pid), ch, 100_000)
            if chapter_id in ids[:2]:
                assert "当前状态：练气状态" in result
                assert "当前状态：金丹状态" not in result
                assert "■ 林远 · 主角【已死亡】" not in result
                assert "职业：剑修·练气" in result and "职业：剑修·金丹" not in result
                assert "副职业：丹师·初品" in result
                assert "山城完好" in result and "城池沦陷" not in result
                assert "地标：古城门" in result and "地标：门楼废墟" not in result
                assert "佩剑：木剑" in result and "佩剑：神剑" not in result
                assert "青云宗（弟子）" in result and "已离开" not in result
                assert "玄天阁（记名弟子）" in result
                assert "【人物关系】" not in result and "关系备注：" not in result
            else:
                assert "当前状态：金丹状态" in result and "■ 林远 · 主角【已死亡】" in result
                assert "职业：剑修·金丹" in result and "城池沦陷" in result
                assert "副职业：丹师·初品" not in result and "玄天阁" not in result
                assert "地标：门楼废墟" in result
                assert "佩剑：神剑" in result and "青云宗（宗主，已离开）" in result
                assert "【人物关系】" in result and "结为盟友" in result
        # 按章投影只能构造只读DTO，不能把较早值写回当前ORM。
        assert hero.current_state == "金丹状态" and hero.life_status == "dead"
        assert not db.is_modified(hero)

    fake_llm.push(analysis_json(foreshadows=[]))
    await run_task(client, await client.post(f"/api/v1/chapters/{ids[0]}/analyze"))
    analysis_prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "当前状态：练气状态" in analysis_prompt and "当前状态：金丹状态" not in analysis_prompt
    assert "职业：剑修·练气" in analysis_prompt and "职业：剑修·金丹" not in analysis_prompt
    assert "副职业：丹师·初品" in analysis_prompt
    assert "青云宗（弟子）" in analysis_prompt and "已离开" not in analysis_prompt
    assert "结为盟友" not in analysis_prompt

    fake_llm.push("第五章正文")
    await run_task(client, await client.post(f"/api/v1/chapters/{ids[0]}/generate"))
    generation_prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "当前状态：练气状态" in generation_prompt and "当前状态：金丹状态" not in generation_prompt
    assert "山城完好" in generation_prompt and "城池沦陷" not in generation_prompt


@pytest.mark.parametrize("builder", ["chapter", "setting"])
async def test_context_read_does_not_flush_unrelated_changes(app, client, builder):
    """整段只读查询不刷新其他脏对象，同时可读取调用方已显式 flush 的时间线。"""
    await register(client)
    project = await create_project(client, title="测试作品")
    unrelated = await create_project(client, title="另一个作品")

    async with app.state.sessionmaker() as db:
        current_project = await db.get(Project, project["id"])
        chapter = Chapter(project_id=project["id"], chapter_no=5, title="第五章")
        world = WorldSetting(project_id=project["id"], title="青云城", content="旧城", category="place")
        db.add_all([chapter, world])
        await db.flush()
        timeline = SettingTimeline(
            project_id=project["id"], subject_kind="world", world_setting_id=world.id, baseline={"content": "旧城"}
        )
        db.add(timeline)
        await db.flush()
        db.add(SettingProgression(
            timeline_id=timeline.id, field_key="content", operation="replace", value="章初新城",
            anchor_kind="planned", chapter_no=5, boundary="start",
        ))
        # 调用方明确 flush 的新剧情变化，在同一事务中仍可用于之后的只读投影。
        await db.flush()
        other_project = await db.get(Project, unrelated["id"])
        other_project.title = "尚未保存的改名"
        assert inspect(other_project).attrs.title.history.has_changes()
        flushes = []

        def before_flush(*_args):
            flushes.append(True)

        event.listen(db.sync_session, "before_flush", before_flush)
        if builder == "chapter":
            result = await build_chapter_context(db, current_project, chapter, 10_000)
        else:
            result = await build_setting_context(db, current_project, chapter, budget=10_000)
        assert "章初新城" in result
        assert not flushes
        assert other_project.title == "尚未保存的改名"
        assert inspect(other_project).attrs.title.history.has_changes()
        with db.no_autoflush:
            stored_title = await db.scalar(select(Project.title).where(Project.id == unrelated["id"]))
        assert stored_title == "另一个作品"
        await db.rollback()


# ---------------- 检索块（knowledge-base） ----------------


def _build_kb(budget: int, *, knowledge: str = "", memories: MemorySection | None = None) -> str:
    return assemble_context(
        project_text="书名：《测试》",
        world_text="世界观设定",
        cast=CastSection(cards=["角色卡"]),
        outline_text="第1卷\n  第1章 a：b",
        prior_chapters=_chapters(6, 300),
        budget=budget,
        knowledge=knowledge,
        memories=memories,
    )


MEMS = MemorySection(
    [
        MemoryLine("- 第1章（情节）：玉佩发光", rank=2),
        MemoryLine("- 第2章（钩子）：钉住的请柬", pinned=True, rank=-1),
        MemoryLine("- 第3章（情节）：玉佩碎裂", rank=0),
        MemoryLine("- 第3章（人物）：林远受伤", rank=1),
    ]
)


def test_empty_retrieval_blocks_context_unchanged():
    assert _build_kb(100_000) == _build(100_000, _chapters(6, 300))
    assert _build_kb(100_000, knowledge="  ", memories=MemorySection([MemoryLine("  ")])) == _build_kb(100_000)
    assert KNOWLEDGE_HEADER not in _build_kb(100_000) and MEMORY_HEADER not in _build_kb(100_000)


def test_retrieval_blocks_position():
    ctx = _build_kb(100_000, knowledge="- 《设定》门派：天剑宗", memories=MEMS)
    assert ctx.index("【世界观】") < ctx.index(KNOWLEDGE_HEADER) < ctx.index("【本章涉及角色】")
    assert ctx.index("【前情提要（更早章节）】") < ctx.index(MEMORY_HEADER) < ctx.index("【最近章节正文】")
    block = ctx.split(MEMORY_HEADER + "\n", 1)[1].split("\n\n", 1)[0]
    assert block.splitlines() == [m.text for m in MEMS.lines]


def test_retrieval_blocks_trim_first():
    knowledge = "- 《设定》门派：" + "天剑宗" * 100
    full = _build_kb(1_000_000, knowledge=knowledge, memories=MEMS)
    summaries = full.split("【前情提要（更早章节）】\n", 1)[1].split("\n\n", 1)[0]

    # 1. 先截断参考资料尾部，其他不动
    ctx = _build_kb(len(full) - 100, knowledge=knowledge, memories=MEMS)
    assert len(ctx) <= len(full) - 100
    assert KNOWLEDGE_HEADER in ctx and "……" in ctx.split(KNOWLEDGE_HEADER)[1].split("\n\n")[0]
    assert all(m.text in ctx for m in MEMS.lines) and summaries in ctx

    # 2. 参考资料整段去掉后，从最不相关的开始丢弃未钉住的旧情节（rank 大的先丢），前情提要不动
    no_kb = len(_build_kb(1_000_000, memories=MEMS))
    ctx = _build_kb(no_kb - 5, knowledge=knowledge, memories=MEMS)
    assert KNOWLEDGE_HEADER not in ctx
    assert "玉佩发光" not in ctx and "林远受伤" in ctx and "玉佩碎裂" in ctx and "钉住的请柬" in ctx
    assert summaries in ctx

    # 3. 未钉住的都丢弃后才开始丢前情摘要；钉住的保留
    pinned_only = len(_build_kb(1_000_000, memories=MemorySection([MEMS.lines[1]])))
    ctx = _build_kb(pinned_only - 5, knowledge=knowledge, memories=MEMS)
    block = ctx.split(MEMORY_HEADER + "\n", 1)[1].split("\n\n", 1)[0]
    assert block == "- 第2章（钩子）：钉住的请柬"
    assert "第1章 标题1：" not in ctx and "第4章 标题4：" in ctx


def test_prior_coverage():
    prior = [
        PriorChapter(1, "a", "正文", summary="摘要一"),
        PriorChapter(2, "b", "正文", summary=""),
        PriorChapter(3, "c", "", summary="摘要三"),
        PriorChapter(4, "d", "正文", summary="摘要四"),
        PriorChapter(5, "e", "正文", summary="摘要五"),
    ]
    full, summarized = prior_coverage(prior)
    assert full == {4, 5} and summarized == {1}
