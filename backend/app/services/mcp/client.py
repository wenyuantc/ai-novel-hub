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
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal, Protocol

import httpx
from jsonschema import validators
from jsonschema.exceptions import SchemaError

from app.ai.endpoint_guard import ensure_endpoint_allowed
from app.ai.providers.base import AIProviderError

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
    def __init__(self, session: Any, headers: dict[str, str]):
        self._session = session
        self._headers = headers

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
                result = await self._session.call_tool(
                    name, arguments, read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT)
                )
        except TimeoutError as exc:
            raise McpError(f"MCP 工具调用超时（{CALL_TIMEOUT:g} 秒）") from exc
        except Exception as exc:  # noqa: BLE001
            raise McpError(describe(exc, self._headers)) from exc
        return to_text(result)


@asynccontextmanager
async def connect(target: McpTarget, *, allow_private: bool) -> AsyncIterator[McpConnection]:
    """建立一次 MCP 会话（initialize 完成后 yield），退出时关闭连接。"""
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client

    await guard_url(target.url, allow_private=allow_private)

    async def check_request(request: httpx.Request) -> None:
        # 每个请求都校验（SSE 的消息地址由服务端下发，DNS 也可能在两次请求之间变化）
        await guard_url(str(request.url), allow_private=allow_private)

    def client_factory(
        headers: dict[str, str] | None = None, timeout: httpx.Timeout | None = None, auth: httpx.Auth | None = None
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={**(headers or {}), **target.headers},
            timeout=httpx.Timeout(CALL_TIMEOUT, read=STREAM_READ_TIMEOUT),
            auth=auth,
            follow_redirects=False,
            # SDK 的 stream_within_origin 只看 max_redirects：0 = 任何重定向都不跟随
            max_redirects=0,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
            event_hooks={"request": [check_request]},
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
                    yield _SdkConnection(session, target.headers)
        finally:
            if http_client is not None:
                await http_client.aclose()
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
