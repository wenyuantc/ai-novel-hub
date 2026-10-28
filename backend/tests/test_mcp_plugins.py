"""MCP 插件：增删改与上限、mcpServers 导入（拒绝 stdio）、请求头加密且不回显、SSRF（保存 / 连接）、
不跟随重定向、刷新工具与 schema 校验、工具偏好、试用调用、部署开关、跨用户 404，以及用 FastMCP 的真实协议集成测试。"""

import asyncio

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from app.models import McpPlugin
from app.services.mcp import client as mcp_client
from app.services.mcp import service as mcp_service
from app.services.mcp.client import McpError, McpResult, McpToolInfo
from tests.helpers import register
from tests.mcp_fakes import FakeMcpConnector, serve, tool

BASE = "/api/v1/mcp-plugins"
SECRET = "Bearer sk-very-secret-token-9876"
URL = "https://mcp.example.com/mcp"


@pytest.fixture
def fake_mcp(app) -> FakeMcpConnector:
    fake = FakeMcpConnector()
    app.state.task_engine.ctx.mcp_connector = fake
    return fake


async def _create(client, **kw):
    body = {"name": "dict-tool", "url": URL, "headers": {"Authorization": SECRET}}
    body.update(kw)
    return await client.post(BASE, json=body)


async def test_crud_headers_encrypted_and_never_echoed(client, app, fake_mcp):
    await register(client)
    resp = await _create(client)
    assert resp.status_code == 201, resp.text
    plugin = resp.json()
    assert plugin["slug"] == "dict-tool" and plugin["transport"] == "streamable_http"
    assert plugin["header_names"] == ["Authorization"] and plugin["has_headers"] is True
    texts = [resp.text]
    async with app.state.sessionmaker() as db:
        row = await db.get(McpPlugin, plugin["id"])
        assert row.headers_encrypted and SECRET not in row.headers_encrypted
    # 不提交 headers = 保持原值
    resp = await client.patch(f"{BASE}/{plugin['id']}", json={"name": "词典"})
    texts.append(resp.text)
    assert resp.json()["header_names"] == ["Authorization"] and resp.json()["name"] == "词典"
    fake_mcp.add(URL, [tool("lookup", {"word": {"type": "string"}}, ["word"])])
    resp = await client.post(f"{BASE}/{plugin['id']}/refresh")
    texts.append(resp.text)
    assert fake_mcp.targets[-1].headers == {"Authorization": SECRET}
    resp = await client.post(f"{BASE}/{plugin['id']}/call", json={"tool": "lookup", "arguments": {"word": "剑"}})
    texts.append(resp.text)
    texts.append((await client.get(BASE)).text)
    texts.append((await client.get(f"{BASE}/{plugin['id']}")).text)
    # 整体替换为另一个头；null 清空
    resp = await client.patch(f"{BASE}/{plugin['id']}", json={"headers": {"X-Api-Key": "other-secret-value"}})
    texts.append(resp.text)
    assert resp.json()["header_names"] == ["X-Api-Key"]
    resp = await client.patch(f"{BASE}/{plugin['id']}", json={"headers": None})
    assert resp.json()["has_headers"] is False and resp.json()["header_names"] == []
    for text in texts:
        assert "sk-very-secret-token" not in text and "other-secret-value" not in text
    assert (await client.delete(f"{BASE}/{plugin['id']}")).status_code == 204
    assert (await client.get(BASE)).json() == []


async def test_limit_slug_and_url_validation(client, monkeypatch):
    await register(client)
    assert (await _create(client, name="同名")).status_code == 400  # 中文名生成不了标识
    assert (await _create(client, name="同名", slug="same")).status_code == 201
    assert (await _create(client, name="另一个", slug="same")).status_code == 409
    assert (await _create(client, slug="Bad_Slug")).status_code == 422
    assert (await _create(client, url="ftp://x.example.com")).status_code == 400
    resp = await _create(client, url="https://user:pass@x.example.com/mcp")
    assert resp.status_code == 400 and "用户名或密码" in resp.json()["detail"]
    assert (await _create(client, headers={"Host": "evil"})).status_code == 422
    assert (await _create(client, headers={"X-A": "a\r\nInjected: 1"})).status_code == 422
    monkeypatch.setattr(mcp_service, "MAX_PLUGINS", 2)
    assert (await _create(client, slug="second")).status_code == 201
    resp = await _create(client, slug="third")
    assert resp.status_code == 400 and "最多 2 个插件" in resp.json()["detail"]


@pytest.mark.parametrize("url", ["http://127.0.0.1:9/mcp", "http://internal.example.test/mcp", "http://[::1]/mcp"])
async def test_private_address_blocked_on_save(client, url):
    await register(client)
    resp = await _create(client, url=url)
    assert resp.status_code == 400 and "内网" in resp.json()["detail"]


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_private_address_allowed_with_switch(client):
    await register(client)
    assert (await _create(client, url="http://127.0.0.1:9/mcp")).status_code == 201


async def test_import_mcp_servers_json(client, fake_mcp):
    await register(client)
    body = {
        "mcpServers": {
            "search": {"url": "https://a.example.com/mcp", "headers": {"Authorization": SECRET}},
            "legacy": {"serverUrl": "https://b.example.com/sse", "type": "sse"},
            "http-typed": {"url": "https://c.example.com/mcp", "transport": "streamable-http"},
            "local": {"command": "npx", "args": ["server"]},
            "中文名": {"url": "https://d.example.com/mcp"},
            "nourl": {"type": "sse"},
            "weird": {"url": "https://e.example.com", "type": "websocket"},
            "inner": {"url": "http://10.1.2.3/mcp"},
            # 值不是对象：只让这一项失败，不让整个请求 422
            "str-item": "https://f.example.com/mcp",
            "list-item": [1, 2],
            "null-item": None,
            "bad-headers": {"url": "https://g.example.com/mcp", "headers": {"X-Num": 1}},
        }
    }
    resp = await client.post(f"{BASE}/import", json=body)
    assert resp.status_code == 200, resp.text
    assert SECRET not in resp.text
    items = {i["name"]: i for i in resp.json()["items"]}
    assert items["search"]["status"] == "created" and items["search"]["plugin"]["header_names"] == ["Authorization"]
    assert items["legacy"]["plugin"]["transport"] == "sse"
    assert items["http-typed"]["plugin"]["transport"] == "streamable_http"
    assert items["local"]["status"] == "error" and "stdio" in items["local"]["detail"]
    assert items["中文名"]["status"] == "error" and "标识" in items["中文名"]["detail"]
    assert items["nourl"]["status"] == "error" and items["weird"]["status"] == "error"
    assert items["inner"]["status"] == "error" and "内网" in items["inner"]["detail"]
    for name in ("str-item", "list-item", "null-item"):
        assert items[name] == {"name": name, "status": "error", "detail": "配置应为对象", "plugin": None}
    assert items["bad-headers"]["status"] == "error" and items["bad-headers"]["detail"]
    assert len((await client.get(BASE)).json()) == 3
    # 顶层仍须是对象
    assert (await client.post(f"{BASE}/import", json={"mcpServers": []})).status_code == 422


async def test_refresh_stores_tools_drops_invalid_schema_and_records_error(client, fake_mcp):
    await register(client)
    plugin = (await _create(client)).json()
    fake_mcp.add(
        URL,
        [
            tool("lookup", {"word": {"type": "string"}}, ["word"], read_only_hint=True),
            McpToolInfo(name="broken", input_schema={"type": "object", "properties": {"x": {"type": 12}}}),
            McpToolInfo(name="notobject", input_schema={"type": "string"}),
            tool("send"),
        ],
    )
    resp = await client.post(f"{BASE}/{plugin['id']}/refresh")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [t["name"] for t in data["tools"]] == ["lookup", "send"]
    lookup = data["tools"][0]
    assert lookup["read_only_hint"] is True and lookup["enabled"] is True and lookup["auto_confirm"] is False
    assert len(lookup["schema_hash"]) == 64
    assert "broken" in data["last_error"] and "notobject" in data["last_error"]
    assert data["tools_refreshed_at"] and data["last_tested_at"]

    fake_mcp.servers[URL].connect_error = McpError("MCP 服务拒绝访问（HTTP 401），请检查请求头中的鉴权信息")
    resp = await client.post(f"{BASE}/{plugin['id']}/refresh")
    assert resp.status_code == 400 and "401" in resp.json()["detail"]
    after = (await client.get(f"{BASE}/{plugin['id']}")).json()
    assert "401" in after["last_error"] and [t["name"] for t in after["tools"]] == ["lookup", "send"]


async def test_tool_prefs_and_trial_call(client, fake_mcp):
    await register(client)
    plugin = (await _create(client)).json()
    fake_mcp.add(
        URL,
        [tool("lookup", {"word": {"type": "string", "maxLength": 5}}, ["word"])],
        handler=lambda name, args: McpResult(text="释义：" + args["word"] * 30000),
    )
    await client.post(f"{BASE}/{plugin['id']}/refresh")
    tools_url = f"{BASE}/{plugin['id']}/tools"
    resp = await client.patch(tools_url, json={"tool": "lookup", "auto_confirm": True})
    assert resp.json()["tools"][0] == {**resp.json()["tools"][0], "enabled": True, "auto_confirm": True}
    resp = await client.patch(tools_url, json={"tool": "lookup", "enabled": False})
    assert resp.json()["tools"][0]["enabled"] is False and resp.json()["tools"][0]["auto_confirm"] is True
    assert (await client.patch(tools_url, json={"tool": "nope", "enabled": True})).status_code == 404
    assert (await client.patch(tools_url, json={"enabled": True})).status_code == 422

    resp = await client.post(f"{BASE}/{plugin['id']}/call", json={"tool": "lookup", "arguments": {}})
    assert resp.status_code == 400 and resp.json()["errors"]
    resp = await client.post(
        f"{BASE}/{plugin['id']}/call", json={"tool": "lookup", "arguments": {"word": "太长的参数值"}}
    )
    assert resp.status_code == 400
    assert fake_mcp.calls == []
    resp = await client.post(f"{BASE}/{plugin['id']}/call", json={"tool": "lookup", "arguments": {"word": "剑"}})
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["text"].startswith("释义：剑") and len(result["text"]) == mcp_service.TRIAL_LIMIT
    assert result["truncated"] is True and result["is_error"] is False
    assert (await client.post(f"{BASE}/{plugin['id']}/call", json={"tool": "nope"})).status_code == 404
    assert fake_mcp.open == 0 and fake_mcp.closes == fake_mcp.connects


async def test_trial_timeout_is_error(client, fake_mcp, monkeypatch):
    await register(client)
    plugin = (await _create(client)).json()

    async def slow(name, args):
        await asyncio.sleep(5)

    fake_mcp.add(URL, [tool("slow")], handler=slow)
    await client.post(f"{BASE}/{plugin['id']}/refresh")
    monkeypatch.setattr(mcp_service, "CALL_TIMEOUT", 0.05)
    resp = await client.post(f"{BASE}/{plugin['id']}/call", json={"tool": "slow"})
    assert resp.status_code == 400 and "超时" in resp.json()["detail"]


async def test_cross_user_404(client, make_client, fake_mcp):
    await register(client)
    plugin = (await _create(client)).json()
    bob = make_client()
    await register(bob, "bob")
    pid = plugin["id"]
    for method, path, body in [
        ("GET", f"{BASE}/{pid}", None),
        ("PATCH", f"{BASE}/{pid}", {"name": "x"}),
        ("DELETE", f"{BASE}/{pid}", None),
        ("POST", f"{BASE}/{pid}/refresh", None),
        ("PATCH", f"{BASE}/{pid}/tools", {"tool": "lookup", "enabled": False}),
        ("POST", f"{BASE}/{pid}/call", {"tool": "lookup"}),
    ]:
        resp = await bob.request(method, path, json=body)
        assert resp.status_code == 404, (method, path, resp.text)
    assert (await bob.get(BASE)).json() == []
    assert fake_mcp.connects == 0


@pytest.mark.parametrize("settings_overrides", [{"mcp_enabled": False}])
async def test_disabled_by_admin_returns_403(client):
    await register(client)
    assert (await client.get("/api/v1/auth/config")).json()["mcp_enabled"] is False
    for method, path, body in [
        ("GET", BASE, None),
        ("POST", BASE, {"name": "x", "url": URL}),
        ("POST", f"{BASE}/import", {"mcpServers": {"x": {"url": URL}}}),
        ("GET", f"{BASE}/1", None),
        ("POST", f"{BASE}/1/refresh", None),
        ("PATCH", f"{BASE}/1/tools", {"tool": "x", "enabled": False}),
        ("POST", f"{BASE}/1/call", {"tool": "x"}),
    ]:
        resp = await client.request(method, path, json=body)
        assert resp.status_code == 403 and resp.json()["detail"] == "管理员已关闭 MCP", (method, path)


# ---------------- 真实连接：SSRF、重定向、超时与 FastMCP 集成 ----------------


class Recorder:
    """记录收到的请求路径；/redirect 返回同源 307，/target 正常应答。"""

    def __init__(self):
        self.paths: list[str] = []

        async def redirect(request):
            self.paths.append(request.url.path)
            return RedirectResponse("/target", status_code=307)

        async def target(request):
            self.paths.append(request.url.path)
            return JSONResponse({"jsonrpc": "2.0", "id": 0, "result": {}})

        self.app = Starlette(
            routes=[
                Route("/redirect", redirect, methods=["GET", "POST"]),
                Route("/target", target, methods=["GET", "POST"]),
            ]
        )


@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_connect_time_ssrf_check_sends_no_request(client, app):
    await register(client)
    recorder = Recorder()
    async with serve(recorder.app) as base:
        plugin = (await _create(client, url=f"{base}/target", headers=None)).json()
        app.state.settings.allow_private_ai_endpoints = False
        resp = await client.post(f"{BASE}/{plugin['id']}/refresh")
        assert resp.status_code == 400 and "内网" in resp.json()["detail"]
        assert recorder.paths == []


async def test_redirect_not_followed():
    recorder = Recorder()
    async with serve(recorder.app) as base:
        target = mcp_client.McpTarget(url=f"{base}/redirect", transport="streamable_http")
        with pytest.raises(McpError) as info:
            async with mcp_client.connect(target, allow_private=True):
                pass
        assert "重定向" in info.value.detail
        assert recorder.paths and set(recorder.paths) == {"/redirect"}


def _fastmcp() -> FastMCP:
    server = FastMCP("novel-tools", transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @server.tool(description="查询词语释义")
    def lookup(word: str) -> str:
        return f"{word}：一种兵器"

    @server.tool()
    async def slow() -> str:
        await asyncio.sleep(5)
        return "太慢"

    return server


@pytest.mark.parametrize("transport", ["streamable_http", "sse"])
@pytest.mark.parametrize("settings_overrides", [{"allow_private_ai_endpoints": True}])
async def test_fastmcp_integration_list_and_call(client, app, monkeypatch, transport):
    """真实协议：经 API 刷新工具、试用调用；请求头被发送到服务端但不回显。"""
    await register(client)
    server = _fastmcp()
    asgi = server.streamable_http_app() if transport == "streamable_http" else server.sse_app()
    seen_headers: list[str] = []

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            seen_headers.extend(v.decode() for k, v in scope["headers"] if k == b"authorization")
        await asgi(scope, receive, send)

    async with serve(wrapped) as base:
        url = f"{base}/mcp" if transport == "streamable_http" else f"{base}/sse"
        plugin = (await _create(client, url=url, transport=transport)).json()
        resp = await client.post(f"{BASE}/{plugin['id']}/refresh")
        assert resp.status_code == 200, resp.text
        tools = {t["name"]: t for t in resp.json()["tools"]}
        assert set(tools) == {"lookup", "slow"} and tools["lookup"]["description"] == "查询词语释义"
        assert tools["lookup"]["input_schema"]["required"] == ["word"]
        resp = await client.post(f"{BASE}/{plugin['id']}/call", json={"tool": "lookup", "arguments": {"word": "剑"}})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"text": "剑：一种兵器", "is_error": False, "truncated": False}
        assert SECRET in seen_headers
        monkeypatch.setattr(mcp_client, "CALL_TIMEOUT", 0.3)
        resp = await client.post(f"{BASE}/{plugin['id']}/call", json={"tool": "slow"})
        assert resp.status_code == 400 and "超时" in resp.json()["detail"]
        assert SECRET not in resp.text


async def test_to_text_converts_content_types():
    from mcp import types

    result = types.CallToolResult(
        content=[
            types.TextContent(type="text", text="第一段"),
            types.ImageContent(type="image", data="aGk=", mimeType="image/png"),
            types.AudioContent(type="audio", data="aGk=", mimeType="audio/wav"),
            types.EmbeddedResource(
                type="resource", resource=types.TextResourceContents(uri="file:///a.txt", text="资源文本")
            ),
            types.EmbeddedResource(
                type="resource", resource=types.BlobResourceContents(uri="file:///b.bin", blob="aGk=")
            ),
        ],
        structuredContent={"count": 2},
        isError=False,
    )
    converted = mcp_client.to_text(result)
    assert converted.text.split("\n") == [
        "第一段",
        "[已省略图片内容]",
        "[已省略音频内容]",
        "资源文本",
        "[已省略二进制资源]",
        '{"count": 2}',
    ]
    error = mcp_client.to_text(
        types.CallToolResult(content=[types.TextContent(type="text", text="坏了")], isError=True)
    )
    assert error.as_tool_data() == {"error": "坏了"}


async def test_describe_redacts_header_values():
    text = mcp_client.describe(RuntimeError(f"server said {SECRET}"), {"Authorization": SECRET})
    assert SECRET not in text and "***" in text
    # 只回显 token 部分也要遮蔽
    token = SECRET.split()[-1]
    text = mcp_client.describe(RuntimeError(f"invalid token {token}"), {"Authorization": SECRET})
    assert token not in text and "***" in text


async def test_plugin_rows_stay_user_scoped(app, client):
    await register(client)
    await _create(client)
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(select(McpPlugin)))
        assert len(rows) == 1 and rows[0].user_id == 1


async def test_tool_pref_for_name_with_slash(client, fake_mcp):
    """工具名可以含 / ? # 等字符：放在请求体里，不会被路径拆错。"""
    await register(client)
    plugin = (await _create(client)).json()
    fake_mcp.add(URL, [tool("files/read"), tool("a?b#c")])
    await client.post(f"{BASE}/{plugin['id']}/refresh")
    tools_url = f"{BASE}/{plugin['id']}/tools"
    for name in ("files/read", "a?b#c"):
        resp = await client.patch(tools_url, json={"tool": name, "auto_confirm": True})
        assert resp.status_code == 200, resp.text
        assert next(t for t in resp.json()["tools"] if t["name"] == name)["auto_confirm"] is True
    others = [t for t in resp.json()["tools"] if t["name"] not in ("files/read", "a?b#c")]
    assert others == []


async def test_connect_checks_address_before_opening_transport(monkeypatch):
    """连接前先校验地址：被拦截的地址连传输层与会话都不建立（每个请求的校验是第二道防线）。"""
    import mcp

    def no_session(*args, **kwargs):
        raise AssertionError("被拦截的地址不应建立 MCP 会话")

    monkeypatch.setattr(mcp, "ClientSession", no_session)
    target = mcp_client.McpTarget(url="http://127.0.0.1:9/mcp", transport="streamable_http")
    with pytest.raises(McpError) as info:
        async with mcp_client.connect(target, allow_private=False):
            pass
    assert "内网" in info.value.detail
