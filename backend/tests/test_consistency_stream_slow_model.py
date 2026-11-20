"""SQLite 慢模型下协作者任务进度流不能被帧期限或可见性锁超时中断（TODOS 第 22 项）。

根因：出站 _send_lease 在等待 HTTP 响应头期间持 SQLite RESERVED 写锁；此前协作者帧租约也用 no-op UPDATE
争同一把写锁，响应头超过 2 秒（_LOCK_WAIT_SECONDS）或 5 秒（frame_timeout）即误报 permission_denied 或切断流。
根治：SQLite 默认回滚日志模式下，_task_frame_lease 改持真实 SHARED 读事务（BEGIN + SELECT），与 RESERVED 兼容
且仍阻塞写方 COMMIT；移除了 consistency 的 SQLITE_SEND_GAP 人工让锁。
"""

import asyncio
import socket
import time

import httpx
import pytest
import uvicorn

from app.services.task_engine import stream_lease
from tests import helpers
from tests.consistency_helpers import comparison, extraction
from tests.helpers import parse_sse, wait_until
from tests.test_collaboration_outline_entry_e4g3 import _terminal, _world

CONTENTS = {number: f"林远在第{number}座山门前拔出佩剑。" for number in range(1, 5)}


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


def slow_headers(monkeypatch, delay: float, *, reached: asyncio.Event | None = None) -> None:
    """模拟模型在返回响应头之前耗时 delay 秒：请求钩子与响应钩子之间等待，期间 _send_lease 一直持有。"""
    original = helpers.current_dispatch_hooks

    class _Hooks:
        def __init__(self, hooks) -> None:
            self._hooks = hooks

        @property
        def event_hooks(self) -> dict[str, list]:
            hooks = self._hooks.event_hooks

            async def wait(_response) -> None:
                if reached is not None:
                    reached.set()
                await asyncio.sleep(delay)

            return {"request": hooks["request"], "response": [wait, *hooks["response"]]}

    def current():
        hooks = original()
        return _Hooks(hooks) if hooks is not None else None

    monkeypatch.setattr(helpers, "current_dispatch_hooks", current)


async def test_editor_stream_survives_multi_call_slow_headers_exceeding_frame_timeout(
    app, client, make_client, fake_llm, monkeypatch,
):
    """多次连续出站、每次响应头耗时均超过 frame_timeout 且无让锁休眠：订阅单次连到底收到全部进度与 done。"""
    original_response = stream_lease.task_sse_response

    def tight_response(*args, **kwargs):
        kwargs["frame_timeout"] = 0.5
        return original_response(*args, **kwargs)

    monkeypatch.setattr("app.api.v1.tasks.task_sse_response", tight_response)
    slow_headers(monkeypatch, 1.2)
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=False)
    for number, content in CONTENTS.items():
        resp = await client.post(f"{world.base}/chapters",
                                 json={"title": f"章{number}", "chapter_no": number, "content": content})
        assert resp.status_code == 201, resp.text
    for _ in CONTENTS:
        fake_llm.push(extraction({"category": "item", "subject": "佩剑", "statement": "拔出佩剑", "quote": "拔出佩剑"}))
    fake_llm.push(comparison())
    created = await guest.post(f"{world.base}/consistency/checks", json={"mode": "all"})
    assert created.status_code == 202, created.text
    task = created.json()
    # 等领取完成进入运行态（避开既有 queued→running 投影瞬间的单次重订阅窗口），再单次连到底验证慢模型不断流
    await wait_until(
        lambda: (hub := app.state.task_engine.hubs.get(task["id"])) is not None and hub.status == "running",
        within=5,
    )
    resp = await asyncio.wait_for(
        guest.get(f"/api/v1/tasks/{task['id']}/stream", params={"task_key": task["task_key"]}), 30,
    )
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert not any(name == "error" for name, _ in events), events
    row = await _terminal(app, task["id"])
    assert row.status == "succeeded", row.error
    name, final = events[-1]
    assert name == "done" and final["task"]["status"] == "succeeded", events
    assert final["result"]["extracted"] == len(CONTENTS)


async def test_editor_stream_survives_default_timeout_exceeding_5s_and_mid_send_subscribe(
    app, client, make_client, fake_llm, monkeypatch,
):
    """未缩改默认 frame_timeout（5 秒）与 _LOCK_WAIT_SECONDS（2 秒）：单次响应头 5.5 秒时，
    等待响应头期间新接入的协作者订阅能立刻发出 snapshot 并一路收到 done，不被 _send_lease 阻塞。"""
    reached = asyncio.Event()
    slow_headers(monkeypatch, 5.5, reached=reached)
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=False)
    created_ch = await client.post(
        f"{world.base}/chapters", json={"title": "章1", "chapter_no": 1, "content": CONTENTS[1]},
    )
    assert created_ch.status_code == 201, created_ch.text
    fake_llm.push(extraction({"category": "item", "subject": "佩剑", "statement": "拔出佩剑", "quote": "拔出佩剑"}))
    fake_llm.push(comparison())
    created = await guest.post(f"{world.base}/consistency/checks", json={"mode": "all"})
    assert created.status_code == 202, created.text
    task = created.json()
    # 等到任务已进入慢模型响应头等待（_send_lease 持 SQLite RESERVED 写锁）再接入真实 TCP 订阅
    await asyncio.wait_for(reached.wait(), 10)
    await wait_until(lambda: task["id"] in app.state.task_engine.hubs, within=5)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(100)
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="warning"))
    runner = asyncio.create_task(server.serve(sockets=[sock]))
    first_frame_at: list[float] = []
    received: list[str] = []
    try:
        await wait_until(lambda: server.started, within=10)
        t0 = time.monotonic()
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", cookies=dict(guest.cookies), timeout=30,
        ) as tcp, tcp.stream(
            "GET", f"/api/v1/tasks/{task['id']}/stream", params={"task_key": task["task_key"]},
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                received.append(line)
                if not first_frame_at and line.startswith("event: snapshot"):
                    first_frame_at.append(time.monotonic() - t0)
    finally:
        server.should_exit = True
        await asyncio.wait_for(runner, 10)
        sock.close()
    assert first_frame_at, received
    # snapshot 必须在慢模型 5.5 秒响应头返回前就送达，证明帧租约没有等待 _send_lease
    assert first_frame_at[0] < 2.0, f"snapshot waited too long: {first_frame_at[0]:.2f}s"
    events = parse_sse("\n".join(received) + "\n")
    assert not any(name == "error" for name, _ in events), events
    row = await _terminal(app, task["id"])
    assert row.status == "succeeded", row.error
    assert events[-1][0] == "done" and events[-1][1]["result"]["extracted"] == 1
