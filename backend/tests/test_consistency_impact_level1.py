"""修改影响第一级：派生内容过期（零模型调用）、确认无需更新、批量判定、上一版台账的保留。"""

import pytest
from sqlalchemy import delete, event, select, update

from app.models import ConsistencyImpact, ConsistencyLedger, SettingProgression
from app.services.consistency import impact
from app.services.consistency import ledger as ledger_svc
from tests.consistency_helpers import API, extraction, seed, target
from tests.helpers import analysis_json, run_task
from tests.test_writeback import _analyze, _world


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


async def _overview(client, base) -> dict:
    resp = await client.get(f"{base}/consistency/impacts")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _detail(client, base, chapter) -> dict:
    resp = await client.get(f"{base}/consistency/impacts/{chapter['id']}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _edit(client, base, chapter, content: str) -> None:
    resp = await client.patch(f"{base}/chapters/{chapter['id']}", json={"content": content})
    assert resp.status_code == 200, resp.text


async def test_edited_chapter_shows_stale_derived_content_until_reanalysis(app, client, fake_llm):
    """AC1：改第 3 章后其摘要、自动记忆、分析写入的设定变化过期；第 5 章不受影响；重新分析后摘要与记忆提示消失。"""
    base, characters, _rel, _career, _stages, _org, chapters = await _world(client)
    third, fifth = chapters[3], chapters[5]
    await _analyze(client, fake_llm, third, character_changes=[{"name": "林远", "current_state": "重伤"}])
    await _analyze(client, fake_llm, fifth, character_changes=[{"name": "张三", "current_state": "下山"}])
    calls = len(fake_llm.calls)

    # 未改动：没有任何过期提示
    assert (await _overview(client, base))["chapters"] == []
    clean = await _detail(client, base, third)
    assert (clean["analyzed"], clean["stale_summary"], clean["stale_memories"], clean["stale_progressions"],
            clean["stale_count"], clean["ledger_state"], clean["assess_block"]) == (
        True, None, [], [], 0, "missing", "no_baseline")

    await _edit(client, base, third, third["content"] + "他的佩剑断成两截。")
    overview = await _overview(client, base)
    (row,) = overview["chapters"]
    assert (row["chapter_id"], row["chapter_no"], row["stale_summary"], row["stale_progressions"],
            row["assessed"], row["ledger_state"]) == (third["id"], 3, True, 1, False, "missing")
    assert row["stale_memories"] >= 1 and row["stale_count"] == 2 + row["stale_memories"]

    detail = await _detail(client, base, third)
    assert detail["stale_summary"]["analysis_id"] and detail["stale_count"] == row["stale_count"]
    memories = (await client.get(f"{base}/memories")).json()["items"]
    # 与记忆页的 source_stale 同一口径
    assert {item["id"] for item in detail["stale_memories"]} == {
        item["id"] for item in memories if item["source_stale"] is True}
    assert all(item["chapter_id"] == third["id"] for item in memories if item["source_stale"] is True)
    (progression,) = detail["stale_progressions"]
    assert (progression["subject_kind"], progression["character_id"], progression["subject_name"],
            progression["field_key"], progression["acknowledged"]) == (
        "character", characters["林远"]["id"], "林远", "current_state", False)
    # 其他章节的派生内容不受影响
    other = await _detail(client, base, fifth)
    assert (other["stale_summary"], other["stale_memories"], other["stale_progressions"], other["stale_count"]) == (
        None, [], [], 0)
    # 第一级不调用模型、不建记录
    assert len(fake_llm.calls) == calls
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(ConsistencyImpact.id)) is None

    # 作者确认该设定变化无需更新：不再计入；尚无记录时 revision 传 0
    bad = await client.patch(f"{base}/consistency/impacts/{fifth['id']}",
                             json={"revision": 0, "acknowledged": [progression["id"]]})
    assert bad.status_code == 400 and bad.json()["detail"] == impact.BAD_ACKNOWLEDGED
    saved = await client.patch(f"{base}/consistency/impacts/{third['id']}",
                               json={"revision": 0, "acknowledged": [progression["id"]]})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert (body["revision"], body["stale_progressions"][0]["acknowledged"], body["stale_count"]) == (
        1, True, row["stale_count"] - 1)
    assert body["assessment"]["assessed"] is False and body["assessment"]["items"] == []
    again = await client.patch(f"{base}/consistency/impacts/{third['id']}", json={"revision": 0, "acknowledged": []})
    assert again.status_code == 409 and again.json()["code"] == "stale_revision"
    row = (await _overview(client, base))["chapters"][0]
    assert (row["stale_progressions"], row["assessed"]) == (0, False)

    # 重新分析第 3 章：摘要与记忆的提示消失
    fake_llm.push(analysis_json(foreshadows=[]))
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{third['id']}/analyze"))
    assert task["status"] == "succeeded", task
    fresh = await _detail(client, base, third)
    assert (fresh["stale_summary"], fresh["stale_memories"]) == (None, [])
    assert fresh["stale_count"] == 0
    assert (await _overview(client, base))["chapters"] == []
    # 取消确认后，旧分析写入的设定变化若仍保留旧哈希则继续提示
    reverted = await client.patch(f"{base}/consistency/impacts/{third['id']}", json={"revision": 1, "acknowledged": []})
    assert reverted.status_code == 200 and reverted.json()["revision"] == 2
    assert reverted.json()["stale_count"] == len(reverted.json()["stale_progressions"])


async def test_acknowledged_patch_merges_instead_of_replacing(app, client, fake_llm):
    """PATCH 的 acknowledged 只替换「本章当前仍过期」的设定变化；此前确认过、现已不再过期的 id 保留，不存在的清理。"""
    base, _characters, _rel, _career, _stages, _org, chapters = await _world(client)
    third = chapters[3]
    await _analyze(client, fake_llm, third, character_changes=[{"name": "林远", "current_state": "重伤"}])
    url = f"{base}/consistency/impacts/{third['id']}"

    async def stored() -> list[int]:
        async with app.state.sessionmaker() as db:
            return list(await db.scalar(select(ConsistencyImpact.acknowledged)))

    await _edit(client, base, third, third["content"] + "改动。")
    (progression,) = (await _detail(client, base, third))["stale_progressions"]
    saved = await client.patch(url, json={"revision": 0, "acknowledged": [progression["id"]]})
    assert saved.status_code == 200 and await stored() == [progression["id"]]

    # 正文改回分析时的原文：该设定变化不再过期。此时提交空列表不会丢掉它的确认
    await _edit(client, base, third, third["content"])
    assert (await _detail(client, base, third))["stale_progressions"] == []
    kept = await client.patch(url, json={"revision": 1, "acknowledged": []})
    assert kept.status_code == 200 and kept.json()["revision"] == 2
    assert await stored() == [progression["id"]]

    # 再次改动：重新过期，仍显示为已确认、不计入过期数
    await _edit(client, base, third, third["content"] + "又改。")
    again = await _detail(client, base, third)
    assert [item["acknowledged"] for item in again["stale_progressions"]] == [True]
    assert again["stale_count"] == int(again["stale_summary"] is not None) + len(again["stale_memories"])
    # 当前仍过期的条目：提交空列表即取消确认
    cleared = await client.patch(url, json={"revision": 2, "acknowledged": []})
    assert cleared.status_code == 200 and await stored() == []
    assert cleared.json()["stale_progressions"][0]["acknowledged"] is False

    # 已确认的设定变化之后被删除：下一次保存时清理
    restored = await client.patch(url, json={"revision": 3, "acknowledged": [progression["id"]]})
    assert restored.status_code == 200 and await stored() == [progression["id"]]
    async with app.state.sessionmaker() as db:
        await db.execute(delete(SettingProgression).where(SettingProgression.id == progression["id"]))
        await db.commit()
    cleaned = await client.patch(url, json={"revision": 4, "acknowledged": []})
    assert cleaned.status_code == 200 and await stored() == []
    # 不属于本章分析结果的 id 仍被拒绝
    bad = await client.patch(url, json={"revision": 5, "acknowledged": [progression["id"]]})
    assert bad.status_code == 400 and bad.json()["detail"] == impact.BAD_ACKNOWLEDGED


async def test_progressions_without_source_hash_are_never_reported(app, client, fake_llm):
    base, _characters, _rel, _career, _stages, _org, chapters = await _world(client)
    third = chapters[3]
    await _analyze(client, fake_llm, third, character_changes=[{"name": "林远", "current_state": "重伤"}])
    async with app.state.sessionmaker() as db:
        # 旧版本写入的记录没有来源哈希；已撤销 / 被替换的记录另见下方
        await db.execute(update(SettingProgression).where(SettingProgression.source == "analysis")
                         .values(source_content_hash=None))
        await db.commit()
    await _edit(client, base, third, third["content"] + "改动。")
    detail = await _detail(client, base, third)
    assert detail["stale_progressions"] == [] and detail["stale_summary"] is not None
    row = (await _overview(client, base))["chapters"][0]
    assert (row["stale_progressions"], row["stale_summary"]) == (0, True)
    async with app.state.sessionmaker() as db:
        # 恢复哈希但标为不再生效：同样不提示
        await db.execute(update(SettingProgression).where(SettingProgression.source == "analysis")
                         .values(source_content_hash="0" * 64, active=False))
        await db.commit()
    assert (await _detail(client, base, third))["stale_progressions"] == []


async def test_overview_query_count_does_not_grow_with_chapters(app, client, fake_llm):
    """作品级汇总按批判定：章节数增加不增加查询次数。"""
    base, _characters, _rel, _career, _stages, _org, chapters = await _world(client)
    await _analyze(client, fake_llm, chapters[3], character_changes=[{"name": "林远", "current_state": "重伤"}])
    await _edit(client, base, chapters[3], chapters[3]["content"] + "改动。")
    statements: list[str] = []
    engine = app.state.engine.sync_engine

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert len((await _overview(client, base))["chapters"]) == 1
        few = len(statements)
        for number in range(10, 22):
            resp = await client.post(f"{base}/chapters", json={
                "title": f"章{number}", "chapter_no": number, "content": f"第{number}章正文。"})
            assert resp.status_code == 201, resp.text
        statements.clear()
        assert len((await _overview(client, base))["chapters"]) == 1
        assert len(statements) == few
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def test_save_ledger_keeps_previous_version_only_when_source_changes(app, client, fake_llm):
    user, project, chapters = await seed(client, {1: "林远的佩剑完好地挂在腰间。"})
    chapter = chapters[1]

    async def extract(statement: str, quote: str, **kwargs):
        fake_llm.push(extraction({"category": "item", "subject": "佩剑", "statement": statement, "quote": quote}))
        result = await ledger_svc.extract_chapter(
            app.state.sessionmaker, target(fake_llm), user_id=user["id"], project_id=project["id"],
            chapter_id=chapter["id"], **kwargs)
        assert result.state == "extracted", result
        async with app.state.sessionmaker() as db:
            return await db.scalar(select(ConsistencyLedger).where(ConsistencyLedger.chapter_id == chapter["id"]))

    first = await extract("佩剑完好", "佩剑完好地挂在腰间")
    assert (first.prev_facts, first.prev_source_hash, first.prev_text_hash) == (None, None, None)
    # 同一正文重复保存（强制重抽）：不移动，上一版不会被同版本的台账顶掉
    same = await extract("佩剑仍然完好", "佩剑完好地挂在腰间", force=True)
    assert same.facts[0]["statement"] == "佩剑仍然完好" and same.source_hash == first.source_hash
    assert (same.prev_facts, same.prev_source_hash, same.prev_text_hash) == (None, None, None)
    # 正文改动后重抽：旧的事实与指纹移入上一版
    resp = await client.patch(f"{API}/{project['id']}/chapters/{chapter['id']}",
                              json={"content": "林远的佩剑在山门前断成两截。"})
    assert resp.status_code == 200, resp.text
    second = await extract("佩剑断成两截", "佩剑在山门前断成两截")
    assert second.source_hash != same.source_hash
    assert (second.prev_facts, second.prev_source_hash, second.prev_text_hash) == (
        same.facts, same.source_hash, same.text_hash)
    # 再次改动：只保留最近一版
    resp = await client.patch(f"{API}/{project['id']}/chapters/{chapter['id']}",
                              json={"content": "林远的佩剑已重铸。"})
    assert resp.status_code == 200, resp.text
    third = await extract("佩剑已重铸", "佩剑已重铸")
    assert (third.prev_facts, third.prev_source_hash) == (second.facts, second.source_hash)
