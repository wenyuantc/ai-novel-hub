"""故事日序、证据归属及检查期间改日期；同一套用例支持隔离 PostgreSQL。"""

import pytest
from sqlalchemy import select

from app.ai.templating import PromptOverride
from app.models import Task
from app.schemas.consistency import StoryTimeEvidence
from app.services.consistency import issues, story_time
from app.services.scene_service import SceneRange
from tests.consistency_helpers import API, add_ledger, comparison, fact, seed
from tests.helpers import Gate, create_ai_config, wait_task
from tests.schema_helpers import isolated_worker_database
from tests.test_consistency_check import _base, _issue, _run, _start

MELT = "铁匠把唯一的佩剑彻底熔毁了。"
CARRY = "他手持那把佩剑出门。"
MEMORY = "他记得当年手持佩剑出门。"


@pytest.fixture(params=["sqlite", "postgres"])
async def settings_overrides(request, monkeypatch):
    options = {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}
    if request.param == "sqlite":
        yield options
    else:
        async with isolated_worker_database(monkeypatch, required=True) as overrides:
            yield {**options, **overrides}


def test_imported_date_snapshot_with_omitted_optional_fields_is_current():
    context = story_time.DateContext(0, {}, {})
    evidence = {"chapter_id": None, "story_time": {"source": "unknown"}}
    assert not story_time.date_stale(context, evidence)


def test_date_requires_a_unique_quote_inside_one_scene():
    content = "😀甲甲。\n乙甲。"
    ctx = story_time.DateContext(0, {5: (content, [SceneRange(7, 0, 4, 0, 0), SceneRange(9, 5, 8, 4, 5)])}, {
        7: StoryTimeEvidence(scene_id=7, source="unmarked"), 9: StoryTimeEvidence(scene_id=9, source="unmarked"),
    })
    for quote in ("甲。", "。\n乙", "\n", "作者备注独有", ""):
        start = content.find(quote)
        assert ctx.locate(5, {"quote": quote, "start": start, "end": start + len(quote)}) == story_time.UNKNOWN
    assert ctx.locate(5, {"quote": "乙甲", "start": 5, "end": 7}).scene_id == 9
    assert ctx.locate(5, {"quote": "😀甲", "start": 0, "end": 2}).scene_id == 7


def test_long_quote_respects_batch_budget_and_existing_template():
    quote = "甲" * 500
    record = {**fact("1", quote, quote), "statement": "乙" * 500, "time_hint": "回忆" * 50}
    evidence = StoryTimeEvidence(
        source="scene", scene_id=1, year=1, month_id="mist", month_order=1, day=2, label="星历1年雾月2日",
    )
    ref = issues.FactRef(1, 3, "回忆", "a" * 64, True, record, evidence, 1)
    batch = issues.CompareBatch(0, ("item",), (ref,), char_budget=1000)
    line = issues.fact_line(ref, char_budget=batch.char_budget)
    assert len(line) + 1 <= batch.char_budget and line.endswith("…")
    assert "星历1年雾月2日" in line and "回忆" * 50 in line
    template = PromptOverride("作者自定义模板", "{{ facts }}\n{{ output_schema }}\n{{ json_rule }}")
    messages = issues.build_compare_messages(batch, template)
    assert messages[0]["content"] == "作者自定义模板" and line in messages[1]["content"]


async def _write(client, base, target, revision, day):
    response = await client.put(f"{base}/{target}/date", json={
        "expected_revision": revision, "date": {"year": 1, "month_id": "mist", "day": day},
    })
    assert response.status_code == 200, response.text
    return response.json()["revision"]


async def test_dated_sword_and_stale_model_response(app, client, fake_llm):
    _, project, chapters = await seed(client, {1: MELT, 3: CARRY + MEMORY})
    await create_ai_config(client)
    first, third = chapters[1], chapters[3]
    pid = project["id"]
    timeline = f"{API}/{pid}/story-timeline"
    scene = (await client.get(f"{API}/{pid}/chapters/{third['id']}/scenes")).json()["items"][0]["id"]
    calendar = await client.put(f"{timeline}/calendar", json={
        "expected_revision": 0, "era_name": "星历", "months": [{"id": "mist", "name": "雾月", "days": 20}],
    })
    assert calendar.status_code == 200, calendar.text
    rev = await _write(client, timeline, f"chapter/{first['id']}", calendar.json()["revision"], 10)
    rev = await _write(client, timeline, f"scene/{scene}", rev, 3)
    async with app.state.sessionmaker() as db:
        await add_ledger(db, pid, first, [fact("1", MELT, MELT)])
        recalled = {**fact("2", third["content"], MEMORY), "time_hint": "回忆当年"}
        await add_ledger(db, pid, third, [fact("1", third["content"], CARRY), recalled])
        await db.commit()

    fake_llm.push(comparison())
    assert (await _run(client, project))["status"] == "succeeded"
    prompt = fake_llm.calls[-1]["messages"][1]["content"]
    assert "星历1年雾月10日（沿用本章，故事日序 2）" in prompt
    assert "星历1年雾月3日（场景指定，故事日序 1）" in prompt
    assert f"原文引句：{MEMORY}" in prompt and "回忆当年" in prompt and "回忆或预告" in prompt

    rev = await _write(client, timeline, f"scene/{scene}", rev, 12)
    fake_llm.push(comparison(_issue(first, third)))
    assert (await _run(client, project))["status"] == "succeeded"
    assert len(fake_llm.calls) == 2  # 只改日期可复用全部抽取结果
    prompt = fake_llm.calls[-1]["messages"][1]["content"]
    assert "星历1年雾月10日（沿用本章，故事日序 1）" in prompt
    assert "星历1年雾月12日（场景指定，故事日序 2）" in prompt
    path = f"{_base(project)}/issues"
    issue, = (await client.get(path)).json()["items"]
    first_ev, third_ev = issue["evidence"]
    assert (first_ev["story_time"]["source"], first_ev["quote"]) == ("chapter", MELT)
    assert (third_ev["story_time"]["scene_id"], third_ev["story_time"]["day"], third_ev["quote"]) == (scene, 12, CARRY)

    gate = Gate()
    fake_llm.push([gate, comparison()])
    started = await _start(client, project)
    assert started.status_code == 202, started.text
    await gate.wait_reached(15)  # 第三个任务已读取 12 日的日期，独立作者事务先提交改为 10 日
    try:
        rev = await _write(client, timeline, f"scene/{scene}", rev, 10)
    finally:
        gate.release()
    task = await wait_task(client, started.json(), within=20)
    assert task["status"] == "succeeded", task
    async with app.state.sessionmaker() as db:
        stats = await db.scalar(select(Task.result).where(Task.id == task["id"]))
    assert (stats["created"], stats["updated"], stats["not_reproduced"]) == (0, 0, 0)
    assert any("请重新核查" in warning for warning in stats["warnings"])
    assert (await client.get(path, params={"needs_recheck": True})).json()["total"] == 1
    detail = (await client.get(f"{path}/{issue['id']}")).json()
    assert detail["needs_recheck"] and not detail["not_reproduced"]
    assert [entry["story_time_stale"] for entry in detail["evidence"]] == [False, True]
    assert not any(entry["stale"] for entry in detail["evidence"])
    assert detail["evidence"][1]["story_time"]["day"] == 12  # 原问题保留当时的日期
    fake_llm.push(comparison())
    assert (await _run(client, project))["status"] == "succeeded"
    assert fake_llm.calls[-1]["messages"][1]["content"].count("故事日序 1）") == 3

    # 同一检查期间作者改期又改回，过期的模型输出仍不能覆盖此前证据。
    gate = Gate()
    fake_llm.push([gate, comparison(_issue(first, third, description="过期返回"))])
    started = await _start(client, project)
    await gate.wait_reached(15)
    try:
        rev = await _write(client, timeline, f"scene/{scene}", rev, 8)
        await _write(client, timeline, f"scene/{scene}", rev, 10)
    finally:
        gate.release()
    task = await wait_task(client, started.json(), within=20)
    assert task["status"] == "succeeded", task
    async with app.state.sessionmaker() as db:
        stats = await db.scalar(select(Task.result).where(Task.id == task["id"]))
    assert stats["updated"] == 0 and any("请重新核查" in warning for warning in stats["warnings"])
    assert (await client.get(f"{path}/{issue['id']}")).json()["description"] == issue["description"]
