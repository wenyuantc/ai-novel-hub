"""真实受管子进程的智能体调用、版本围栏与断连不重放。"""

import json
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import AgentChange
from app.services.mcp.client import McpRequestGate
from tests.helpers import create_ai_config, create_project, register, task_params, wait_task
from tests.test_agent_mcp_skills import call, change_action, send_task

pytestmark = pytest.mark.skipif(os.name != "posix", reason="本机 stdio 运行环境要求 POSIX")


@pytest.fixture
def history():
    # 该测试进程在 root 环境降权为 nobody，独立临时文件也须允许其追加计数。
    with tempfile.NamedTemporaryFile(prefix="ainovel-agent-stdio-") as file:
        os.chmod(file.name, 0o666)
        yield Path(file.name)


@pytest.mark.parametrize("mode", ["confirm", "auto", "version_before_spawn", "version_before_write", "disconnect"])
async def test_real_stdio_agent(app, client, fake_llm, tmp_path, history, mode, monkeypatch):
    catalog = tmp_path / "catalog.json"
    script = (Path(__file__).parent / "fixtures" / "stdio_plugin.py").read_text()
    entry = {"id": "local", "name": "local", "version": "v1", "command": sys.executable,
             "args": ["-I", "-c", script, str(history)]}
    catalog.write_text(json.dumps({"plugins": [entry]}))
    app.state.settings.mcp_stdio_catalog = str(catalog)
    await register(client)
    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    conversation = (await client.post(base + "/conversations", json={})).json()
    url = base + f"/conversations/{conversation['id']}"
    plugins = "/api/v1/mcp-plugins"
    response = await client.post(plugins + "/catalog/local/enable")
    assert response.status_code in (200, 201), response.text
    plugin = response.json()
    plugin_url = f"{plugins}/{plugin['id']}"
    response = await client.post(plugin_url + "/refresh")
    assert response.status_code == 200, response.text
    assert [json.loads(line)["event"] for line in history.read_text().splitlines()] == ["start"]
    if mode != "confirm":
        response = await client.patch(plugin_url + "/tools", json={"tool": "record", "auto_confirm": True})
        assert response.status_code == 200, response.text
    response = await client.patch(url, json={"mcp_plugin_ids": [plugin["id"]]})
    assert response.status_code == 200, response.text

    if mode.startswith("version_before_"):
        name = "stdio_start" if mode == "version_before_spawn" else "stdio_write"
        original = getattr(McpRequestGate, name)

        @asynccontextmanager
        async def rotate_at_boundary(gate, *args):
            if not args or args[0].get("method") == "tools/call":
                catalog.write_text(json.dumps({"plugins": [{**entry, "version": "v2"}]}))
            async with original(gate, *args):
                yield

        monkeypatch.setattr(McpRequestGate, name, rotate_at_boundary)

    args = {"disconnect": True} if mode == "disconnect" else {}
    name = f"mcp__{plugin['slug']}__record"
    calls = [call(name, args)]
    if mode == "auto":
        calls.append(call(name, {"second": True}, ident="c2"))
    fake_llm.push_tools(calls)
    fake_llm.push_tools([("text", {"text": "完成"})])
    task = await send_task(client, url)
    if mode == "confirm":
        paused = await wait_task(client, task, within=60, statuses=("paused", "failed"))
        assert paused["status"] == "paused"
        change = (await client.get(url)).json()["changes"][0]
        async with app.state.sessionmaker() as db:
            stored = await db.get(AgentChange, change["id"])
            assert stored.preview["guards"][0]["catalog_version"] == "v1"
        response = await client.post(base + f"/changes/{change['id']}/confirm", json=change_action(change))
        assert response.status_code == 200 and response.json()["status"] == "executed", response.text
    finished = await wait_task(client, task, within=60, statuses=("failed", "succeeded"))
    events = [json.loads(line)["event"] for line in history.read_text().splitlines()]
    if mode.startswith("version_before_"):
        assert events == (["start"] if mode == "version_before_spawn" else ["start", "start"])
        assert finished["status"] == "failed"
    elif mode == "disconnect":
        assert events == ["start", "start", "call"] and finished["status"] == "failed"
        async with app.state.sessionmaker() as db:
            change = await db.scalar(select(AgentChange).where(AgentChange.turn_task_id == task["id"]))
            assert change.status == "failed" and change.result["_external_outcome_unknown"]
        retry = await client.post(f"/api/v1/tasks/{task['id']}/retry", params=task_params(task))
        new_turn = await client.post(url + "/messages", json={"content": "再试一次"})
        assert retry.status_code == new_turn.status_code == 409
        assert [json.loads(line)["event"] for line in history.read_text().splitlines()] == events
    else:
        assert finished["status"] == "succeeded", finished
        assert events == ["start", "start", "call"] + (["call"] if mode == "auto" else [])
