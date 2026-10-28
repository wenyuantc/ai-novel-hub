// MCP 插件的纯函数：mcpServers JSON 解析（本地识别 stdio）、请求头编辑模型、试用参数解析

import type { McpPlugin } from '../api/types'

/** 与后端 McpImportRequest 的上限一致 */
export const MAX_IMPORT_SERVERS = 20

export type ServersParse =
  | {
      ok: true
      /** 可提交的远程服务 */
      servers: Record<string, Record<string, unknown>>
      /** stdio（command / args / type=stdio）——不支持，已跳过 */
      stdio: string[]
      /** 本地即可判定无效的项 */
      invalid: { name: string; reason: string }[]
    }
  | { ok: false; error: string }

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isStdio(value: Record<string, unknown>): boolean {
  const kind = String(value.type ?? value.transport ?? '').trim().toLowerCase()
  return 'command' in value || 'args' in value || kind === 'stdio'
}

/**
 * 解析粘贴的 `{"mcpServers": {name: {url | serverUrl, type?, headers?}}}`；也接受不带外层 mcpServers 的映射。
 * stdio 项在本地识别并跳过（后端同样会拒绝），只把远程服务交给后端逐项创建。
 */
export function parseMcpServersJson(text: string): ServersParse {
  let data: unknown
  try {
    data = JSON.parse(text)
  } catch {
    return { ok: false, error: '不是合法的 JSON' }
  }
  if (!isRecord(data)) return { ok: false, error: 'JSON 应为对象，形如 {"mcpServers": {...}}' }
  let map: Record<string, unknown>
  if ('mcpServers' in data) {
    if (!isRecord(data.mcpServers)) return { ok: false, error: 'mcpServers 应为对象' }
    map = data.mcpServers
  } else if (Object.keys(data).length > 0 && Object.values(data).every(isRecord)) {
    map = data
  } else {
    return { ok: false, error: '缺少 mcpServers' }
  }
  const names = Object.keys(map)
  if (names.length === 0) return { ok: false, error: 'mcpServers 中没有服务' }
  if (names.length > MAX_IMPORT_SERVERS) return { ok: false, error: `一次最多导入 ${MAX_IMPORT_SERVERS} 个服务` }
  const servers: Record<string, Record<string, unknown>> = {}
  const stdio: string[] = []
  const invalid: { name: string; reason: string }[] = []
  for (const name of names) {
    const value = map[name]
    if (!isRecord(value)) {
      invalid.push({ name, reason: '配置应为对象' })
    } else if (isStdio(value)) {
      stdio.push(name)
    } else if (typeof (value.url ?? value.serverUrl) !== 'string' || !String(value.url ?? value.serverUrl).trim()) {
      invalid.push({ name, reason: '缺少 url' })
    } else if (value.headers !== undefined && !isRecord(value.headers)) {
      invalid.push({ name, reason: 'headers 应为对象' })
    } else {
      servers[name] = value
    }
  }
  return { ok: true, servers, stdio, invalid }
}

export function stdioNotice(names: string[]): string {
  return `已跳过 stdio 类型的服务：${names.join('、')}。只支持远程 Streamable HTTP 或 SSE 服务，不会在服务器上启动本地进程。`
}

// ---------------- 请求头编辑 ----------------

export interface HeaderRow {
  key: number
  name: string
  value: string
}

/**
 * keep = 保持已保存的请求头（PATCH 不提交 headers）；edit = 重新填写并整体替换。
 * 已保存的取值不会回显，因此一旦进入 edit，所有请求头都要重新填写。
 */
export type HeadersState = { mode: 'keep' } | { mode: 'edit'; rows: HeaderRow[] }

export const MAX_HEADERS = 20
const HEADER_NAME_RE = /^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,100}$/
const RESERVED_HEADERS = new Set([
  'host',
  'content-length',
  'content-type',
  'transfer-encoding',
  'connection',
  'accept',
  'mcp-session-id',
  'mcp-protocol-version',
  'last-event-id',
])

let rowKey = 0
export function headerRow(name = '', value = ''): HeaderRow {
  rowKey += 1
  return { key: rowKey, name, value }
}

/** 编辑行 → 请求头对象；空行忽略；有问题时返回错误文案 */
export function headersFromRows(rows: HeaderRow[]): { headers: Record<string, string> } | { error: string } {
  const headers: Record<string, string> = {}
  const seen = new Set<string>()
  for (const row of rows) {
    const name = row.name.trim()
    if (!name && !row.value.trim()) continue
    if (!name) return { error: '请求头名称不能为空' }
    if (!HEADER_NAME_RE.test(name)) return { error: `请求头名称「${name}」不合法` }
    if (RESERVED_HEADERS.has(name.toLowerCase())) return { error: `请求头 ${name} 由系统管理，不能自定义` }
    if (seen.has(name.toLowerCase())) return { error: `请求头 ${name} 重复` }
    if (!row.value.trim()) return { error: `请填写请求头 ${name} 的值` }
    if (/[\r\n\0]/.test(row.value)) return { error: `请求头 ${name} 的值不能包含换行` }
    seen.add(name.toLowerCase())
    headers[name] = row.value.trim()
  }
  if (Object.keys(headers).length > MAX_HEADERS) return { error: `请求头最多 ${MAX_HEADERS} 个` }
  return { headers }
}

/** PATCH 用：keep → undefined（不提交 headers 字段，保持原值）；edit → 整体替换（{} = 清空） */
export function headersPatch(state: HeadersState): { headers?: Record<string, string> } | { error: string } {
  if (state.mode === 'keep') return {}
  const parsed = headersFromRows(state.rows)
  return 'error' in parsed ? parsed : { headers: parsed.headers }
}

// ---------------- 试用调用 ----------------

/** 试用参数文本 → 对象（空白 = {}） */
export function parseToolArguments(text: string): { args: Record<string, unknown> } | { error: string } {
  if (!text.trim()) return { args: {} }
  let data: unknown
  try {
    data = JSON.parse(text)
  } catch {
    return { error: '参数不是合法的 JSON' }
  }
  return isRecord(data) ? { args: data } : { error: '参数应为 JSON 对象' }
}

/** 从 schema 的 properties 生成参数示例骨架 */
export function argumentsTemplate(schema: Record<string, unknown>): string {
  const props = isRecord(schema.properties) ? schema.properties : {}
  const sample: Record<string, unknown> = {}
  for (const [key, def] of Object.entries(props)) {
    const type = isRecord(def) ? def.type : undefined
    sample[key] = type === 'number' || type === 'integer' ? 0 : type === 'boolean' ? false : type === 'array' ? [] : type === 'object' ? {} : ''
  }
  return JSON.stringify(sample, null, 2)
}

/** 400 响应体里的参数校验错误 → 文案列表 */
export function callErrors(body: Record<string, unknown> | null): string[] {
  const errors = body?.errors
  if (!Array.isArray(errors)) return []
  return errors.map((e) => {
    if (typeof e === 'string') return e
    if (isRecord(e)) {
      const field = typeof e.field === 'string' && e.field ? `${e.field}：` : ''
      return `${field}${typeof e.message === 'string' ? e.message : JSON.stringify(e)}`
    }
    return String(e)
  })
}

// ---------------- 表单 → 请求体 ----------------

export interface PluginFormValues {
  name: string
  slug?: string
  transport: 'streamable_http' | 'sse'
  url: string
}

/** 新建：请求头为空时不提交 */
export function pluginCreateBody(values: PluginFormValues, headers: HeadersState) {
  const parsed = headersPatch(headers)
  if ('error' in parsed) return parsed
  const body = {
    name: values.name.trim(),
    slug: values.slug?.trim() || null,
    transport: values.transport,
    url: values.url.trim(),
    ...(parsed.headers && Object.keys(parsed.headers).length ? { headers: parsed.headers } : {}),
  }
  return { body }
}

/** 修改：只提交变化的字段；请求头保持原值时不带 headers 字段 */
export function pluginUpdateBody(
  plugin: { name: string; slug: string; transport: string; url: string },
  values: PluginFormValues,
  headers: HeadersState,
) {
  const parsed = headersPatch(headers)
  if ('error' in parsed) return parsed
  const body: { name?: string; slug?: string; transport?: PluginFormValues['transport']; url?: string; headers?: Record<string, string> } = {}
  if (values.name.trim() !== plugin.name) body.name = values.name.trim()
  const slug = values.slug?.trim()
  if (slug && slug !== plugin.slug) body.slug = slug
  if (values.transport !== plugin.transport) body.transport = values.transport
  if (values.url.trim() !== plugin.url) body.url = values.url.trim()
  if (parsed.headers !== undefined) body.headers = parsed.headers
  return { body }
}

/** 插件状态：最近一次测试失败 > 已刷新工具 > 尚未测试 */
export function pluginStatus(plugin: Pick<McpPlugin, 'last_error' | 'tools_refreshed_at'>): { label: string; color: string } {
  if (plugin.last_error) return { label: '连接失败', color: 'error' }
  if (plugin.tools_refreshed_at) return { label: '已连接', color: 'success' }
  return { label: '未测试', color: 'default' }
}
