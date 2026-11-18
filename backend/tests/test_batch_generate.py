"""批量顺序生成（chapter.batch）：跳过 / 重写、顺序与上下文、任务内分析、失败重试与续跑、审核点、范围锁、
取消 / 中断、目标字数、预览与校验，以及引擎对 paused 状态的处理。"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import update

from app.ai.providers import AIProviderError
from app.core.errors import BadRequestError, ConflictError
from app.models import AIConfig, Chapter, Project, Scene, Task
from app.services import ai_config_service, batch_service, generation_service, project_lifecycle
from tests.helpers import (
    Gate,
    analysis_json,
    chapter_title_save_body,
    create_ai_config,
    create_project,
    register,
    run_task,
    stream_task,
    task_params,
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
    engine = app.state.task_engine
    await engine.stop()
    response = await _start(client, base, c1, c3)
    assert response.status_code == 202, response.text
    task_ref = response.json()
    stream = asyncio.create_task(stream_task(client, task_ref))
    try:
        # 先注册订阅再开调度，事件不回放，确保逐章事件的计数测量完整。
        await wait_until(lambda: (
            (hub := engine.hubs.get(task_ref["id"])) is not None
            and hub.statistics_key == task_ref["task_key"] and hub.subscriber_count > 0
        ))
        await engine.start()
        events = await stream
    finally:
        if not stream.done():
            stream.cancel()
            await asyncio.gather(stream, return_exceptions=True)
        if not engine.running:
            await engine.start()
    task = events[-1][1]["task"]
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
    output = await client.get(f"/api/v1/tasks/{task['id']}/output", params=task_params(task))
    assert output.json()["text"] == "第二章正文"


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
    resp = await client.post(f"/api/v1/tasks/{task['id']}/retry", params=task_params(task))
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
    usage = (await client.get(f"{base}/chapters/{c1['id']}/ai-usage")).json()
    assert {(row["kind"], row["status"]) for row in usage["items"]} == {
        ("chapter.generate", "failed"), ("chapter.generate.retry", "succeeded"),
    }


async def test_build_error_not_retried_and_restores_chapter(app, client, fake_llm, monkeypatch):
    """第二章组装阶段业务错误不重试，第一章保留、当前章还原。"""
    builds: list[int] = []
    original_build = generation_service.build_chapter_job

    async def counting_build(db, **kw):
        builds.append(kw["chapter_id"])
        if len(builds) == 2:
            raise BadRequestError("测试章节组装失败")
        return await original_build(db, **kw)

    monkeypatch.setattr(generation_service, "build_chapter_job", counting_build)
    _, base, (c1, c2), _ = await _setup(client, ["", ""])
    fake_llm.push("第一章正文")
    task_ref = (await _start(client, base, c1, c2)).json()
    task = await wait_task(client, task_ref, within=20)
    assert task["status"] == "failed" and task["step_cursor"] == 1, (task, builds, fake_llm.calls)
    assert len(fake_llm.calls) == 1
    assert builds == [c1["id"], c2["id"]]  # 第 2 章只组装一次，不重试
    first, second = await _chapter(app, c1["id"]), await _chapter(app, c2["id"])
    assert first.status == "done" and first.content == "第一章正文"
    assert second.status == "draft" and second.content == ""
    assert first.generation_claim_key is None and second.generation_claim_key is None


async def test_model_removed_during_first_stream_blocks_commit_and_releases_claim(app, client, fake_llm):
    """模型流中撤销原配置后，不得保存旧输出或推进游标。"""
    _, base, (c1, c2), _ = await _setup(client, ["", ""])
    cfg = (await client.get("/api/v1/ai-configs")).json()[0]
    gate = Gate()
    fake_llm.push(["第一章", gate, "正文"])
    task_ref = (await _start(client, base, c1, c2)).json()
    try:
        await gate.wait_reached()
        original = await _chapter(app, c1["id"])
        assert original.status == "generating" and original.generation_claim_key is not None
        assert original.generation_claim_task_key == task_ref["task_key"]
        assert (await client.delete(f"/api/v1/ai-configs/{cfg['id']}")).status_code == 204
    finally:
        gate.release()
    task = await wait_task(client, task_ref)
    assert task["status"] == "failed" and task["step_cursor"] == 0
    assert len(fake_llm.calls) == 1
    for chapter_id in (c1["id"], c2["id"]):
        chapter = await _chapter(app, chapter_id)
        assert chapter.status == "draft" and chapter.content == ""
        assert chapter.generation_claim_key is None and chapter.generation_claim_task_key is None
        assert (await client.get(f"{base}/chapters/{chapter_id}/versions")).json() == []
    assert task["checkpoint"]["current"] is None


async def test_future_chapter_deleted_during_current_proposal_invalidates_planning_snapshot(app, client, fake_llm):
    _, base, (c1, c2, c3), _ = await _setup(client, ["", "", ""])
    gate = Gate()
    fake_llm.push(["第一章", gate, "正文"])
    fake_llm.push("第三章正文")
    task_ref = (await _start(client, base, c1, c3)).json()
    await gate.wait_reached()
    assert (await client.delete(f"{base}/chapters/{c2['id']}")).status_code == 204
    gate.release()
    task = await wait_task(client, task_ref)
    # 场景设计 §8：删除章会推进项目规划版本，不能给已发送的当前章旧消息重签。
    # 这不是在下章构建前发现缺章的普通 skip；当前未提交提案必须失败且不半写。
    assert task["status"] == "failed" and task["step_cursor"] == 0
    # 引擎输出门禁在发布 delta 前即失败关闭（scene_context_changed），runner 不再有机会记录本项；
    # 断点保留未完成的 pending 且已释放占用，重试会重新生成该章。
    assert task["checkpoint"]["items"][0]["state"] == "pending" and task["checkpoint"]["current"] is None
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
    task_ref = (await _start(client, base, c1, c2, review_every=1)).json()
    task_id = task_ref["id"]
    await wait_task(client, task_ref, statuses=PAUSED)
    fake_llm.push("范围外的第三章")
    single, _ = await run_task(client, await client.post(f"/api/v1/chapters/{c3['id']}/generate", json={}))
    assert single["status"] == "succeeded"
    assert (await client.get(f"/api/v1/tasks/{task_id}", params=task_params(task_ref))).json()["status"] == "paused"


async def test_review_pause_then_resume_uses_edits(app, client, fake_llm):
    _, base, chs, _ = await _setup(client, ["", "", "", ""])
    c1, c2, c3, c4 = chs
    fake_llm.push("第一章正文")
    fake_llm.push("第二章正文")
    resp = await _start(client, base, c1, c4, review_every=2)
    assert resp.status_code == 202
    task_ref = resp.json()
    task_id = task_ref["id"]
    events = await stream_task(client, task_ref)
    assert events[-1][0] == "paused"
    paused = await wait_task(client, task_ref, statuses=PAUSED)
    assert paused["stage"] == "待审核" and paused["finished_at"] is None
    assert paused["checkpoint"]["since_review"] == 0
    engine = app.state.task_engine
    await wait_until(lambda: task_id not in engine._running)
    # 订阅已暂停的任务：快照 + paused
    assert [e for e, _ in await stream_task(client, task_ref)] == ["snapshot", "paused"]
    # 「进行中」列表包含待审核；不能删除；重启恢复不影响
    active = (await client.get("/api/v1/tasks", params={"active": "true"})).json()
    assert [t["id"] for t in active] == [task_id]
    resp = await client.delete(f"/api/v1/tasks/{task_id}", params=task_params(task_ref))
    assert resp.status_code == 409 and "待审核" in resp.json()["detail"]
    await engine.recover()
    assert (await client.get(f"/api/v1/tasks/{task_id}", params=task_params(task_ref))).json()["status"] == "paused"
    # 待审核期间不能发起新批量
    resp = await _start(client, base, c3, c4)
    assert resp.status_code == 409 and resp.json()["detail"] == "已有进行中或待审核的批量生成"

    # 审核期间修改第 2 章，继续后第 3 章能看到
    assert (
        await client.patch(f"{base}/chapters/{c2['id']}", json={"content": "审核修改后的第二章"})
    ).status_code == 200
    fake_llm.push("第三章正文")
    fake_llm.push("第四章正文")
    resp = await client.post(f"/api/v1/tasks/{task_id}/resume", params=task_params(task_ref))
    assert resp.status_code == 202 and resp.json()["id"] == task_id and resp.json()["status"] == "queued"
    final = await wait_task(client, task_ref)
    assert final["status"] == "succeeded"  # 最后一章写完不再暂停
    gens = _gen_calls(fake_llm)
    assert len(gens) == 4
    assert "审核修改后的第二章" in _prompt(gens[2])
    assert (await _chapter(app, c4["id"])).content == "第四章正文"
    assert (await _task_result(app, task_id))["done"] == 4
    resp = await client.post(f"/api/v1/tasks/{task_id}/resume", params=task_params(task_ref))
    assert resp.status_code == 409


async def test_review_pause_frees_range_and_finish(app, client, fake_llm):
    _, base, (c1, c2, c3), _ = await _setup(client, ["", "", ""])
    fake_llm.push("第一章正文")
    resp = await _start(client, base, c1, c3, review_every=1)
    task_ref = resp.json()
    task_id = task_ref["id"]
    await wait_task(client, task_ref, statuses=PAUSED)
    # 待审核不锁范围：范围内章节可单独生成
    fake_llm.push("单独生成的第二章")
    single, _ = await run_task(client, await client.post(f"/api/v1/chapters/{c2['id']}/generate", json={}))
    assert single["status"] == "succeeded"
    # 结束：以已完成收尾
    resp = await client.post(f"/api/v1/tasks/{task_id}/finish", params=task_params(task_ref))
    assert resp.status_code == 200
    out = resp.json()
    assert out["status"] == "succeeded" and out["progress"] == 100 and out["finished_at"]
    detail = (await client.get(f"/api/v1/tasks/{task_id}", params=task_params(task_ref))).json()
    task_row_result = await _task_result(app, task_id)
    assert task_row_result["ended_early"] is True and task_row_result["remaining"] == 2
    assert task_row_result["done"] == 1
    assert detail["status"] == "succeeded"
    assert (await _chapter(app, c3["id"])).content == ""
    assert (await client.post(f"/api/v1/tasks/{task_id}/finish", params=task_params(task_ref))).status_code == 409


async def test_cancel_paused_batch(app, client, fake_llm):
    _, base, (c1, c2), _ = await _setup(client, ["", ""])
    task_ref = (await _start(client, base, c1, c2, review_every=1)).json()
    task_id = task_ref["id"]
    await wait_task(client, task_ref, statuses=PAUSED)
    resp = await client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task_ref))
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
    task_ref = (await _start(client, base, c1, c2, review_every=1)).json()
    task_id = task_ref["id"]
    await gate.wait_reached()
    resp = await client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task_ref))
    assert resp.status_code == 200 and resp.json()["status"] == "paused"
    task = await wait_task(client, task_ref, statuses=PAUSED)
    assert task["error"] == "" and task["step_cursor"] == 1


async def _task_result(app, task_id: int) -> dict:
    async with app.state.sessionmaker() as db:
        return (await db.get(Task, task_id)).result


# ---------- 范围锁与唯一性 ----------


async def test_range_lock_and_single_batch_per_project(app, client, fake_llm):
    _, base, (c1, c2, c3, c4), _ = await _setup(client, ["", "", "", ""])
    gate = Gate()
    fake_llm.push(["第一章", gate, "正文"])
    task_ref = (await _start(client, base, c1, c3)).json()
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
    url = f"{base}/chapters/{c3['id']}"
    rename = await chapter_title_save_body(client, url, "改名")
    assert (await client.patch(f"{url}/editor-title", json=rename)).status_code == 200
    gate.release()
    # 人工维护被允许不代表在飞行的旧项目规划快照仍可提交；改名推进规划版本。
    assert (await wait_task(client, task_ref))["status"] == "failed"
    assert (await _chapter(app, c1["id"])).content == ""


# ---------- 取消 / 中断 ----------


async def test_cancel_keeps_done_and_restores_current(app, client, fake_llm):
    _, base, (c1, c2), _ = await _setup(client, ["", "旧稿"])
    gate = Gate()
    fake_llm.push("第一章正文")
    fake_llm.push(["第二章半截", gate, "后半"])
    task_ref = (await _start(client, base, c1, c2, include_written=True)).json()
    task_id = task_ref["id"]
    await gate.wait_reached()
    assert (await _chapter(app, c2["id"])).status == "generating"
    resp = await client.post(f"/api/v1/tasks/{task_id}/cancel", params=task_params(task_ref))
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    assert (await _chapter(app, c1["id"])).content == "第一章正文"
    row = await _chapter(app, c2["id"])
    assert row.status == "draft" and row.content == "旧稿"
    output = await client.get(f"/api/v1/tasks/{task_id}/output", params=task_params(task_ref))
    assert output.json()["text"] == "第二章半截"


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
async def test_interrupt_then_retry_resumes_analysis_phase(app, client, fake_llm):
    _, base, (c1, c2), _ = await _setup(client, ["", ""])
    gate = Gate()
    fake_llm.push("第一章正文")
    fake_llm.push([gate, analysis_json()])
    task_ref = (await _start(client, base, c1, c2)).json()
    task_id = task_ref["id"]
    await gate.wait_reached()
    assert len(fake_llm.calls) == 2  # 第一章分析已进入发送，结果未知。
    engine = app.state.task_engine
    await engine.stop()
    interrupted = (await client.get(f"/api/v1/tasks/{task_id}", params=task_params(task_ref))).json()
    assert interrupted["status"] == "interrupted"
    assert interrupted["checkpoint"]["phase"] == "analyze" and interrupted["checkpoint"]["current"] is None
    assert interrupted["step_cursor"] == 0  # 分析阶段中断：断点仍停在该章
    versions_before = (await client.get(f"{base}/chapters/{c1['id']}/versions")).json()
    await engine.start()

    fake_llm.push("第二章正文")
    fake_llm.push(analysis_json())
    response = await client.post(f"/api/v1/tasks/{task_id}/retry", params=task_params(task_ref))
    retried, events = await run_task(client, response)
    assert retried["status"] == "succeeded"
    # 第 1 章结果未知，不重复分析也不重写；第 2 章仍正常生成和分析。
    assert len(_gen_calls(fake_llm)) == 2
    assert len(fake_llm.calls) == 4
    assert (await _chapter(app, c1["id"])).content == "第一章正文"
    assert (await _chapter(app, c2["id"])).content == "第二章正文"
    assert (await client.get(f"{base}/chapters/{c1['id']}/versions")).json() == versions_before
    items = retried["checkpoint"]["items"]
    assert [i["analysis"] for i in items] == ["failed", "ok"]
    assert "未自动重复" in items[0]["warning"]
    assert [item["chapter_id"] for item in events[-1][1]["result"]["analysis_failed"]] == [c1["id"]]


async def test_interrupt_restores_current_chapter(app, client, fake_llm):
    _, base, (c1, c2), _ = await _setup(client, ["", ""])
    gate = Gate()
    fake_llm.push("第一章正文")
    fake_llm.push(["半截", gate, "后半"])
    task_ref = (await _start(client, base, c1, c2)).json()
    task_id = task_ref["id"]
    await gate.wait_reached()
    async with app.state.sessionmaker() as db:
        original = await db.get(Task, task_id)
        snapshot = batch_service.capture_abort_snapshot(original, recovery=True)
        assert snapshot.claim is not None and snapshot.claim.chapter_id == c2["id"]
        assert (await db.get(Chapter, c2["id"])).generation_claim_key == snapshot.claim.claim_key
    engine = app.state.task_engine
    await engine.stop()
    task = (await client.get(f"/api/v1/tasks/{task_id}", params=task_params(task_ref))).json()
    assert task["status"] == "interrupted" and task["checkpoint"]["current"] is None
    row = await _chapter(app, c2["id"])
    assert row.status == "draft" and row.content == "" and row.generation_claim_key is None
    await engine.start()
    retry_gate = Gate()
    fake_llm.push([retry_gate, "第二章正文"])
    response = await client.post(f"/api/v1/tasks/{task_id}/retry", params=task_params(task_ref))
    assert response.status_code == 202, response.text
    retry_ref = response.json()
    try:
        await retry_gate.wait_reached()
        newer = await _chapter(app, c2["id"])
        assert newer.status == "generating" and newer.generation_claim_key != snapshot.claim.claim_key
        with pytest.raises(ConflictError):
            await batch_service.restore_abort_snapshot(engine.ctx, snapshot)
        still_newer = await _chapter(app, c2["id"])
        assert still_newer.status == "generating" and still_newer.generation_claim_key == newer.generation_claim_key
    finally:
        retry_gate.release()
    retried = await wait_task(client, retry_ref)
    assert retried["status"] == "succeeded"
    assert (await _chapter(app, c1["id"])).content == "第一章正文"
    assert (await _chapter(app, c2["id"])).content == "第二章正文"
    assert len(_gen_calls(fake_llm)) == 3


async def test_crash_recovery_restores_current_chapter(app, client, fake_llm):
    """只凭真实运行中的原 claim 模拟崩溃，恢复时可还原当前章。"""

    _, base, (c1,), _ = await _setup(client, ["旧稿"])
    async with app.state.sessionmaker() as db:
        await db.execute(update(Chapter).where(Chapter.id == c1["id"]).values(status="done"))
        await db.commit()
    engine = app.state.task_engine
    gate = Gate()
    fake_llm.push([gate, "不应保存"])
    task_ref = (await _start(client, base, c1, c1, include_written=True)).json()
    task_id = task_ref["id"]
    await gate.wait_reached()
    async with app.state.sessionmaker() as db:
        snapshot = batch_service.capture_abort_snapshot(await db.get(Task, task_id), recovery=True)
        assert snapshot.claim is not None and snapshot.claim.chapter_id == c1["id"]
        assert snapshot.claim.prev_status == "done"
        assert (await db.get(Chapter, c1["id"])).generation_claim_key == snapshot.claim.claim_key
    await engine.stop()
    gate.release()
    async with app.state.sessionmaker() as db:
        # 恢复在真实调用中捕获的持久事实，模拟崩溃未执行正常排空。
        await db.execute(update(Chapter).where(Chapter.id == c1["id"]).values(status="generating"))
        await db.execute(update(Chapter).where(Chapter.id == c1["id"]).values(
            generation_claim_key=snapshot.claim.claim_key, generation_claim_task_key=snapshot.claim.task_key,
            generation_claim_prev_status=snapshot.claim.prev_status,
        ))
        await db.execute(
            update(Task)
            .where(Task.id == task_id, Task.statistics_key == task_ref["task_key"])
            .values(status="running", execution_key=snapshot.execution_key,
                    checkpoint=snapshot.checkpoint, step_cursor=snapshot.cursor)
        )
        await db.commit()
    engine.hubs.clear()
    await engine.start()
    recovered = await client.get(f"/api/v1/tasks/{task_id}", params=task_params(task_ref))
    assert recovered.json()["status"] == "interrupted"
    row = await _chapter(app, c1["id"])
    assert row.status == "done" and row.content == "旧稿" and row.generation_claim_key is None
    assert len(fake_llm.calls) == 1


async def test_crash_recovery_keeps_unproven_legacy_chapter_unchanged(app, client, fake_llm):
    _, base, (c1,), _ = await _setup(client, ["旧稿"])
    engine = app.state.task_engine
    await engine.stop()
    task_ref = (await _start(client, base, c1, c1, include_written=True)).json()
    async with app.state.sessionmaker() as db:
        await db.execute(update(Chapter).where(Chapter.id == c1["id"]).values(status="generating"))
        await db.execute(update(Task).where(
            Task.id == task_ref["id"], Task.statistics_key == task_ref["task_key"],
        ).values(status="running", checkpoint={"current": {"chapter_id": c1["id"], "prev_status": "done"}}))
        await db.commit()
    await engine.start()
    recovered = await client.get(f"/api/v1/tasks/{task_ref['id']}", params=task_params(task_ref))
    assert recovered.json()["status"] == "running"
    row = await _chapter(app, c1["id"])
    assert row.status == "generating" and row.content == "旧稿" and row.generation_claim_key is None
    retry = await client.post(f"/api/v1/tasks/{task_ref['id']}/retry", params=task_params(task_ref))
    assert retry.status_code == 409 and fake_llm.calls == []


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


@pytest.mark.parametrize("settings_overrides", [AUTO_ON])
async def test_preview_cost_lists_generation_and_analysis_without_sending(app, client, fake_llm):
    _, base, (chapter,), _ = await _setup(client, [""])
    body = {"start_chapter_id": chapter["id"], "end_chapter_id": chapter["id"], "target_words": 2300}
    response = await client.post(f"{base}/batch-generate/preview", json=body)
    assert response.status_code == 200, response.text
    cost = response.json()["cost"]
    assert cost["status"] == "missing_price"
    assert cost["estimated_amount"] is None
    assert [step["kind"] for step in cost["steps"]] == ["generation", "analysis"]
    assert cost["steps"][0]["estimated_output_tokens"] == 2300

    assert (await client.patch(base, json={"auto_analyze": False})).status_code == 200
    response = await client.post(f"{base}/batch-generate/preview", json={**body, "instruction": "补充要求"})
    assert response.status_code == 200, response.text
    updated = response.json()["cost"]["steps"]
    assert [step["kind"] for step in updated] == ["generation"]
    assert updated[0]["estimated_input_tokens"] > cost["steps"][0]["estimated_input_tokens"]
    assert not fake_llm.calls
    assert (await client.get("/api/v1/tasks")).json() == []


async def test_preview_cost_counts_one_generation_per_scene(app, client, fake_llm):
    _, base, (chapter,), _ = await _setup(client, [""])
    async with app.state.sessionmaker() as db:
        db.add(Scene(chapter_id=chapter["id"], sort=1, title="第二场"))
        await db.commit()

    response = await client.post(f"{base}/batch-generate/preview", json={
        "start_chapter_id": chapter["id"], "end_chapter_id": chapter["id"], "target_words": 3001,
    })
    assert response.status_code == 200, response.text
    generation = response.json()["cost"]["steps"][0]
    assert generation["kind"] == "generation" and generation["calls"] == 2
    assert generation["estimated_output_tokens"] == 3001
    assert not fake_llm.calls


async def test_preview_price_uses_selected_model_and_projects_matching_budgets(app, client, fake_llm):
    from app.services import ai_usage_storage

    project, base, (chapter,), _ = await _setup(client, [""])
    config = await create_ai_config(client, name="其他模型", model="other", is_default=False)
    async with app.state.sessionmaker() as db:
        cfg = await db.get(AIConfig, config["id"])
        owner_id = (await db.get(Project, project["id"])).user_id
        await ai_usage_storage.set_price(
            db, cfg, currency="CNY", input_price_nano=10**9, output_price_nano=10**9,
        )
        await ai_usage_storage.set_budget(
            db, user_id=owner_id, currency="CNY", limit_nano=10**6, threshold_percent=75,
        )
        await ai_usage_storage.set_budget(
            db, project_id=project["id"], currency="CNY", limit_nano=10**6, threshold_percent=75,
        )
        await db.commit()

    route = f"{base}/batch-generate/preview"
    body = {"start_chapter_id": chapter["id"], "end_chapter_id": chapter["id"]}
    default = await client.post(route, json=body)
    assert default.status_code == 200, default.text
    assert default.json()["cost"]["status"] == "missing_price"
    response = await client.post(route, json={**body, "ai_config_id": config["id"]})
    assert response.status_code == 200, response.text
    cost = response.json()["cost"]
    assert cost["status"] == "estimated" and cost["currency"] == "CNY"
    assert cost["price_period"] == "fixed" and cost["price_timezone"] is None
    assert cost["assumes_uncached_input"] is False
    expected = sum(
        Decimal(step["estimated_input_tokens"] + step["estimated_output_tokens"]) / 10**6
        for step in cost["steps"]
    )
    assert Decimal(cost["estimated_amount"]) == expected
    assert {row["scope"] for row in cost["budgets"]} == {"account", "project"}
    assert all(row["state"] == "exceeded" and Decimal(row["projected_spent"]) == expected for row in cost["budgets"])
    assert not fake_llm.calls


async def test_batch_preview_uses_current_tariff_and_budget_projection(app, client, fake_llm, monkeypatch):
    from app.services import ai_usage_storage

    project, base, (chapter,), _ = await _setup(client, [""])
    async with app.state.sessionmaker() as db:
        owner_id = (await db.get(Project, project["id"])).user_id
        cfg = await ai_config_service.resolve_config(db, owner_id, None)
        await ai_usage_storage.set_price(
            db, cfg, currency="CNY", input_price_nano=3 * 10**9, output_price_nano=3 * 10**9,
            off_peak=ai_usage_storage.OffPeakPrice(
                start="23:00", end="07:00", input_price_nano=10**9, output_price_nano=10**9,
                cached_input_price_nano=0,
                additional_windows=(ai_usage_storage.OffPeakWindow(start="13:00", end="14:00"),),
            ),
        )
        await ai_usage_storage.set_budget(db, user_id=owner_id, currency="CNY", limit_nano=30_000_000,
                                          threshold_percent=80)
        await db.commit()

    route = f"{base}/batch-generate/preview"
    body = {"start_chapter_id": chapter["id"], "end_chapter_id": chapter["id"]}
    for hour, period, rate, state in (
        (15, "off_peak", 1, "normal"), (5, "off_peak", 1, "normal"), (6, "peak", 3, "exceeded"),
    ):
        now = datetime(2026, 11, 18, hour, tzinfo=UTC)
        monkeypatch.setattr(project_lifecycle, "now_utc", lambda now=now: now)
        response = await client.post(route, json=body)
        assert response.status_code == 200, response.text
        cost = response.json()["cost"]
        assert cost["price_period"] == period and cost["price_timezone"] == "Asia/Shanghai"
        assert cost["status"] == "estimated" and cost["currency"] == "CNY"
        assert cost["assumes_uncached_input"] is (period == "off_peak")
        tokens = sum(item["estimated_input_tokens"] + item["estimated_output_tokens"] for item in cost["steps"])
        assert Decimal(cost["estimated_amount"]) == Decimal(tokens * rate) / 10**6
        assert cost["budgets"][0]["state"] == state
        assert cost["budgets"][0]["projected_spent"] == cost["estimated_amount"]
    assert not fake_llm.calls


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
    events = await stream_task(client, resp.json())
    values = [d["progress"] for e, d in events if e == "progress" and d["status"] == "running"]
    assert values == sorted(values) and all(v <= 95 for v in values)
    assert events[-1][1]["task"]["progress"] == 100
