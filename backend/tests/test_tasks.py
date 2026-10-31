"""后台任务引擎：订阅、取消、重启恢复、重试、并发上限、资源互斥、越权与保留裁剪。"""

import asyncio
import time

import pytest
from sqlalchemy import func, select, update

from app.ai.providers import AIProviderError
from app.models import Chapter, Task
from app.services import task_service
from app.services.task_engine.hub import TaskHub
from tests.helpers import (
    Gate,
    create_ai_config,
    create_project,
    parse_sse,
    register,
    run_task,
    stream_task,
    streamed_text,
    wait_task,
    wait_until,
)


async def _setup(client, *, chapters: int = 1, content: str = ""):
    await register(client)
    p = await create_project(client)
    await create_ai_config(client)
    base = f"/api/v1/projects/{p['id']}"
    chs = []
    for i in range(chapters):
        chs.append((await client.post(f"{base}/chapters", json={"title": f"章{i + 1}", "content": content})).json())
    return p, base, chs


def _generate(client, ch, **body):
    return client.post(f"/api/v1/chapters/{ch['id']}/generate", json=body)


async def _chapter(app, chapter_id: int) -> Chapter:
    async with app.state.sessionmaker() as db:
        return await db.get(Chapter, chapter_id)


# ---------- AC2：快照 + 增量，不重复不遗漏 ----------


def test_hub_snapshot_then_delta_no_gap():
    hub = TaskHub(1, status="running")
    hub.append("甲")
    text, sub = hub.subscribe()
    hub.append("乙")
    hub.close("done", {})
    frames = []
    while not sub.queue.empty():
        frames.append(sub.queue.get_nowait())
    assert text == "甲"
    assert frames == [("delta", {"text": "乙"}), ("done", {})]
    assert hub.closed and hub.subscriber_count == 0


async def test_subscribe_mid_stream_two_subscribers(app, client, fake_llm):
    _, _, (ch,) = await _setup(client)
    gate = Gate()
    fake_llm.push(["甲甲", gate, "乙乙", "丙丙"])
    task = (await _generate(client, ch, target_words=300)).json()
    await gate.wait_reached()
    # 运行中可直接查看已输出文本
    assert (await client.get(f"/api/v1/tasks/{task['id']}/output")).json()["text"] == "甲甲"

    engine = app.state.task_engine
    first = asyncio.ensure_future(client.get(f"/api/v1/tasks/{task['id']}/stream"))
    second = asyncio.ensure_future(client.get(f"/api/v1/tasks/{task['id']}/stream"))
    await wait_until(lambda: engine.hubs[task["id"]].subscriber_count == 2)
    gate.release()
    for resp in await asyncio.gather(first, second):
        events = parse_sse(resp.text)
        assert events[0] == ("snapshot", events[0][1])
        assert events[0][1]["text"] == "甲甲"
        assert events[0][1]["task"]["status"] == "running"
        assert "".join(d["text"] for e, d in events if e == "delta") == "乙乙丙丙"
        assert streamed_text(events) == "甲甲乙乙丙丙"
        assert events[-1][0] == "done"
        assert events[-1][1]["result"]["chapter"]["content"] == "甲甲乙乙丙丙"
        assert [e for e, _ in events].count("done") == 1

    # 已结束的任务：快照取自库里的 output_text，紧接着 done
    events = await stream_task(client, task["id"])
    assert [e for e, _ in events] == ["snapshot", "done"]
    assert events[0][1]["text"] == "甲甲乙乙丙丙"
    assert events[0][1]["task"]["status"] == "succeeded"


async def test_disconnect_does_not_stop_task(app, client, fake_llm):
    """关闭页面 = 订阅断开：任务照常完成并落库（章节与向导）。"""
    p, base, (ch,) = await _setup(client)
    gate = Gate()
    fake_llm.push(["开头", gate, "结尾"])
    task = (await _generate(client, ch)).json()
    await gate.wait_reached()
    engine = app.state.task_engine
    sub = engine.subscribe(task["id"])
    event, data = await sub.__anext__()
    assert event == "snapshot" and data["text"] == "开头"
    await sub.aclose()  # 模拟客户端断开
    assert engine.hubs[task["id"]].subscriber_count == 0
    gate.release()
    assert (await wait_task(client, task["id"]))["status"] == "succeeded"
    assert (await client.get(f"{base}/chapters/{ch['id']}")).json()["content"] == "开头结尾"
    assert fake_llm.aborted == 0

    # 向导：从未订阅也照常写入结果
    fake_llm.push('[{"title": "星际联邦", "content": "十二星系"}]')
    w = (await client.post(f"{base}/wizard/world")).json()
    assert (await wait_task(client, w["id"]))["status"] == "succeeded"
    assert [x["title"] for x in (await client.get(f"{base}/world")).json()] == ["星际联邦"]


async def test_stream_heartbeat(app, client, fake_llm):
    _, _, (ch,) = await _setup(client)
    app.state.task_engine.heartbeat_seconds = 0.02
    gate = Gate()
    fake_llm.push([gate, "正文"])
    task = (await _generate(client, ch)).json()
    await gate.wait_reached()
    pending = asyncio.ensure_future(client.get(f"/api/v1/tasks/{task['id']}/stream"))
    await asyncio.sleep(0.1)
    gate.release()
    resp = await pending
    assert ": ping" in resp.text
    assert parse_sse(resp.text)[-1][0] == "done"


# ---------- AC3：取消 ----------


async def test_cancel_running_task(app, client, fake_llm):
    _, base, (ch,) = await _setup(client, content="旧稿")
    url = f"{base}/chapters/{ch['id']}"
    gate = Gate()
    fake_llm.push(["第一块", gate, "第二块"])
    task = (await _generate(client, ch)).json()
    await gate.wait_reached()
    engine = app.state.task_engine
    subscriber = asyncio.ensure_future(client.get(f"/api/v1/tasks/{task['id']}/stream"))
    await wait_until(lambda: engine.hubs[task["id"]].subscriber_count == 1)

    started = time.monotonic()
    resp = await client.post(f"/api/v1/tasks/{task['id']}/cancel")
    assert time.monotonic() - started < 1
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "cancelled" and out["error"] == "任务已取消" and out["stage"] == "已取消"
    assert fake_llm.aborted == 1  # AI 流被中断

    events = parse_sse((await subscriber).text)
    assert events[-1][0] == "error"
    assert events[-1][1]["detail"] == "任务已取消" and events[-1][1]["task"]["status"] == "cancelled"

    saved = (await client.get(url)).json()
    assert saved["status"] == "draft" and saved["content"] == "旧稿"
    assert [v["source"] for v in (await client.get(f"{url}/versions")).json()] == ["manual"]
    logs = (await client.get(f"{url}/generation-logs")).json()
    assert [(lg["status"], lg["task_id"]) for lg in logs] == [("cancelled", task["id"])]
    assert (await client.get(f"/api/v1/tasks/{task['id']}/output")).json()["text"] == "第一块"

    # 已结束的任务不能再取消
    resp = await client.post(f"/api/v1/tasks/{task['id']}/cancel")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "任务已结束，无法取消"


@pytest.mark.parametrize("settings_overrides", [{"task_max_per_user": 1}])
async def test_cancel_queued_task(app, client, fake_llm):
    _, _, (ch1, ch2) = await _setup(client, chapters=2)
    gate = Gate()
    fake_llm.push([gate, "第一章"])
    t1 = (await _generate(client, ch1)).json()
    await gate.wait_reached()
    t2 = (await _generate(client, ch2)).json()
    assert t2["status"] == "queued" and t2["queue_position"] == 0
    assert (await _chapter(app, ch2["id"])).status == "generating"
    # 排队期间章节已被抢占：局部改写预览同样 409
    resp = await client.post(f"/api/v1/chapters/{ch2['id']}/rewrite", json={"op": "continue", "start": 0, "end": 0})
    assert resp.status_code == 409, resp.text

    resp = await client.post(f"/api/v1/tasks/{t2['id']}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled" and resp.json()["queue_position"] is None
    assert (await _chapter(app, ch2["id"])).status == "draft"

    gate.release()
    assert (await wait_task(client, t1["id"]))["status"] == "succeeded"
    await asyncio.sleep(0.05)
    assert len(fake_llm.calls) == 1  # 已取消的排队任务不会再被执行
    assert (await client.get(f"/api/v1/tasks/{t2['id']}")).json()["status"] == "cancelled"


# ---------- AC4：重启恢复 + 重试 ----------


@pytest.mark.parametrize("settings_overrides", [{"task_max_per_user": 1}])
async def test_stop_interrupts_running_and_keeps_queued(app, client, fake_llm):
    _, base, (ch1, ch2) = await _setup(client, chapters=2, content="旧稿")
    gate = Gate()
    fake_llm.push(["第一块", gate])
    fake_llm.push("乙章正文")
    t1 = (await _generate(client, ch1)).json()
    await gate.wait_reached()
    t2 = (await _generate(client, ch2)).json()
    assert t2["status"] == "queued"

    engine = app.state.task_engine
    await engine.stop()
    a = (await client.get(f"/api/v1/tasks/{t1['id']}")).json()
    assert a["status"] == "interrupted" and a["error"] == "服务重启，任务已中断" and a["finished_at"]
    assert (await client.get(f"/api/v1/tasks/{t1['id']}/output")).json()["text"] == "第一块"
    ch1_row = await _chapter(app, ch1["id"])
    assert ch1_row.status == "draft" and ch1_row.content == "旧稿"
    assert (await client.get(f"/api/v1/tasks/{t2['id']}")).json()["status"] == "queued"

    await engine.start()
    b = await wait_task(client, t2["id"])
    assert b["status"] == "succeeded"
    assert (await _chapter(app, ch2["id"])).content == "乙章正文"

    fake_llm.push("甲章正文")
    resp = await client.post(f"/api/v1/tasks/{t1['id']}/retry")
    assert resp.status_code == 202
    retried, _ = await run_task(client, resp)
    assert retried["retry_of_id"] == t1["id"] and retried["id"] != t1["id"]
    assert retried["status"] == "succeeded" and retried["kind"] == "chapter.generate"
    assert (await _chapter(app, ch1["id"])).content == "甲章正文"

    # 成功的任务不能重试
    resp = await client.post(f"/api/v1/tasks/{retried['id']}/retry")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "只有失败、已取消或已中断的任务可以重试"


async def test_start_recovers_crashed_running_task(app, client, fake_llm):
    """进程崩溃（stop 未执行）：下次 start 把遗留 running 任务置为 interrupted 并还原章节。"""
    _, _, (ch,) = await _setup(client, content="旧稿")
    engine = app.state.task_engine
    await engine.stop()
    task = (await _generate(client, ch)).json()
    async with app.state.sessionmaker() as db:
        await db.execute(
            update(Task)
            .where(Task.id == task["id"])
            .values(status="running", output_text="半截", started_at=func.now())
        )
        await db.commit()
    engine.hubs.clear()  # 模拟新进程：内存状态为空

    await engine.start()
    out = (await client.get(f"/api/v1/tasks/{task['id']}")).json()
    assert out["status"] == "interrupted" and out["error"] == "服务重启，任务已中断"
    assert (await client.get(f"/api/v1/tasks/{task['id']}/output")).json()["text"] == "半截"
    row = await _chapter(app, ch["id"])
    assert row.status == "draft" and row.content == "旧稿"
    assert fake_llm.calls == []
    events = await stream_task(client, task["id"])
    assert [e for e, _ in events] == ["snapshot", "error"]
    assert events[-1][1]["detail"] == "服务重启，任务已中断"


async def test_reset_while_queued_fails_task(app, client, fake_llm):
    """排队期间用户复位了章节：任务不再覆盖正文，直接失败。"""
    _, base, (ch,) = await _setup(client, content="旧稿")
    engine = app.state.task_engine
    await engine.stop()
    task = (await _generate(client, ch)).json()
    assert (await client.patch(f"{base}/chapters/{ch['id']}", json={"status": "draft"})).status_code == 200
    await engine.start()
    out = await wait_task(client, task["id"])
    assert out["status"] == "failed" and out["error"] == "章节生成状态已被复位，任务未执行"
    assert fake_llm.calls == []
    assert (await _chapter(app, ch["id"])).content == "旧稿"


async def test_config_deleted_while_queued_fails_task(app, client, fake_llm):
    _, _, (ch,) = await _setup(client, content="旧稿")
    extra = await create_ai_config(client, name="备用")
    engine = app.state.task_engine
    await engine.stop()
    task = (await _generate(client, ch, ai_config_id=extra["id"])).json()
    assert (await client.delete(f"/api/v1/ai-configs/{extra['id']}")).status_code == 204
    await engine.start()
    out = await wait_task(client, task["id"])
    assert out["status"] == "failed" and out["error"] == "模型配置不存在"
    assert fake_llm.calls == []
    row = await _chapter(app, ch["id"])
    assert row.status == "draft" and row.content == "旧稿"


# ---------- AC5：并发上限、排队位置、资源互斥 ----------


async def test_per_user_limit_and_queue_position(app, client, fake_llm):
    _, _, chs = await _setup(client, chapters=3)
    gates = [Gate(), Gate(), Gate()]
    for i, gate in enumerate(gates):
        fake_llm.push([gate, f"正文{i}"])
    tasks = [(await _generate(client, ch)).json() for ch in chs]
    await gates[0].wait_reached()
    await gates[1].wait_reached()
    await asyncio.sleep(0.05)
    assert not gates[2].reached.is_set()

    active = (await client.get("/api/v1/tasks", params={"active": "true"})).json()
    by_id = {t["id"]: t for t in active}
    assert [by_id[t["id"]]["status"] for t in tasks] == ["running", "running", "queued"]
    assert by_id[tasks[2]["id"]]["queue_position"] == 0
    assert by_id[tasks[0]["id"]]["queue_position"] is None
    assert [t["id"] for t in active] == sorted(by_id, reverse=True)  # 按 id 倒序

    gates[0].release()
    await gates[2].wait_reached()  # 有空位后第 3 个开始执行
    gates[1].release()
    gates[2].release()
    for t in tasks:
        assert (await wait_task(client, t["id"]))["status"] == "succeeded"


@pytest.mark.parametrize("settings_overrides", [{"task_max_concurrency": 1}])
async def test_global_limit(app, client, make_client, fake_llm):
    _, _, (ch,) = await _setup(client)
    bob = make_client()
    _, _, (bob_ch1, bob_ch2) = await _setup_other(bob)
    gate = Gate()
    fake_llm.push([gate, "甲"])
    t1 = (await _generate(client, ch)).json()
    await gate.wait_reached()
    b1 = (await _generate(bob, bob_ch1)).json()
    b2 = (await _generate(bob, bob_ch2)).json()
    await asyncio.sleep(0.05)
    assert (await bob.get(f"/api/v1/tasks/{b1['id']}")).json()["queue_position"] == 0
    assert (await bob.get(f"/api/v1/tasks/{b2['id']}")).json()["queue_position"] == 1
    assert len(fake_llm.calls) == 1

    # 取消排在前面的任务后，后面的排队位置前移
    await bob.post(f"/api/v1/tasks/{b1['id']}/cancel")
    assert (await bob.get(f"/api/v1/tasks/{b2['id']}")).json()["queue_position"] == 0
    gate.release()
    assert (await wait_task(bob, b2["id"]))["status"] == "succeeded"
    assert (await wait_task(client, t1["id"]))["status"] == "succeeded"


async def _setup_other(c):
    await register(c, "bob")
    p = await create_project(c)
    await create_ai_config(c)
    base = f"/api/v1/projects/{p['id']}"
    chs = [(await c.post(f"{base}/chapters", json={"title": f"乙{i}"})).json() for i in range(2)]
    return p, base, chs


async def test_resource_conflicts(app, client, fake_llm):
    p, _, (ch,) = await _setup(client, content="正文")
    gate = Gate()
    fake_llm.push([gate, "新正文"])
    task = (await _generate(client, ch)).json()
    for path in ("generate", "polish"):
        resp = await client.post(f"/api/v1/chapters/{ch['id']}/{path}")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "该章节正在生成中，请稍候再试"

    wgate = Gate()
    fake_llm.push([wgate, "[]"])
    w1 = await client.post(f"/api/v1/projects/{p['id']}/wizard/world")
    assert w1.status_code == 202
    resp = await client.post(f"/api/v1/projects/{p['id']}/wizard/world")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "已有任务在进行中，请等待其完成后再试"
    # 不同类型的向导互不影响
    fake_llm.push('[{"name": "林舟"}]')
    other = await client.post(f"/api/v1/projects/{p['id']}/wizard/characters")
    assert other.status_code == 202

    gate.release()
    wgate.release()
    assert (await wait_task(client, task["id"]))["status"] == "succeeded"
    assert (await wait_task(client, w1.json()["id"]))["status"] == "failed"  # "[]" 解析不到条目
    assert (await wait_task(client, other.json()["id"]))["status"] == "succeeded"
    # 结束后可再次提交同类向导
    fake_llm.push('[{"title": "设定", "content": "x"}]')
    task2, _ = await run_task(client, await client.post(f"/api/v1/projects/{p['id']}/wizard/world"))
    assert task2["status"] == "succeeded"


# ---------- 列表 / 删除 / 清理 ----------


async def test_list_filters_delete_and_clear(app, client, fake_llm):
    p, _, (ch1, ch2) = await _setup(client, chapters=2)
    fake_llm.push("一")
    t1, _ = await run_task(client, await _generate(client, ch1))
    fake_llm.push('[{"title": "设定"}]')
    w, _ = await run_task(client, await client.post(f"/api/v1/projects/{p['id']}/wizard/world"))
    gate = Gate()
    fake_llm.push([gate, "二"])
    t2 = (await _generate(client, ch2)).json()
    await gate.wait_reached()

    def ids(resp):
        return [t["id"] for t in resp.json()]

    assert ids(await client.get("/api/v1/tasks")) == [t2["id"], w["id"], t1["id"]]
    assert ids(await client.get("/api/v1/tasks", params={"chapter_id": ch1["id"]})) == [t1["id"]]
    assert ids(await client.get("/api/v1/tasks", params={"kind": "wizard.world"})) == [w["id"]]
    assert ids(await client.get("/api/v1/tasks", params={"active": "false"})) == [w["id"], t1["id"]]
    assert ids(await client.get("/api/v1/tasks", params={"project_id": p["id"], "limit": 1})) == [t2["id"]]
    assert (await client.get("/api/v1/tasks", params={"kind": "nope"})).status_code == 422

    resp = await client.delete(f"/api/v1/tasks/{t2['id']}")
    assert resp.status_code == 409 and resp.json()["detail"] == "任务进行中，无法删除，请先取消"
    assert (await client.delete(f"/api/v1/tasks/{t1['id']}")).status_code == 204
    assert (await client.get(f"/api/v1/tasks/{t1['id']}")).status_code == 404

    resp = await client.delete("/api/v1/tasks", params={"finished": "true", "project_id": p["id"]})
    assert resp.json() == {"deleted": 1}
    assert ids(await client.get("/api/v1/tasks")) == [t2["id"]]  # 进行中的任务不受清理影响
    gate.release()
    await wait_task(client, t2["id"])


# ---------- AC8：越权 ----------


async def test_tasks_cross_user_404(app, client, make_client, fake_llm):
    _, _, (ch,) = await _setup(client)
    fake_llm.push(["甲", AIProviderError("模型调用失败：额度不足")])
    task, _ = await run_task(client, await _generate(client, ch))
    assert task["status"] == "failed"
    tid = task["id"]

    bob = make_client()
    await register(bob, "bob")
    await create_ai_config(bob)
    checks = [
        ("GET", f"/api/v1/tasks/{tid}"),
        ("GET", f"/api/v1/tasks/{tid}/output"),
        ("GET", f"/api/v1/tasks/{tid}/stream"),
        ("POST", f"/api/v1/tasks/{tid}/cancel"),
        ("POST", f"/api/v1/tasks/{tid}/retry"),
        ("DELETE", f"/api/v1/tasks/{tid}"),
    ]
    for method, path in checks:
        resp = await bob.request(method, path)
        assert resp.status_code == 404, (method, path, resp.text)
        assert resp.json()["detail"] == "任务不存在"
    assert (await bob.get("/api/v1/tasks")).json() == []
    assert (await bob.get("/api/v1/tasks", params={"chapter_id": ch["id"]})).json() == []
    assert (await bob.delete("/api/v1/tasks", params={"finished": "true"})).json() == {"deleted": 0}
    assert (await client.get(f"/api/v1/tasks/{tid}")).status_code == 200
    assert len(fake_llm.calls) == 1  # 越权重试没有调用模型
    # 未登录
    anon = make_client()
    assert (await anon.get("/api/v1/tasks")).status_code == 401


@pytest.mark.parametrize("settings_overrides", [{"task_max_per_user": 1}])
async def test_subscribed_task_deleted_ends_with_error(app, client, fake_llm):
    """作品软删除先取消排队任务并关闭订阅，保留行但不能经新订阅读取。"""
    _, _, (ch1,) = await _setup(client)
    p2 = await create_project(client, title="另一个项目")
    ch2 = (await client.post(f"/api/v1/projects/{p2['id']}/chapters", json={"title": "章"})).json()
    gate = Gate()
    fake_llm.push([gate, "第一章"])
    t1 = (await _generate(client, ch1)).json()
    await gate.wait_reached()
    t2 = (await _generate(client, ch2)).json()
    assert t2["status"] == "queued"

    engine = app.state.task_engine
    engine.heartbeat_seconds = 0.05
    subscriber = asyncio.ensure_future(client.get(f"/api/v1/tasks/{t2['id']}/stream"))
    await wait_until(lambda: engine.hubs[t2["id"]].subscriber_count == 1)
    assert (await client.delete(f"/api/v1/projects/{p2['id']}")).status_code == 204

    events = parse_sse((await asyncio.wait_for(subscriber, 5)).text)
    assert events[0][0] == "snapshot"
    assert events[-1][0] == "error" and events[-1][1]["detail"] == "任务已取消"
    assert events[-1][1]["task"]["status"] == "cancelled"
    assert (await client.get(f"/api/v1/tasks/{t2['id']}")).status_code == 404
    assert t2["id"] not in engine.hubs

    # 订阅一个刚被删除的任务（越过归属校验后才消失）：同样以 error 结束而不是空流
    events = [e async for e in engine.subscribe(t2["id"])]
    assert events == [("error", {"task": None, "detail": "任务不存在"})]

    gate.release()
    assert (await wait_task(client, t1["id"]))["status"] == "succeeded"


# ---------- AC9：保留裁剪 ----------


async def test_finished_tasks_pruned_to_limit(app, client, fake_llm):
    _, _, (ch,) = await _setup(client)
    me = (await client.get("/api/v1/auth/me")).json()
    limit = task_service.MAX_FINISHED_PER_USER
    async with app.state.sessionmaker() as db:
        db.add_all(
            Task(user_id=me["id"], kind="wizard.world", status="succeeded", input={}, title="旧任务")
            for _ in range(limit)
        )
        await db.commit()
        oldest = await db.scalar(select(func.min(Task.id)))

    fake_llm.push("正文")
    task, _ = await run_task(client, await _generate(client, ch))
    async with app.state.sessionmaker() as db:
        count = await db.scalar(select(func.count(Task.id)).where(Task.user_id == me["id"]))
        assert count == limit
        assert await db.get(Task, oldest) is None
        assert await db.get(Task, task["id"]) is not None


# ---------- lifespan ----------


def test_lifespan_starts_and_stops_engine(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import Base
    from tests.helpers import FakeLLM, make_settings

    application = create_app(make_settings(tmp_path), provider_factory=FakeLLM().factory)

    async def _create_tables():
        async with application.state.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await application.state.engine.dispose()

    asyncio.run(_create_tables())
    engine = application.state.task_engine
    with TestClient(application) as tc:
        assert engine.running
        assert tc.get("/healthz").json() == {"status": "ok"}
    assert not engine.running


def test_chapter_title_skips_default_title():
    from app.services.task_engine.handlers.chapter import chapter_title

    assert chapter_title(3, "第3章", "generate") == "第 3 章 · AI 生成"
    assert chapter_title(3, "", "polish") == "第 3 章 · AI 润色"
    assert chapter_title(3, "山门", "generate") == "第 3 章 山门 · AI 生成"


# ---------- serial_key：同键串行、异键并行、与每用户上限共同生效 ----------


async def _analysis_setup(client, n_projects: int):
    """每个项目一个有正文的章节（分析任务 serial_key = analysis:{project_id}）。"""
    chapters = []
    for i in range(n_projects):
        p = await create_project(client, title=f"项目{i}")
        base = f"/api/v1/projects/{p['id']}"
        chapters.append((await client.post(f"{base}/chapters", json={"title": "章", "content": "正文"})).json())
    return chapters


def _analyze(client, ch):
    return client.post(f"/api/v1/chapters/{ch['id']}/analyze")


@pytest.mark.parametrize("settings_overrides", [{"task_max_per_user": 2}])
async def test_serial_key_same_key_serial_other_keys_parallel(app, client, fake_llm):
    from tests.helpers import analysis_json

    await register(client)
    await create_ai_config(client)
    a1, b1, c1 = await _analysis_setup(client, 3)
    base_a = f"/api/v1/projects/{a1['project_id']}"
    a2 = (await client.post(f"{base_a}/chapters", json={"title": "章2", "content": "正文2"})).json()
    gate_a1, gate_b1, gate_a2 = Gate(), Gate(), Gate()
    # 领取顺序：A1、B1（A2 与 A1 同键排队），A1 结束后是 A2（先于更晚入队的 C1）
    fake_llm.push([gate_a1, analysis_json()])
    fake_llm.push([gate_b1, analysis_json()])
    fake_llm.push([gate_a2, analysis_json()])
    fake_llm.push(analysis_json())
    ta1 = (await _analyze(client, a1)).json()
    ta2 = (await _analyze(client, a2)).json()
    tb1 = (await _analyze(client, b1)).json()
    tc1 = (await _analyze(client, c1)).json()
    await gate_a1.wait_reached()
    await gate_b1.wait_reached()
    await asyncio.sleep(0.05)
    status = {t["id"]: (await client.get(f"/api/v1/tasks/{t['id']}")).json()["status"] for t in (ta1, ta2, tb1, tc1)}
    # A2 同键等待；C1 异键但已达每用户上限
    assert status == {ta1["id"]: "running", ta2["id"]: "queued", tb1["id"]: "running", tc1["id"]: "queued"}
    assert len(fake_llm.calls) == 2

    gate_a1.release()
    await gate_a2.wait_reached()  # 同键的下一个（先入队）开始执行
    await asyncio.sleep(0.05)
    assert (await client.get(f"/api/v1/tasks/{tc1['id']}")).json()["status"] == "queued"
    gate_b1.release()
    assert (await wait_task(client, tc1["id"]))["status"] == "succeeded"  # 名额空出后异键任务执行
    gate_a2.release()
    for t in (ta1, ta2, tb1):
        assert (await wait_task(client, t["id"]))["status"] == "succeeded"
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(Task).where(Task.kind == "chapter.analyze").order_by(Task.started_at)))
    assert rows[0].serial_key == f"analysis:{a1['project_id']}"
    assert {r.serial_key for r in rows} == {f"analysis:{c['project_id']}" for c in (a1, b1, c1)}


@pytest.mark.parametrize("settings_overrides", [{"task_max_per_user": 1}])
async def test_serial_key_keeps_fifo_when_user_limit_blocks(app, client, fake_llm):
    """每用户上限为 1：生成任务与同项目的两个分析按入队顺序依次执行（同键内先进先出）。"""
    from tests.helpers import analysis_json

    await register(client)
    await create_ai_config(client)
    (a1,) = await _analysis_setup(client, 1)
    a2 = (await client.post(f"/api/v1/projects/{a1['project_id']}/chapters", json={"content": "正文2"})).json()
    engine = app.state.task_engine
    await engine.stop()
    gen = (await _generate(client, a1)).json()
    t1 = (await _analyze(client, a2)).json()
    t2 = (await _analyze(client, a1)).json()
    fake_llm.push("生成的正文")
    fake_llm.push(analysis_json(summary="第一"))
    fake_llm.push(analysis_json(summary="第二"))
    await engine.start()
    for t in (gen, t1, t2):
        assert (await wait_task(client, t["id"]))["status"] == "succeeded"
    async with app.state.sessionmaker() as db:
        started = list(await db.scalars(select(Task.id).order_by(Task.started_at, Task.id)))
    assert started == [gen["id"], t1["id"], t2["id"]]


async def test_serial_key_after_restart(app, client, fake_llm):
    """重启：运行中的同键任务被中断并释放串行键，start 后排队中的同键任务照常执行。"""
    from tests.helpers import analysis_json

    await register(client)
    await create_ai_config(client)
    (a1,) = await _analysis_setup(client, 1)
    a2 = (await client.post(f"/api/v1/projects/{a1['project_id']}/chapters", json={"content": "正文2"})).json()
    gate = Gate()
    fake_llm.push([gate, analysis_json()])
    t1 = (await _analyze(client, a1)).json()
    await gate.wait_reached()
    t2 = (await _analyze(client, a2)).json()
    await asyncio.sleep(0.05)
    assert (await client.get(f"/api/v1/tasks/{t2['id']}")).json()["status"] == "queued"

    engine = app.state.task_engine
    await engine.stop()
    assert (await client.get(f"/api/v1/tasks/{t1['id']}")).json()["status"] == "interrupted"
    assert engine._running_serial == {}
    fake_llm.push(analysis_json(summary="第二章"))
    await engine.start()
    assert (await wait_task(client, t2["id"]))["status"] == "succeeded"
    # 中断的分析可重试，仍归入同一串行键
    fake_llm.push(analysis_json())
    retried, _ = await run_task(client, await client.post(f"/api/v1/tasks/{t1['id']}/retry"))
    assert retried["status"] == "succeeded" and retried["retry_of_id"] == t1["id"]
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, retried["id"])
    assert row.serial_key == f"analysis:{a1['project_id']}"


async def test_on_success_failure_does_not_affect_task(app, client, fake_llm, monkeypatch):
    from app.services import analysis_service

    calls = []

    async def boom(ctx, project_id, chapter_id):
        calls.append((project_id, chapter_id))
        raise RuntimeError("hook failed")

    monkeypatch.setattr(analysis_service, "enqueue_auto", boom)
    _, base, (ch,) = await _setup(client)
    fake_llm.push("正文")
    task, events = await run_task(client, await _generate(client, ch))
    assert task["status"] == "succeeded" and events[-1][0] == "done"
    assert calls == [(ch["project_id"], ch["id"])]
    # 失败的任务不调用 on_success
    fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    task, _ = await run_task(client, await _generate(client, ch))
    assert task["status"] == "failed" and len(calls) == 1
