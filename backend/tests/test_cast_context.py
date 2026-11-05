from types import SimpleNamespace

from sqlalchemy import select

from app.models import Chapter, Character, Project, Relationship, RelationshipType, SettingProgression, SettingTimeline
from app.services.cast_service import is_protagonist, match_characters
from app.services.context_builder import CastSection, RelationLine, assemble_context, build_setting_context
from tests.helpers import create_ai_config, create_project, register, run_task


def _c(cid: int, name: str, aliases: list[str] | None = None, **kw):
    return SimpleNamespace(
        id=cid,
        name=name,
        aliases=aliases or [],
        role_type=kw.get("role_type", ""),
        always_in_context=kw.get("always_in_context", False),
    )


def test_match_characters_rules():
    lin = _c(1, "林")  # 单字名跳过
    linyuan = _c(2, "林远")
    linyuanshan = _c(3, "林远山")
    su = _c(4, "苏晚", ["晚晚", "苏"])
    chars = [lin, linyuan, linyuanshan, su]
    # 长名优先：「林远山」命中后其中的「林远」不再单独计
    assert match_characters("林远山推门而入", chars) == [3]
    # 两者都单独出现时都命中
    assert match_characters("林远山与林远对视", chars) == [2, 3]
    # 别名命中；单字别名「苏」跳过
    assert match_characters("晚晚笑了", chars) == [4]
    assert match_characters("苏州城外，林中小路", chars) == []
    assert match_characters("", chars) == []
    # 同一别名属于两个角色时都命中
    assert match_characters("师兄来了", [_c(5, "甲", ["师兄"]), _c(6, "乙乙", ["师兄"])]) == [5, 6]


def test_is_protagonist():
    assert is_protagonist(_c(1, "a", role_type="主角"))
    assert is_protagonist(_c(1, "a", role_type=" Protagonist "))
    assert is_protagonist(_c(1, "a", role_type="配角", always_in_context=True))
    assert not is_protagonist(_c(1, "a", role_type="女主"))


EXTRA = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥金木水火"


async def test_chapter_context_cast(client, fake_llm):
    """AC8：30 个角色，梗概提到 2 个（其中一个用别名）+ 手动指定 1 个 + 主角 → 完整卡片；其余只进名单。"""
    await register(client)
    await create_ai_config(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"

    async def char(name, **kw):
        resp = await client.post(f"{base}/characters", json={"name": name, **kw})
        assert resp.status_code == 201, resp.text
        return resp.json()

    hero = await char("林远", role_type="主角", personality="坚韧", age="18", gender="男")
    await char("苏晚", role_type="重要配角", aliases=["晚晚"], relations_text="林远的青梅竹马")
    zhang = await char("张三", role_type="配角", relations_text="林远的师父（旧备注）")
    bai = await char("白夜", role_type="反派", life_status="dead")
    extras = [await char(f"龙套{x}", role_type="其他") for x in EXTRA[:26]]
    assert len(extras) + 4 == 30

    master = next(
        t["id"]
        for t in (await client.get(f"{base}/relationship-types")).json()
        if (t["role_a"], t["role_b"]) == ("师父", "徒弟")
    )
    await client.post(
        f"{base}/relationships",
        json={"a_id": zhang["id"], "b_id": hero["id"], "type_id": master, "intimacy": 80, "description": "传授剑法"},
    )
    await client.post(f"{base}/relationships", json={"a_id": hero["id"], "b_id": extras[0]["id"], "role_a": "朋友"})
    org = (
        await client.post(f"{base}/organizations", json={"name": "青云宗", "org_type": "宗门", "purpose": "修仙"})
    ).json()
    await client.post(
        f"{base}/organizations/{org['id']}/members", json={"character_id": hero["id"], "position": "弟子"}
    )
    other_org = (await client.post(f"{base}/organizations", json={"name": "黑风寨"})).json()
    await client.post(f"{base}/organizations/{other_org['id']}/members", json={"character_id": extras[1]["id"]})
    await client.post(f"{base}/world", json={"title": "青云山", "content": "仙山", "category": "place"})
    await client.post(f"{base}/world", json={"title": "灵力", "content": "修炼体系", "category": "rule"})

    outline = (
        await client.post(
            f"{base}/outlines", json={"title": "下山", "summary": "林远与晚晚在山门遇到张三", "chapter_no": 2}
        )
    ).json()
    # 上一章：龙套甲 只出现在 1500 字之前，不应被匹配
    await client.post(f"{base}/chapters", json={"title": "一", "content": "龙套甲" + "。" * 2000 + "结尾"})
    ch = (await client.post(f"{base}/chapters", json={"outline_id": outline["id"]})).json()
    resp = await client.put(f"{base}/chapters/{ch['id']}/cast", json={"character_ids": [bai["id"], bai["id"]]})
    assert resp.json() == {"character_ids": [bai["id"]]}
    assert (await client.get(f"{base}/chapters/{ch['id']}/cast")).json() == {"character_ids": [bai["id"]]}

    fake_llm.push("正文")
    await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/generate"))
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]

    cards = prompt.split("【本章涉及角色】")[1].split("【人物关系】")[0]
    heads = [line for line in cards.splitlines() if line.startswith("■ ")]
    # 主角 → 手动指定 → 自动匹配（按角色排序）
    assert heads == ["■ 林远 · 主角", "■ 白夜 · 反派【已死亡】", "■ 苏晚（别名：晚晚） · 重要配角", "■ 张三 · 配角"]
    assert "年龄：18；性别：男" in cards and "性格：坚韧" in cards
    assert "所属组织：青云宗（弟子）" in cards
    # 没有结构化关系的角色保留旧备注；已有结构化关系的不再重复
    assert "关系备注：林远的青梅竹马" in cards and "旧备注" not in cards
    relations = prompt.split("【人物关系】")[1].split("【相关组织】")[0]
    assert relations.strip() == "张三 是 林远 的师父（亲密度 80，正常）：传授剑法"
    orgs = prompt.split("【相关组织】")[1].split("【其他角色（仅名单）】")[0]
    assert orgs.strip() == "■ 青云宗（宗门）：修仙"
    roster = prompt.split("【其他角色（仅名单）】")[1].split("【")[0]
    assert roster.strip().split("，") == [f"龙套{x}（其他）" for x in EXTRA[:26]]
    assert "■ 龙套" not in prompt
    # 世界观按分类分组
    world = prompt.split("【世界观】")[1].split("【本章涉及角色】")[0]
    assert world.index("〔地点〕") < world.index("青云山") < world.index("〔规则〕") < world.index("灵力")

    # 生成记录中保存的提示词与实际发送的一致：能看到涉及角色卡与名单
    logs = (await client.get(f"{base}/chapters/{ch['id']}/generation-logs")).json()
    detail = (await client.get(f"{base}/chapters/{ch['id']}/generation-logs/{logs[0]['id']}")).json()
    logged = detail["messages"][-1]["content"]
    assert logged == prompt
    assert "【本章涉及角色】" in logged and "■ 苏晚（别名：晚晚） · 重要配角" in logged
    assert "【其他角色（仅名单）】" in logged and "龙套乙（其他）" in logged

    with_bad = await client.put(f"{base}/chapters/{ch['id']}/cast", json={"character_ids": [99999]})
    assert with_bad.status_code == 400 and with_bad.json()["detail"] == "部分角色不存在或不属于该项目"


def _cast() -> CastSection:
    return CastSection(
        cards=["■ 主角\n" + "主" * 300, "■ 配角\n" + "配" * 300],
        relations=[RelationLine("主角 是 配角 的师父（亲密度 80，正常）", "描" * 200)],
        organizations=["■ 青云宗（宗门）：" + "宗" * 100],
        roster=[f"龙套{i}（其他）" for i in range(50)],
    )


def _ctx(budget: int) -> str:
    return assemble_context(
        project_text="书名：《测试》",
        world_text="",
        cast=_cast(),
        outline_text="第1卷\n  第1章 a：b",
        prior_chapters=[],
        budget=budget,
    )


def test_cast_trim_order():
    """D4：先裁名单 → 再裁关系描述 → 再裁组织 / 角色卡；涉及角色卡最后才被截。"""
    full = _ctx(100_000)
    assert "龙套49" in full and "描" * 200 in full

    # 稍紧：名单尾部被裁，关系描述保留
    ctx = _ctx(len(full) - 100)
    assert len(ctx) <= len(full) - 100
    assert "龙套0（其他）" in ctx and "龙套49" not in ctx
    assert "描" * 200 in ctx

    # 更紧：名单全部去掉，关系只剩本身，卡片完整
    no_roster = len(full) - len("【其他角色（仅名单）】\n" + "，".join(f"龙套{i}（其他）" for i in range(50))) - 2
    ctx = _ctx(no_roster - 100)
    assert "【其他角色" not in ctx
    assert "主角 是 配角 的师父（亲密度 80，正常）" in ctx and "描" not in ctx
    assert "主" * 300 in ctx and "配" * 300 in ctx and "宗" * 100 in ctx

    # 很紧：组织被截后才截角色卡；后面的角色卡先被截，主角卡保留
    ctx = _ctx(no_roster - 200 - 300 - 150)
    assert "【人物关系】" not in ctx
    assert "宗" * 100 not in ctx
    assert "主" * 300 in ctx and "配" * 300 not in ctx
    assert "【当前卷大纲】" in ctx


async def test_setting_context_uses_historical_hidden_relation_and_text_hint(app, client):
    """当前已终止的关系仍可回溯，选区提到的配角使用相同匹配规则。"""
    await register(client)
    project = await create_project(client)
    pid = project["id"]
    async with app.state.sessionmaker() as db:
        hero = Character(project_id=pid, name="林远", role_type="主角", relations_text="未来和苏晚决裂")
        partner = Character(project_id=pid, name="苏晚")
        chapters = [Chapter(project_id=pid, chapter_no=n, title=str(n)) for n in (5, 10, 15, 20, 21)]
        db.add_all([hero, partner, *chapters])
        await db.flush()
        friend = await db.scalar(select(RelationshipType).where(RelationshipType.role_a == "朋友"))
        enemy = await db.scalar(select(RelationshipType).where(RelationshipType.role_a == "仇人"))
        assert friend is not None and enemy is not None
        db.add(Relationship(
            project_id=pid, a_id=hero.id, b_id=partner.id, pair_low=hero.id, pair_high=partner.id,
            type_id=enemy.id, is_present=False,
        ))
        timeline = SettingTimeline(
            project_id=pid, subject_kind="relationship", character_id=hero.id, related_character_id=partner.id,
            baseline={"exists": True, "identity": {"a_id": hero.id, "b_id": partner.id, "type_id": friend.id},
                      "intimacy": 30, "status": "normal", "description": "旧友"},
        )
        db.add(timeline)
        await db.flush()
        db.add_all([
            SettingProgression(
                timeline_id=timeline.id, field_key="identity", operation="replace",
                value={"a_id": hero.id, "b_id": partner.id, "type_id": enemy.id}, anchor_kind="chapter",
                chapter_id=chapters[1].id, boundary="start",
            ),
            SettingProgression(
                timeline_id=timeline.id, field_key="presence", operation="replace", value={"exists": False},
                anchor_kind="chapter", chapter_id=chapters[3].id, boundary="end",
            ),
        ])
        await db.commit()
        book = await db.get(Project, pid)
        ctx5 = await build_setting_context(db, book, chapters[0], text_hint="苏晚走来", budget=1000)
        ctx15 = await build_setting_context(db, book, chapters[2], text_hint="苏晚走来", budget=1000)
        ctx21 = await build_setting_context(db, book, chapters[4], text_hint="苏晚走来", budget=1000)
        assert ctx5.startswith("【本章有效设定】\n") and "林远 与 苏晚 是朋友" in ctx5
        assert "林远 与 苏晚 是仇人" in ctx15
        assert "【人物关系】" not in ctx21 and "关系备注：" not in ctx21
        assert len(await build_setting_context(db, book, chapters[0], text_hint="苏晚", budget=120)) <= 120
        assert not db.dirty
