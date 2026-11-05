"""章节分析（chapter.analyze）：结果结构与历史、过期判断、自动分析开关、一键分析与串行、前情摘要、越权。"""

import asyncio

import pytest
from sqlalchemy import func, select

from app.models import ChapterAnalysis, GenerationLog, Task
from app.services.analysis_service import compute_metrics, content_hash, normalize_analysis, normalize_scores
from tests.helpers import (
    Gate,
    analysis_json,
    create_ai_config,
    create_project,
    register,
    run_task,
    wait_task,
)

AUTO_ON = {"auto_analyze_enabled": True}


async def _setup(client, contents=("林远走进山门。\n\n「来者何人？」守卫喝道。",)):
    await register(client)
    await create_ai_config(client)
    p = await create_project(client, title="剑道", genre="仙侠")
    base = f"/api/v1/projects/{p['id']}"
    chs = []
    for i, content in enumerate(contents, start=1):
        resp = await client.post(f"{base}/chapters", json={"title": f"章{i}", "chapter_no": i, "content": content})
        chs.append(resp.json())
    return p, base, chs


def _analyze(client, ch, **body):
    return client.post(f"/api/v1/chapters/{ch['id']}/analyze", json=body or None)


async def _analysis_tasks(app, chapter_id=None) -> list[Task]:
    async with app.state.sessionmaker() as db:
        stmt = select(Task).where(Task.kind == "chapter.analyze")
        if chapter_id is not None:
            stmt = stmt.where(Task.chapter_id == chapter_id)
        return list(await db.scalars(stmt.order_by(Task.id)))


# ---------- 纯函数 ----------


def test_compute_metrics():
    text = "林远走进山门。\n\n「来者何人？」守卫喝道。\n\n他笑了笑：“过路人。”"
    m = compute_metrics(text)
    assert m["paragraphs"] == 3
    assert m["words"] == len("林远走进山门。「来者何人？」守卫喝道。他笑了笑：“过路人。”")
    assert m["dialogue_ratio"] == round((len("来者何人？") + len("过路人。")) / m["words"], 3)
    assert m["avg_paragraph_chars"] == round(m["words"] / 3, 1)
    # 没有空行时按非空行切分
    assert compute_metrics("第一段\n第二段\n第三段")["paragraphs"] == 3
    assert compute_metrics("") == {"words": 0, "paragraphs": 0, "avg_paragraph_chars": 0, "dialogue_ratio": 0}


def test_normalize_scores_and_items():
    # overall 由服务端复算，忽略模型给出的值；越界截断
    assert normalize_scores({"overall": 10, "pacing": 12, "engagement": 0, "coherence": "6.66"}) == {
        "overall": 5.9,
        "pacing": 10.0,
        "engagement": 1.0,
        "coherence": 6.7,
    }
    assert normalize_scores({"pacing": 6})["overall"] == 6.0
    assert normalize_scores(None)["overall"] is None

    data, warnings = normalize_analysis(
        {
            "summary": "  摘要  ",
            "hooks": [{"type": "未知", "strength": 99, "text": "钩子"}, {"text": ""}],
            "character_changes": [{"name": "甲", "life_status": "已经死亡"}, {"name": "乙", "life_status": "飞升"}],
            "relationship_changes": [{"a": "甲", "b": "乙", "intimacy": 300, "status": "决裂"}],
            "organization_changes": [{"name": "甲", "organization": "宗门", "action": "加入"}],
            "suggestions": [{"category": "其它", "priority": "高", "text": "改"}, {"text": ""}],
            "scores": {"pacing": 5, "engagement": 5, "coherence": 5},
        }
    )
    assert data["summary"] == "摘要"
    assert data["hooks"] == [{"type": "其他", "strength": 10, "text": "钩子"}]
    assert data["character_changes"] == [{"name": "甲", "current_state": None, "life_status": "dead"}]
    assert any("飞升" in w for w in warnings)
    assert data["relationship_changes"][0]["intimacy"] == 100
    assert data["relationship_changes"][0]["status"] == "broken"
    assert data["organization_changes"][0]["action"] == "join"
    assert data["suggestions"] == [{"id": 1, "category": "其他", "priority": "high", "text": "改"}]


# ---------- AC1：结构完整 + 历史保留 ----------


async def test_analyze_chapter_structure_and_history(app, client, fake_llm):
    _, base, (ch,) = await _setup(client)
    fake_llm.push(analysis_json(scores={"pacing": 7, "engagement": 8, "coherence": 6, "overall": 1}))
    resp = await _analyze(client, ch)
    assert resp.status_code == 202, resp.text
    assert resp.json()["kind"] == "chapter.analyze" and resp.json()["title"] == "第 1 章 章1 · 章节分析"
    task, events = await run_task(client, resp)
    assert task["status"] == "succeeded"
    result = events[-1][1]["result"]
    assert result["scores"] == {"overall": 7.0, "pacing": 7.0, "engagement": 8.0, "coherence": 6.0}
    prompt = fake_llm.calls[-1]["messages"][1]["content"]
    assert "林远走进山门" in prompt and "【评分标准】" in prompt

    history = (await client.get(f"/api/v1/chapters/{ch['id']}/analyses")).json()
    assert len(history) == 1 and history[0]["stale"] is False and history[0]["task_id"] == task["id"]
    detail = (await client.get(f"/api/v1/analyses/{history[0]['id']}")).json()
    assert detail["summary"].startswith("林远在山门试炼")
    assert [p["text"] for p in detail["plot_points"]] == ["山门试炼开始", "林远击败对手"]
    assert detail["hooks"] == [{"type": "悬念", "strength": 8, "text": "神秘人现身"}]
    assert detail["conflict"] == {"level": 7, "types": ["人与人"]}
    assert detail["emotion"]["tone"] == "紧张"
    assert [s["id"] for s in detail["suggestions"]] == [1, 2, 3]
    assert detail["foreshadow_candidates"][0]["keyword"] == "玉佩"
    assert detail["metrics"]["paragraphs"] == 2 and detail["metrics"]["dialogue_ratio"] > 0
    # 没有编号的新伏笔只进入待确认（foreshadows），不写入其他设定
    assert [(c["kind"], c["status"], c["after"]["title"]) for c in detail["changes"]] == [
        ("foreshadow_new", "pending", "玉佩")
    ]
    # 分析不写生成记录
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(GenerationLog.id))) == 0

    fake_llm.push(analysis_json(scores={"pacing": 9, "engagement": 9, "coherence": 9}))
    task2, _ = await run_task(client, await _analyze(client, ch))
    assert task2["status"] == "succeeded"
    history = (await client.get(f"/api/v1/chapters/{ch['id']}/analyses")).json()
    assert [h["scores"]["overall"] for h in history] == [9.0, 7.0]  # 新的在前，旧结果保留
    state = (await client.get(f"/api/v1/chapters/{ch['id']}/analysis-status")).json()
    assert state["status"] == "analyzed" and state["latest_analysis_id"] == history[0]["id"]


async def test_analyze_errors(app, client, fake_llm):
    _, base, (ch,) = await _setup(client)
    empty = (await client.post(f"{base}/chapters", json={"title": "空"})).json()
    resp = await _analyze(client, empty)
    assert resp.status_code == 400 and resp.json()["detail"] == "章节正文为空，无法分析"

    fake_llm.push("这不是 JSON")
    task, events = await run_task(client, await _analyze(client, ch))
    assert task["status"] == "failed" and task["error"].startswith("AI 输出解析失败")
    assert (await client.get(f"/api/v1/chapters/{ch['id']}/analyses")).json() == []
    state = (await client.get(f"/api/v1/chapters/{ch['id']}/analysis-status")).json()
    assert state["status"] == "none"


@pytest.mark.parametrize("edit", [{"content": "后来修改过的正文"}, {"chapter_no": 9}])
async def test_inflight_analysis_does_not_apply_after_chapter_changes(app, client, fake_llm, edit):
    """模型运行期间正文或章号变化，旧结果不能回写设定或覆盖剧情记忆。"""
    _, base, (ch,) = await _setup(client)
    hero = (await client.post(f"{base}/characters", json={"name": "林远", "current_state": "练气"})).json()
    gate = Gate()
    fake_llm.push([gate, analysis_json(
        character_changes=[{"name": "林远", "current_state": "金丹"}], foreshadows=[],
    )])
    task = (await _analyze(client, ch)).json()
    await gate.wait_reached()
    changed = await client.patch(f"{base}/chapters/{ch['id']}", json=edit)
    assert changed.status_code == 200, changed.text
    gate.release()
    result = await wait_task(client, task["id"])
    assert result["status"] == "failed"
    assert "变化" in result["error"]
    assert (await client.get(f"{base}/characters/{hero['id']}")).json()["current_state"] == "练气"
    assert (await client.get(f"/api/v1/chapters/{ch['id']}/analyses")).json() == []


async def test_analyze_requires_ai_config(client):
    await register(client)
    p = await create_project(client)
    ch = (await client.post(f"/api/v1/projects/{p['id']}/chapters", json={"content": "正文"})).json()
    resp = await _analyze(client, ch)
    assert resp.status_code == 400 and "尚未配置" in resp.json()["detail"]


# ---------- AC2：过期 + 自动分析开关 ----------


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
async def test_stale_after_manual_edit_and_auto_analyze(app, client, fake_llm):
    p, base, (ch,) = await _setup(client)
    assert (await client.get(f"{base}/analysis/statuses")).json()[0]["status"] == "none"
    fake_llm.push(analysis_json())
    await run_task(client, await _analyze(client, ch))
    statuses = (await client.get(f"{base}/analysis/statuses")).json()
    assert statuses[0]["status"] == "analyzed" and statuses[0]["scores"]["overall"] == 7.0

    # 手动编辑：变为已过期，不触发分析
    resp = await client.patch(f"{base}/chapters/{ch['id']}", json={"content": "改过的正文"})
    assert resp.status_code == 200
    await asyncio.sleep(0.05)
    assert (await client.get(f"{base}/analysis/statuses")).json()[0]["status"] == "stale"
    assert (await client.get(f"/api/v1/chapters/{ch['id']}/analyses")).json()[0]["stale"] is True
    assert len(await _analysis_tasks(app)) == 1

    # AI 整章生成成功后自动排队分析
    fake_llm.push("新的正文内容")
    fake_llm.push(analysis_json(summary="生成后的摘要"))
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/generate"))
    assert task["status"] == "succeeded"
    auto = await _analysis_tasks(app)
    assert len(auto) == 2 and auto[-1].input.get("auto") is True and auto[-1].serial_key == f"analysis:{p['id']}"
    done = await wait_task(client, auto[-1].id)
    assert done["status"] == "succeeded"
    statuses = (await client.get(f"{base}/analysis/statuses")).json()
    assert statuses[0]["status"] == "analyzed"
    detail = (await client.get(f"/api/v1/analyses/{statuses[0]['latest_analysis_id']}")).json()
    assert detail["summary"] == "生成后的摘要"
    assert content_hash("新的正文内容") == content_hash(
        (await client.get(f"{base}/chapters/{ch['id']}")).json()["content"]
    )

    # 关闭项目开关：生成成功也不再自动分析
    resp = await client.patch(base, json={"auto_analyze": False})
    assert resp.status_code == 200 and resp.json()["auto_analyze"] is False
    fake_llm.push("再次生成的正文")
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/polish"))
    assert task["status"] == "succeeded"
    await asyncio.sleep(0.05)
    assert len(await _analysis_tasks(app)) == 2
    assert (await client.get(f"{base}/analysis/statuses")).json()[0]["status"] == "stale"


async def test_deployment_switch_disables_auto_analyze(app, client, fake_llm):
    """部署级开关关闭（测试默认）：项目开关为开也不自动分析。"""
    p, base, (ch,) = await _setup(client)
    assert (await client.get(base)).json()["auto_analyze"] is True
    fake_llm.push("正文")
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{ch['id']}/generate"))
    assert task["status"] == "succeeded"
    await asyncio.sleep(0.05)
    assert await _analysis_tasks(app) == []


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
async def test_enqueue_auto_dedup_queued_but_not_running(app, client, fake_llm):
    """enqueue_auto：同章已有排队中的分析不重复入队；运行中的不算（可再排以分析最新正文）；
    空正文 / 项目开关关闭不入队。"""
    from app.services.analysis_service import enqueue_auto

    p, base, (ch,) = await _setup(client)
    empty = (await client.post(f"{base}/chapters", json={"title": "空章", "chapter_no": 2})).json()
    engine = app.state.task_engine
    ctx = engine.ctx
    await engine.stop()
    first = await enqueue_auto(ctx, p["id"], ch["id"])
    assert first is not None
    assert await enqueue_auto(ctx, p["id"], ch["id"]) is None  # 已排队
    assert await enqueue_auto(ctx, p["id"], empty["id"]) is None  # 正文为空
    assert [t.id for t in await _analysis_tasks(app)] == [first]
    (task,) = await _analysis_tasks(app)
    owner = (await client.get("/api/v1/auth/me")).json()["id"]
    assert (task.user_id, task.input["auto"], task.serial_key) == (owner, True, f"analysis:{p['id']}")

    gate = Gate()
    fake_llm.push([gate, analysis_json()])
    fake_llm.push(analysis_json(summary="第二次"))
    await engine.start()
    await gate.wait_reached()
    assert (await client.get(f"/api/v1/tasks/{first}")).json()["status"] == "running"
    second = await enqueue_auto(ctx, p["id"], ch["id"])  # 运行中不算已排队
    assert second is not None and second != first
    assert await enqueue_auto(ctx, p["id"], ch["id"]) is None
    gate.release()
    for tid in (first, second):
        assert (await wait_task(client, tid))["status"] == "succeeded"

    await client.patch(base, json={"auto_analyze": False})
    assert await enqueue_auto(ctx, p["id"], ch["id"]) is None
    assert len(await _analysis_tasks(app)) == 2


# ---------- AC3：一键分析、串行、去重、失败不阻塞 ----------


async def test_analyze_pending_serial_in_order_and_dedup(app, client, fake_llm):
    p, base, chs = await _setup(client, contents=("第一章正文", "第二章正文", "第三章正文"))
    empty = (await client.post(f"{base}/chapters", json={"title": "空章", "chapter_no": 4})).json()
    engine = app.state.task_engine
    await engine.stop()
    # 同一章重复触发只排队一次
    first = await _analyze(client, chs[1])
    assert first.status_code == 202
    again = await _analyze(client, chs[1])
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]

    resp = await client.post(f"{base}/analysis/analyze-pending")
    assert resp.status_code == 200, resp.text
    pending = resp.json()
    assert pending["queued"] == 2  # 第 2 章已排队、空章跳过
    tasks = await _analysis_tasks(app)
    assert [t.chapter_id for t in tasks] == [chs[1]["id"], chs[0]["id"], chs[2]["id"]]
    assert empty["id"] not in [t.chapter_id for t in tasks]
    states = {s["chapter_id"]: s["status"] for s in (await client.get(f"{base}/analysis/statuses")).json()}
    assert states[chs[0]["id"]] == "analyzing" and states[empty["id"]] == "none"

    gate = Gate()
    fake_llm.push([gate, analysis_json(summary="二")])
    fake_llm.push(analysis_json(summary="一"))
    fake_llm.push(analysis_json(summary="三"))
    await engine.start()
    await gate.wait_reached()
    await asyncio.sleep(0.05)
    # 同项目串行：每用户上限为 2，但第二个分析仍在排队
    assert len(fake_llm.calls) == 1
    assert (await client.get(f"/api/v1/tasks/{tasks[1].id}")).json()["status"] == "queued"
    # 运行中（第 2 章）与排队中的章节都处于「分析中」：再次一键分析不重复入队
    assert (await client.post(f"{base}/analysis/analyze-pending")).json() == {"queued": 0, "task_ids": []}
    gate.release()
    for t in tasks:
        assert (await wait_task(client, t.id))["status"] == "succeeded"
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(ChapterAnalysis).order_by(ChapterAnalysis.id)))
    assert [r.summary for r in rows] == ["二", "一", "三"]  # 按入队顺序执行

    # 全部已分析：再次一键分析不入队
    assert (await client.post(f"{base}/analysis/analyze-pending")).json() == {"queued": 0, "task_ids": []}


async def test_analysis_runs_parallel_across_projects_and_does_not_block_generation(app, client, fake_llm):
    p, base, (ch1, ch2) = await _setup(client, contents=("甲章", "乙章"))
    p2 = await create_project(client, title="另一本")
    other = (await client.post(f"/api/v1/projects/{p2['id']}/chapters", json={"content": "丙章"})).json()

    gate = Gate()
    fake_llm.push([gate, "不是 JSON"])  # 本项目的分析：阻塞后失败
    t1 = (await _analyze(client, ch1)).json()
    await gate.wait_reached()
    # 不同项目的分析并行执行
    fake_llm.push(analysis_json())
    t2, _ = await run_task(client, await _analyze(client, other))
    assert t2["status"] == "succeeded"
    gate.release()
    assert (await wait_task(client, t1["id"]))["status"] == "failed"
    # 分析失败不影响下一章生成
    fake_llm.push("第二章新正文")
    gen, _ = await run_task(client, await client.post(f"/api/v1/chapters/{ch2['id']}/generate"))
    assert gen["status"] == "succeeded"


# ---------- AC6：前情提要使用摘要 ----------


async def test_prior_summary_uses_latest_analysis(app, client, fake_llm):
    contents = [f"第{i}章开头正文" + "字" * 50 for i in range(1, 5)]
    _, base, chs = await _setup(client, contents=contents)
    target = (await client.post(f"{base}/chapters", json={"title": "章5", "chapter_no": 5})).json()
    fake_llm.push(analysis_json(summary="旧摘要"))
    await run_task(client, await _analyze(client, chs[1]))
    fake_llm.push(analysis_json(summary="第二章真正的摘要"))
    await run_task(client, await _analyze(client, chs[1]))
    # 正文手动修改后分析已过期，摘要照常使用
    await client.patch(f"{base}/chapters/{chs[1]['id']}", json={"content": "改过的第二章"})

    fake_llm.push("第五章正文")
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{target['id']}/generate"))
    assert task["status"] == "succeeded"
    prompt = fake_llm.calls[-1]["messages"][1]["content"]
    summaries = prompt.split("【前情提要（更早章节）】")[1].split("【最近章节正文】")[0]
    assert "第2章 章2：第二章真正的摘要" in summaries
    assert "旧摘要" not in summaries and "改过的第二章" not in summaries
    # 没有分析的章节仍截取正文开头
    assert "第1章 章1：第1章开头正文" in summaries


# ---------- AC8：越权 ----------


async def test_analysis_cross_user_404(app, client, make_client, fake_llm):
    p, base, (ch,) = await _setup(client)
    char = (await client.post(f"{base}/characters", json={"name": "林远", "role_type": "主角"})).json()
    fake_llm.push(analysis_json(character_changes=[{"name": "林远", "current_state": "重伤"}]))
    await run_task(client, await _analyze(client, ch))
    analysis = (await client.get(f"/api/v1/chapters/{ch['id']}/analyses")).json()[0]
    change = (await client.get(f"/api/v1/analyses/{analysis['id']}")).json()["changes"][0]
    assert change["target"]["character_id"] == char["id"]

    bob = make_client()
    await register(bob, "bob")
    await create_ai_config(bob)
    checks = [
        ("POST", f"/api/v1/chapters/{ch['id']}/analyze", None),
        ("POST", f"{base}/analysis/analyze-pending", None),
        ("GET", f"{base}/analysis/statuses", None),
        ("GET", f"/api/v1/chapters/{ch['id']}/analysis-status", None),
        ("GET", f"/api/v1/chapters/{ch['id']}/analyses", None),
        ("GET", f"/api/v1/analyses/{analysis['id']}", None),
        ("POST", f"/api/v1/analysis-changes/{change['id']}/revert", None),
        ("POST", f"/api/v1/analysis-changes/{change['id']}/confirm", None),
        ("POST", f"/api/v1/analysis-changes/{change['id']}/confirm", {"overrides": {"title": "x"}}),
        ("POST", f"/api/v1/analysis-changes/{change['id']}/reject", None),
        ("POST", f"/api/v1/chapters/{ch['id']}/revise", {"analysis_id": analysis["id"], "suggestion_ids": [1]}),
    ]
    for method, path, body in checks:
        resp = await bob.request(method, path, json=body)
        assert resp.status_code == 404, (method, path, resp.text)
    # 越权请求没有改动任何数据
    assert (await client.get(f"{base}/characters/{char['id']}")).json()["current_state"] == "重伤"
    assert len(fake_llm.calls) == 1
