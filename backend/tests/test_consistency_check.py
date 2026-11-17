"""consistency.check 任务（作者路径）。

预览、互斥、上限、全流程、复用、取消与重试续跑、单章失败、检查期间改章、比对批次失败、范围口径、管理员监控。
"""

import pytest
from sqlalchemy import select

from app.ai.providers import AIProviderError
from app.models import ConsistencyIssue, ConsistencyLedger, ConsistencyRun, Task
from app.services.consistency import check, ledger
from app.services.mentions.matcher import text_hash
from tests.consistency_helpers import API, add_ledger, comparison, extraction, fact, new_run, seed
from tests.helpers import (
    Gate,
    create_ai_config,
    parse_sse,
    stream_with_gate,
    streamed_text,
    task_params,
    wait_task,
)

BROKEN = "😀 林远的佩剑在山门前断成两截。"
USED = "林远拔出佩剑，一剑逼退来敌。"
OTHER = "苏婉在药庐里守了一夜。"


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False, "context_char_budget": 1000}


def _sword(quote: str, statement: str) -> str:
    return extraction({"category": "item", "subject": "佩剑", "statement": statement, "quote": quote})


def _issue(*chapters: dict, **fields) -> dict:
    return {"existing_issue_id": None, "category": "item", "title": "佩剑去向存在潜在冲突",
            "description": "前文佩剑已毁，后文仍在使用。", "suggestion": "补写重铸，或改用其他兵器。",
            "severity": "high", "foreshadow_id": None,
            "evidence": [{"chapter_id": chapter["id"], "fact_id": "1"} for chapter in chapters], **fields}


def _base(project: dict) -> str:
    return f"{API}/{project['id']}/consistency"


async def _start(client, project, **body):
    return await client.post(f"{_base(project)}/checks", json={"mode": "all", **body})


async def _run(client, project, **body) -> dict:
    created = await _start(client, project, **body)
    assert created.status_code == 202, created.text
    return await wait_task(client, created.json(), within=15)


async def _ledgers(app, project_id: int) -> dict[int, ConsistencyLedger]:
    async with app.state.sessionmaker() as db:
        rows = await db.scalars(select(ConsistencyLedger).where(ConsistencyLedger.project_id == project_id))
        return {row.chapter_id: row for row in rows}


async def _issues(app, project_id: int) -> list[ConsistencyIssue]:
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(ConsistencyIssue).where(ConsistencyIssue.project_id == project_id)
                                     .order_by(ConsistencyIssue.id)))


async def _runs(app, project_id: int) -> list[ConsistencyRun]:
    async with app.state.sessionmaker() as db:
        return list(await db.scalars(select(ConsistencyRun).where(ConsistencyRun.project_id == project_id)
                                     .order_by(ConsistencyRun.id)))


async def test_preview_counts_and_range_errors(app, client):
    _user, project, chapters = await seed(client, {1: BROKEN, 2: USED, 3: "", 4: OTHER})
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[1], [fact("1", BROKEN, "佩剑在山门前断成两截")])
        await add_ledger(db, project["id"], chapters[2], [fact("1", USED, "拔出佩剑")], fresh=False)
        await db.commit()
    resp = await client.post(f"{_base(project)}/checks/preview", json={"mode": "all"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"total": 4, "to_extract": 2, "reusable": 1, "empty": 1, "warnings": []}
    ranged = await client.post(f"{_base(project)}/checks/preview", json={
        "mode": "range", "start_chapter_id": chapters[1]["id"], "end_chapter_id": chapters[3]["id"],
    })
    assert ranged.json() == {"total": 3, "to_extract": 1, "reusable": 1, "empty": 1, "warnings": []}
    empty = await client.post(f"{_base(project)}/checks/preview", json={
        "mode": "range", "start_chapter_id": chapters[3]["id"], "end_chapter_id": chapters[3]["id"],
    })
    assert empty.json()["warnings"] == [check.NO_CHAPTERS]
    reversed_range = await client.post(f"{_base(project)}/checks/preview", json={
        "mode": "range", "start_chapter_id": chapters[4]["id"], "end_chapter_id": chapters[1]["id"],
    })
    assert reversed_range.status_code == 400 and "起始章节不能晚于结束章节" in reversed_range.text
    foreign = await client.post(f"{_base(project)}/checks/preview", json={
        "mode": "range", "start_chapter_id": 9999, "end_chapter_id": chapters[1]["id"],
    })
    assert foreign.status_code == 400
    # 范围字段与模式不匹配由 schema 拒绝
    assert (await client.post(f"{_base(project)}/checks/preview", json={"mode": "range"})).status_code == 422
    # 预览不建任务、不调用模型
    async with app.state.sessionmaker() as db:
        assert (await db.scalars(select(Task))).all() == []


async def test_full_check_reports_broken_sword(app, client, fake_llm):
    """AC1：经任务全流程产出一个物品去向类问题，两处证据带当前章号与服务端偏移；模型原文不进入任务输出。"""
    _user, project, chapters = await seed(client, {5: BROKEN, 12: USED})
    await create_ai_config(client)
    fake_llm.push(_sword("佩剑在山门前断成两截", "林远的佩剑断成两截"))
    fake_llm.push(_sword("拔出佩剑", "林远拔出佩剑迎敌"))
    fake_llm.push(comparison(_issue(chapters[12], chapters[5])))
    created = await _start(client, project)
    assert created.status_code == 202, created.text
    assert created.json()["kind"] == "consistency.check" and created.json()["title"] == "一致性检查 · 全书"
    task = await wait_task(client, created.json(), within=15)
    assert task["status"] == "succeeded", task
    assert task["progress"] == 100
    run = (await _runs(app, project["id"]))[0]
    result = (await client.get(f"/api/v1/tasks/{task['id']}", params=task_params(task))).json()
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task["id"])
    assert row.result == {
        "run_id": run.id, "total": 2, "extracted": 2, "reused": 0, "skipped": 0, "failed": 0, "created": 1,
        "updated": 0, "reappeared": 0, "not_reproduced": 0, "stale_outside": 0, "batches": 1, "failed_batches": 0,
        "skipped_chapters": [], "warnings": [], "phase": "done", "cursor": 2,
    }
    # 运行期间不改写 Task 断点；进度记录在运行记录里
    assert (row.step_cursor, row.checkpoint) == (0, None) and result["status"] == "succeeded"
    assert run.status == "succeeded" and run.task_id == task["id"] and run.finished_at is not None
    assert run.scope == {"mode": "all", "start_chapter_id": None, "end_chapter_id": None,
                         "chapter_ids": [chapters[5]["id"], chapters[12]["id"]]}
    # 任务输出只有进度日志，没有模型原文
    output = (await client.get(f"/api/v1/tasks/{task['id']}/output", params=task_params(task))).json()["text"]
    assert output == "第 1/2 章：抽取 1 条事实\n第 2/2 章：抽取 1 条事实\n比对 1/1 批：完成\n"
    # 抽取只发送正文，比对只发送台账事实
    assert BROKEN in str(fake_llm.calls[0]["messages"]) and USED not in str(fake_llm.calls[0]["messages"])
    compare_prompt = str(fake_llm.calls[2]["messages"])
    assert BROKEN not in compare_prompt and "林远的佩剑断成两截" in compare_prompt
    page = (await client.get(f"{_base(project)}/issues")).json()
    assert page["total"] == 1
    issue = page["items"][0]
    assert (issue["category"], issue["status"], issue["severity"]) == ("item", "open", "high")
    assert (issue["needs_recheck"], issue["not_reproduced"], issue["reappeared"]) == (False, False, False)
    start = BROKEN.index("佩剑在山门前断成两截")
    assert [(e["chapter_no"], e["chapter_title"], e["quote"], e["start"], e["end"], e["stale"])
            for e in issue["evidence"]] == [
        (5, "章5", "佩剑在山门前断成两截", start, start + 10, False),
        (12, "章12", "拔出佩剑", 2, 6, False),
    ]
    assert issue["evidence"][0]["text_hash"] == text_hash(BROKEN)
    summary = (await client.get(f"{_base(project)}/summary")).json()
    assert summary["counts_by_status"] == {"open": 1, "resolved": 0, "ignored": 0}
    assert summary["ledger_coverage"] == {"fresh": 2, "stale": 0, "missing": 0}
    assert (summary["running_task_id"], summary["running_task_key"]) == (None, None)
    assert summary["last_run"]["status"] == "succeeded" and summary["last_run"]["stats"]["created"] == 1


async def test_rerun_reuses_ledgers_and_only_changed_chapter_is_extracted(app, client, fake_llm):
    """AC2：未改动的章节不再调用模型；只修改一章后重跑，仅该章重新抽取。同一矛盾不产生重复问题。"""
    _user, project, chapters = await seed(client, {5: BROKEN, 12: USED})
    await create_ai_config(client)
    fake_llm.push(_sword("佩剑在山门前断成两截", "林远的佩剑断成两截"))
    fake_llm.push(_sword("拔出佩剑", "林远拔出佩剑迎敌"))
    fake_llm.push(comparison(_issue(chapters[5], chapters[12])))
    assert (await _run(client, project))["status"] == "succeeded"
    assert len(fake_llm.calls) == 3

    fake_llm.push(comparison(_issue(chapters[5], chapters[12])))
    second = await _run(client, project)
    assert second["status"] == "succeeded" and len(fake_llm.calls) == 4
    async with app.state.sessionmaker() as db:
        stats = (await db.get(Task, second["id"])).result
    assert (stats["extracted"], stats["reused"], stats["created"], stats["updated"]) == (0, 2, 0, 1)

    changed = USED + "剑身寒光如旧。"
    resp = await client.patch(f"{API}/{project['id']}/chapters/{chapters[12]['id']}", json={"content": changed})
    assert resp.status_code == 200, resp.text
    listed = (await client.get(f"{_base(project)}/issues")).json()["items"][0]
    assert listed["needs_recheck"] is True and [e["stale"] for e in listed["evidence"]] == [False, True]
    preview = (await client.post(f"{_base(project)}/checks/preview", json={"mode": "all"})).json()
    assert (preview["to_extract"], preview["reusable"]) == (1, 1)
    fake_llm.push(_sword("拔出佩剑", "林远拔出佩剑迎敌"))
    fake_llm.push(comparison(_issue(chapters[5], chapters[12])))
    third = await _run(client, project)
    assert third["status"] == "succeeded" and len(fake_llm.calls) == 6
    assert changed in str(fake_llm.calls[4]["messages"]) and BROKEN not in str(fake_llm.calls[4]["messages"])
    issues = await _issues(app, project["id"])
    assert len(issues) == 1
    refreshed = (await client.get(f"{_base(project)}/issues/{issues[0].id}")).json()
    assert refreshed["needs_recheck"] is False and refreshed["revision"] == 1


async def test_second_check_is_rejected_while_one_is_active(app, client, fake_llm):
    """C13：同一作品同一时间只允许一个进行中的检查；结束后可以再次发起。"""
    _user, project, chapters = await seed(client, {1: BROKEN, 2: USED})
    await create_ai_config(client)
    gate = Gate()
    fake_llm.push([gate, _sword("佩剑在山门前断成两截", "断剑")])
    fake_llm.push(_sword("拔出佩剑", "用剑"))
    fake_llm.push(comparison())
    created = await _start(client, project)
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    summary = (await client.get(f"{_base(project)}/summary")).json()
    assert summary["running_task_id"] == created.json()["id"]
    assert summary["running_task_key"] == created.json()["task_key"]
    assert summary["last_run"]["status"] == "running"
    again = await _start(client, project)
    assert again.status_code == 409 and again.json()["detail"] == check.OPEN_CHECK
    recheck_preview = (await client.post(f"{_base(project)}/checks/preview", json={"mode": "all"})).json()
    assert "已有进行中的一致性检查" in recheck_preview["warnings"]
    gate.release()
    assert (await wait_task(client, created.json(), within=15))["status"] == "succeeded"
    fake_llm.push(comparison())
    assert (await _run(client, project))["status"] == "succeeded"


async def test_extract_limit_rejects_oversized_scope(app, client, fake_llm, monkeypatch):
    _user, project, chapters = await seed(client, {1: BROKEN, 2: USED, 3: OTHER})
    await create_ai_config(client)
    monkeypatch.setattr(check, "MAX_EXTRACT", 2)
    resp = await _start(client, project)
    assert resp.status_code == 400 and "上限 2 章" in resp.json()["detail"]
    preview = (await client.post(f"{_base(project)}/checks/preview", json={"mode": "all"})).json()
    assert preview["to_extract"] == 3 and any("超过单次上限 2 章" in item for item in preview["warnings"])
    # 可复用台账的章节不计入上限
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[1], [fact("1", BROKEN, "佩剑在山门前断成两截")])
        await db.commit()
    fake_llm.push(_sword("拔出佩剑", "用剑"))
    fake_llm.push(extraction())
    fake_llm.push(comparison())
    assert (await _run(client, project))["status"] == "succeeded"
    async with app.state.sessionmaker() as db:
        assert (await db.scalars(select(Task))).all().__len__() == 1


async def test_start_requires_model_and_non_empty_scope(app, client, fake_llm):
    _user, project, chapters = await seed(client, {1: ""})
    empty = await _start(client, project)
    assert empty.status_code == 400 and empty.json()["detail"] == check.NO_CHAPTERS
    resp = await client.post(f"{API}/{project['id']}/chapters", json={"title": "章2", "chapter_no": 2, "content": USED})
    assert resp.status_code == 201
    no_model = await _start(client, project)
    assert no_model.status_code == 400
    async with app.state.sessionmaker() as db:
        assert (await db.scalars(select(Task))).all() == []
    assert fake_llm.calls == []


async def test_cancel_keeps_ledgers_and_retry_resumes_without_repeating(app, client, fake_llm):
    """AC2：取消后已完成章节的台账保留、运行记录为终态；重试沿用原范围，已抽取的章节不再调用模型。"""
    _user, project, chapters = await seed(client, {1: BROKEN, 2: USED, 3: OTHER})
    await create_ai_config(client)
    gate = Gate()
    fake_llm.push(_sword("佩剑在山门前断成两截", "断剑"))
    fake_llm.push([gate, _sword("拔出佩剑", "用剑")])
    created = await _start(client, project)
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    cancelled = await client.post(f"/api/v1/tasks/{created.json()['id']}/cancel", params=task_params(created.json()))
    assert cancelled.status_code == 200, cancelled.text
    task = await wait_task(client, created.json(), within=15)
    assert task["status"] == "cancelled"
    ledgers = await _ledgers(app, project["id"])
    assert set(ledgers) == {chapters[1]["id"]} and ledgers[chapters[1]["id"]].task_id == task["id"]
    run = (await _runs(app, project["id"]))[0]
    assert run.status == "cancelled" and run.finished_at is not None
    assert (run.stats["extracted"], run.stats["cursor"], run.stats["phase"]) == (1, 1, "extract")
    summary = (await client.get(f"{_base(project)}/summary")).json()
    assert summary["last_run"]["status"] == "cancelled" and summary["running_task_id"] is None
    assert summary["ledger_coverage"] == {"fresh": 1, "stale": 0, "missing": 2}
    assert await _issues(app, project["id"]) == []

    calls = len(fake_llm.calls)
    fake_llm.push(_sword("拔出佩剑", "用剑"))
    fake_llm.push(extraction())
    fake_llm.push(comparison(_issue(chapters[1], chapters[2])))
    retried = await client.post(f"/api/v1/tasks/{task['id']}/retry", params=task_params(task))
    assert retried.status_code == 202, retried.text
    assert retried.json()["retry_of_id"] == task["id"]
    done = await wait_task(client, retried.json(), within=15)
    assert done["status"] == "succeeded", done
    # 第 1 章已抽取：重试只调用第 2、3 章的抽取与一次比对
    new_calls = fake_llm.calls[calls:]
    assert len(new_calls) == 3 and all(BROKEN not in str(call["messages"]) for call in new_calls[:2])
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, done["id"])
    assert row.input["chapter_ids"] == [chapters[number]["id"] for number in (1, 2, 3)]
    assert (row.result["reused"], row.result["extracted"], row.result["created"]) == (1, 2, 1)
    runs = await _runs(app, project["id"])
    assert [item.status for item in runs] == ["cancelled", "succeeded"]


async def test_single_chapter_failure_does_not_stop_the_check(app, client, fake_llm):
    _user, project, chapters = await seed(client, {1: BROKEN, 2: USED, 3: OTHER})
    await create_ai_config(client)
    fake_llm.push(_sword("佩剑在山门前断成两截", "断剑"))
    fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    fake_llm.push("这不是 JSON")
    fake_llm.push(comparison())
    task = await _run(client, project)
    assert task["status"] == "succeeded", task
    async with app.state.sessionmaker() as db:
        result = (await db.get(Task, task["id"])).result
    assert (result["extracted"], result["failed"], result["batches"]) == (1, 2, 0)
    assert [(item["chapter_id"], item["state"]) for item in result["skipped_chapters"]] == [
        (chapters[2]["id"], "failed"), (chapters[3]["id"], "failed"),
    ]
    assert result["skipped_chapters"][0]["error"] == "模型调用失败：额度不足"
    assert result["skipped_chapters"][1]["error"].startswith("AI 输出解析失败")
    assert set(await _ledgers(app, project["id"])) == {chapters[1]["id"]}
    last_run = (await client.get(f"{_base(project)}/summary")).json()["last_run"]
    assert [(item["chapter_no"], item["chapter_title"], item["state"])
            for item in last_run["stats"]["skipped_chapters"]] == [(2, "章2", "failed"), (3, "章3", "failed")]


async def test_all_extractions_failing_fails_the_task_and_keeps_existing_data(app, client, fake_llm):
    _user, project, chapters = await seed(client, {1: BROKEN, 2: USED})
    await create_ai_config(client)
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[1], [fact("1", BROKEN, "佩剑在山门前断成两截")])
        await db.commit()
    fake_llm.push(AIProviderError("模型调用失败：网络错误"))
    task = await _run(client, project)
    assert task["status"] == "failed" and task["error"] == "事实抽取全部失败：模型调用失败：网络错误"
    assert len(fake_llm.calls) == 1
    run = (await _runs(app, project["id"]))[0]
    assert run.status == "failed" and run.stats["failed"] == 1 and run.stats["reused"] == 1
    assert set(await _ledgers(app, project["id"])) == {chapters[1]["id"]}
    # 失败后资源键已释放，可以重试
    fake_llm.push(_sword("拔出佩剑", "用剑"))
    fake_llm.push(comparison())
    retried = await client.post(f"/api/v1/tasks/{task['id']}/retry", params=task_params(task))
    assert retried.status_code == 202, retried.text
    assert (await wait_task(client, retried.json(), within=15))["status"] == "succeeded"


async def test_chapter_changed_during_check_is_not_saved(app, client, fake_llm):
    """C13 / AC10：检查期间正文被修改的章节不保存旧结果，并在概要中列出。"""
    _user, project, chapters = await seed(client, {1: BROKEN, 2: USED})
    await create_ai_config(client)
    gate = Gate()
    fake_llm.push([gate, _sword("佩剑在山门前断成两截", "断剑")])
    fake_llm.push(_sword("拔出佩剑", "用剑"))
    created = await _start(client, project)
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    resp = await client.patch(f"{API}/{project['id']}/chapters/{chapters[1]['id']}",
                              json={"content": BROKEN + "后来又重铸。"})
    assert resp.status_code == 200, resp.text
    gate.release()
    task = await wait_task(client, created.json(), within=15)
    assert task["status"] == "succeeded", task
    async with app.state.sessionmaker() as db:
        result = (await db.get(Task, task["id"])).result
    assert (result["extracted"], result["skipped"]) == (1, 1)
    assert result["skipped_chapters"] == [
        {"chapter_id": chapters[1]["id"], "state": "skipped_changed", "error": ledger.CHANGED},
    ]
    assert set(await _ledgers(app, project["id"])) == {chapters[2]["id"]}
    output = (await client.get(f"/api/v1/tasks/{task['id']}/output", params=task_params(task))).json()["text"]
    assert "第 1/2 章：正文在检查期间发生变化，本次结果未保存\n" in output


async def test_chapter_changed_after_its_extraction_is_listed_and_not_compared(app, client, fake_llm):
    """C13：某章抽取完成后、比对开始前正文又被修改——它的台账已过期，不参与比对，并在概要中列出。"""
    _user, project, chapters = await seed(client, {1: BROKEN, 2: USED})
    await create_ai_config(client)
    gate = Gate()
    fake_llm.push(_sword("佩剑在山门前断成两截", "断剑"))
    fake_llm.push([gate, _sword("拔出佩剑", "用剑")])
    created = await _start(client, project)
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    # 第 1 章的台账此时已提交；第 2 章仍在抽取中
    assert set(await _ledgers(app, project["id"])) == {chapters[1]["id"]}
    resp = await client.patch(f"{API}/{project['id']}/chapters/{chapters[1]['id']}",
                              json={"content": BROKEN + "后来又重铸。"})
    assert resp.status_code == 200, resp.text
    gate.release()
    task = await wait_task(client, created.json(), within=15)
    assert task["status"] == "succeeded", task
    # 只剩一章有新鲜台账：没有可比对的组，不再调用模型，也不会基于旧正文产出问题
    assert len(fake_llm.calls) == 2 and await _issues(app, project["id"]) == []
    async with app.state.sessionmaker() as db:
        result = (await db.get(Task, task["id"])).result
    assert (result["extracted"], result["skipped"], result["batches"]) == (1, 1, 0)
    assert result["skipped_chapters"] == [
        {"chapter_id": chapters[1]["id"], "state": "skipped_changed", "error": ledger.CHANGED},
    ]
    last_run = (await client.get(f"{_base(project)}/summary")).json()
    assert last_run["last_run"]["stats"]["skipped_chapters"][0]["chapter_no"] == 1
    assert last_run["ledger_coverage"] == {"fresh": 1, "stale": 1, "missing": 0}


async def test_outbound_never_carries_structured_settings_or_hidden_records(app, client, fake_llm):
    """C11 / AC8：抽取只发正文，比对只发台账与允许进入上下文的伏笔；任何档位的结构化设定都不出站。"""
    from app.models import Character, Foreshadow, WorldSetting

    planted = BROKEN + "老者留下预言：十年后剑将重铸。"
    _user, project, chapters = await seed(client, {1: planted, 2: USED})
    await create_ai_config(client)
    async with app.state.sessionmaker() as db:
        db.add_all([
            # 即使是「始终加入」的设定也不属于一致性检查的出站内容；「永不加入」与隐藏字段更不会
            Character(project_id=project["id"], name="林远", profile="档案机密甲", background="身世机密乙",
                      ai_mode="always", always_in_context=True),
            Character(project_id=project["id"], name="佩剑", profile="永不加入机密丙", ai_mode="never"),
            WorldSetting(project_id=project["id"], title="山门", content="世界观机密丁", ai_mode="always",
                         ai_hidden_builtin_fields=["content"]),
            Foreshadow(project_id=project["id"], title="可见伏笔", content="允许进入上下文", status="planted",
                       include_in_context=True),
            Foreshadow(project_id=project["id"], title="隐藏伏笔", content="伏笔机密戊", status="planted",
                       include_in_context=False),
        ])
        await db.commit()
    fake_llm.push(extraction(
        {"category": "item", "subject": "佩剑", "statement": "断剑", "quote": "佩剑在山门前断成两截"},
        {"category": "foreshadow", "subject": "预言", "statement": "预言重铸", "quote": "十年后剑将重铸"},
    ))
    fake_llm.push(_sword("拔出佩剑", "用剑"))
    fake_llm.push(comparison(_issue(chapters[1], chapters[2])))
    assert (await _run(client, project))["status"] == "succeeded"
    assert len(fake_llm.calls) == 3
    sent = [str(call["messages"]) for call in fake_llm.calls]
    for secret in ("档案机密甲", "身世机密乙", "永不加入机密丙", "世界观机密丁", "伏笔机密戊", "隐藏伏笔"):
        assert all(secret not in text for text in sent), secret
    # 正例：抽取带正文，比对带台账陈述与允许进入上下文的伏笔记录，但不带正文原文
    assert planted in sent[0] and USED in sent[1]
    assert "断剑" in sent[2] and "可见伏笔" in sent[2] and "允许进入上下文" in sent[2]
    assert planted not in sent[2] and USED not in sent[2]


async def test_chapter_deleted_before_its_turn_is_skipped(app, client, fake_llm):
    _user, project, chapters = await seed(client, {1: BROKEN, 2: USED, 3: OTHER})
    await create_ai_config(client)
    gate = Gate()
    fake_llm.push([gate, _sword("佩剑在山门前断成两截", "断剑")])
    fake_llm.push(extraction())
    fake_llm.push(comparison())
    created = await _start(client, project)
    await gate.wait_reached(10)
    deleted = await client.delete(f"{API}/{project['id']}/chapters/{chapters[2]['id']}")
    assert deleted.status_code == 204, deleted.text
    gate.release()
    task = await wait_task(client, created.json(), within=15)
    assert task["status"] == "succeeded", task
    async with app.state.sessionmaker() as db:
        result = (await db.get(Task, task["id"])).result
    assert result["skipped_chapters"] == [{"chapter_id": chapters[2]["id"], "state": "skipped_missing", "error": ""}]
    last_run = (await client.get(f"{_base(project)}/summary")).json()["last_run"]
    assert (last_run["stats"]["skipped_chapters"][0]["chapter_no"],
            last_run["stats"]["skipped_chapters"][0]["chapter_title"]) == (None, None)


def _long_fact(fact_id: str, content: str, subject: str) -> dict:
    # 每条事实约 300 字：两个主体各两章即超过 1000 字的批次预算，比对分成两批
    return fact(fact_id, content, content[:6], subject=subject, statement=subject + "的经历" + "甲" * 290)


async def _two_batch_world(app, client):
    _user, project, chapters = await seed(client, {1: BROKEN, 2: USED})
    await create_ai_config(client)
    async with app.state.sessionmaker() as db:
        for number, content in ((1, BROKEN), (2, USED)):
            await add_ledger(db, project["id"], chapters[number], [
                _long_fact("1", content, "佩剑"), _long_fact("2", content, "苏婉"),
            ])
        await db.commit()
    run_id = await new_run(app, project["id"], [chapters[1]["id"], chapters[2]["id"]])
    async with app.state.sessionmaker() as db:
        db.add(ConsistencyIssue(
            project_id=project["id"], category="item", title="旧问题", severity="medium", status="open",
            identity_key="a" * 64, first_run_id=run_id, last_seen_run_id=run_id, last_checked_run_id=run_id,
            evidence=[{"chapter_id": chapters[number]["id"], "fact_id": "1", "quote": "", "start": None, "end": None,
                       "text_hash": text_hash(content)} for number, content in ((1, BROKEN), (2, USED))],
        ))
        await db.commit()
    return project


async def test_failed_compare_batch_does_not_mark_not_reproduced(app, client, fake_llm):
    """有比对批次失败时不能把未检出的待处理问题标成「本次未复现」；全部批次成功时才标记。"""
    project = await _two_batch_world(app, client)
    fake_llm.push(comparison())
    fake_llm.push(AIProviderError("模型调用失败：超时"))
    task = await _run(client, project)
    assert task["status"] == "succeeded", task
    async with app.state.sessionmaker() as db:
        result = (await db.get(Task, task["id"])).result
    assert (result["batches"], result["failed_batches"], result["not_reproduced"]) == (2, 1, 0)
    assert result["warnings"] == ["第 2 批比对失败：模型调用失败：超时"]
    issue = (await client.get(f"{_base(project)}/issues")).json()["items"][0]
    assert issue["not_reproduced"] is False

    fake_llm.push(comparison())
    fake_llm.push(comparison())
    task = await _run(client, project)
    async with app.state.sessionmaker() as db:
        result = (await db.get(Task, task["id"])).result
    assert (result["batches"], result["failed_batches"], result["not_reproduced"]) == (2, 0, 1)
    issue = (await client.get(f"{_base(project)}/issues")).json()["items"][0]
    assert issue["not_reproduced"] is True and issue["status"] == "open"


async def test_all_compare_batches_failing_fails_the_task(app, client, fake_llm):
    project = await _two_batch_world(app, client)
    fake_llm.push(AIProviderError("模型调用失败：超时"))
    fake_llm.push("不是 JSON")
    task = await _run(client, project)
    assert task["status"] == "failed" and task["error"] == "跨章比对全部失败：模型调用失败：超时"
    issue = (await _issues(app, project["id"]))[0]
    assert issue.last_checked_run_id == issue.last_seen_run_id
    assert (await _runs(app, project["id"]))[-1].status == "failed"


async def test_range_check_only_extracts_scope_and_uses_outside_ledgers(app, client, fake_llm):
    """AC3：仅检查第 10–15 章时不重新抽取范围外章节，仍能报告第 5 章与第 12 章之间的问题。"""
    _user, project, chapters = await seed(client, {5: BROKEN, 12: USED, 20: OTHER})
    await create_ai_config(client)
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[5], [fact("1", BROKEN, "佩剑在山门前断成两截")])
        await db.commit()
    fake_llm.push(_sword("拔出佩剑", "林远拔出佩剑迎敌"))
    fake_llm.push(comparison(_issue(chapters[5], chapters[12])))
    created = await _start(client, project, mode="range", start_chapter_id=chapters[12]["id"],
                           end_chapter_id=chapters[12]["id"])
    assert created.status_code == 202 and created.json()["title"] == "一致性检查 · 第12章"
    task = await wait_task(client, created.json(), within=15)
    assert task["status"] == "succeeded", task
    assert len(fake_llm.calls) == 2
    assert set(await _ledgers(app, project["id"])) == {chapters[5]["id"], chapters[12]["id"]}
    issues = await _issues(app, project["id"])
    assert len(issues) == 1 and {item["chapter_id"] for item in issues[0].evidence} == {
        chapters[5]["id"], chapters[12]["id"],
    }
    run = (await _runs(app, project["id"]))[0]
    assert run.scope["mode"] == "range" and run.scope["chapter_ids"] == [chapters[12]["id"]]


async def test_admin_monitor_lists_the_task_with_safe_stage(app, client, fake_llm, system_admin):
    _user, project, chapters = await seed(client, {1: BROKEN, 2: USED})
    await create_ai_config(client)
    gate = Gate()
    fake_llm.push([gate, _sword("佩剑在山门前断成两截", "断剑")])
    fake_llm.push(_sword("拔出佩剑", "用剑"))
    fake_llm.push(comparison())
    created = await _start(client, project)
    await gate.wait_reached(10)
    listed = await system_admin.get("/api/v1/admin/tasks", params={"kind": "consistency.check"})
    assert listed.status_code == 200, listed.text
    item = listed.json()["items"][0]
    assert item["kind"] == "consistency.check" and item["title"] == f"一致性检查 #{created.json()['id']}"
    assert item["stage"] == "第 1/2 章 · 抽取事实"
    gate.release()
    assert (await wait_task(client, created.json(), within=15))["status"] == "succeeded"


async def test_stream_emits_progress_log_and_done_summary(app, client, fake_llm):
    """任务流：快照 + 进度日志 delta + progress（阶段文案、单调进度）+ done 概要；不含模型原文。"""
    _user, project, chapters = await seed(client, {5: BROKEN, 12: USED})
    await create_ai_config(client)
    gate = Gate()
    raw = _sword("佩剑在山门前断成两截", "林远的佩剑断成两截")
    fake_llm.push([raw[:1], gate, raw[1:]])
    fake_llm.push(_sword("拔出佩剑", "林远拔出佩剑迎敌"))
    fake_llm.push(comparison(_issue(chapters[5], chapters[12])))
    created = await _start(client, project)
    assert created.status_code == 202, created.text
    resp = await stream_with_gate(app, client, created.json(), gate)
    assert resp.status_code == 200, resp.text
    events = parse_sse(resp.text)
    name, final = events[-1]
    assert name == "done" and final["task"]["status"] == "succeeded"
    assert (final["result"]["created"], final["result"]["extracted"], final["result"]["total"]) == (1, 2, 2)
    assert streamed_text(events) == "第 1/2 章：抽取 1 条事实\n第 2/2 章：抽取 1 条事实\n比对 1/1 批：完成\n"
    assert raw not in str(events) and "林远的佩剑断成两截" not in str(events)
    progress = [data for event, data in events if event == "progress" and data["status"] == "running"]
    values = [item["progress"] for item in progress]
    assert values == sorted(values) and max(values) <= 95
    stages = [item["stage"] for item in progress]
    assert "比对 1/1 批" in stages and "合并问题" in stages
