"""通过管理API更新配置后，验证真实消费链及MCP可控竞态。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial

import pytest

from app.models import ApiKey
from app.schemas.system_settings import MAX_OPEN_API_RATE_LIMIT_PER_MINUTE
from app.services import api_key_service
from app.services.agent import mcp_tools
from app.services.mcp.client import _register_test_connector
from tests.helpers import (
    Gate,
    analysis_json,
    create_ai_config,
    create_project,
    register,
    run_task,
    stream_task,
    task_params,
    wait_task,
    wait_until,
)
from tests.mcp_fakes import FakeMcpConnector, tool
from tests.test_agent_mcp_skills import call, change_action, send_task
from tests.test_system_settings import save_settings

MCP_URL = "https://runtime.example.com/mcp"


async def setup_chapters(client, count=1):
    await register(client)
    project = await create_project(client)
    await create_ai_config(client)
    base = f"/api/v1/projects/{project['id']}"
    chapters = []
    for index in range(count):
        response = await client.post(f"{base}/chapters", json={"title": f"第{index + 1}章"})
        assert response.status_code == 201, response.text
        chapters.append(response.json())
    return base, chapters


@pytest.mark.parametrize("lower", [
    {"task_max_concurrency": 1}, {"task_max_per_user": 1},
])
async def test_hot_concurrency_lower_preserves_running_and_raise_releases_queue(
    app, client, system_admin, fake_llm, monkeypatch, lower,
):
    _, chapters = await setup_chapters(client, 4)
    await save_settings(system_admin, task_max_concurrency=3, task_max_per_user=2)
    gates = [Gate() for _ in chapters]
    for index, gate in enumerate(gates):
        fake_llm.push([gate, f"正文{index}"])
    tasks = []
    try:
        for chapter in chapters:
            response = await client.post(f"/api/v1/chapters/{chapter['id']}/generate", json={})
            assert response.status_code == 202, response.text
            tasks.append(response.json())
        await gates[0].wait_reached()
        await gates[1].wait_reached()
        await save_settings(system_admin, **lower)
        assert len(fake_llm.calls) == 2 and fake_llm.aborted == 0
        for row in tasks[:2]:
            status = await client.get(f"/api/v1/tasks/{row['id']}", params=task_params(row))
            assert status.json()["status"] == "running"
        engine = app.state.task_engine
        original = engine._dispatch
        original_done = engine._on_done
        dispatched = asyncio.Event()
        completed: asyncio.Queue[int] = asyncio.Queue()
        completion_observed = False

        async def observed():
            after_completion = completion_observed
            await original()
            if after_completion:
                dispatched.set()

        def observed_done(task_id):
            original_done(task_id)
            completed.put_nowait(task_id)

        monkeypatch.setattr(engine, "_dispatch", observed)
        monkeypatch.setattr(engine, "_on_done", observed_done)
        gates[0].release()
        # 两个任务异步组装，消费FakeLLM脚本的顺序不一定等于Task.id顺序。
        # 等待实际完成身份，再验证一次完成后的真实调度，不猜gate属于哪个ID。
        finished_id = await asyncio.wait_for(completed.get(), 5)
        assert finished_id in {row["id"] for row in tasks[:2]}
        finished_ref = next(row for row in tasks[:2] if row["id"] == finished_id)
        assert (await wait_task(client, finished_ref))["status"] == "succeeded"
        completion_observed = True
        engine.notify()
        await asyncio.wait_for(dispatched.wait(), 5)
        assert len(fake_llm.calls) == 2 and not gates[2].reached.is_set()
        queued = await client.get(f"/api/v1/tasks/{tasks[2]['id']}", params=task_params(tasks[2]))
        assert queued.json()["status"] == "queued"
        await save_settings(system_admin, task_max_concurrency=3, task_max_per_user=2)
        await gates[2].wait_reached()
        assert len(fake_llm.calls) == 3 and fake_llm.aborted == 0
    finally:
        for gate in gates:
            gate.release()
    for row in tasks:
        assert (await wait_task(client, row))["status"] == "succeeded"
    assert fake_llm.aborted == 0


async def test_auto_analysis_hot_switch_uses_trigger_time_and_preserves_existing_work(
    app, client, system_admin, fake_llm,
):
    _, (first, second) = await setup_chapters(client, 2)
    await save_settings(system_admin, auto_analyze_enabled=True)
    generation = Gate()
    fake_llm.push([generation, "第一章正文"])
    started = await client.post(f"/api/v1/chapters/{first['id']}/generate", json={})
    await generation.wait_reached()
    await save_settings(system_admin, auto_analyze_enabled=False)
    generation.release()
    assert (await stream_task(client, started.json()))[-1][0] == "done"
    assert (await client.get("/api/v1/tasks", params={"kind": "chapter.analyze"})).json() == []
    assert len(fake_llm.calls) == 1

    await save_settings(system_admin, auto_analyze_enabled=True)
    analysis_gate = Gate()
    fake_llm.push("第二章正文")
    fake_llm.push([analysis_gate, analysis_json()])
    started = await client.post(f"/api/v1/chapters/{second['id']}/generate", json={})
    assert (await stream_task(client, started.json()))[-1][0] == "done"
    await analysis_gate.wait_reached()
    analyzing = (await client.get("/api/v1/tasks", params={"kind": "chapter.analyze"})).json()
    assert len(analyzing) == 1
    await save_settings(system_admin, auto_analyze_enabled=False)
    assert fake_llm.aborted == 0
    analysis_gate.release()
    assert (await wait_task(client, analyzing[0]))["status"] == "succeeded"
    fake_llm.push(analysis_json())
    manual, _ = await run_task(client, await client.post(f"/api/v1/chapters/{first['id']}/analyze", json={}))
    assert manual["status"] == "succeeded" and fake_llm.aborted == 0


async def test_batch_next_analysis_stage_reads_hot_switch(client, system_admin, fake_llm):
    base, (chapter,) = await setup_chapters(client)
    await save_settings(system_admin, auto_analyze_enabled=True)
    gate = Gate()
    fake_llm.push([gate, "批量正文"])
    started = await client.post(base + "/batch-generate", json={
        "start_chapter_id": chapter["id"], "end_chapter_id": chapter["id"], "auto_continue": False,
    })
    assert started.status_code == 202, started.text
    await gate.wait_reached()
    await save_settings(system_admin, auto_analyze_enabled=False)
    gate.release()
    assert (await stream_task(client, started.json()))[-1][0] == "done"
    finished = (await client.get(f"/api/v1/tasks/{started.json()['id']}", params=task_params(started.json()))).json()
    assert finished["checkpoint"]["items"][0]["analysis"] == "skipped"
    assert len(fake_llm.calls) == 1


async def test_hot_rate_limit_keeps_same_minute_count(app, client, system_admin, make_client, monkeypatch):
    now = datetime(2026, 11, 1, 8, 0, 12, tzinfo=UTC)
    monkeypatch.setattr(api_key_service, "_now_utc", lambda: now)
    await register(client)
    created = await client.post("/api/v1/api-keys", json={"name": "运行限流", "all_projects": True})
    assert created.status_code == 201, created.text
    key = created.json()
    anonymous = make_client()
    headers = {"X-API-Key": key["key"]}
    await save_settings(system_admin, open_api_rate_limit_per_minute=3)
    assert (await anonymous.get("/api/open/v1/me", headers=headers)).status_code == 200
    assert (await anonymous.get("/api/open/v1/me", headers=headers)).status_code == 200
    await save_settings(system_admin, open_api_rate_limit_per_minute=1)
    limited = await anonymous.get("/api/open/v1/me", headers=headers)
    assert limited.status_code == 429 and limited.headers["retry-after"] == "48"
    async with app.state.sessionmaker() as db:
        row = await db.get(ApiKey, key["id"])
        assert row.rate_count == 2 and row.rate_window == int(now.timestamp()) // 60
    await save_settings(system_admin, open_api_rate_limit_per_minute=3)
    assert (await anonymous.get("/api/open/v1/me", headers=headers)).status_code == 200
    assert (await anonymous.get("/api/open/v1/me", headers=headers)).status_code == 429
    assert (await client.get("/api/v1/projects")).status_code == 200


async def test_largest_executable_rate_limit_and_null_restore_reach_real_open_api(
    app, client, system_admin, make_client,
):
    await register(client)
    created = await client.post("/api/v1/api-keys", json={"name": "最大技术限额", "all_projects": True})
    assert created.status_code == 201, created.text
    key = created.json()
    anonymous = make_client()
    headers = {"X-API-Key": key["key"]}
    await save_settings(system_admin, open_api_rate_limit_per_minute=MAX_OPEN_API_RATE_LIMIT_PER_MINUTE)
    assert (await anonymous.get("/api/open/v1/me", headers=headers)).status_code == 200
    await save_settings(system_admin, open_api_rate_limit_per_minute=None)
    assert app.state.settings.open_api_rate_limit_per_minute == 120
    assert (await anonymous.get("/api/open/v1/me", headers=headers)).status_code == 200
    async with app.state.sessionmaker() as db:
        assert (await db.get(ApiKey, key["id"])).rate_count == 2


async def setup_plugin(app, client):
    await register(client)
    connector = FakeMcpConnector()
    connector.add(MCP_URL, [tool("lookup")])
    app.state.task_engine.ctx.mcp_connector = connector
    response = await client.post("/api/v1/mcp-plugins", json={"name": "runtime", "url": MCP_URL})
    assert response.status_code == 201, response.text
    plugin = response.json()
    assert (await client.post(f"/api/v1/mcp-plugins/{plugin['id']}/refresh")).status_code == 200
    return plugin, connector


async def _paused_mcp_confirmations(client, fake_llm, plugin, count):
    """每个会话独立暂停，通过正常确认入口取得原 worker 和签名。"""
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    name = f"mcp__{plugin['slug']}__lookup"
    confirmations = []
    for index in range(1, count + 1):
        conversation = await client.post(base + "/conversations", json={})
        assert conversation.status_code == 201, conversation.text
        url = base + f"/conversations/{conversation.json()['id']}"
        attached = await client.patch(url, json={"mcp_plugin_ids": [plugin["id"]]})
        assert attached.status_code == 200, attached.text
        fake_llm.push_tools([call(name, {"n": index}, f"call-{index}")])
        task = await send_task(client, url)
        assert (await wait_task(client, task, statuses=("paused", "failed")))["status"] == "paused"
        change, = (await client.get(url)).json()["changes"]
        assert change["turn_task_key"] == task["task_key"] and change["arguments"] == {"n": index}
        confirmations.append(partial(client.post, f"{base}/changes/{change['id']}/confirm", json=change_action(change)))
    return confirmations


def _share_mcp_worker(app, fake, monkeypatch):
    """让独立确认共用指定插件的真实发送队列，记录每个请求自己的发送状态。"""
    sessions = mcp_tools.McpSessions(fake, app.state.cipher, False, settings=app.state.settings)
    states = {}
    call_session = mcp_tools.McpSessions.call

    async def shared_call(_self, plugin, tool, arguments, send_state=None, *, identity=None, resolved=None):
        assert identity.worker_key == app.state.task_engine.request_worker.worker_key
        states[arguments["n"]] = send_state
        return await call_session(sessions, plugin, tool, arguments, send_state, identity=identity, resolved=resolved)

    monkeypatch.setattr(mcp_tools.McpSessions, "call", shared_call)
    return sessions, states


@pytest.mark.parametrize("operation", ["refresh", "call"])
async def test_mcp_close_during_connect_prevents_unstarted_trial_or_discovery(
    app, client, system_admin, operation,
):
    plugin, fake = await setup_plugin(app, client)
    reached, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def connector(target, *, allow_private, request_gate):
        async with fake(target, allow_private=allow_private, request_gate=request_gate) as connection:
            reached.set()
            await release.wait()
            yield connection

    _register_test_connector(connector)
    app.state.task_engine.ctx.mcp_connector = connector
    pending = asyncio.create_task(client.post(
        f"/api/v1/mcp-plugins/{plugin['id']}/{operation}",
        json={"tool": "lookup", "arguments": {}} if operation == "call" else None,
    ))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        await save_settings(system_admin, mcp_enabled=False)
    finally:
        release.set()
    result = await pending
    assert result.status_code == 403, result.text
    assert fake.calls == [] and fake.open == 0
    assert (await client.get("/api/v1/mcp-plugins")).status_code == 403


async def test_agent_worker_rechecks_after_connect(app, client, system_admin, fake_llm, monkeypatch):
    plugin, fake = await setup_plugin(app, client)
    confirm, = await _paused_mcp_confirmations(client, fake_llm, plugin, 1)
    reached, release = asyncio.Event(), asyncio.Event()
    original_connect = FakeMcpConnector.__call__

    @asynccontextmanager
    async def held_connect(self, target, *, allow_private, request_gate=None):
        async with original_connect(self, target, allow_private=allow_private, request_gate=request_gate) as connection:
            if self is fake:
                reached.set()
                await release.wait()
            yield connection

    monkeypatch.setattr(FakeMcpConnector, "__call__", held_connect)
    sessions, states = _share_mcp_worker(app, fake, monkeypatch)
    pending = asyncio.create_task(confirm())
    try:
        await asyncio.wait_for(reached.wait(), 5)
        await save_settings(system_admin, mcp_enabled=False)
        release.set()
        blocked = await pending
        assert blocked.status_code == 409, blocked.text
        assert blocked.json() == {"detail": "MCP 功能已关闭", "code": "mcp_disabled"}
        assert fake.calls == [] and not states[1].started
    finally:
        release.set()
        await sessions.aclose()
    assert fake.open == 0


async def test_worker_queued_call_is_blocked_but_already_issued_call_completes(
    app, client, system_admin, fake_llm, monkeypatch,
):
    plugin, fake = await setup_plugin(app, client)
    first_confirm, second_confirm = await _paused_mcp_confirmations(client, fake_llm, plugin, 2)
    reached, release = asyncio.Event(), asyncio.Event()

    async def handler(_name, _arguments):
        reached.set()
        await release.wait()
        return "完成"

    fake.servers[MCP_URL].handler = handler
    sessions, states = _share_mcp_worker(app, fake, monkeypatch)
    first = asyncio.create_task(first_confirm())
    try:
        await asyncio.wait_for(reached.wait(), 5)
        second = asyncio.create_task(second_confirm())
        await wait_until(lambda: sessions._workers[plugin["id"]]._queue.qsize() == 1)
        await save_settings(system_admin, mcp_enabled=False)
        release.set()
        completed, blocked = await asyncio.gather(first, second)
        assert completed.status_code == 200 and completed.json()["status"] == "executed", completed.text
        assert completed.json()["result"]["text"] == "完成"
        assert blocked.status_code == 409, blocked.text
        assert blocked.json() == {"detail": "MCP 功能已关闭", "code": "mcp_disabled"}
        assert states[1].started and not states[2].started
        assert fake.calls == [(MCP_URL, "lookup", {"n": 1})]
    finally:
        release.set()
        await sessions.aclose()
    assert fake.open == 0
