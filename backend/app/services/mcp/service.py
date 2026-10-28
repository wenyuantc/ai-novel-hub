"""MCP 插件：增删改、mcpServers 导入、刷新工具列表、工具偏好、试用调用。

- 插件按用户私有，他人一律 404；每个用户最多 MAX_PLUGINS 个；
- 地址保存时与每次连接时都做 SSRF 校验；URL 中不能带用户名密码（凭据只能放在加密的请求头里）；
- 请求头整体 JSON → Fernet 加密存 headers_encrypted，接口只回显名称；
- 部署开关 MCP_ENABLED=false 时所有接口 403。
"""

import asyncio
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.core.security import SecretCipher
from app.models import McpPlugin
from app.schemas.mcp import (
    McpCallResult,
    McpImportItem,
    McpImportResult,
    McpPluginCreate,
    McpPluginOut,
    McpPluginUpdate,
    McpToolOut,
    McpToolPrefUpdate,
)
from app.services.mcp.client import (
    CALL_TIMEOUT,
    McpConnector,
    McpError,
    McpTarget,
    check_tool_schema,
    guard_url,
    schema_hash,
    validate_arguments,
)

MAX_PLUGINS = 10
TRIAL_LIMIT = 20000
DISABLED = "管理员已关闭 MCP"
NOT_FOUND = "插件不存在"


def ensure_enabled(settings: Settings) -> None:
    if not settings.mcp_enabled:
        raise ForbiddenError(DISABLED)


def headers_of(cipher: SecretCipher, plugin: McpPlugin) -> dict[str, str]:
    if not plugin.headers_encrypted:
        return {}
    try:
        value = json.loads(cipher.decrypt(plugin.headers_encrypted))
    except ValueError as exc:
        raise BadRequestError("插件请求头解密失败，请检查 ENCRYPTION_KEY 是否被更换，或重新填写请求头") from exc
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _header_names(cipher: SecretCipher, plugin: McpPlugin) -> list[str]:
    try:
        return sorted(headers_of(cipher, plugin))
    except BadRequestError:
        return ["（解密失败）"]


def target_of(cipher: SecretCipher, plugin: McpPlugin) -> McpTarget:
    return McpTarget(url=plugin.url, transport=plugin.transport, headers=headers_of(cipher, plugin))  # type: ignore[arg-type]


def tool_pref(plugin: McpPlugin, name: str) -> tuple[bool, bool]:
    """(enabled, auto_confirm)；缺省 = 启用且需要确认。"""
    pref = (plugin.tool_prefs or {}).get(name) or {}
    return bool(pref.get("enabled", True)), bool(pref.get("auto_confirm", False))


def find_tool(plugin: McpPlugin, name: str) -> dict[str, Any] | None:
    return next((t for t in plugin.tools or [] if t.get("name") == name), None)


def to_out(cipher: SecretCipher, plugin: McpPlugin) -> McpPluginOut:
    names = _header_names(cipher, plugin)
    tools = []
    for tool in plugin.tools or []:
        enabled, auto = tool_pref(plugin, tool["name"])
        tools.append(
            McpToolOut(
                name=tool["name"],
                description=tool.get("description", ""),
                input_schema=tool.get("input_schema") or {},
                read_only_hint=tool.get("read_only_hint"),
                schema_hash=tool.get("schema_hash", ""),
                enabled=enabled,
                auto_confirm=auto,
            )
        )
    return McpPluginOut(
        id=plugin.id,
        slug=plugin.slug,
        name=plugin.name,
        transport=plugin.transport,  # type: ignore[arg-type]
        url=plugin.url,
        enabled=plugin.enabled,
        header_names=names,
        has_headers=bool(plugin.headers_encrypted),
        tools=tools,
        tools_refreshed_at=plugin.tools_refreshed_at,
        last_tested_at=plugin.last_tested_at,
        last_error=plugin.last_error,
        created_at=plugin.created_at,
        updated_at=plugin.updated_at,
    )


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:24].strip("-")
    if not slug:
        raise BadRequestError(f"无法从名称「{name[:50]}」生成插件标识，请改用英文名称或填写标识")
    return slug


async def check_url(url: str, *, allow_private: bool) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BadRequestError("插件地址格式不正确，应以 http:// 或 https:// 开头")
    if parts.username or parts.password:
        raise BadRequestError("插件地址中不能包含用户名或密码，请把鉴权信息放在请求头中")
    try:
        await guard_url(url, allow_private=allow_private)
    except McpError as exc:
        raise BadRequestError(exc.detail) from exc
    return url


async def list_plugins(db: AsyncSession, cipher: SecretCipher, user_id: int) -> list[McpPluginOut]:
    rows = await db.scalars(select(McpPlugin).where(McpPlugin.user_id == user_id).order_by(McpPlugin.id))
    return [to_out(cipher, row) for row in rows]


async def get_owned_plugin(db: AsyncSession, user_id: int, plugin_id: int) -> McpPlugin:
    row = await db.scalar(select(McpPlugin).where(McpPlugin.id == plugin_id, McpPlugin.user_id == user_id))
    if row is None:
        raise NotFoundError(NOT_FOUND)
    return row


async def _commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("插件标识已被使用，请换一个") from exc


async def create_plugin(
    db: AsyncSession, settings: Settings, cipher: SecretCipher, user_id: int, req: McpPluginCreate
) -> McpPluginOut:
    count = await db.scalar(select(func.count(McpPlugin.id)).where(McpPlugin.user_id == user_id))
    if (count or 0) >= MAX_PLUGINS:
        raise BadRequestError(f"每个用户最多 {MAX_PLUGINS} 个插件")
    url = await check_url(req.url, allow_private=settings.allow_private_ai_endpoints)
    plugin = McpPlugin(
        user_id=user_id,
        slug=req.slug or slugify(req.name),
        name=req.name.strip(),
        transport=req.transport,
        url=url,
        headers_encrypted=cipher.encrypt(json.dumps(req.headers, ensure_ascii=False)) if req.headers else None,
        enabled=req.enabled,
        tools=[],
        tool_prefs={},
    )
    db.add(plugin)
    await _commit(db)
    await db.refresh(plugin)
    return to_out(cipher, plugin)


async def update_plugin(
    db: AsyncSession, settings: Settings, cipher: SecretCipher, user_id: int, plugin_id: int, req: McpPluginUpdate
) -> McpPluginOut:
    plugin = await get_owned_plugin(db, user_id, plugin_id)
    data = req.model_dump(exclude_unset=True)
    if data.get("name") is not None:
        plugin.name = data["name"].strip()
    if data.get("slug") is not None:
        plugin.slug = data["slug"]
    if data.get("transport") is not None:
        plugin.transport = data["transport"]
    if data.get("url") is not None:
        plugin.url = await check_url(data["url"], allow_private=settings.allow_private_ai_endpoints)
    if "headers" in data:
        headers = data["headers"]
        plugin.headers_encrypted = cipher.encrypt(json.dumps(headers, ensure_ascii=False)) if headers else None
    if data.get("enabled") is not None:
        plugin.enabled = data["enabled"]
    await _commit(db)
    await db.refresh(plugin)
    return to_out(cipher, plugin)


async def delete_plugin(db: AsyncSession, user_id: int, plugin_id: int) -> None:
    plugin = await get_owned_plugin(db, user_id, plugin_id)
    await db.delete(plugin)
    await db.commit()


def _server_request(name: str, value: dict[str, Any]) -> McpPluginCreate:
    if "command" in value or "args" in value:
        raise BadRequestError("不支持 stdio 类型的 MCP（command），只能接入远程 Streamable HTTP 或 SSE 服务")
    url = value.get("url") or value.get("serverUrl")
    if not isinstance(url, str) or not url.strip():
        raise BadRequestError("缺少 url")
    kind = str(value.get("type") or value.get("transport") or "").strip().lower().replace("-", "_")
    if kind in ("", "http", "streamable_http", "streamablehttp"):
        transport = "streamable_http"
    elif kind == "sse":
        transport = "sse"
    elif kind == "stdio":
        raise BadRequestError("不支持 stdio 类型的 MCP，只能接入远程 Streamable HTTP 或 SSE 服务")
    else:
        raise BadRequestError(f"不支持的传输类型：{kind[:30]}")
    headers = value.get("headers")
    if headers is not None and not isinstance(headers, dict):
        raise BadRequestError("headers 应为对象")
    try:
        return McpPluginCreate(name=name[:100], transport=transport, url=url, headers=headers)
    except ValidationError as exc:
        reasons = "；".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors(include_input=False))
        raise BadRequestError(reasons) from exc


async def import_servers(
    db: AsyncSession, settings: Settings, cipher: SecretCipher, user_id: int, servers: dict[str, Any]
) -> McpImportResult:
    items: list[McpImportItem] = []
    for name, value in servers.items():
        try:
            if not isinstance(value, dict):
                raise BadRequestError("配置应为对象")
            req = _server_request(str(name), value)
            items.append(
                McpImportItem(
                    name=str(name), status="created", plugin=await create_plugin(db, settings, cipher, user_id, req)
                )
            )
        except AppError as exc:
            items.append(McpImportItem(name=str(name), status="error", detail=exc.detail))
    return McpImportResult(items=items)


def normalize_tools(infos: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """校验工具 schema，返回 (可用工具, 丢弃的工具名)。"""
    kept, dropped, seen = [], [], set()
    for info in infos:
        name = str(info.name).strip()
        if not name or len(name) > 128 or name in seen or not check_tool_schema(info.input_schema):
            dropped.append(name or "（无名称）")
            continue
        seen.add(name)
        kept.append(
            {
                "name": name,
                "description": info.description,
                "input_schema": info.input_schema,
                "read_only_hint": info.read_only_hint,
                "schema_hash": schema_hash(info.input_schema),
            }
        )
    return kept, dropped


async def refresh_tools(
    db: AsyncSession,
    settings: Settings,
    cipher: SecretCipher,
    connector: McpConnector,
    user_id: int,
    plugin_id: int,
) -> McpPluginOut:
    """测试连接并刷新工具列表（存库）；失败写入 last_error 并返回 400。"""
    plugin = await get_owned_plugin(db, user_id, plugin_id)
    target = target_of(cipher, plugin)
    error = ""
    infos: list[Any] = []
    try:
        async with asyncio.timeout(CALL_TIMEOUT * 2):
            async with connector(target, allow_private=settings.allow_private_ai_endpoints) as conn:
                infos = await conn.list_tools()
    except McpError as exc:
        error = exc.detail
    except TimeoutError:
        error = "MCP 服务响应超时"
    now = datetime.now(UTC)
    plugin.last_tested_at = now
    if error:
        plugin.last_error = error
        await db.commit()
        raise BadRequestError(error)
    tools, dropped = normalize_tools(infos)
    plugin.tools = tools
    plugin.tools_refreshed_at = now
    plugin.last_error = f"已丢弃 {len(dropped)} 个参数结构不合法的工具：{'、'.join(dropped[:10])}" if dropped else ""
    await db.commit()
    await db.refresh(plugin)
    return to_out(cipher, plugin)


async def update_tool_pref(
    db: AsyncSession, cipher: SecretCipher, user_id: int, plugin_id: int, req: McpToolPrefUpdate
) -> McpPluginOut:
    plugin = await get_owned_plugin(db, user_id, plugin_id)
    tool_name = req.tool
    if find_tool(plugin, tool_name) is None:
        raise NotFoundError("工具不存在")
    enabled, auto = tool_pref(plugin, tool_name)
    prefs = dict(plugin.tool_prefs or {})
    prefs[tool_name] = {
        "enabled": enabled if req.enabled is None else req.enabled,
        "auto_confirm": auto if req.auto_confirm is None else req.auto_confirm,
    }
    plugin.tool_prefs = prefs
    await db.commit()
    await db.refresh(plugin)
    return to_out(cipher, plugin)


async def call_once(
    connector: McpConnector, target: McpTarget, tool: str, arguments: dict[str, Any], *, allow_private: bool
) -> Any:
    """单独连接、调用一次、关闭；超时与连接失败统一为 McpError。"""
    try:
        async with asyncio.timeout(CALL_TIMEOUT * 2):
            async with connector(target, allow_private=allow_private) as conn:
                return await conn.call_tool(tool, arguments)
    except TimeoutError as exc:
        raise McpError(f"MCP 工具调用超时（{CALL_TIMEOUT:g} 秒）") from exc


async def call_tool(
    db: AsyncSession,
    settings: Settings,
    cipher: SecretCipher,
    connector: McpConnector,
    user_id: int,
    plugin_id: int,
    tool_name: str,
    arguments: dict[str, Any],
) -> McpCallResult:
    """本人发起的试用调用（不经提案）；参数先按 inputSchema 校验。"""
    plugin = await get_owned_plugin(db, user_id, plugin_id)
    tool = find_tool(plugin, tool_name)
    if tool is None:
        raise NotFoundError("工具不存在，请先刷新工具列表")
    errors = validate_arguments(tool.get("input_schema") or {}, arguments)
    if errors:
        raise BadRequestError("工具参数校验失败", extra={"errors": errors})
    target = target_of(cipher, plugin)
    await db.close()
    try:
        result = await call_once(
            connector, target, tool_name, arguments, allow_private=settings.allow_private_ai_endpoints
        )
    except McpError as exc:
        raise BadRequestError(exc.detail) from exc
    text = result.text
    return McpCallResult(text=text[:TRIAL_LIMIT], is_error=result.is_error, truncated=len(text) > TRIAL_LIMIT)
