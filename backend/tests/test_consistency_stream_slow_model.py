"""模型迟迟不返回响应头时，协作者（编辑者）的一致性检查任务流不能被帧期限中断（浏览器验收缺陷 2）。

根因：出站 lease 从请求发出持有到响应头到达；协作者订阅的每一帧要在帧租约里取锁，超过帧期限（5 秒）
整个订阅被中断。SQLite 只有一把库级写锁，两者互斥且不排队；一致性检查每次出站前都刚发布过阶段事件，
又连续多次出站，进度帧被接连饿死。修复：协作者任务在 SQLite 上每次出站前短暂让出库锁（check._before_send）。
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.services.consistency import check
from tests import helpers
from tests.consistency_helpers import comparison, extraction
from tests.helpers import parse_sse
from tests.test_collaboration_outline_entry_e4g3 import _terminal, _world

CONTENTS = {number: f"林远在第{number}座山门前拔出佩剑。" for number in range(1, 5)}
# 单次调用短于帧租约的单次锁等待（2 秒），四次连续调用合计超过帧期限（5 秒）
DELAY = 2.0


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


def slow_headers(monkeypatch, delay: float) -> None:
    """模拟模型在返回响应头之前耗时 delay 秒：FakeLLM 的请求钩子与响应钩子之间等待，期间发送 lease 一直持有。"""
    original = helpers.current_dispatch_hooks

    class _Hooks:
        def __init__(self, hooks) -> None:
            self._hooks = hooks

        @property
        def event_hooks(self) -> dict[str, list]:
            hooks = self._hooks.event_hooks

            async def wait(_response) -> None:
                await asyncio.sleep(delay)

            return {"request": hooks["request"], "response": [wait, *hooks["response"]]}

    def current():
        hooks = original()
        return _Hooks(hooks) if hooks is not None else None

    monkeypatch.setattr(helpers, "current_dispatch_hooks", current)


async def test_editor_stream_survives_slow_response_headers(app, client, make_client, fake_llm, monkeypatch):
    """编辑者使用作者授权模型，每次抽取 2 秒才返回响应头：订阅完整收到进度与完成事件。"""
    slow_headers(monkeypatch, DELAY)
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
    params = {"task_key": task["task_key"]}
    for _ in range(3):
        # 流被帧期限中断时响应不完整，ASGITransport 抛 AssertionError（修复前的表现）。
        resp = await asyncio.wait_for(guest.get(f"/api/v1/tasks/{task['id']}/stream", params=params), 40)
        events = parse_sse(resp.text)
        # 恰在领取瞬间订阅时首帧可能无法证明而以 permission_denied 结束（任务流既有行为，与模型快慢无关）：
        # 与前端一致，重新订阅。
        if events[-1][0] != "error" or events[-1][1].get("code") != "permission_denied":
            break
    row = await _terminal(app, task["id"])
    assert row.status == "succeeded", row.error
    name, final = events[-1]
    assert name == "done" and final["task"]["status"] == "succeeded", events
    assert final["result"]["extracted"] == len(CONTENTS)


@pytest.mark.parametrize(("grant", "dialect", "expected"), [
    (("invitation", 1), "sqlite", True),
    (None, "sqlite", False),
])
def test_send_gap_only_for_collaborators_on_sqlite(grant, dialect, expected):
    job = SimpleNamespace(execution=SimpleNamespace(grant_identity=grant))
    sessionmaker = SimpleNamespace(kw={"bind": SimpleNamespace(dialect=SimpleNamespace(name=dialect))})
    assert check._needs_send_gap(job, sessionmaker) is expected  # noqa: SLF001
