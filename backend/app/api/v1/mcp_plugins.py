"""MCP 插件（用户私有）。部署开关 MCP_ENABLED=false 时全部 403。固定路径（/import）先于 /{plugin_id}。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status

from app.core.deps import CipherDep, CurrentUser, DbDep, SettingsDep
from app.schemas.mcp import (
    McpCallRequest,
    McpCallResult,
    McpImportRequest,
    McpImportResult,
    McpPluginCreate,
    McpPluginOut,
    McpPluginUpdate,
    McpToolPrefUpdate,
)
from app.services.mcp import service as svc


def require_mcp(settings: SettingsDep) -> None:
    svc.ensure_enabled(settings)


def get_connector(request: Request) -> Any:
    return request.app.state.task_engine.ctx.mcp_connector


ConnectorDep = Annotated[Any, Depends(get_connector)]

router = APIRouter(prefix="/mcp-plugins", tags=["mcp"], dependencies=[Depends(require_mcp)])


@router.get("", response_model=list[McpPluginOut])
async def list_plugins(user: CurrentUser, db: DbDep, cipher: CipherDep) -> list[McpPluginOut]:
    return await svc.list_plugins(db, cipher, user.id)


@router.post("", response_model=McpPluginOut, status_code=status.HTTP_201_CREATED)
async def create_plugin(
    body: McpPluginCreate, user: CurrentUser, db: DbDep, settings: SettingsDep, cipher: CipherDep
) -> McpPluginOut:
    return await svc.create_plugin(db, settings, cipher, user.id, body)


@router.post("/import", response_model=McpImportResult)
async def import_plugins(
    body: McpImportRequest, user: CurrentUser, db: DbDep, settings: SettingsDep, cipher: CipherDep
) -> McpImportResult:
    return await svc.import_servers(db, settings, cipher, user.id, body.mcpServers)


@router.get("/{plugin_id}", response_model=McpPluginOut)
async def get_plugin(plugin_id: int, user: CurrentUser, db: DbDep, cipher: CipherDep) -> McpPluginOut:
    return svc.to_out(cipher, await svc.get_owned_plugin(db, user.id, plugin_id))


@router.patch("/{plugin_id}", response_model=McpPluginOut)
async def update_plugin(
    plugin_id: int, body: McpPluginUpdate, user: CurrentUser, db: DbDep, settings: SettingsDep, cipher: CipherDep
) -> McpPluginOut:
    return await svc.update_plugin(db, settings, cipher, user.id, plugin_id, body)


@router.delete("/{plugin_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plugin(plugin_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_plugin(db, user.id, plugin_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{plugin_id}/refresh", response_model=McpPluginOut)
async def refresh_plugin(
    plugin_id: int, user: CurrentUser, db: DbDep, settings: SettingsDep, cipher: CipherDep, connector: ConnectorDep
) -> McpPluginOut:
    return await svc.refresh_tools(db, settings, cipher, connector, user.id, plugin_id)


@router.patch("/{plugin_id}/tools", response_model=McpPluginOut)
async def update_tool(
    plugin_id: int, body: McpToolPrefUpdate, user: CurrentUser, db: DbDep, cipher: CipherDep
) -> McpPluginOut:
    # 工具名在请求体里（可能含 /，放在路径中会 404）
    return await svc.update_tool_pref(db, cipher, user.id, plugin_id, body)


@router.post("/{plugin_id}/call", response_model=McpCallResult)
async def call_tool(
    plugin_id: int,
    body: McpCallRequest,
    user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    cipher: CipherDep,
    connector: ConnectorDep,
) -> McpCallResult:
    return await svc.call_tool(db, settings, cipher, connector, user.id, plugin_id, body.tool, body.arguments)
