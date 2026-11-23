"""MCP 适配层：官方 mcp SDK（1.x）的远程客户端（Streamable HTTP / 旧版 SSE），不支持 stdio。

- 连接前与每个 HTTP 请求前都做 SSRF 校验（endpoint_guard，沿用 ALLOW_PRIVATE_AI_ENDPOINTS）；
- 不跟随任何重定向：SDK 1.x 会自行跟随同源重定向（不看 follow_redirects），这里用 max_redirects=0 关掉；
- 初始化 15 秒、单次调用 30 秒（asyncio.timeout）；
- 统一抛 McpError(detail)，detail 不包含请求头内容（按请求头取值做脱敏）。

测试通过 TaskContext.mcp_connector 注入替身（tests.mcp_fakes.FakeMcpConnector）。
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from typing import Any, Literal, Protocol
from weakref import WeakValueDictionary

import httpx
from jsonschema import validators
from jsonschema.exceptions import SchemaError

from app.ai.endpoint_guard import ensure_endpoint_allowed
from app.ai.providers.base import AIProviderError
from app.services.mcp.management import ManagementHttpClient, ManagementRequestGate

logger = logging.getLogger(__name__)

Transport = Literal["streamable_http", "sse"]
CONNECT_TIMEOUT = 15.0
CALL_TIMEOUT = 30.0
# 长连接流（SSE / Streamable HTTP 的 GET 流）在两次调用之间可能空闲较久；调用本身由 CALL_TIMEOUT 限制
STREAM_READ_TIMEOUT = 300.0
MAX_TOOLS = 100
DESCRIPTION_LIMIT = 2000


class McpError(Exception):
    """MCP 连接 / 调用失败；detail 为可展示的中文说明，不含请求头内容。"""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass
class McpToolInfo:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    read_only_hint: bool | None = None


@dataclass
class McpResult:
    """to_text 的结果：只保留文本；二进制内容替换为说明。"""

    text: str
    is_error: bool = False

    def as_tool_data(self) -> dict[str, Any]:
        return {"error": self.text} if self.is_error else {"text": self.text}


class McpConnection(Protocol):
    async def list_tools(self) -> list[McpToolInfo]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpResult: ...


@dataclass(frozen=True)
class McpTarget:
    url: str
    transport: Transport
    headers: dict[str, str] = field(default_factory=dict)


McpConnector = Callable[..., AbstractAsyncContextManager[McpConnection]]


_test_connectors: WeakValueDictionary[int, Any] = WeakValueDictionary()


def _register_test_connector(connector: Any) -> None:
    """测试替身须显式登记，且仍必须执行同一请求门禁。"""
    _test_connectors[id(connector)] = connector


def _trusted_connector(connector: Any) -> bool:
    return connector is connect or _test_connectors.get(id(connector)) is connector


class McpRequestGate:
    """将一个串行工具调用绑定到 SDK 真正发出的唯一 JSON-RPC POST。"""

    def __init__(self) -> None:
        self._call = None
        self._leases: dict[int, AsyncExitStack] = {}
        self.failure: asyncio.Future | None = None

    def arm(self, tool: str, arguments: dict, state: Any, acquire: Callable, *, future: asyncio.Future) -> None:
        if self._call is not None or self._leases:
            raise McpError("外部工具发送状态异常")
        # 冻结参数，后续不能用修改后的可变对象替换原请求。
        frozen = json.loads(json.dumps(arguments, ensure_ascii=False, sort_keys=True))
        self._call = {"tool": tool, "arguments": frozen, "state": state, "acquire": acquire, "used": False,
                      "future": future}
        self.failure = asyncio.get_running_loop().create_future()

    def disarm(self) -> None:
        self._call = None

    def _reject(self) -> None:
        error = McpError("外部工具发送身份无法确认")
        if self.failure is not None and not self.failure.done():
            self.failure.set_result(error)
        raise error

    async def before_request(self, request: httpx.Request) -> None:
        if request.method != "POST":
            return
        try:
            body = json.loads(request.content)
        except (ValueError, httpx.RequestNotRead):
            if self._call is not None:
                self._reject()
            return
        if not isinstance(body, dict):
            if self._call is not None:
                self._reject()
            return
        if body.get("method") != "tools/call":
            return
        call = self._call
        if (call is None or call["used"] or not isinstance(body.get("params"), dict)
                or body["params"].get("name") != call["tool"]
                or body["params"].get("arguments", {}) != call["arguments"]
                or type(body.get("id")) not in (str, int)):
            self._reject()
        # 在第一个 await 前占用一次性资格；重复 POST 无法重放。
        call["used"] = True
        lease = AsyncExitStack()
        try:
            await lease.enter_async_context(call["acquire"]())
            if call["future"].cancelled():
                raise McpError("外部工具调用已取消")
            self._leases[id(request)] = lease
            call["state"].started = True
        except BaseException as exc:
            await lease.aclose()
            if self.failure is not None and not self.failure.done():
                self.failure.set_result(exc)
            raise

    async def after_headers(self, response: httpx.Response) -> None:
        if lease := self._leases.pop(id(response.request), None):
            await lease.aclose()

    async def aclose(self) -> None:
        leases, self._leases = list(self._leases.values()), {}
        for lease in leases:
            await lease.aclose()
        self.disarm()


def schema_hash(schema: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def check_tool_schema(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    if schema.get("type", "object") != "object":
        return False
    try:
        validators.validator_for(schema).check_schema(schema)
    except SchemaError:
        return False
    return True


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> list[dict[str, str]]:
    """按工具 inputSchema 校验参数，返回 [{field, message}]（为空 = 通过）。"""
    validator_cls = validators.validator_for(schema)
    errors = sorted(validator_cls(schema).iter_errors(arguments), key=lambda e: list(e.absolute_path))
    return [
        {"field": ".".join(str(p) for p in e.absolute_path) or "(参数)", "message": e.message[:300]}
        for e in errors[:20]
    ]


def to_text(result: Any) -> McpResult:
    """CallToolResult → 文本：text 按顺序换行拼接，图片 / 音频 / 二进制资源替换为说明，structuredContent 追加 JSON。"""
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        kind = getattr(item, "type", "")
        if kind == "text":
            parts.append(str(getattr(item, "text", "")))
        elif kind == "image":
            parts.append("[已省略图片内容]")
        elif kind == "audio":
            parts.append("[已省略音频内容]")
        elif kind == "resource":
            resource = getattr(item, "resource", None)
            text = getattr(resource, "text", None)
            parts.append(str(text) if isinstance(text, str) else "[已省略二进制资源]")
        elif kind == "resource_link":
            parts.append(f"[资源链接：{getattr(item, 'name', '')} {getattr(item, 'uri', '')}]".strip())
        else:
            parts.append("[已省略不支持的内容]")
    structured = getattr(result, "structuredContent", None)
    # FastMCP 等会把返回值同时放进 {"result": 文本}；与文本内容相同时不重复追加
    if structured is not None and structured != {"result": "\n".join(parts)}:
        parts.append(json.dumps(structured, ensure_ascii=False, default=str))
    return McpResult(text="\n".join(parts), is_error=bool(getattr(result, "isError", False)))


def _redact(text: str, headers: dict[str, str]) -> str:
    # 服务端可能只回显 token 部分（如 "Bearer xxx" 里的 xxx），按空白拆开的长片段也要遮蔽；长的先替换
    secrets = set()
    for value in headers.values():
        if value and len(value) >= 3:
            secrets.add(value)
            secrets.update(part for part in value.split() if len(part) >= 6)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text[:500]


def _leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def describe(exc: BaseException, headers: dict[str, str]) -> str:
    """异常 → 中文说明（不回显请求头、不带堆栈）。"""
    from mcp.shared.exceptions import McpError as SdkMcpError

    exc = _leaf(exc)
    if isinstance(exc, McpError):
        return exc.detail
    if isinstance(exc, AIProviderError):
        return _plugin_wording(str(exc))
    if isinstance(exc, TimeoutError | httpx.TimeoutException):
        return "MCP 服务响应超时"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if 300 <= status < 400:
            return f"MCP 服务返回重定向（HTTP {status}），出于安全考虑不跟随，请直接填写最终地址"
        if status in (401, 403):
            return f"MCP 服务拒绝访问（HTTP {status}），请检查请求头中的鉴权信息"
        return f"MCP 服务返回错误（HTTP {status}）"
    if isinstance(exc, httpx.ConnectError):
        return "无法连接 MCP 服务，请检查地址是否正确、服务是否已启动"
    if isinstance(exc, httpx.HTTPError):
        return f"MCP 连接失败：{_redact(exc.__class__.__name__, headers)}"
    if isinstance(exc, SdkMcpError):
        return f"MCP 服务返回错误：{_redact(str(exc.error.message), headers)}"
    return f"MCP 调用失败：{_redact(str(exc) or exc.__class__.__name__, headers)}"


def _plugin_wording(text: str) -> str:
    return text.replace(" Base URL ", "插件地址").replace("Base URL ", "插件地址")


async def guard_url(url: str, *, allow_private: bool) -> None:
    if allow_private:
        return
    try:
        await ensure_endpoint_allowed(url)
    except AIProviderError as exc:
        raise McpError(_plugin_wording(str(exc))) from exc


def _tool_info(tool: Any) -> McpToolInfo:
    annotations = getattr(tool, "annotations", None)
    hint = getattr(annotations, "readOnlyHint", None) if annotations is not None else None
    return McpToolInfo(
        name=str(tool.name),
        description=str(getattr(tool, "description", "") or "")[:DESCRIPTION_LIMIT],
        input_schema=dict(getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}),
        read_only_hint=hint if isinstance(hint, bool) else None,
    )


class _SdkConnection:
    def __init__(self, session: Any, headers: dict[str, str], *, management: bool):
        self._session = session
        self._headers = headers
        self._management = management

    async def list_tools(self) -> list[McpToolInfo]:
        tools: list[McpToolInfo] = []
        cursor = None
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                while len(tools) < MAX_TOOLS:
                    page = await self._session.list_tools(cursor=cursor) if cursor else await self._session.list_tools()
                    tools.extend(_tool_info(t) for t in page.tools)
                    cursor = page.nextCursor
                    if not cursor:
                        break
        except McpError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise McpError(describe(exc, self._headers)) from exc
        return tools[:MAX_TOOLS]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> McpResult:
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                # SDK 在成功调用后会查首个工具页来验证 outputSchema。试用时先取得同一页，
                # 这样关闭开关后仍能完成本地校验，无须再次外连或重放调用。
                listed = True
                if self._management:
                    page = await self._session.list_tools()
                    listed = any(item.name == name for item in page.tools)
                if listed:
                    result = await self._session.call_tool(
                        name, arguments, read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT),
                    )
                else:
                    # 旧插件可能仍可调用但没有在该页声明输出 schema。沿用 SDK 对这种结果的
                    # CallToolResult 解析；已取得工具页，再次调用 list_tools 不能补充验证。
                    from mcp import types

                    result = await self._session.send_request(
                        types.ClientRequest(types.CallToolRequest(
                            params=types.CallToolRequestParams(name=name, arguments=arguments),
                        )),
                        types.CallToolResult, request_read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT),
                    )
        except TimeoutError as exc:
            raise McpError(f"MCP 工具调用超时（{CALL_TIMEOUT:g} 秒）") from exc
        except Exception as exc:  # noqa: BLE001
            raise McpError(describe(exc, self._headers)) from exc
        return to_text(result)


@asynccontextmanager
async def connect(
    target: McpTarget, *, allow_private: bool, request_gate: McpRequestGate | ManagementRequestGate | None = None,
) -> AsyncIterator[McpConnection]:
    """建立一次 MCP 会话（initialize 完成后 yield），退出时关闭连接。"""
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client

    management = request_gate if isinstance(request_gate, ManagementRequestGate) else None
    if management is not None:
        await management.check()
    await guard_url(target.url, allow_private=allow_private)

    async def check_request(request: httpx.Request) -> None:
        # 对端可能提供新的 SSE 地址。DNS 在短读之后、物理发送门之前进行。
        if management is not None and not allow_private:
            await management.check(request)
        await guard_url(str(request.url), allow_private=allow_private)
        if request_gate is not None:
            await request_gate.before_request(request)

    def client_factory(
        headers: dict[str, str] | None = None, timeout: httpx.Timeout | None = None, auth: httpx.Auth | None = None
    ) -> httpx.AsyncClient:
        client_type = partial(ManagementHttpClient, management) if management is not None else httpx.AsyncClient
        return client_type(
            headers={**(headers or {}), **target.headers},
            timeout=httpx.Timeout(CALL_TIMEOUT, read=STREAM_READ_TIMEOUT),
            auth=auth,
            follow_redirects=False,
            # SDK 的 stream_within_origin 只看 max_redirects：0 = 任何重定向都不跟随
            max_redirects=0,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            event_hooks={"request": [check_request],
                         "response": [request_gate.after_headers] if request_gate else []},
        )

    stack_ready = False
    try:
        if target.transport == "sse":
            transport_cm = sse_client(
                target.url,
                timeout=CALL_TIMEOUT,
                sse_read_timeout=STREAM_READ_TIMEOUT,
                httpx_client_factory=client_factory,
            )
            http_client = None
        else:
            http_client = client_factory()
            transport_cm = streamable_http_client(target.url, http_client=http_client)
        try:
            async with transport_cm as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT)) as session:
                    try:
                        async with asyncio.timeout(CONNECT_TIMEOUT):
                            await session.initialize()
                    except TimeoutError as exc:
                        raise McpError(f"MCP 服务初始化超时（{CONNECT_TIMEOUT:g} 秒）") from exc
                    except McpError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        raise McpError(describe(exc, target.headers)) from exc
                    stack_ready = True
                    yield _SdkConnection(session, target.headers, management=management is not None)
        finally:
            try:
                if http_client is not None:
                    await http_client.aclose()
            finally:
                if request_gate is not None:
                    # 先结束 SDK 传输，再释放无响应头时仍持有的门禁。
                    await request_gate.aclose()
    except BaseExceptionGroup as group:
        leaf = _leaf(group)
        if isinstance(leaf, Exception) and not stack_ready:
            raise McpError(describe(leaf, target.headers)) from None
        raise leaf from None
    except McpError:
        raise
    except Exception as exc:
        if stack_ready:
            # 调用方在 with 体内抛出的异常（以及其他非 SDK 异常）原样传播
            raise
        raise McpError(describe(exc, target.headers)) from exc
