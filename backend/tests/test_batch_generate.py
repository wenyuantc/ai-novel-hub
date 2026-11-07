"""批量顺序生成（chapter.batch）：跳过 / 重写、顺序与上下文、任务内分析、失败重试与续跑、审核点、范围锁、
取消 / 中断、目标字数、预览与校验，以及引擎对 paused 状态的处理。"""

import pytest
from sqlalchemy import update

from app.ai.providers import AIProviderError
from app.models import Chapter, Task
from app.services import batch_service, generation_service
from tests.helpers import (
    Gate,
    analysis_json,
    create_ai_config,
    create_project,
    register,
    run_task,
    stream_task,
    wait_task,
    wait_until,
)

AUTO_ON = {"auto_analyze_enabled": True}
PAUSED = ("paused",)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(batch_service, "RETRY_DELAYS", (0.0, 0.0))


async def _setup(client, contents: list[str], *, protagonist: bool = False):
    await register(client)
    p = await create_project(client)
    await create_ai_config(client)
    base = f"/api/v1/projects/{p['id']}"
    chs = []
    for i, content in enumerate(contents):
        resp = await client.post(f"{base}/chapters", json={"title": f"章{i + 1}", "content": content})
        assert resp.status_code == 201, resp.text
        chs.append(resp.json())
    char = None
    if protagonist:
        char = (
            await client.post(
                f"{base}/characters", json={"name": "林远", "role_type": "主角", "current_state": "初入山门"}
            )
        ).json()
    return p, base, chs, char


def _start(client, base, first, last, **body):
    payload = {"start_chapter_id": first["id"], "end_chapter_id": last["id"], "auto_continue": False, **body}
    return client.post(f"{base}/batch-generate", json=payload)


async def _chapter(app, chapter_id: int) -> Chapter:
    async with app.state.sessionmaker() as db:
        return await db.get(Chapter, chapter_id)


def _prompt(call: dict) -> str:
    return "\n".join(m["content"] for m in call["messages"])


def _gen_calls(fake_llm) -> list[dict]:
    """生成正文的调用（带目标字数），区别于分析调用。"""
    return [c for c in fake_llm.calls if "目标字数" in _prompt(c)]


# ---------- 跳过已写 / 包含已写重写 ----------


async def test_skips_written_and_include_written_keeps_versions(app, client, fake_llm):
    _, base, (c1, c2, c3), _ = await _setup(client, ["", "已写好的第二章", ""])
    fake_llm.push("第一章正文")
    fake_llm.push("第三章正文")
    task, events = await run_task(client, await _start(client, base, c1, c3))
    assert task["status"] == "succeeded" and task["kind"] == "chapter.batch"
    assert task["chapter_id"] is None and task["title"] == "批量生成 第1–3章"
    result = events[-1][1]["result"]
    assert result["done"] == 2 and result["skipped_written"] == 1 and result["failed"] == []
    assert (await _chapter(app, c1["id"])).content == "第一章正文"
    assert (await _chapter(app, c2["id"])).content == "已写好的第二章"
    assert (await _chapter(app, c3["id"])).content == "第三章正文"
    assert len(fake_llm.calls) == 2
    names = [e for e, _ in events]
    assert names.count("chapter_start") == 2 and names.count("chapter_end") == 3
    # 断点：每章结果
    states = [i["state"] for i in task["checkpoint"]["items"]]
    assert states == ["done", "skipped_written", "done"]
    assert task["checkpoint"]["items"][0]["analysis"] == "skipped"

    # 包含已写：覆盖并留版本
    fake_llm.push("重写后的第二章")
    task, _ = await run_task(client, await _start(client, base, c2, c2, include_written=True))
    assert task["status"] == "succeeded"
    assert (await _chapter(app, c2["id"])).content == "重写后的第二章"
    versions = (await client.get(f"{base}/chapters/{c2['id']}/versions")).json()
    assert {v["source"] for v in versions} >= {"generate"}
    assert any(v["source"] in ("baseline", "manual") for v in versions)


# ---------- 顺序与上下文：第 N 章看得到第 N-1 章正文与分析回写 ----------


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
async def test_sequential_context_with_analysis_writeback(app, client, fake_llm):
    _, base, (c1, c2), char = await _setup(client, ["", ""], protagonist=True)
    fake_llm.push("林远踏入山门，第一章正文到此。")
    fake_llm.push(analysis_json(character_changes=[{"name": "林远", "current_state": "金丹大成"}]))
    fake_llm.push("第二章正文")
    fake_llm.push(analysis_json())
    task, events = await run_task(client, await _start(client, base, c1, c2))
    assert task["status"] == "succeeded"
    assert len(fake_llm.calls) == 4
    gen2 = _prompt(fake_llm.calls[2])
    assert "目标字数" in gen2
    assert "林远踏入山门，第一章正文到此。" in gen2
    assert "金丹大成" in gen2 and "初入山门" not in gen2
    items = task["checkpoint"]["items"]
    assert [i["analysis"] for i in items] == ["ok", "ok"]
    assert (await client.get(f"{base}/characters/{char['id']}")).json()["current_state"] == "金丹大成"
    # 分析在任务内完成：没有另外排队的 chapter.analyze
    assert (await client.get("/api/v1/tasks", params={"kind": "chapter.analyze"})).json() == []
    states = (await client.get(f"{base}/analysis/statuses")).json()
    assert {s["status"] for s in states} == {"analyzed"}
    assert any(e == "progress" and "分析中" in d["stage"] for e, d in events)
    # 每章开始前清空实时输出：任务输出只保留最后一章
    assert (await client.get(f"/api/v1/tasks/{task['id']}/output")).json()["text"] == "第二章正文"


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
async def test_project_auto_analyze_off_skips_analysis(app, client, fake_llm):
    _, base, (c1, c2), _ = await _setup(client, ["", ""])
    assert (await client.patch(base, json={"auto_analyze": False})).status_code == 200
    task, _ = await run_task(client, await _start(client, base, c1, c2))
    assert task["status"] == "succeeded"
    assert len(fake_llm.calls) == 2
    assert [i["analysis"] for i in task["checkpoint"]["items"]] == ["skipped", "skipped"]


async def test_deployment_switch_off_skips_analysis(app, client, fake_llm):
    _, base, (c1,), _ = await _setup(client, [""])
    task, _ = await run_task(client, await _start(client, base, c1, c1))
    assert task["status"] == "succeeded" and len(fake_llm.calls) == 1


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
async def test_analysis_failure_only_warns(app, client, fake_llm):
    _, base, (c1, c2), _ = await _setup(client, ["", ""])
    fake_llm.push("第一章正文")
    fake_llm.push("这不是 JSON")
    fake_llm.push("第二章正文")
    fake_llm.push(analysis_json())
    task, events = await run_task(client, await _start(client, base, c1, c2))
    assert task["status"] == "succeeded"
    result = events[-1][1]["result"]
    assert result["done"] == 2
    assert [a["chapter_id"] for a in result["analysis_failed"]] == [c1["id"]]
    assert "分析失败" in result["warnings"][0]
    assert [i["analysis"] for i in task["checkpoint"]["items"]] == ["failed", "ok"]
    assert (await _chapter(app, c2["id"])).content == "第二章正文"


# ---------- 失败：重试 2 次 → 默认停下，重试续跑；skip_on_fail 继续 ----------


async def test_failure_stops_and_retry_resumes(app, client, fake_llm):
    _, base, (c1, c2, c3), _ = await _setup(client, ["", "", ""])
    fake_llm.push("第一章正文")
    for _ in range(3):
        fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    task, events = await run_task(client, await _start(client, base, c1, c3))
    assert task["status"] == "failed"
    assert task["error"] == "第 2 章生成失败：模型调用失败：额度不足"
    assert len(fake_llm.calls) == 4  # 第 2 章首次 + 2 次重试
    row = await _chapter(app, c2["id"])
    assert row.status == "draft" and row.content == ""
    assert [i["state"] for i in task["checkpoint"]["items"]] == ["done", "failed", "pending"]
    assert task["step_cursor"] == 1  # 断点停在失败章（前端据此显示「从第 2 章续跑」）

    fake_llm.push("第二章正文")
    fake_llm.push("第三章正文")
    resp = await client.post(f"/api/v1/tasks/{task['id']}/retry")
    retried, _ = await run_task(client, resp)
    assert retried["status"] == "succeeded" and retried["retry_of_id"] == task["id"]
    assert len(fake_llm.calls) == 6  # 已完成的第 1 章不重写
    assert (await _chapter(app, c1["id"])).content == "第一章正文"
    assert (await _chapter(app, c2["id"])).content == "第二章正文"
    assert (await _chapter(app, c3["id"])).content == "第三章正文"
    assert [i["state"] for i in retried["checkpoint"]["items"]] == ["done", "done", "done"]


async def test_skip_on_fail_continues_and_lists_failed(app, client, fake_llm):
    _, base, (c1, c2, c3), _ = await _setup(client, ["", "", ""])
    fake_llm.push("第一章正文")
    for _ in range(3):
        fake_llm.push(AIProviderError("模型调用失败：超时"))
    fake_llm.push("第三章正文")
    task, events = await run_task(client, await _start(client, base, c1, c3, skip_on_fail=True))
    assert task["status"] == "succeeded"
    result = events[-1][1]["result"]
    assert result["done"] == 2
    assert result["failed"] == [
        {"chapter_id": c2["id"], "chapter_no": 2, "title": "章2", "error": "模型调用失败：超时"}
    ]
    assert (await _chapter(app, c3["id"])).content == "第三章正文"
    assert (await _chapter(app, c2["id"])).status == "draft"


async def test_retry_succeeds_on_second_attempt(app, client, fake_llm):
    _, base, (c1,), _ = await _setup(client, [""])
    fake_llm.push(AIProviderError("模型调用失败：抖动"))
    fake_llm.push("第一章正文")
    task, _ = await run_task(client, await _start(client, base, c1, c1))
    assert task["status"] == "succeeded" and len(fake_llm.calls) == 2
    assert (await _chapter(app, c1["id"])).content == "第一章正文"


async def test_build_error_not_retried_and_restores_chapter(app, client, fake_llm, monkeypatch):
    """组装阶段的业务错误（如模型配置被删）不重试：任务失败，当前章还原。"""
    builds: list[int] = []
    original_build = generation_service.build_chapter_job

    async def counting_build(db, **kw):
        builds.append(kw["chapter_id"])
        return await original_build(db, **kw)

    monkeypatch.setattr(generation_service, "build_chapter_job", counting_build)
    _, base, (c1, c2), _ = await _setup(client, ["", ""])
    cfg = (await client.get("/api/v1/ai-configs")).json()[0]
    gate = Gate()
    fake_llm.push(["第一章", gate, "正文"])
    task_id = (await _start(client, base, c1, c2)).json()["id"]
    await gate.wait_reached()
    assert (await client.delete(f"/api/v1/ai-configs/{cfg['id']}")).status_code == 204
    gate.release()
    task = await wait_task(client, task_id)
    assert task["status"] == "failed" and task["step_cursor"] == 1
    assert len(fake_llm.calls) == 1
    assert builds == [c1["id"], c2["id"]]  # 第 2 章只组装一次，不重试
    row = await _chapter(app, c2["id"])
    assert row.status == "draft" and row.content == ""


async def test_future_chapter_deleted_during_current_proposal_invalidates_planning_snapshot(app, client, fake_llm):
    _, base, (c1, c2, c3), _ = await _setup(client, ["", "", ""])
    gate = Gate()
    fake_llm.push(["第一章", gate, "正文"])
    fake_llm.push("第三章正文")
    task_id = (await _start(client, base, c1, c3)).json()["id"]
    await gate.wait_reached()
    assert (await client.delete(f"{base}/chapters/{c2['id']}")).status_code == 204
    gate.release()
    task = await wait_task(client, task_id)
    # 场景设计 §8：删除章会推进项目规划版本，不能给已发送的当前章旧消息重签。
    # 这不是在下章构建前发现缺章的普通 skip；当前未提交提案必须失败且不半写。
    assert task["status"] == "failed" and task["step_cursor"] == 0
    assert task["checkpoint"]["items"][0]["state"] == "failed"
    assert "已变化" in task["error"]
    assert (await _chapter(app, c1["id"])).content == (await _chapter(app, c3["id"])).content == ""
    assert len(fake_llm.calls) == 1
    versions = (await client.get(f"{base}/chapters/{c1['id']}/versions")).json()
    assert versions == []


# ---------- 审核点：待审核 / 继续 / 结束 ----------


@pytest.mark.parametrize("settings_overrides", [{"task_max_per_user": 1}])
async def test_paused_batch_does_not_hold_user_slot(app, client, fake_llm):
    """待审核不占每用户并发名额：上限为 1 时，暂停期间别的任务照常运行。"""
    _, base, (c1, c2, c3), _ = await _setup(client, ["", "", ""])
    task_id = (await _start(client, base, c1, c2, review_every=1)).json()["id"]
    await wait_task(client, task_id, statuses=PAUSED)
    fake_llm.push("范围外的第三章")
    single, _ = await run_task(client, await client.post(f"/api/v1/chapters/{c3['id']}/generate", json={}))
    assert single["status"] == "succeeded"
    assert (await client.get(f"/api/v1/tasks/{task_id}")).json()["status"] == "paused"


async def test_review_pause_then_resume_uses_edits(app, client, fake_llm):
    _, base, chs, _ = await _setup(client, ["", "", "", ""])
    c1, c2, c3, c4 = chs
    fake_llm.push("第一章正文")
    fake_llm.push("第二章正文")
    resp = await _start(client, base, c1, c4, review_every=2)
    assert resp.status_code == 202
    task_id = resp.json()["id"]
    events = await stream_task(client, task_id)
    assert events[-1][0] == "paused"
    paused = await wait_task(client, task_id, statuses=PAUSED)
    assert paused["stage"] == "待审核" and paused["finished_at"] is None
    assert paused["checkpoint"]["since_review"] == 0
    engine = app.state.task_engine
    await wait_until(lambda: task_id not in engine._running)
    # 订阅已暂停的任务：快照 + paused
    assert [e for e, _ in await stream_task(client, task_id)] == ["snapshot", "paused"]
    # 「进行中」列表包含待审核；不能删除；重启恢复不影响
    active = (await client.get("/api/v1/tasks", params={"active": "true"})).json()
    assert [t["id"] for t in active] == [task_id]
    resp = await client.delete(f"/api/v1/tasks/{task_id}")
    assert resp.status_code == 409 and "待审核" in resp.json()["detail"]
    await engine.recover()
    assert (await client.get(f"/api/v1/tasks/{task_id}")).json()["status"] == "paused"
    # 待审核期间不能发起新批量
    resp = await _start(client, base, c3, c4)
    assert resp.status_code == 409 and resp.json()["detail"] == "已有进行中或待审核的批量生成"

    # 审核期间修改第 2 章，继续后第 3 章能看到
    assert (
        await client.patch(f"{base}/chapters/{c2['id']}", json={"content": "审核修改后的第二章"})
    ).status_code == 200
    fake_llm.push("第三章正文")
    fake_llm.push("第四章正文")
    resp = await client.post(f"/api/v1/tasks/{task_id}/resume")
    assert resp.status_code == 202 and resp.json()["id"] == task_id and resp.json()["status"] == "queued"
    final = await wait_task(client, task_id)
    assert final["status"] == "succeeded"  # 最后一章写完不再暂停
    gens = _gen_calls(fake_llm)
    assert len(gens) == 4
    assert "审核修改后的第二章" in _prompt(gens[2])
    assert (await _chapter(app, c4["id"])).content == "第四章正文"
    assert (await _task_result(app, task_id))["done"] == 4
    resp = await client.post(f"/api/v1/tasks/{task_id}/resume")
    assert resp.status_code == 409


async def test_review_pause_frees_range_and_finish(app, client, fake_llm):
    _, base, (c1, c2, c3), _ = await _setup(client, ["", "", ""])
    fake_llm.push("第一章正文")
    resp = await _start(client, base, c1, c3, review_every=1)
    task_id = resp.json()["id"]
    await wait_task(client, task_id, statuses=PAUSED)
    # 待审核不锁范围：范围内章节可单独生成
    fake_llm.push("单独生成的第二章")
    single, _ = await run_task(client, await client.post(f"/api/v1/chapters/{c2['id']}/generate", json={}))
    assert single["status"] == "succeeded"
    # 结束：以已完成收尾
    resp = await client.post(f"/api/v1/tasks/{task_id}/finish")
    assert resp.status_code == 200
    out = resp.json()
    assert out["status"] == "succeeded" and out["progress"] == 100 and out["finished_at"]
    detail = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    task_row_result = await _task_result(app, task_id)
    assert task_row_result["ended_early"] is True and task_row_result["remaining"] == 2
    assert task_row_result["done"] == 1
    assert detail["status"] == "succeeded"
    assert (await _chapter(app, c3["id"])).content == ""
    assert (await client.post(f"/api/v1/tasks/{task_id}/finish")).status_code == 409


async def test_cancel_paused_batch(app, client, fake_llm):
    _, base, (c1, c2), _ = await _setup(client, ["", ""])
    task_id = (await _start(client, base, c1, c2, review_every=1)).json()["id"]
    await wait_task(client, task_id, statuses=PAUSED)
    resp = await client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    # 取消后可以发起新批量
    task, _ = await run_task(client, await _start(client, base, c1, c2))
    assert task["status"] == "succeeded"


async def test_cancel_after_paused_event_keeps_paused(app, client, fake_llm, monkeypatch):
    """取消落在 paused 产出之后（断点已落库）：仍按待审核收尾，不记为取消。"""
    gate = Gate()
    original_run = batch_service.run_batch_job

    async def run_then_block(job, ctx):
        async for event in original_run(job, ctx):
            yield event
            if event[0] == "paused":
                gate.reached.set()
                await gate.released.wait()

    monkeypatch.setattr(batch_service, "run_batch_job", run_then_block)
    _, base, (c1, c2), _ = await _setup(client, ["", ""])
    task_id = (await _start(client, base, c1, c2, review_every=1)).json()["id"]
    await gate.wait_reached()
    resp = await client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert resp.status_code == 200 and resp.json()["status"] == "paused"
    task = await wait_task(client, task_id, statuses=PAUSED)
    assert task["error"] == "" and task["step_cursor"] == 1


async def _task_result(app, task_id: int) -> dict:
    async with app.state.sessionmaker() as db:
        return (await db.get(Task, task_id)).result


# ---------- 范围锁与唯一性 ----------


async def test_range_lock_and_single_batch_per_project(app, client, fake_llm):
    _, base, (c1, c2, c3, c4), _ = await _setup(client, ["", "", "", ""])
    gate = Gate()
    fake_llm.push(["第一章", gate, "正文"])
    task_id = (await _start(client, base, c1, c3)).json()["id"]
    await gate.wait_reached()
    for path in ("generate", "polish"):
        resp = await client.post(f"/api/v1/chapters/{c2['id']}/{path}", json={})
        assert resp.status_code == 409
        assert resp.json()["detail"] == "该章节在批量生成范围内，请等待批量完成或暂停后再操作"
    # 当前正在写的章节：409（生成中）
    assert (await client.post(f"/api/v1/chapters/{c1['id']}/generate", json={})).status_code == 409
    # 范围外不受影响
    fake_llm.push("第四章正文")
    resp = await client.post(f"/api/v1/chapters/{c4['id']}/generate", json={})
    assert resp.status_code == 202
    # 同项目第二个批量
    resp = await _start(client, base, c4, c4, include_written=True)
    assert resp.status_code == 409
    # 手动编辑不受范围锁限制
    assert (await client.patch(f"{base}/chapters/{c3['id']}", json={"title": "改名"})).status_code == 200
    gate.release()
    # 人工维护被允许不代表在飞行的旧项目规划快照仍可提交；改名推进规划版本。
    assert (await wait_task(client, task_id))["status"] == "failed"
    assert (await _chapter(app, c1["id"])).content == ""


# ---------- 取消 / 中断 ----------


async def test_cancel_keeps_done_and_restores_current(app, client, fake_llm):
    _, base, (c1, c2), _ = await _setup(client, ["", "旧稿"])
    gate = Gate()
    fake_llm.push("第一章正文")
    fake_llm.push(["第二章半截", gate, "后半"])
    task_id = (await _start(client, base, c1, c2, include_written=True)).json()["id"]
    await gate.wait_reached()
    assert (await _chapter(app, c2["id"])).status == "generating"
    resp = await client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    assert (await _chapter(app, c1["id"])).content == "第一章正文"
    row = await _chapter(app, c2["id"])
    assert row.status == "draft" and row.content == "旧稿"
    assert (await client.get(f"/api/v1/tasks/{task_id}/output")).json()["text"] == "第二章半截"


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
async def test_interrupt_then_retry_resumes_analysis_phase(app, client, fake_llm):
    _, base, (c1, c2), _ = await _setup(client, ["", ""])
    gate = Gate()
    fake_llm.push("第一章正文")
    fake_llm.push([gate, analysis_json()])
    task_id = (await _start(client, base, c1, c2)).json()["id"]
    await gate.wait_reached()
    engine = app.state.task_engine
    await engine.stop()
    interrupted = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert interrupted["status"] == "interrupted"
    assert interrupted["checkpoint"]["phase"] == "analyze" and interrupted["checkpoint"]["current"] is None
    assert interrupted["step_cursor"] == 0  # 分析阶段中断：断点仍停在该章
    versions_before = (await client.get(f"{base}/chapters/{c1['id']}/versions")).json()
    await engine.start()

    fake_llm.push(analysis_json())
    fake_llm.push("第二章正文")
    fake_llm.push(analysis_json())
    retried, _ = await run_task(client, await client.post(f"/api/v1/tasks/{task_id}/retry"))
    assert retried["status"] == "succeeded"
    # 第 1 章只补做分析，不重写
    assert len(_gen_calls(fake_llm)) == 2
    assert (await _chapter(app, c1["id"])).content == "第一章正文"
    assert (await client.get(f"{base}/chapters/{c1['id']}/versions")).json() == versions_before
    assert [i["analysis"] for i in retried["checkpoint"]["items"]] == ["ok", "ok"]


async def test_interrupt_restores_current_chapter(app, client, fake_llm):
    _, base, (c1, c2), _ = await _setup(client, ["", ""])
    gate = Gate()
    fake_llm.push("第一章正文")
    fake_llm.push(["半截", gate, "后半"])
    task_id = (await _start(client, base, c1, c2)).json()["id"]
    await gate.wait_reached()
    engine = app.state.task_engine
    await engine.stop()
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert task["status"] == "interrupted" and task["checkpoint"]["current"]["chapter_id"] == c2["id"]
    row = await _chapter(app, c2["id"])
    assert row.status == "draft" and row.content == ""
    await engine.start()
    fake_llm.push("第二章正文")
    retried, _ = await run_task(client, await client.post(f"/api/v1/tasks/{task_id}/retry"))
    assert retried["status"] == "succeeded"
    assert (await _chapter(app, c1["id"])).content == "第一章正文"
    assert (await _chapter(app, c2["id"])).content == "第二章正文"
    assert len(_gen_calls(fake_llm)) == 3


async def test_crash_recovery_restores_current_chapter(app, client, fake_llm):
    """进程崩溃：下次 start 把遗留 running 批量置为 interrupted，按断点还原当前章。"""

    _, base, (c1,), _ = await _setup(client, ["旧稿"])
    engine = app.state.task_engine
    await engine.stop()
    task_id = (await _start(client, base, c1, c1, include_written=True)).json()["id"]
    async with app.state.sessionmaker() as db:
        await db.execute(update(Chapter).where(Chapter.id == c1["id"]).values(status="generating"))
        await db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(status="running", checkpoint={"current": {"chapter_id": c1["id"], "prev_status": "done"}})
        )
        await db.commit()
    engine.hubs.clear()
    await engine.start()
    assert (await client.get(f"/api/v1/tasks/{task_id}")).json()["status"] == "interrupted"
    row = await _chapter(app, c1["id"])
    assert row.status == "done" and row.content == "旧稿"


# ---------- 目标字数 ----------


async def test_target_words_from_plan_or_uniform(app, client, fake_llm):
    _, base, (c1,), _ = await _setup(client, [""])
    outline = (await client.post(f"{base}/outlines", json={"title": "纲", "chapter_no": 2})).json()
    plans = [{"title": "细纲一", "est_words": 2500}]
    assert (await client.put(f"{base}/outlines/{outline['id']}/plans", json={"plans": plans})).status_code == 200
    built = await client.post(f"{base}/outlines/{outline['id']}/plans/build")
    assert built.status_code == 200, built.text
    chapters = (await client.get(f"{base}/chapters")).json()
    planned = next(c for c in chapters if c["id"] != c1["id"])

    preview = (
        await client.post(
            f"{base}/batch-generate/preview",
            json={"start_chapter_id": c1["id"], "end_chapter_id": planned["id"], "target_words": 1200},
        )
    ).json()
    assert [(i["target_words"], i["words_source"]) for i in preview["items"]] == [(1200, "uniform"), (2500, "plan")]

    task, _ = await run_task(client, await _start(client, base, c1, planned, target_words=1200))
    assert task["status"] == "succeeded"
    prompts = [_prompt(c) for c in _gen_calls(fake_llm)]
    assert "约 1200 字" in prompts[0] and "约 2500 字" in prompts[1]

    # 统一字数覆盖全部章节
    fake_llm.calls.clear()
    task, _ = await run_task(
        client,
        await _start(client, base, planned, planned, include_written=True, target_words=1500, use_plan_words=False),
    )
    assert task["status"] == "succeeded"
    assert "约 1500 字" in _prompt(_gen_calls(fake_llm)[0])


# ---------- 预览与校验 ----------


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
async def test_preview_lists_and_warns(app, client, fake_llm):
    _, base, (c1, c2, c3), _ = await _setup(client, ["", "已写", ""])
    assert (await client.patch(base, json={"auto_analyze": False})).status_code == 200
    async with app.state.sessionmaker() as db:
        await db.execute(update(Chapter).where(Chapter.id == c3["id"]).values(status="generating"))
        await db.commit()
    body = {"start_chapter_id": c1["id"], "end_chapter_id": c3["id"], "target_words": 3000}
    preview = (await client.post(f"{base}/batch-generate/preview", json=body)).json()
    assert preview["total"] == 3
    assert [i["chapter_id"] for i in preview["items"]] == [c1["id"], c3["id"]]
    assert [s["chapter_id"] for s in preview["skipped"]] == [c2["id"]]
    assert preview["analyze"] is False and preview["estimated_minutes"] == 4
    assert any("第 3 章正在生成中" in w for w in preview["warnings"])
    assert any("关闭自动分析" in w for w in preview["warnings"])
    # 发起：范围内要生成的章节在生成中 → 409
    resp = await _start(client, base, c1, c3)
    assert resp.status_code == 409 and "第 3 章" in resp.json()["detail"]
    # 不创建任务
    assert (await client.get("/api/v1/tasks")).json() == []


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
async def test_preview_estimate_with_analysis(app, client, fake_llm):
    _, base, (c1, c2), _ = await _setup(client, ["", ""])
    body = {"start_chapter_id": c1["id"], "end_chapter_id": c2["id"], "target_words": 6000}
    preview = (await client.post(f"{base}/batch-generate/preview", json=body)).json()
    assert preview["analyze"] is True and preview["estimated_minutes"] == 10 and preview["warnings"] == []


async def test_validation_errors(app, client, fake_llm):
    _, base, (c1, c2), _ = await _setup(client, ["已写", "已写"])
    resp = await _start(client, base, c1, c2)
    assert resp.status_code == 400 and resp.json()["detail"] == "范围内没有需要生成的章节"
    resp = await _start(client, base, c2, c1)
    assert resp.status_code == 400 and resp.json()["detail"] == "起始章节不能晚于结束章节"
    other = await create_project(client, title="别的项目")
    oc = (await client.post(f"/api/v1/projects/{other['id']}/chapters", json={"title": "x"})).json()
    resp = await _start(client, base, c1, oc)
    assert resp.status_code == 400 and resp.json()["detail"] == "起止章节不存在或不属于该项目"
    resp = await _start(client, base, c1, c2, include_written=True, review_every=51)
    assert resp.status_code == 422
    assert fake_llm.calls == []


async def test_progress_monotonic_and_capped(app, client, fake_llm):
    _, base, (c1, c2), _ = await _setup(client, ["", ""])
    fake_llm.push("一" * 70)
    fake_llm.push("二" * 70)
    resp = await _start(client, base, c1, c2, target_words=300)
    events = await stream_task(client, resp.json()["id"])
    values = [d["progress"] for e, d in events if e == "progress" and d["status"] == "running"]
    assert values == sorted(values) and all(v <= 95 for v in values)
    assert events[-1][1]["task"]["progress"] == 100
