"""consistency.impact 任务（作者路径）与确认状态。

佩剑示例、只改错别字、台账已新鲜不重抽、无改动前台账 400、uncovered、确认继承、changed / outdated 派生、
评估不改问题清单、与检查互斥、输出校验、确认状态 CAS、管理员监控阶段。
"""

import json

import pytest
from sqlalchemy import select

from app.models import ConsistencyImpact, ConsistencyIssue, ConsistencyLedger, Task
from app.services.consistency import check, impact, impact_task
from app.services.mentions.matcher import text_hash
from tests.consistency_helpers import API, add_ledger, comparison, extraction, fact, seed
from tests.helpers import (
    Gate,
    create_ai_config,
    parse_sse,
    stream_with_gate,
    streamed_text,
    task_params,
    wait_task,
)

INTACT = "林远的佩剑完好地挂在腰间。"
BROKEN = "😀 林远的佩剑在山门前断成两截。"
USED = "林远拔出佩剑，一剑逼退来敌。"
OTHER = "苏婉在药庐里守了一夜。"
LATE = "多年以后，佩剑的碎片仍埋在松树下。"


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


def _sword(quote: str, statement: str) -> str:
    return extraction({"category": "item", "subject": "佩剑", "statement": statement, "quote": quote})


def _verdict(*changes: dict) -> str:
    return json.dumps({"changes": list(changes)}, ensure_ascii=False)


CHANGED = {"kind": "changed", "before_id": "A1", "after_id": "B1"}


def _base(project: dict) -> str:
    return f"{API}/{project['id']}/consistency"


def _assess(client, project, chapter, **body):
    return client.post(f"{_base(project)}/impacts/{chapter['id']}/assess", json=body or None)


async def _finish(app, client, created: dict) -> dict:
    """等待任务结束；TaskOut 不含 result 与输出全文，从任务行补上。"""
    task = await wait_task(client, created, within=15)
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task["id"])
        return {**task, "result": row.result, "output_text": row.output_text}


async def _run(app, client, project, chapter) -> dict:
    created = await _assess(client, project, chapter)
    assert created.status_code == 202, created.text
    return await _finish(app, client, created.json())


async def _detail(client, project, chapter) -> dict:
    resp = await client.get(f"{_base(project)}/impacts/{chapter['id']}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _edit(client, project, chapter, content: str) -> None:
    resp = await client.patch(f"{API}/{project['id']}/chapters/{chapter['id']}", json={"content": content})
    assert resp.status_code == 200, resp.text


async def _ledger(app, chapter) -> ConsistencyLedger | None:
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(ConsistencyLedger).where(ConsistencyLedger.chapter_id == chapter["id"]))


async def _world(app, client):
    """第 5 章佩剑完好（有台账）、第 12 章用剑（有台账）、第 13 章无关（有台账）、第 20 章提到佩剑（无台账）。"""
    _user, project, chapters = await seed(client, {5: INTACT, 12: USED, 13: OTHER, 20: LATE})
    await create_ai_config(client)
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[5],
                         [fact("1", INTACT, "佩剑完好地挂在腰间", statement="林远的佩剑完好")])
        await add_ledger(db, project["id"], chapters[12], [fact("1", USED, "拔出佩剑", statement="林远拔剑迎敌")])
        await add_ledger(db, project["id"], chapters[13],
                         [fact("1", OTHER, "守了一夜", category="character_state", subject="苏婉")])
        db.add(ConsistencyIssue(
            project_id=project["id"], category="item", title="旧问题", severity="medium", status="ignored",
            note="有意为之", revision=4, identity_key="a" * 64, evidence=[],
        ))
        await db.commit()
    return project, chapters


async def _issue_rows(app, project) -> list[tuple]:
    async with app.state.sessionmaker() as db:
        rows = await db.scalars(select(ConsistencyIssue).where(ConsistencyIssue.project_id == project["id"]))
        return [(r.id, r.status, r.note, r.revision, r.title, r.evidence, r.reappeared, r.last_seen_run_id,
                 r.last_checked_run_id, r.updated_at) for r in rows]


async def test_broken_sword_lists_chapter_12_with_evidence(app, client, fake_llm):
    """AC2 / AC3 / T9：改第 5 章为「佩剑已毁」后评估，第 12 章进入清单并带原文证据；确认可保持与继承。"""
    project, chapters = await _world(app, client)
    issues_before = await _issue_rows(app, project)

    # 没有改动：台账与正文一致且没有上一版；没有台账的章节提示先运行检查。均不建任务、不调用模型。
    unchanged = await _assess(client, project, chapters[5])
    assert unchanged.status_code == 400 and unchanged.json()["detail"] == impact.UNCHANGED
    missing = await _assess(client, project, chapters[20])
    assert missing.status_code == 400 and "先运行一次一致性检查" in missing.json()["detail"]
    assert (await _assess(client, project, {"id": 987654})).status_code == 404
    first = await _detail(client, project, chapters[5])
    assert (first["can_assess"], first["assess_block"], first["assessment"], first["revision"]) == (
        False, "unchanged", None, 0)
    assert (await _detail(client, project, chapters[20]))["assess_block"] == "no_baseline"
    async with app.state.sessionmaker() as db:
        assert (await db.scalars(select(Task))).all() == []

    await _edit(client, project, chapters[5], BROKEN)
    ready = await _detail(client, project, chapters[5])
    assert (ready["can_assess"], ready["ledger_state"]) == (True, "stale")
    old_ledger = await _ledger(app, chapters[5])
    raw = _sword("佩剑在山门前断成两截", "林远的佩剑断成两截")
    fake_llm.push(raw)
    fake_llm.push(_verdict(CHANGED))
    task = await _run(app, client, project, chapters[5])
    assert task["status"] == "succeeded", task
    assert task["kind"] == "consistency.impact" and task["title"] == "修改影响评估 · 第5章"
    assert task["result"] == {"chapter_id": chapters[5]["id"], "extracted": True, "no_change": False, "changes": 1,
                              "items": 1, "uncovered": 1, "warnings": []}
    assert task["checkpoint"] is None and raw not in task["output_text"]

    # 两次出站：重抽只发正文；判定只发两边字面不同的事实陈述，不含正文与引句
    assert len(fake_llm.calls) == 2
    verdict_prompt = fake_llm.calls[1]["messages"][1]["content"]
    assert "[A1] 物品去向｜佩剑｜林远的佩剑完好｜" in verdict_prompt
    assert "[B1] 物品去向｜佩剑｜林远的佩剑断成两截｜" in verdict_prompt
    assert BROKEN not in verdict_prompt and "山门前" not in verdict_prompt

    # 台账更新，改动前的事实留在上一版
    new_ledger = await _ledger(app, chapters[5])
    assert new_ledger.facts[0]["statement"] == "林远的佩剑断成两截" and new_ledger.text_hash == text_hash(BROKEN)
    assert (new_ledger.prev_facts, new_ledger.prev_source_hash, new_ledger.prev_text_hash) == (
        old_ledger.facts, old_ledger.source_hash, old_ledger.text_hash)

    detail = await _detail(client, project, chapters[5])
    result = detail["assessment"]
    assert (result["assessed"], result["outdated"], result["no_change"], result["model"]) == (
        True, False, False, "gpt-test")
    assert result["changes"] == [{"kind": "changed", "category": "item", "subject": "佩剑", "subject_key": "佩剑",
                                  "before": "林远的佩剑完好", "after": "林远的佩剑断成两截"}]
    (item,) = result["items"]
    assert (item["chapter_id"], item["chapter_no"], item["chapter_title"], item["status"], item["changed"]) == (
        chapters[12]["id"], 12, "章12", "pending", False)
    assert item["subjects"] == ["佩剑"] and item["fact_count"] == 1
    assert item["evidence"] == [{"fact_id": "1", "quote": "拔出佩剑", "start": 2, "end": 6,
                                 "text_hash": text_hash(USED)}]
    # 第 20 章没有台账：无法判断，明确列出；第 13 章台账新鲜但与佩剑无关，不列出
    assert result["uncovered"] == [{"chapter_id": chapters[20]["id"], "chapter_no": 20, "chapter_title": "章20"}]
    assert (detail["revision"], detail["ledger_state"], detail["can_assess"]) == (1, "fresh", True)

    # 评估不改问题清单
    assert await _issue_rows(app, project) == issues_before

    # 确认：已复核；修订递增；旧修订冲突
    saved = await client.patch(f"{_base(project)}/impacts/{chapters[5]['id']}", json={
        "revision": 1, "items": [{"chapter_id": chapters[12]["id"], "status": "reviewed"}]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 2 and saved.json()["assessment"]["items"][0]["status"] == "reviewed"
    conflict = await client.patch(f"{_base(project)}/impacts/{chapters[5]['id']}", json={
        "revision": 1, "items": [{"chapter_id": chapters[12]["id"], "status": "dismissed"}]})
    assert conflict.status_code == 409 and conflict.json()["code"] == "stale_revision"
    unknown = await client.patch(f"{_base(project)}/impacts/{chapters[5]['id']}", json={
        "revision": 2, "items": [{"chapter_id": chapters[13]["id"], "status": "reviewed"}]})
    assert unknown.status_code == 400 and unknown.json()["detail"] == impact.ITEM_NOT_FOUND
    for body in ({"revision": 2}, {"revision": 2, "items": None}, {"revision": 2, "items": [], "extra": 1},
                 {"revision": 2, "items": [{"chapter_id": chapters[12]["id"], "status": "open"}]}):
        assert (await client.patch(f"{_base(project)}/impacts/{chapters[5]['id']}", json=body)).status_code == 422

    # 重新评估：台账已新鲜 → 不调用抽取，只做一次判定；未变化的受影响项继承确认状态
    fake_llm.push(_verdict(CHANGED))
    again = await _run(app, client, project, chapters[5])
    assert again["status"] == "succeeded" and again["result"]["extracted"] is False
    assert len(fake_llm.calls) == 3 and "[A1]" in fake_llm.calls[2]["messages"][1]["content"]
    detail = await _detail(client, project, chapters[5])
    assert detail["assessment"]["items"][0]["status"] == "reviewed" and detail["revision"] == 3
    overview = (await client.get(f"{_base(project)}/impacts")).json()
    (row,) = overview["chapters"]
    assert (row["chapter_id"], row["assessed"], row["no_change"], row["changes"], row["items"], row["pending_items"],
            row["changed_items"], row["uncovered"], row["outdated"], row["stale_count"]) == (
        chapters[5]["id"], True, False, 1, 1, 0, 0, 1, False, 0)
    assert overview["running_task_id"] is None
    # 概览给出最近一次评估的任务 id：任务面板的跳转据此定位到章节
    assert row["task_id"] == again["id"]
    assert (await client.get(f"{_base(project)}/impacts/running")).json() == {
        "running_task_id": None, "running_task_key": None, "running_chapter_id": None}

    # 被列章节之后又改：该项提示已变更（确认状态保留）
    await _edit(client, project, chapters[12], USED + "剑身完好。")
    item = (await _detail(client, project, chapters[5]))["assessment"]["items"][0]
    assert (item["changed"], item["status"]) == (True, "reviewed")
    assert (await client.get(f"{_base(project)}/impacts")).json()["chapters"][0]["changed_items"] == 1
    # 评估后本章再改：提示重新评估
    await _edit(client, project, chapters[5], BROKEN + "断剑被埋在松树下。")
    detail = await _detail(client, project, chapters[5])
    assert detail["assessment"]["outdated"] is True and detail["ledger_state"] == "stale"
    # 再评估：第 12 章台账已过期 → 不再是受影响项，进入无法判断的列表；确认状态不再沿用
    fake_llm.push(_sword("佩剑在山门前断成两截", "林远的佩剑断成两截"))
    third = await _run(app, client, project, chapters[5])
    assert third["status"] == "succeeded" and third["result"]["extracted"] is True
    # 新旧事实字面相同：不再调用判定模型，结论为未发现变化（相对上一版），不列出后续章节
    assert third["result"] == {"chapter_id": chapters[5]["id"], "extracted": True, "no_change": True, "changes": 0,
                               "items": 0, "uncovered": 0, "warnings": []}
    assert len(fake_llm.calls) == 4
    assert await _issue_rows(app, project) == issues_before


@pytest.mark.parametrize("case", ["same_statement", "reworded"])
async def test_typo_fix_reports_no_story_change(app, client, fake_llm, case):
    """AC2：只改错别字 → 未发现剧情事实变化，不列出后续章节。措辞相同零额外调用；措辞不同由模型判定。"""
    project, chapters = await _world(app, client)
    await _edit(client, project, chapters[5], INTACT.replace("挂在", "悬在"))
    if case == "same_statement":
        # 全半角、空白与大小写差异不算字面不同
        fake_llm.push(_sword("佩剑完好地悬在腰间", " 林远的佩剑 完好"))
    else:
        fake_llm.push(_sword("佩剑完好地悬在腰间", "林远腰间的佩剑完好无损"))
        fake_llm.push(_verdict())
    task = await _run(app, client, project, chapters[5])
    assert task["status"] == "succeeded", task
    assert task["result"] == {"chapter_id": chapters[5]["id"], "extracted": True, "no_change": True, "changes": 0,
                              "items": 0, "uncovered": 0, "warnings": []}
    assert len(fake_llm.calls) == (1 if case == "same_statement" else 2)
    assert "未发现剧情事实变化" in task["output_text"]
    result = (await _detail(client, project, chapters[5]))["assessment"]
    assert (result["assessed"], result["no_change"], result["changes"], result["items"], result["uncovered"]) == (
        True, True, [], [], [])


async def test_invalid_verdict_references_are_dropped(app, client, fake_llm):
    project, chapters = await _world(app, client)
    await _edit(client, project, chapters[5], BROKEN)
    fake_llm.push(_sword("佩剑在山门前断成两截", "林远的佩剑断成两截"))
    fake_llm.push(_verdict(
        {"kind": "changed", "before_id": "A9", "after_id": "B1"},  # 编造的编号
        {"kind": "added", "before_id": "A1", "after_id": None},    # added 必须引用改动后的事实
        {"kind": "weird", "before_id": "A1", "after_id": "B1"},
        "乱写",
        {"kind": "removed", "before_id": "a1", "after_id": None},
        {"kind": "added", "before_id": None, "after_id": "B1"},
        {"kind": "changed", "before_id": "A1", "after_id": "B1"},   # 编号已用过
    ))
    task = await _run(app, client, project, chapters[5])
    assert task["status"] == "succeeded", task
    assert task["result"]["changes"] == 2 and task["result"]["warnings"] == ["模型给出的 5 处变化引用无效，已忽略"]
    result = (await _detail(client, project, chapters[5]))["assessment"]
    assert [(c["kind"], c["before"], c["after"]) for c in result["changes"]] == [
        ("removed", "林远的佩剑完好", ""), ("added", "", "林远的佩剑断成两截")]
    assert [item["chapter_id"] for item in result["items"]] == [chapters[12]["id"]]


@pytest.mark.parametrize("output", ["不是 JSON", '{"verdict": "none"}', ""])
async def test_malformed_verdict_fails_and_retry_skips_extraction(app, client, fake_llm, output):
    """判定输出畸形不能当成「没有变化」：任务失败、不写影响记录；重抽的台账保留，重试只做判定。"""
    project, chapters = await _world(app, client)
    await _edit(client, project, chapters[5], BROKEN)
    fake_llm.push(_sword("佩剑在山门前断成两截", "林远的佩剑断成两截"))
    fake_llm.push(output)
    task = await _run(app, client, project, chapters[5])
    assert task["status"] == "failed", task
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(ConsistencyImpact.id)) is None
    assert (await _ledger(app, chapters[5])).prev_facts is not None
    fake_llm.push(_verdict(CHANGED))
    retried = await client.post(f"/api/v1/tasks/{task['id']}/retry", params=task_params(task))
    assert retried.status_code in (200, 202), retried.text
    done = await _finish(app, client, retried.json())
    assert done["status"] == "succeeded" and done["result"]["extracted"] is False
    assert done["result"]["items"] == 1 and len(fake_llm.calls) == 3


async def test_chapter_changed_during_extraction_saves_nothing(app, client, fake_llm):
    project, chapters = await _world(app, client)
    await _edit(client, project, chapters[5], BROKEN)
    before = await _ledger(app, chapters[5])
    gate = Gate()
    fake_llm.push([gate, _sword("佩剑在山门前断成两截", "林远的佩剑断成两截")])
    created = await _assess(client, project, chapters[5])
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    await _edit(client, project, chapters[5], BROKEN + "又改了一句。")
    gate.release()
    task = await _finish(app, client, created.json())
    assert task["status"] == "failed" and task["error"] == impact_task.CHANGED_DURING
    after = await _ledger(app, chapters[5])
    assert (after.facts, after.source_hash, after.prev_facts) == (before.facts, before.source_hash, None)
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(ConsistencyImpact.id)) is None
    assert len(fake_llm.calls) == 1


async def test_chapter_changed_during_verdict_is_saved_as_outdated(app, client, fake_llm):
    """台账落库之后（判定期间）本章又改：结果按评估时的指纹保存，读取时标记「评估后正文又有修改」，不冒充当前正文的结论。"""
    project, chapters = await _world(app, client)
    await _edit(client, project, chapters[5], BROKEN)
    fake_llm.push(_sword("佩剑在山门前断成两截", "林远的佩剑断成两截"))
    gate = Gate()
    fake_llm.push([gate, _verdict(CHANGED)])
    created = await _assess(client, project, chapters[5])
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    await _edit(client, project, chapters[5], BROKEN + "又改了一句。")
    gate.release()
    task = await _finish(app, client, created.json())
    assert task["status"] == "succeeded", task
    detail = await _detail(client, project, chapters[5])
    assert (detail["assessment"]["outdated"], detail["ledger_state"]) == (True, "stale")
    async with app.state.sessionmaker() as db:
        saved = await db.scalar(select(ConsistencyImpact))
        assert saved.text_hash == text_hash(BROKEN)


async def test_chapter_lifecycle_renumber_delete_and_content_restore(app, client, fake_llm):
    """改章号不影响结果（不存章号，读取时取当前值）；被列章节删除后提示已变更；本章正文写回旧内容（版本恢复同理）
    后提示重新评估；删除本章时台账与影响记录随之级联删除。"""
    project, chapters = await _world(app, client)
    await _edit(client, project, chapters[5], BROKEN)
    fake_llm.push(_sword("佩剑在山门前断成两截", "林远的佩剑断成两截"))
    fake_llm.push(_verdict(CHANGED))
    assert (await _run(app, client, project, chapters[5]))["status"] == "succeeded"
    chapter_url = f"{API}/{project['id']}/chapters"

    moved = await client.patch(f"{chapter_url}/{chapters[12]['id']}", json={"chapter_no": 30})
    assert moved.status_code == 200, moved.text
    result = (await _detail(client, project, chapters[5]))["assessment"]
    assert [(item["chapter_no"], item["changed"]) for item in result["items"]] == [(30, False)]
    assert result["outdated"] is False

    for number in (12, 20):
        assert (await client.delete(f"{chapter_url}/{chapters[number]['id']}")).status_code in (200, 204)
    result = (await _detail(client, project, chapters[5]))["assessment"]
    assert [(item["chapter_id"], item["chapter_no"], item["changed"]) for item in result["items"]] == [
        (chapters[12]["id"], None, True)]
    assert result["uncovered"] == [{"chapter_id": chapters[20]["id"], "chapter_no": None, "chapter_title": None}]
    row = (await client.get(f"{_base(project)}/impacts")).json()["chapters"][0]
    assert (row["items"], row["changed_items"], row["uncovered"]) == (1, 1, 1)

    await _edit(client, project, chapters[5], INTACT)
    detail = await _detail(client, project, chapters[5])
    assert (detail["assessment"]["outdated"], detail["ledger_state"], detail["can_assess"]) == (True, "stale", True)

    assert (await client.delete(f"{chapter_url}/{chapters[5]['id']}")).status_code in (200, 204)
    assert (await client.get(f"{_base(project)}/impacts/{chapters[5]['id']}")).status_code == 404
    assert (await client.get(f"{_base(project)}/impacts")).json()["chapters"] == []
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(ConsistencyImpact.id)) is None
        assert await db.scalar(select(ConsistencyLedger.id).where(
            ConsistencyLedger.chapter_id == chapters[5]["id"])) is None


async def test_assessment_and_check_are_mutually_exclusive(app, client, fake_llm, system_admin):
    project, chapters = await _world(app, client)
    await _edit(client, project, chapters[5], BROKEN)
    gate = Gate()
    fake_llm.push([gate, _sword("佩剑在山门前断成两截", "林远的佩剑断成两截")])
    fake_llm.push(_verdict())
    created = await _assess(client, project, chapters[5])
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    # 评估进行中：不能再发起评估或检查；概要与清单给出进行中的任务
    second = await _assess(client, project, chapters[5])
    assert second.status_code == 409 and second.json()["detail"] == check.OPEN_IMPACT
    started = await client.post(f"{_base(project)}/checks", json={"mode": "all"})
    assert started.status_code == 409 and started.json()["detail"] == check.OPEN_IMPACT
    preview = await client.post(f"{_base(project)}/checks/preview", json={"mode": "all"})
    assert "已有进行中的修改影响评估" in preview.json()["warnings"]
    overview = (await client.get(f"{_base(project)}/impacts")).json()
    assert (overview["running_task_id"], overview["running_task_key"], overview["running_chapter_id"]) == (
        created.json()["id"], created.json()["task_key"], chapters[5]["id"])
    # 轮询用的轻量接口与概览口径一致
    assert (await client.get(f"{_base(project)}/impacts/running")).json() == {
        key: overview[key] for key in ("running_task_id", "running_task_key", "running_chapter_id")}
    # 检查页的概要只报告检查任务
    assert (await client.get(f"{_base(project)}/summary")).json()["running_task_id"] is None
    # 管理员监控：种类标签与安全阶段
    listed = await system_admin.get("/api/v1/admin/tasks", params={"kind": "consistency.impact"})
    assert listed.status_code == 200, listed.text
    monitored = listed.json()["items"][0]
    assert monitored["title"] == f"修改影响评估 #{created.json()['id']}" and monitored["stage"] == "抽取本章事实"
    gate.release()
    assert (await wait_task(client, created.json(), within=15))["status"] == "succeeded"

    # 反向：检查进行中不能发起评估
    await _edit(client, project, chapters[5], BROKEN + "又一句。")
    gate = Gate()
    fake_llm.push([gate, extraction()])
    for _ in range(3):
        fake_llm.push(extraction())
    fake_llm.push(comparison())
    running = await client.post(f"{_base(project)}/checks", json={"mode": "all"})
    assert running.status_code == 202, running.text
    await gate.wait_reached(10)
    blocked = await _assess(client, project, chapters[5])
    assert blocked.status_code == 409 and blocked.json()["detail"] == check.OPEN_CHECK
    gate.release()
    assert (await wait_task(client, running.json(), within=15))["status"] in ("succeeded", "failed")


async def test_stream_emits_progress_log_and_done_summary(app, client, fake_llm):
    project, chapters = await _world(app, client)
    await _edit(client, project, chapters[5], BROKEN)
    gate = Gate()
    raw = _sword("佩剑在山门前断成两截", "林远的佩剑断成两截")
    fake_llm.push([raw[:1], gate, raw[1:]])
    fake_llm.push(_verdict(CHANGED))
    created = await _assess(client, project, chapters[5])
    assert created.status_code == 202, created.text
    resp = await stream_with_gate(app, client, created.json(), gate)
    assert resp.status_code == 200, resp.text
    events = parse_sse(resp.text)
    name, final = events[-1]
    assert name == "done" and final["task"]["status"] == "succeeded" and final["result"]["items"] == 1
    assert streamed_text(events) == (
        "重新抽取 1 条事实\n发现 1 处剧情事实变化\n后续 1 章可能受影响，1 章无法判断\n")
    assert raw not in str(events) and "林远的佩剑断成两截" not in str(events)
    progress = [data for event, data in events if event == "progress" and data["status"] == "running"]
    values = [item["progress"] for item in progress]
    assert values == sorted(values) and max(values) <= 95
    stages = [item["stage"] for item in progress]
    assert "判定事实变化" in stages and "保存评估结果" in stages


def test_literal_diff_and_status_inheritance_are_pure():
    old = [{"category": "item", "subject_key": "佩剑", "statement": "ＡＢＣ 完好"},
           {"category": "item", "subject_key": "玉佩", "statement": "丢失"}]
    new = [{"category": "item", "subject_key": "佩剑", "statement": "abc完好"},
           {"category": "timeline", "subject_key": "", "statement": "三日后"}]
    old_only, new_only = impact_task.literal_diff(old, new)
    assert (old_only, new_only) == ([old[1]], [new[1]])
    assert impact_task.literal_diff(old, list(reversed(old))) == ([], [])
    items = [{"chapter_id": 1, "evidence": [{"text_hash": "x"}], "status": "pending"},
             {"chapter_id": 2, "evidence": [{"text_hash": "new"}], "status": "pending"},
             {"chapter_id": 3, "evidence": [{"text_hash": "z"}], "status": "pending"}]
    impact_task.inherit_statuses(items, [
        {"chapter_id": 1, "evidence": [{"text_hash": "x"}], "status": "dismissed"},
        {"chapter_id": 2, "evidence": [{"text_hash": "old"}], "status": "reviewed"},
        {"chapter_id": 9, "evidence": [{"text_hash": "z"}], "status": "reviewed"},
    ])
    assert [item["status"] for item in items] == ["dismissed", "pending", "pending"]
