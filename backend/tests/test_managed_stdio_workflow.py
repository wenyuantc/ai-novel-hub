"""从作者目录到真实 stdio 插件、提案确认的集成回归。"""

import json
import os
import sys
from pathlib import Path

import pytest

from tests.helpers import create_ai_config, create_project, register, stream_task, wait_task
from tests.schema_helpers import isolated_worker_database
from tests.test_agent_mcp_skills import call, change_action, send_task
from tests.test_mcp_plugins import BASE

pytestmark = pytest.mark.skipif(os.name != "posix", reason="本机托管需要 POSIX 进程组")


@pytest.fixture
def catalog_path(tmp_path):
    example = Path(__file__).resolve().parents[1] / "examples" / "manuscript_stats_mcp.py"
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"plugins": [{
        "id": "stats", "name": "字符统计", "description": "稿件计数", "version": "1",
        "command": sys.executable, "args": [str(example)], "cwd": str(example.parent),
    }]}), encoding="utf-8")
    return catalog


@pytest.fixture
async def settings_overrides(catalog_path, monkeypatch):
    async with isolated_worker_database(monkeypatch) as overrides:
        yield {**overrides, "mcp_stdio_catalog": str(catalog_path)}


async def test_catalog_to_confirmed_real_tool(client, make_client, fake_llm):
    await register(client)
    catalog = await client.get(f"{BASE}/catalog")
    assert catalog.status_code == 200, catalog.text
    assert [entry["id"] for entry in catalog.json()] == ["stats"]
    assert all(field not in catalog.json()[0] for field in ("command", "args", "cwd", "env"))

    enabled = await client.post(f"{BASE}/catalog/stats/enable")
    assert enabled.is_success, enabled.text
    target = enabled.json()
    plugin_path = f"{BASE}/{target['id']}"
    other = make_client()
    await register(other, "other-writer")
    assert (await other.get(plugin_path)).status_code == 404
    refresh = await client.post(f"{plugin_path}/refresh")
    assert refresh.is_success, refresh.text
    assert "count_characters" in {tool["name"] for tool in refresh.json()["tools"]}
    arguments = {"text": "星 海\n纪元"}
    trial = await client.post(f"{plugin_path}/call", json={"tool": "count_characters", "arguments": arguments})
    assert trial.is_success, trial.text
    assert trial.json()["text"] == "4"

    await create_ai_config(client)
    project = await create_project(client)
    base = f"/api/v1/projects/{project['id']}/agent"
    conversation = (await client.post(f"{base}/conversations", json={})).json()
    url = f"{base}/conversations/{conversation['id']}"
    patched = await client.patch(url, json={"mcp_plugin_ids": [target["id"]]})
    assert patched.is_success, patched.text
    fake_llm.push_tools([call(f"mcp__{target['slug']}__count_characters", arguments)])
    fake_llm.push_tools([("text", {"text": "四个字。"})])
    task_ref = await send_task(client, url)
    assert (await stream_task(client, task_ref))[-1][0] == "paused"
    proposed = (await client.get(url)).json()["changes"][0]
    assert proposed["status"] == "proposed"
    response = await client.post(f"{base}/changes/{proposed['id']}/confirm", json=change_action(proposed))
    assert response.is_success, response.text
    assert response.json()["result"] == {"text": "4"}
    assert (await wait_task(client, task_ref, within=30))["status"] == "succeeded"
