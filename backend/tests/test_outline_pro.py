"""大纲进阶：续写（AC1）、展开 + 建章 + 章节号平移（AC2）、重新展开保留有正文的细纲（AC3）、
细纲接入生成（AC4）、分批展开去重、排序、概览、向导进度。"""

import json

import pytest
from sqlalchemy import select

from app.core.errors import ConflictError
from app.models import ChapterAnalysis, RelationshipHistory, SettingProgression, SettingTimeline, WorldSetting
from app.services import setting_progression_service
from tests.helpers import Gate, create_ai_config, create_project, register, run_task, wait_task


async def _setup(client, n: int = 5) -> tuple[dict, str]:
    await register(client)
    await create_ai_config(client)
    p = await create_project(client)
    base = f"/api/v1/projects/{p['id']}"
    for i in range(1, n + 1):
        resp = await client.post(
            f"{base}/outlines",
            json={"volume_no": 1, "volume_title": "启程", "chapter_no": i, "title": f"纲{i}", "summary": f"梗概{i}"},
        )
        assert resp.status_code == 201, resp.text
    return p, base


def _continue_json(n: int, volume_title: str = "") -> str:
    data = {
        "volume_title": volume_title,
        "outlines": [{"title": f"续{i}", "summary": f"续写梗概{i}"} for i in range(n)],
    }
    return json.dumps(data, ensure_ascii=False)


def _plan(i: int, **kw) -> dict:
    d = {
        "title": f"细{i}",
        "summary": f"细纲梗概{i}",
        "key_events": [f"事件{i}a", f"事件{i}b"],
        "characters": ["林远"],
        "emotion": "紧张",
        "goal": f"目标{i}",
        "est_words": 2500,
    }
    d.update(kw)
    return d


def _expand_json(items: list[dict]) -> str:
    return json.dumps({"chapters": items}, ensure_ascii=False)


def _draft_to_plan(d: dict) -> dict:
    return {
        "title": d["title"],
        "summary": d["summary"],
        "key_events": d["key_events"],
        "character_ids": [c["character_id"] for c in d["characters"] if c["resolved"]],
        "emotion": d["emotion"],
        "goal": d["goal"],
        "est_words": d["est_words"],
    }


async def _outlines(client, base) -> list[dict]:
    return (await client.get(f"{base}/outlines")).json()


async def _chapters(client, base) -> list[dict]:
    return (await client.get(f"{base}/chapters")).json()


# ---------------- AC1 续写 ----------------


async def test_continue_preview_then_append(client, fake_llm):
    p, base = await _setup(client, 10)
    await client.post(f"{base}/foreshadows", json={"title": "神秘玉佩", "status": "planted", "actual_plant_chapter": 2})
    fake_llm.push(_continue_json(5))
    task, events = await run_task(
        client, await client.post(f"{base}/outlines/continue", json={"count": 5, "direction": "主角进入宗门"})
    )
    assert task["status"] == "succeeded" and task["kind"] == "outline.continue" and task["title"] == "AI 续写大纲"
    result = events[-1][1]["result"]
    drafts = result["drafts"]
    assert [(d["volume_no"], d["volume_title"], d["chapter_no"]) for d in drafts] == [
        (1, "启程", 11 + i) for i in range(5)
    ]
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "主角进入宗门" in prompt and "第10章 纲10" in prompt and "神秘玉佩" in prompt and "第 11 章开始" in prompt
    # 只产出草稿
    assert len(await _outlines(client, base)) == 10

    # 预览中删 1 条、改 1 条后采纳：追加 4 条，章节号 11–14
    items = [{k: d[k] for k in ("volume_no", "volume_title", "title", "summary")} for d in drafts]
    del items[2]
    items[0]["title"] = "改过的标题"
    resp = await client.post(f"{base}/outlines/append", json={"items": items})
    assert resp.status_code == 201, resp.text
    assert [(o["chapter_no"], o["title"]) for o in resp.json()] == [
        (11, "改过的标题"),
        (12, "续1"),
        (13, "续3"),
        (14, "续4"),
    ]
    outlines = await _outlines(client, base)
    assert len(outlines) == 14 and outlines[-1]["chapter_no"] == 14


async def test_continue_new_volume_and_truncate(client, fake_llm):
    p, base = await _setup(client, 2)
    fake_llm.push(_continue_json(4, volume_title="模型卷名"))
    _, events = await run_task(
        client, await client.post(f"{base}/outlines/continue", json={"count": 3, "new_volume": {"title": ""}})
    )
    result = events[-1][1]["result"]
    assert [(d["volume_no"], d["volume_title"], d["chapter_no"]) for d in result["drafts"]] == [
        (2, "模型卷名", 3),
        (2, "模型卷名", 4),
        (2, "模型卷名", 5),
    ]
    assert any("只保留前 3 章" in w for w in result["warnings"])
    assert "新的一卷：第 2 卷" in fake_llm.calls[-1]["messages"][-1]["content"]


async def test_continue_resource_conflict(app, client, fake_llm):
    p, base = await _setup(client, 1)
    gate = Gate()
    fake_llm.push([gate, _continue_json(1)])
    first = await client.post(f"{base}/outlines/continue", json={"count": 1})
    assert first.status_code == 202
    await gate.wait_reached()
    second = await client.post(f"{base}/outlines/continue", json={"count": 1})
    assert second.status_code == 409
    gate.release()
    assert (await wait_task(client, first.json()["id"]))["status"] == "succeeded"


# ---------------- AC2 展开 + 建章 ----------------


async def _expand(client, base, outline_id: int, count: int, payload: str, fake_llm) -> dict:
    fake_llm.push(payload)
    task, events = await run_task(
        client,
        await client.post(f"{base}/outlines/{outline_id}/expand", json={"count": count, "instruction": "多写打斗"}),
    )
    assert task["status"] == "succeeded", events[-1]
    assert task["kind"] == "outline.expand" and task["outline_id"] == outline_id
    return events[-1][1]["result"]


async def test_expand_build_shifts_chapter_numbers(app, client, fake_llm):
    p, base = await _setup(client, 5)
    lin = (await client.post(f"{base}/characters", json={"name": "林远", "aliases": ["远哥"]})).json()
    other = (await client.post(f"{base}/characters", json={"name": "苏晚"})).json()
    chapters = (await client.post(f"{base}/chapters/from-outlines", json={})).json()
    assert [c["chapter_no"] for c in chapters] == [1, 2, 3, 4, 5]
    by_no = {c["chapter_no"]: c for c in chapters}
    fs4 = (await client.post(f"{base}/foreshadows", json={"title": "四", "planned_resolve_chapter": 4})).json()
    fs2 = (await client.post(f"{base}/foreshadows", json={"title": "二", "planned_resolve_chapter": 2})).json()
    rel = (
        await client.post(f"{base}/relationships", json={"a_id": lin["id"], "b_id": other["id"], "role_a": "朋友"})
    ).json()
    async with app.state.sessionmaker() as db:
        db.add(ChapterAnalysis(project_id=p["id"], chapter_id=by_no[4]["id"], content_hash="x", chapter_no=4))
        db.add(ChapterAnalysis(project_id=p["id"], chapter_id=by_no[2]["id"], content_hash="x", chapter_no=2))
        db.add(RelationshipHistory(relationship_id=rel["id"], chapter_no=5, source="manual", changes={}))
        await db.commit()

    outlines = await _outlines(client, base)
    o3 = outlines[2]
    result = await _expand(
        client, base, o3["id"], 3, _expand_json([_plan(1, characters=["远哥", "路人"]), _plan(2), _plan(3)]), fake_llm
    )
    assert result["outline_id"] == o3["id"] and result["existing"] == []
    assert [c["character_id"] for c in result["drafts"][0]["characters"]] == [lin["id"], None]
    assert any("路人" in w for w in result["warnings"])
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert (
        "第3章 纲3" in prompt and "（前）第2章 纲2" in prompt and "（后）第4章 纲4" in prompt and "多写打斗" in prompt
    )
    # 只产出草稿
    assert (await client.get(f"{base}/outlines/{o3['id']}/plans")).json() == []

    resp = await client.put(
        f"{base}/outlines/{o3['id']}/plans", json={"plans": [_draft_to_plan(d) for d in result["drafts"]]}
    )
    assert resp.status_code == 200, resp.text
    plans = resp.json()
    assert [(pl["seq"], pl["chapter_id"], pl["character_ids"]) for pl in plans] == [
        (1, None, [lin["id"]]),
        (2, None, [lin["id"]]),
        (3, None, [lin["id"]]),
    ]
    # 首次保存细纲时没有旧细纲，不存快照
    assert (await client.get(f"{base}/outline-snapshots")).json() == []

    resp = await client.post(f"{base}/outlines/{o3['id']}/plans/build")
    assert resp.status_code == 200, resp.text
    built = resp.json()
    # 原 1 纲 1 章的第 3 章绑定第 1 条细纲，其余 2 章插在其后
    assert [(c["chapter_no"], c["plan_id"], c["outline_id"]) for c in built["chapters"]] == [
        (3, plans[0]["id"], o3["id"]),
        (4, plans[1]["id"], o3["id"]),
        (5, plans[2]["id"], o3["id"]),
    ]
    assert built["chapters"][0]["id"] == by_no[3]["id"] and built["chapters"][0]["title"] == "纲3"
    assert [pl["chapter_no"] for pl in built["plans"]] == [3, 4, 5]
    # 原第 4 章及以后整体后移 2
    after = {c["id"]: c["chapter_no"] for c in await _chapters(client, base)}
    assert after[by_no[4]["id"]] == 6 and after[by_no[5]["id"]] == 7 and after[by_no[2]["id"]] == 2
    assert [o["chapter_no"] for o in await _outlines(client, base)] == [1, 2, 3, 6, 7]
    # 引用章节号的数据同步后移，未受影响的不变
    assert (await client.get(f"{base}/foreshadows/{fs4['id']}")).json()["planned_resolve_chapter"] == 6
    assert (await client.get(f"{base}/foreshadows/{fs2['id']}")).json()["planned_resolve_chapter"] == 2
    async with app.state.sessionmaker() as db:
        nos = dict((await db.execute(select(ChapterAnalysis.chapter_id, ChapterAnalysis.chapter_no))).all())
        assert nos == {by_no[4]["id"]: 6, by_no[2]["id"]: 2}
        assert await db.scalar(select(RelationshipHistory.chapter_no).where(RelationshipHistory.chapter_no == 7)) == 7
    # 细纲的出场角色写入章节出场角色
    new_ch = built["chapters"][1]["id"]
    assert (await client.get(f"{base}/chapters/{new_ch}/cast")).json()["character_ids"] == [lin["id"]]
    # 全部建章后再建 → 400
    resp = await client.post(f"{base}/outlines/{o3['id']}/plans/build")
    assert resp.status_code == 400 and resp.json()["detail"] == "细纲都已建章"
    # from-outlines 跳过已展开的大纲
    assert (await client.post(f"{base}/chapters/from-outlines", json={})).json() == []

    tree = (await client.get(f"{base}/outlines/tree")).json()
    t3 = next(t for t in tree if t["id"] == o3["id"])
    assert (t3["plan_count"], t3["built_count"], t3["chapter_id"]) == (3, 3, None)
    t1 = tree[0]
    assert (t1["plan_count"], t1["chapter_id"]) == (0, by_no[1]["id"])


async def test_build_without_existing_chapter_inserts_at_outline(client, fake_llm):
    p, base = await _setup(client, 3)
    outlines = await _outlines(client, base)
    o2 = outlines[1]
    resp = await client.put(f"{base}/outlines/{o2['id']}/plans", json={"plans": [{"title": "甲"}, {"title": "乙"}]})
    assert resp.status_code == 200
    # 已展开（有细纲）但尚未建章的大纲：from-outlines 不按 1 纲 1 章建章
    resp = await client.post(f"{base}/chapters/from-outlines", json={"outline_ids": [o2["id"]]})
    assert resp.status_code == 201 and resp.json() == []
    built = (await client.post(f"{base}/outlines/{o2['id']}/plans/build")).json()
    assert [c["chapter_no"] for c in built["chapters"]] == [2, 3]
    # 大纲的 chapter_no 保持为第一章，后续大纲整体后移
    assert [o["chapter_no"] for o in await _outlines(client, base)] == [1, 2, 4]
    # 新增一条细纲后再建章：插在已建章的最后一章之后
    plans = built["plans"]
    resp = await client.put(
        f"{base}/outlines/{o2['id']}/plans",
        json={"plans": [{"id": plans[0]["id"], "title": "甲"}, {"id": plans[1]["id"], "title": "乙"}, {"title": "丙"}]},
    )
    built = (await client.post(f"{base}/outlines/{o2['id']}/plans/build")).json()
    assert [c["chapter_no"] for c in built["chapters"]] == [4]
    assert [o["chapter_no"] for o in await _outlines(client, base)] == [1, 2, 5]
    # 删光细纲（章节都为空）：章节随之删除，大纲保留自己的 1 个章节号
    resp = await client.put(f"{base}/outlines/{o2['id']}/plans", json={"plans": []})
    assert resp.status_code == 200 and resp.json() == []
    assert await _chapters(client, base) == []
    assert [o["chapter_no"] for o in await _outlines(client, base)] == [1, 2, 3]
    # 没有细纲的大纲建章 → 400
    resp = await client.post(f"{base}/outlines/{outlines[0]['id']}/plans/build")
    assert resp.status_code == 400 and resp.json()["detail"] == "该大纲还没有细纲，请先展开"


async def test_sync_plans_orphans_removed_chapter_and_recomputes_setting(app, client):
    """重新规划删除空章时，同一事务回退设定；后来创建同号章节不恢复孤儿变化。"""
    project, base = await _setup(client, 2)
    outline = (await _outlines(client, base))[0]
    response = await client.put(
        f"{base}/outlines/{outline['id']}/plans", json={"plans": [{"title": "第一幕"}, {"title": "第二幕"}]}
    )
    assert response.status_code == 200, response.text
    plans = response.json()
    response = await client.post(f"{base}/outlines/{outline['id']}/plans/build")
    assert response.status_code == 200, response.text
    first_chapter, second_chapter = response.json()["chapters"]
    assert [first_chapter["chapter_no"], second_chapter["chapter_no"]] == [1, 2]

    async with app.state.sessionmaker() as db:
        world = WorldSetting(project_id=project["id"], title="城池", content="城池被毁")
        db.add(world)
        await db.flush()
        timeline = SettingTimeline(
            project_id=project["id"], subject_kind="world", world_setting_id=world.id,
            baseline={"content": "城池完好"},
        )
        db.add(timeline)
        await db.flush()
        event = SettingProgression(
            timeline_id=timeline.id, field_key="content", operation="replace", value="城池被毁",
            anchor_kind="chapter", chapter_id=second_chapter["id"], anchor_label="第2章 第二幕",
        )
        db.add(event)
        await db.commit()
        world_id, event_id = world.id, event.id

    response = await client.put(
        f"{base}/outlines/{outline['id']}/plans", json={"plans": [{"id": plans[0]["id"], "title": "第一幕"}]}
    )
    assert response.status_code == 200, response.text
    async with app.state.sessionmaker() as db:
        event = await db.get(SettingProgression, event_id)
        world = await db.get(WorldSetting, world_id)
        assert event is not None and event.anchor_kind == "chapter" and event.chapter_id is None
        assert event.anchor_label == "第2章 第二幕"
        assert world is not None and world.content == "城池完好"

    response = await client.put(
        f"{base}/outlines/{outline['id']}/plans",
        json={"plans": [{"id": plans[0]["id"], "title": "第一幕"}, {"title": "新第二幕"}]},
    )
    assert response.status_code == 200, response.text
    response = await client.post(f"{base}/outlines/{outline['id']}/plans/build")
    assert response.status_code == 200, response.text
    assert response.json()["chapters"][0]["chapter_no"] == 2
    async with app.state.sessionmaker() as db:
        event = await db.get(SettingProgression, event_id)
        world = await db.get(WorldSetting, world_id)
        assert event is not None and event.chapter_id is None
        assert world is not None and world.content == "城池完好"


async def test_sync_plans_rolls_back_chapter_deletion_on_projection_conflict(app, client, monkeypatch):
    """最终重算不允许通过时，细纲、章节和设定锚点不能部分提交。"""
    project, base = await _setup(client, 2)
    outline = (await _outlines(client, base))[0]
    response = await client.put(
        f"{base}/outlines/{outline['id']}/plans", json={"plans": [{"title": "第一幕"}, {"title": "第二幕"}]}
    )
    assert response.status_code == 200, response.text
    plans = response.json()
    response = await client.post(f"{base}/outlines/{outline['id']}/plans/build")
    assert response.status_code == 200, response.text
    removed_chapter = response.json()["chapters"][1]

    async with app.state.sessionmaker() as db:
        world = WorldSetting(project_id=project["id"], title="城池", content="城池被毁")
        db.add(world)
        await db.flush()
        timeline = SettingTimeline(
            project_id=project["id"], subject_kind="world", world_setting_id=world.id,
            baseline={"content": "城池完好"},
        )
        db.add(timeline)
        await db.flush()
        event = SettingProgression(
            timeline_id=timeline.id, field_key="content", operation="replace", value="城池被毁",
            anchor_kind="chapter", chapter_id=removed_chapter["id"], anchor_label="第2章 第二幕",
        )
        db.add(event)
        await db.commit()
        world_id, event_id = world.id, event.id

    async def conflict_on_recompute(_db, _project_id):
        raise ConflictError("调整后职业阶段发生冲突")

    monkeypatch.setattr(setting_progression_service, "on_story_positions_changed", conflict_on_recompute)
    response = await client.put(
        f"{base}/outlines/{outline['id']}/plans", json={"plans": [{"id": plans[0]["id"], "title": "第一幕"}]}
    )
    assert response.status_code == 409
    remaining = (await client.get(f"{base}/outlines/{outline['id']}/plans")).json()
    assert [item["id"] for item in remaining] == [item["id"] for item in plans]
    assert any(item["id"] == removed_chapter["id"] for item in await _chapters(client, base))
    async with app.state.sessionmaker() as db:
        event = await db.get(SettingProgression, event_id)
        world = await db.get(WorldSetting, world_id)
        assert event is not None and event.chapter_id == removed_chapter["id"]
        assert world is not None and world.content == "城池被毁"


# ---------------- AC3 重新展开 ----------------


async def test_reexpand_keeps_written_chapter(app, client, fake_llm):
    p, base = await _setup(client, 4)
    await client.post(f"{base}/chapters/from-outlines", json={})
    outlines = await _outlines(client, base)
    o3 = outlines[2]
    fs = (await client.post(f"{base}/foreshadows", json={"title": "末", "planned_resolve_chapter": 6})).json()
    result = await _expand(client, base, o3["id"], 3, _expand_json([_plan(1), _plan(2), _plan(3)]), fake_llm)
    await client.put(f"{base}/outlines/{o3['id']}/plans", json={"plans": [_draft_to_plan(d) for d in result["drafts"]]})
    built = (await client.post(f"{base}/outlines/{o3['id']}/plans/build")).json()
    first_ch = built["chapters"][0]
    # 原第 4 章 → 6；伏笔计划回收章 6 → 8
    assert (await client.get(f"{base}/foreshadows/{fs['id']}")).json()["planned_resolve_chapter"] == 8
    # 第 1 章写入正文（产生版本）
    resp = await client.patch(f"{base}/chapters/{first_ch['id']}", json={"content": "已写的正文"})
    assert resp.status_code == 200
    versions_before = (await client.get(f"{base}/chapters/{first_ch['id']}/versions")).json()

    result = await _expand(client, base, o3["id"], 4, _expand_json([_plan(4), _plan(5), _plan(6)]), fake_llm)
    existing = result["existing"]
    assert [(e["seq"], e["has_content"]) for e in existing] == [(1, True), (2, False), (3, False)]
    assert "细1" in fake_llm.calls[-1]["messages"][-1]["content"]  # 保留细纲进入提示词
    assert "共展开为 4 章；本次输出其中的 3 章" in fake_llm.calls[-1]["messages"][-1]["content"]
    kept = existing[0]

    # 缺少有正文的细纲 → 409，整体不生效
    resp = await client.put(
        f"{base}/outlines/{o3['id']}/plans", json={"plans": [_draft_to_plan(d) for d in result["drafts"]]}
    )
    assert resp.status_code == 409 and resp.json()["detail"] == "已有正文的细纲不能删除"
    assert len((await client.get(f"{base}/outlines/{o3['id']}/plans")).json()) == 3

    body = [{"id": kept["id"], "title": "细1（改）", "summary": "保留"}] + [_draft_to_plan(d) for d in result["drafts"]]
    resp = await client.put(f"{base}/outlines/{o3['id']}/plans", json={"plans": body})
    assert resp.status_code == 200, resp.text
    plans = resp.json()
    assert [(pl["seq"], pl["title"], pl["chapter_id"]) for pl in plans] == [
        (1, "细1（改）", first_ch["id"]),
        (2, "细4", None),
        (3, "细5", None),
        (4, "细6", None),
    ]
    # 空章节随细纲删除，其后章节号 -2；伏笔 8 → 6
    nos = [(c["chapter_no"], c["title"]) for c in await _chapters(client, base)]
    assert nos == [(1, "纲1"), (2, "纲2"), (3, "纲3"), (4, "纲4")]
    assert (await client.get(f"{base}/foreshadows/{fs['id']}")).json()["planned_resolve_chapter"] == 6
    # 已写章节的正文、版本不受影响
    ch = (await client.get(f"{base}/chapters/{first_ch['id']}")).json()
    assert ch["content"] == "已写的正文" and ch["plan_id"] == kept["id"]
    assert (await client.get(f"{base}/chapters/{first_ch['id']}/versions")).json() == versions_before
    # 重新展开前存了快照
    snaps = (await client.get(f"{base}/outline-snapshots")).json()
    assert snaps[0]["reason"] == "reexpand"

    built = (await client.post(f"{base}/outlines/{o3['id']}/plans/build")).json()
    assert [c["chapter_no"] for c in built["chapters"]] == [4, 5, 6]
    assert [c["chapter_no"] for c in await _chapters(client, base)] == [1, 2, 3, 4, 5, 6, 7]
    assert (await client.get(f"{base}/foreshadows/{fs['id']}")).json()["planned_resolve_chapter"] == 9


async def test_expand_count_must_exceed_kept(client, fake_llm):
    p, base = await _setup(client, 1)
    o = (await _outlines(client, base))[0]
    await client.put(f"{base}/outlines/{o['id']}/plans", json={"plans": [{"title": "甲"}, {"title": "乙"}]})
    built = (await client.post(f"{base}/outlines/{o['id']}/plans/build")).json()
    for c in built["chapters"]:
        await client.patch(f"{base}/chapters/{c['id']}", json={"content": "正文"})
    resp = await client.post(f"{base}/outlines/{o['id']}/expand", json={"count": 2})
    assert resp.status_code == 400 and resp.json()["detail"] == "展开章数需大于已有正文的细纲数（2 章）"
    resp = await client.post(f"{base}/outlines/999999/expand", json={"count": 2})
    assert resp.status_code == 404


async def test_expand_in_batches_dedupes_key_events(client, fake_llm):
    p, base = await _setup(client, 1)
    o = (await _outlines(client, base))[0]
    first = [_plan(i) for i in range(1, 6)]
    second = [_plan(6, key_events=["事件1a", "新事件"]), _plan(7)]
    fake_llm.push(_expand_json(first))
    fake_llm.push(_expand_json(second))
    _, events = await run_task(client, await client.post(f"{base}/outlines/{o['id']}/expand", json={"count": 7}))
    result = events[-1][1]["result"]
    assert [d["title"] for d in result["drafts"]] == [f"细{i}" for i in range(1, 8)]
    assert result["drafts"][5]["key_events"] == ["新事件"]
    assert any("事件1a" in w for w in result["warnings"])
    assert len(fake_llm.calls) == 2
    second_prompt = fake_llm.calls[1]["messages"][-1]["content"]
    assert "本次输出其中的 2 章" in second_prompt and "- 事件1a" in second_prompt and "- 事件5b" in second_prompt


async def test_sync_validation(client, make_client):
    p, base = await _setup(client, 2)
    o1, o2 = await _outlines(client, base)
    plans = (await client.put(f"{base}/outlines/{o1['id']}/plans", json={"plans": [{"title": "甲"}]})).json()
    resp = await client.put(f"{base}/outlines/{o2['id']}/plans", json={"plans": [{"id": plans[0]["id"], "title": "x"}]})
    assert resp.status_code == 400 and resp.json()["detail"] == "细纲不存在或不属于该大纲"
    resp = await client.put(
        f"{base}/outlines/{o2['id']}/plans", json={"plans": [{"title": "x", "character_ids": [999]}]}
    )
    assert resp.status_code == 400 and resp.json()["detail"] == "部分角色不存在或不属于该项目"
    resp = await client.put(f"{base}/outlines/{o2['id']}/plans", json={"plans": [{"title": "  "}]})
    assert resp.status_code == 422
    # 行内编辑
    resp = await client.patch(
        f"{base}/outlines/{o1['id']}/plans/{plans[0]['id']}", json={"goal": "新目标", "key_events": [" a ", "a", "b"]}
    )
    assert resp.status_code == 200 and resp.json()["goal"] == "新目标" and resp.json()["key_events"] == ["a", "b"]
    resp = await client.patch(f"{base}/outlines/{o2['id']}/plans/{plans[0]['id']}", json={"goal": "x"})
    assert resp.status_code == 404


# ---------------- AC4 细纲接入生成 ----------------


async def test_generation_uses_plan(client, fake_llm):
    p, base = await _setup(client, 1)
    lin = (await client.post(f"{base}/characters", json={"name": "林远"})).json()
    o = (await _outlines(client, base))[0]
    await client.put(
        f"{base}/outlines/{o['id']}/plans",
        json={
            "plans": [
                {
                    "title": "试炼",
                    "summary": "细纲的梗概",
                    "key_events": ["拔剑", "认输"],
                    "goal": "立住主角形象",
                    "emotion": "热血",
                    "est_words": 4200,
                    "character_ids": [lin["id"]],
                }
            ]
        },
    )
    built = (await client.post(f"{base}/outlines/{o['id']}/plans/build")).json()
    ch = built["chapters"][0]
    plan = (await client.get(f"{base}/chapters/{ch['id']}/plan")).json()
    assert plan["est_words"] == 4200 and plan["title"] == "试炼"
    fake_llm.push("正文" * 50)
    task, _ = await run_task(
        client, await client.post(f"/api/v1/chapters/{ch['id']}/generate", json={"target_words": 4200})
    )
    assert task["status"] == "succeeded"
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    task_part = prompt[prompt.index("【本章任务】") :]
    assert "本章梗概：细纲的梗概" in task_part and "梗概1" not in task_part
    assert "关键事件：\n1. 拔剑\n2. 认输" in task_part
    assert "叙事目标：立住主角形象" in task_part and "情感基调：热血" in task_part and "约 4200 字" in task_part
    assert "【本章涉及角色】\n■ 林远" in prompt

    # 没有细纲的章节：本章任务仍用大纲梗概
    plain = (await client.post(f"{base}/chapters", json={"title": "散章"})).json()
    assert (await client.get(f"{base}/chapters/{plain['id']}/plan")).json() is None


# ---------------- 排序 / 向导进度 ----------------


async def test_move_outline_only_changes_order(client):
    p, base = await _setup(client, 3)
    o1, o2, o3 = await _outlines(client, base)
    resp = await client.post(f"{base}/outlines/{o3['id']}/move", json={"direction": "up"})
    assert resp.status_code == 200
    assert [o["title"] for o in resp.json()] == ["纲1", "纲3", "纲2"]
    listed = await _outlines(client, base)
    assert [(o["title"], o["chapter_no"]) for o in listed] == [("纲1", 1), ("纲3", 3), ("纲2", 2)]
    # 已在最前：不变
    resp = await client.post(f"{base}/outlines/{o1['id']}/move", json={"direction": "up"})
    assert [o["title"] for o in resp.json()] == ["纲1", "纲3", "纲2"]
    resp = await client.post(f"{base}/outlines/{o1['id']}/move", json={"direction": "down"})
    assert [o["title"] for o in resp.json()] == ["纲3", "纲1", "纲2"]


async def test_wizard_progress(client):
    await register(client)
    p = await create_project(client)
    assert p["wizard_progress"] == {"done": [], "skipped": [], "current": None, "params": None}
    url = f"/api/v1/projects/{p['id']}/wizard-progress"
    resp = await client.patch(
        url, json={"done": ["world", "careers", "world"], "skipped": ["careers"], "current": "characters"}
    )
    assert resp.status_code == 200
    assert resp.json()["wizard_progress"] == {
        "done": ["world", "careers"],
        "skipped": ["careers"],
        "current": "characters",
        "params": None,
    }
    # 只改 current，其余保持；显式 null 清空
    resp = await client.patch(url, json={"current": None})
    assert resp.json()["wizard_progress"] == {
        "done": ["world", "careers"],
        "skipped": ["careers"],
        "current": None,
        "params": None,
    }
    got = (await client.get(f"/api/v1/projects/{p['id']}")).json()
    assert got["wizard_progress"]["done"] == ["world", "careers"]
    listed = (await client.get("/api/v1/projects")).json()
    assert listed[0]["wizard_progress"]["skipped"] == ["careers"]


@pytest.mark.parametrize("body", [{"items": []}, {"items": [{"title": ""}]}])
async def test_append_validation(client, body):
    p, base = await _setup(client, 0)
    assert (await client.post(f"{base}/outlines/append", json=body)).status_code == 422
