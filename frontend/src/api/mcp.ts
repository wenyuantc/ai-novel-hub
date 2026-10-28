// MCP 插件（/api/v1/mcp-plugins，用户私有；部署关闭 MCP 时全部 403）

import { api } from './client'
import type {
  McpCallResult,
  McpImportResult,
  McpPlugin,
  McpPluginCreate,
  McpPluginUpdate,
  McpToolPrefRequest,
  McpToolPrefUpdate,
} from './types'

export const mcpApi = {
  list: () => api.get<McpPlugin[]>('/mcp-plugins'),
  get: (id: number) => api.get<McpPlugin>(`/mcp-plugins/${id}`),
  create: (body: McpPluginCreate) => api.post<McpPlugin>('/mcp-plugins', body),
  /** 标准 mcpServers JSON，逐项返回结果（某一项不是对象只让该项失败） */
  import: (mcpServers: Record<string, unknown>) =>
    api.post<McpImportResult>('/mcp-plugins/import', { mcpServers }),
  /** 不带 headers = 保持原值 */
  update: (id: number, body: McpPluginUpdate) => api.patch<McpPlugin>(`/mcp-plugins/${id}`, body),
  remove: (id: number) => api.del(`/mcp-plugins/${id}`),
  /** 测试连接并刷新工具列表；失败 400（last_error 同时写库） */
  refresh: (id: number) => api.post<McpPlugin>(`/mcp-plugins/${id}/refresh`),
  /** 工具名放在请求体里（工具名可以含 /，放进路径会 404） */
  updateTool: (id: number, tool: string, body: McpToolPrefUpdate) =>
    api.patch<McpPlugin>(`/mcp-plugins/${id}/tools`, { ...body, tool } satisfies McpToolPrefRequest),
  /** 试用调用；参数不符合 inputSchema → 400，ApiError.body.errors */
  call: (id: number, tool: string, args: Record<string, unknown>) =>
    api.post<McpCallResult>(`/mcp-plugins/${id}/call`, { tool, arguments: args }),
}

export const MAX_MCP_PLUGINS = 10
export const MCP_TRANSPORT_LABELS: Record<McpPlugin['transport'], string> = {
  streamable_http: 'Streamable HTTP',
  sse: 'SSE（旧版）',
}
