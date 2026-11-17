"""一致性检查接口：筛选与分页、概要、状态与备注 CAS、删章后的证据、跨项目与回收站、单问题重新核查的范围。"""

import pytest
from sqlalchemy import select

from app.models import ConsistencyIssue, ConsistencyRun, Task
from app.services.consistency import check
from app.services.mentions.matcher import text_hash
from tests.consistency_helpers import API, add_ledger, comparison, fact, new_run, seed
from tests.helpers import create_ai_config, create_project, register, wait_task

A = "😀 林远的佩剑在山门前断成两截。"
B = "林远拔出佩剑，一剑逼退来敌。"
C = "苏婉在药庐里守了一夜。"


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


def _base(project: dict) -> str:
    return f"{API}/{project['id']}/consistency"


def _evidence(chapter: dict, quote: str, *, content: str | None = None) -> dict:
    text = content if content is not None else chapter["content"]
    start = text.index(quote)
    return {"chapter_id": chapter["id"], "fact_id": "1", "quote": quote, "start": start, "end": start + len(quote),
            "text_hash": text_hash(text)}


async def _add_issue(app, project: dict, key: str, evidence: list[dict], **fields) -> int:
    async with app.state.sessionmaker() as db:
        issue = ConsistencyIssue(
            project_id=project["id"], identity_key=key * 64, evidence=evidence,
            **{"category": "item", "title": f"问题{key}", "severity": "medium", "status": "open", **fields},
        )
        db.add(issue)
        await db.commit()
        return issue.id


async def _world(app, client):
    _user, project, chapters = await seed(client, {1: A, 2: B, 3: C})
    ids = {
        "a": await _add_issue(app, project, "a", [_evidence(chapters[1], "佩剑"), _evidence(chapters[2], "佩剑")],
                              severity="high"),
        "b": await _add_issue(app, project, "b", [_evidence(chapters[2], "林远"), _evidence(chapters[3], "苏婉")],
                              category="character_state", status="resolved"),
        "c": await _add_issue(app, project, "c", [_evidence(chapters[1], "林远"), _evidence(chapters[3], "药庐")],
                              category="timeline", status="ignored", severity="low"),
        "d": await _add_issue(app, project, "d", [_evidence(chapters[1], "山门"), _evidence(chapters[2], "来敌")],
                              severity="low"),
    }
    return project, chapters, ids


async def _list(client, project, **params) -> dict:
    resp = await client.get(f"{_base(project)}/issues", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_list_filters_ordering_and_pagination(app, client):
    project, chapters, ids = await _world(app, client)
    everything = await _list(client, project)
    # 待处理在前（严重程度高的优先），其次已解决、已忽略
    assert [item["id"] for item in everything["items"]] == [ids["a"], ids["d"], ids["b"], ids["c"]]
    assert (everything["total"], everything["page"], everything["page_size"]) == (4, 1, 20)
    assert [item["id"] for item in (await _list(client, project, status="open"))["items"]] == [ids["a"], ids["d"]]
    assert [item["id"] for item in (await _list(client, project, category="timeline"))["items"]] == [ids["c"]]
    by_chapter = await _list(client, project, chapter_id=chapters[3]["id"])
    assert [item["id"] for item in by_chapter["items"]] == [ids["b"], ids["c"]] and by_chapter["total"] == 2
    combined = await _list(client, project, chapter_id=chapters[1]["id"], status="open", page_size=1, page=2)
    assert [item["id"] for item in combined["items"]] == [ids["d"]] and combined["total"] == 2
    second_page = await _list(client, project, page=2, page_size=3)
    assert [item["id"] for item in second_page["items"]] == [ids["c"]] and second_page["total"] == 4
    assert (await _list(client, project, page=9))["items"] == []
    for bad in ({"status": "closed"}, {"category": "plot"}, {"page": 0}, {"page_size": 101}, {"chapter_id": 0}):
        assert (await client.get(f"{_base(project)}/issues", params=bad)).status_code == 422


async def test_needs_recheck_filter_is_applied_before_pagination(app, client):
    project, chapters, ids = await _world(app, client)
    assert (await _list(client, project, needs_recheck=True))["total"] == 0
    resp = await client.patch(f"{API}/{project['id']}/chapters/{chapters[3]['id']}", json={"content": C + "天亮了。"})
    assert resp.status_code == 200, resp.text
    stale = await _list(client, project, needs_recheck=True, page_size=1)
    assert stale["total"] == 2 and [item["id"] for item in stale["items"]] == [ids["b"]]
    assert stale["items"][0]["needs_recheck"] is True
    assert [e["stale"] for e in stale["items"][0]["evidence"]] == [False, True]
    stale_two = await _list(client, project, needs_recheck=True, page_size=1, page=2)
    assert [item["id"] for item in stale_two["items"]] == [ids["c"]]
    fresh = await _list(client, project, needs_recheck=False)
    assert [item["id"] for item in fresh["items"]] == [ids["a"], ids["d"]]
    assert all(item["needs_recheck"] is False for item in fresh["items"])
    both = await _list(client, project, needs_recheck=True, status="ignored", chapter_id=chapters[1]["id"])
    assert [item["id"] for item in both["items"]] == [ids["c"]]


async def test_summary_counts_coverage_and_last_run(app, client):
    project, chapters, _ids = await _world(app, client)
    empty = await client.post(f"{API}/{project['id']}/chapters", json={"title": "空章", "chapter_no": 4, "content": ""})
    assert empty.status_code == 201
    blank = (await client.get(f"{_base(project)}/summary")).json()
    assert blank["last_run"] is None and blank["running_task_id"] is None
    assert blank["counts_by_status"] == {"open": 2, "resolved": 1, "ignored": 1}
    assert blank["ledger_coverage"] == {"fresh": 0, "stale": 0, "missing": 3}
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[1], [fact("1", A, "佩剑")])
        await add_ledger(db, project["id"], chapters[2], [fact("1", B, "佩剑")], fresh=False)
        await db.commit()
    await new_run(app, project["id"], [chapters[1]["id"]])
    run_id = await new_run(app, project["id"], [chapters[1]["id"], chapters[2]["id"]])
    async with app.state.sessionmaker() as db:
        run = await db.get(ConsistencyRun, run_id)
        run.status, run.stats = "succeeded", {"total": 2, "extracted": 1, "reused": 1, "created": 3}
        await db.commit()
    data = (await client.get(f"{_base(project)}/summary")).json()
    assert data["ledger_coverage"] == {"fresh": 1, "stale": 1, "missing": 1}
    assert data["last_run"]["id"] == run_id and data["last_run"]["status"] == "succeeded"
    assert data["last_run"]["scope"]["chapter_ids"] == [chapters[1]["id"], chapters[2]["id"]]
    assert (data["last_run"]["stats"]["created"], data["last_run"]["stats"]["failed"]) == (3, 0)
    # 没有任务行、状态仍为 running 的旧记录按中断显示（进程崩溃后的残留）
    orphan = await new_run(app, project["id"], [chapters[3]["id"]])
    data = (await client.get(f"{_base(project)}/summary")).json()
    assert (data["last_run"]["id"], data["last_run"]["status"]) == (orphan, "interrupted")


async def test_status_and_note_cas(app, client):
    project, _chapters, ids = await _world(app, client)
    url = f"{_base(project)}/issues/{ids['a']}"
    async with app.state.sessionmaker() as db:
        (await db.get(ConsistencyIssue, ids["a"])).reappeared = True
        await db.commit()
    first = await client.patch(url, json={"revision": 1, "status": "resolved", "note": "第 8 章已补写重铸"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["status"], body["note"], body["revision"], body["reappeared"]) == (
        "resolved", "第 8 章已补写重铸", 2, False,
    )
    assert body["status_changed_by"] is not None and body["status_changed_at"] is not None
    # 另一端仍拿着旧修订：冲突，数据不变
    stale = await client.patch(url, json={"revision": 1, "note": "另一端的备注"})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
    assert (await client.get(url)).json()["note"] == "第 8 章已补写重铸"
    # 只改备注不动状态信息
    noted = await client.patch(url, json={"revision": 2, "note": ""})
    assert noted.status_code == 200 and noted.json()["note"] == "" and noted.json()["revision"] == 3
    assert noted.json()["status_changed_at"] == body["status_changed_at"]
    for bad in ({"revision": 3}, {"revision": 3, "status": None}, {"revision": 3, "status": "closed"},
                {"revision": 0, "note": "x"}, {"revision": 3, "note": "x" * 2001}, {"note": "x"},
                {"revision": 3, "note": "x", "title": "改标题"}):
        assert (await client.patch(url, json=bad)).status_code == 422, bad
    assert (await client.patch(f"{_base(project)}/issues/99999", json={"revision": 1, "note": "x"})).status_code == 404


async def test_check_does_not_bump_revision_and_keeps_author_status(app, client, fake_llm):
    """检查更新证据不递增 revision：作者拿着检查前的修订仍可保存备注；已忽略的问题再次检出保持已忽略。"""
    project, chapters, ids = await _world(app, client)
    await create_ai_config(client)
    async with app.state.sessionmaker() as db:
        for number, content in ((1, A), (2, B), (3, C)):
            # 时间线类事实：同类别且证据章节都在本批内的已有问题（c）才会随比对给出、才能被指认
            await add_ledger(db, project["id"], chapters[number], [
                fact("1", content, content[2:6], category="timeline", subject="")])
        await db.commit()
    fake_llm.push(comparison({
        "existing_issue_id": ids["c"], "category": "timeline", "title": "时间线潜在冲突", "description": "新的说明",
        "suggestion": "", "severity": "low", "foreshadow_id": None,
        "evidence": [{"chapter_id": chapters[number]["id"], "fact_id": "1"} for number in (1, 3)],
    }))
    created = await client.post(f"{_base(project)}/checks", json={"mode": "all"})
    assert created.status_code == 202, created.text
    assert (await wait_task(client, created.json(), within=15))["status"] == "succeeded"
    detail = (await client.get(f"{_base(project)}/issues/{ids['c']}")).json()
    assert (detail["status"], detail["description"], detail["revision"]) == ("ignored", "新的说明", 1)
    saved = await client.patch(f"{_base(project)}/issues/{ids['c']}", json={"revision": 1, "note": "仍然忽略"})
    assert saved.status_code == 200 and saved.json()["revision"] == 2


async def test_deleted_chapter_evidence_is_shown_without_error(app, client):
    project, chapters, ids = await _world(app, client)
    deleted = await client.delete(f"{API}/{project['id']}/chapters/{chapters[2]['id']}")
    assert deleted.status_code == 204, deleted.text
    detail = await client.get(f"{_base(project)}/issues/{ids['a']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["needs_recheck"] is True
    kept, gone = body["evidence"]
    assert (kept["chapter_no"], kept["chapter_title"], kept["stale"]) == (1, "章1", False)
    assert (gone["chapter_id"], gone["chapter_no"], gone["chapter_title"], gone["stale"], gone["quote"]) == (
        chapters[2]["id"], None, None, True, "佩剑",
    )
    assert (await _list(client, project, needs_recheck=True))["total"] == 3
    # 备份导入后证据章节映射不到（chapter_id 为 null）同样视为需重新核查
    orphan = await _add_issue(app, project, "e", [
        {"chapter_id": None, "fact_id": "1", "quote": "旧引句", "start": None, "end": None, "text_hash": "0" * 64},
        _evidence(chapters[1], "佩剑"),
    ])
    body = (await client.get(f"{_base(project)}/issues/{orphan}")).json()
    assert body["needs_recheck"] is True and body["evidence"][0]["chapter_id"] is None


async def test_renumber_and_content_writes_follow_current_chapter_state(app, client):
    """章节生命周期：不存章号，改章号后证据读到当前章号；任何正文写入按指纹判定，写回原文后恢复有效。"""
    project, chapters, ids = await _world(app, client)
    async with app.state.sessionmaker() as db:
        await add_ledger(db, project["id"], chapters[2], [fact("1", B, "拔出佩剑")])
        await db.commit()
    url = f"{API}/{project['id']}/chapters/{chapters[2]['id']}"
    moved = await client.patch(url, json={"chapter_no": 9, "title": "改名后"})
    assert moved.status_code == 200, moved.text
    body = (await client.get(f"{_base(project)}/issues/{ids['a']}")).json()
    assert [(item["chapter_no"], item["chapter_title"], item["stale"]) for item in body["evidence"]] == [
        (1, "章1", False), (9, "改名后", False)]
    # 改章号、改标题不是正文变化：不需要重新核查，台账仍新鲜
    assert body["needs_recheck"] is False
    assert (await client.get(f"{_base(project)}/summary")).json()["ledger_coverage"]["fresh"] == 1
    assert (await _list(client, project, chapter_id=chapters[2]["id"]))["total"] == 3

    edited = await client.patch(url, json={"content": B + "改稿"})
    assert edited.status_code == 200, edited.text
    body = (await client.get(f"{_base(project)}/issues/{ids['a']}")).json()
    assert body["needs_recheck"] is True and [item["stale"] for item in body["evidence"]] == [False, True]
    assert (await client.get(f"{_base(project)}/summary")).json()["ledger_coverage"]["stale"] == 1
    # 正文写回检查时的原文（版本恢复同理）：指纹重新一致，证据位置重新有效
    restored = await client.patch(url, json={"content": B})
    assert restored.status_code == 200, restored.text
    body = (await client.get(f"{_base(project)}/issues/{ids['a']}")).json()
    assert body["needs_recheck"] is False and [item["stale"] for item in body["evidence"]] == [False, False]
    assert (await client.get(f"{_base(project)}/summary")).json()["ledger_coverage"]["fresh"] == 1


async def test_cross_project_other_user_and_trash_are_rejected(app, client, make_client, fake_llm):
    project, chapters, ids = await _world(app, client)
    await create_ai_config(client)
    other = await create_project(client, title="另一部作品")
    cross = f"{API}/{other['id']}/consistency/issues/{ids['a']}"
    assert (await client.get(cross)).status_code == 404
    assert (await client.patch(cross, json={"revision": 1, "note": "x"})).status_code == 404
    assert (await client.post(f"{cross}/recheck")).status_code == 404
    assert (await client.get(f"{API}/{other['id']}/consistency/issues")).json()["total"] == 0
    # 修改影响：别的作品的章节 id 不可读、不可确认、不可评估
    cross_impact = f"{API}/{other['id']}/consistency/impacts/{chapters[1]['id']}"
    assert (await client.get(cross_impact)).status_code == 404
    assert (await client.patch(cross_impact, json={"revision": 0, "acknowledged": []})).status_code == 404
    assert (await client.post(f"{cross_impact}/assess")).status_code == 404
    cross_range = await client.post(f"{API}/{other['id']}/consistency/checks", json={
        "mode": "range", "start_chapter_id": chapters[1]["id"], "end_chapter_id": chapters[2]["id"],
    })
    assert cross_range.status_code == 400

    stranger = make_client()
    await register(stranger, "stranger")
    await create_ai_config(stranger)
    base = _base(project)
    for method, path, body in (
        ("get", f"{base}/summary", None), ("get", f"{base}/issues", None), ("get", f"{base}/issues/{ids['a']}", None),
        ("patch", f"{base}/issues/{ids['a']}", {"revision": 1, "note": "x"}),
        ("post", f"{base}/checks/preview", {"mode": "all"}), ("post", f"{base}/checks", {"mode": "all"}),
        ("post", f"{base}/issues/{ids['a']}/recheck", None),
        ("get", f"{base}/impacts", None), ("get", f"{base}/impacts/running", None),
        ("get", f"{base}/impacts/{chapters[1]['id']}", None),
        ("patch", f"{base}/impacts/{chapters[1]['id']}", {"revision": 0, "acknowledged": []}),
        ("post", f"{base}/impacts/{chapters[1]['id']}/assess", None),
    ):
        resp = await getattr(stranger, method)(path, **({"json": body} if body is not None else {}))
        assert resp.status_code == 404, (method, path, resp.text)

    trashed = await client.delete(f"{API}/{project['id']}")
    assert trashed.status_code in (200, 204), trashed.text
    for method, path, body in (
        ("get", f"{base}/summary", None), ("get", f"{base}/issues", None),
        ("patch", f"{base}/issues/{ids['a']}", {"revision": 1, "note": "x"}),
        ("post", f"{base}/checks", {"mode": "all"}), ("post", f"{base}/issues/{ids['a']}/recheck", None),
        ("get", f"{base}/impacts", None), ("get", f"{base}/impacts/running", None),
        ("get", f"{base}/impacts/{chapters[1]['id']}", None),
        ("patch", f"{base}/impacts/{chapters[1]['id']}", {"revision": 0, "acknowledged": []}),
        ("post", f"{base}/impacts/{chapters[1]['id']}/assess", None),
    ):
        resp = await getattr(client, method)(path, **({"json": body} if body is not None else {}))
        assert resp.status_code == 404, (method, path, resp.text)
    async with app.state.sessionmaker() as db:
        assert (await db.scalars(select(Task))).all() == []
        assert (await db.get(ConsistencyIssue, ids["a"])).note == ""
    assert fake_llm.calls == []


async def test_recheck_scope_is_the_surviving_evidence_chapters(app, client, fake_llm):
    """C7：单个问题重新核查 = 对其现存证据章节运行一次检查；证据章节都不在时 400。"""
    project, chapters, ids = await _world(app, client)
    await create_ai_config(client)
    async with app.state.sessionmaker() as db:
        for number, content in ((1, A), (3, C)):
            await add_ledger(db, project["id"], chapters[number], [fact("1", content, content[2:6])])
        await db.commit()
    fake_llm.push(comparison())
    created = await client.post(f"{_base(project)}/issues/{ids['c']}/recheck")
    assert created.status_code == 202, created.text
    assert created.json()["title"] == f"一致性检查 · 重新核查问题 #{ids['c']}"
    task = await wait_task(client, created.json(), within=15)
    assert task["status"] == "succeeded", task
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task["id"])
        run = await db.scalar(select(ConsistencyRun).where(ConsistencyRun.task_id == task["id"]))
    assert row.input["chapter_ids"] == [chapters[1]["id"], chapters[3]["id"]]
    assert row.input["request"] == {"project_id": project["id"], "mode": "issue", "issue_id": ids["c"],
                                    "ai_config_id": None}
    assert run.scope == {"mode": "issue", "start_chapter_id": None, "end_chapter_id": None, "issue_id": ids["c"],
                         "chapter_ids": [chapters[1]["id"], chapters[3]["id"]]}
    # 只发了一次比对：两章台账都新鲜，范围外的第 2 章不抽取
    assert len(fake_llm.calls) == 1
    last_run = (await client.get(f"{_base(project)}/summary")).json()["last_run"]
    assert (last_run["scope"]["mode"], last_run["scope"]["issue_id"]) == ("issue", ids["c"])

    # 证据章节删掉一章：范围只剩现存章节
    assert (await client.delete(f"{API}/{project['id']}/chapters/{chapters[3]['id']}")).status_code == 204
    fake_llm.push(comparison())
    again = await client.post(f"{_base(project)}/issues/{ids['c']}/recheck", json={})
    assert again.status_code == 202, again.text
    assert (await wait_task(client, again.json(), within=15))["status"] == "succeeded"
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, again.json()["id"])).input["chapter_ids"] == [chapters[1]["id"]]
    assert (await client.delete(f"{API}/{project['id']}/chapters/{chapters[1]['id']}")).status_code == 204
    none_left = await client.post(f"{_base(project)}/issues/{ids['c']}/recheck")
    assert none_left.status_code == 400 and none_left.json()["detail"] == check.ISSUE_NO_CHAPTERS
    assert (await client.post(f"{_base(project)}/issues/99999/recheck")).status_code == 404
