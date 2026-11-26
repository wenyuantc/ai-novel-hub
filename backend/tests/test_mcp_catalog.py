"""部署清单和个人启用 API，仅启动管理明确登记的程序。"""

import asyncio
import json
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from tests.helpers import register

BASE = "/api/v1/mcp-plugins"


@pytest.fixture
def catalog_file(tmp_path, app):
    path = tmp_path / "server-plugins.json"
    entry = dict(
        id="local",
        version="v1",
        name="本地测试",
        description="工具",
        command=sys.executable,
        args=["--version"],
        cwd=str(tmp_path),
        env={"DUMMY": "internal-value"},
    )
    path.write_text(json.dumps({"plugins": [entry]}))
    app.state.settings.mcp_stdio_catalog = str(path)
    return path


async def test_catalog_private_idempotent_and_fixed_config(app, client, catalog_file):
    await register(client)
    directory = await client.get(BASE + "/catalog")
    assert directory.json() == [
        {"id": "local", "version": "v1", "name": "本地测试", "description": "工具", "available": True}
    ]
    path = BASE + "/catalog/local/enable"
    for body in ({"command": "run"}, {"cwd": "/"}, {"env": {}}, {"args": []}):
        assert (await client.post(path, json=body)).status_code == 422
    assert (await client.post(path, params={"catalog_version": "old-node"})).status_code == 409
    responses = await asyncio.gather(client.post(path), client.post(path, params={"catalog_version": "v1"}))
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    first = responses[0].json()
    assert first["transport"] == "stdio" and first["catalog_version"] == "v1"
    assert first["id"] == responses[1].json()["id"]
    assert len((await client.get(BASE)).json()) == 1
    assert "internal-value" not in responses[0].text and sys.executable not in responses[0].text
    plugin = f"{BASE}/{first['id']}"
    assert (await client.patch(plugin, json={"url": "https://wrong.test"})).status_code == 400
    assert (await client.patch(plugin, json={"headers": None})).status_code == 400
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        await register(other, "catalog-other")
        assert (await other.get(plugin)).status_code == 404
        assert (await other.post(path)).json()["id"] != first["id"]
    manifest = json.loads(catalog_file.read_text())
    manifest["plugins"][0]["version"] = "v2"
    catalog_file.write_text(json.dumps(manifest))
    assert (await client.post(plugin + "/refresh")).status_code == 409
    assert (await client.post(plugin + "/refresh", params={"catalog_version": "v1"})).status_code == 409
    assert (await client.patch(plugin, json={"enabled": False})).json()["enabled"] is False
    catalog_file.write_text('{"plugins":[]}')
    assert (await client.post(path)).status_code == 400
    assert (await client.patch(plugin, json={"enabled": True})).status_code == 400
    assert (await client.get(plugin)).json()["catalog_id"] == "local"
    assert (await client.delete(plugin)).status_code == 204


async def test_catalog_invalid_manifest_is_sanitized(client, catalog_file):
    await register(client)
    catalog_file.write_text('{"plugins":[{"command":"internal-secret"}]}')
    resp = await client.get(BASE + "/catalog")
    assert resp.status_code == 400 and "internal-secret" not in resp.text
