import { describe, expect, it } from 'vitest'
import {
  argumentsTemplate,
  callErrors,
  headerRow,
  headersFromRows,
  headersPatch,
  parseMcpServersJson,
  parseToolArguments,
  pluginCreateBody,
  pluginStatus,
  pluginUpdateBody,
  stdioNotice,
} from './mcp'

const plugin = { name: '词典', slug: 'dict', transport: 'streamable_http', url: 'https://example.com/mcp' }
const values = { name: '词典', slug: 'dict', transport: 'streamable_http' as const, url: 'https://example.com/mcp' }

describe('mcpServers JSON 导入', () => {
  it('解析标准格式与不带外层 mcpServers 的映射', () => {
    const text = JSON.stringify({ mcpServers: { dict: { url: 'https://a.example/mcp', headers: { Authorization: 'x' } }, old: { type: 'sse', serverUrl: 'https://b.example/sse' } } })
    const r = parseMcpServersJson(text)
    expect(r).toEqual({ ok: true, servers: { dict: { url: 'https://a.example/mcp', headers: { Authorization: 'x' } }, old: { type: 'sse', serverUrl: 'https://b.example/sse' } }, stdio: [], invalid: [] })
    expect(parseMcpServersJson('{"x": {"url": "https://c.example"}}')).toMatchObject({ ok: true, servers: { x: { url: 'https://c.example' } } })
  })
  it('stdio（command / args / type=stdio）在本地识别并跳过，给出提示', () => {
    const r = parseMcpServersJson(JSON.stringify({ mcpServers: { fs: { command: 'npx', args: ['server'] }, py: { type: 'stdio' }, web: { url: 'https://w.example' } } }))
    expect(r).toMatchObject({ ok: true, stdio: ['fs', 'py'], servers: { web: { url: 'https://w.example' } } })
    expect(stdioNotice(['fs', 'py'])).toContain('fs、py')
    expect(stdioNotice(['fs'])).toContain('只支持远程 Streamable HTTP 或 SSE')
    const onlyStdio = parseMcpServersJson('{"mcpServers": {"fs": {"command": "node"}}}')
    expect(onlyStdio.ok && Object.keys(onlyStdio.servers)).toEqual([])
  })
  it('非法输入：JSON 语法、非对象、缺 mcpServers、缺 url、headers 类型、数量上限', () => {
    expect(parseMcpServersJson('{')).toEqual({ ok: false, error: '不是合法的 JSON' })
    expect(parseMcpServersJson('[]')).toMatchObject({ ok: false })
    expect(parseMcpServersJson('{"name": "x"}')).toEqual({ ok: false, error: '缺少 mcpServers' })
    expect(parseMcpServersJson('{"mcpServers": {}}')).toMatchObject({ ok: false })
    expect(parseMcpServersJson('{"mcpServers": {"a": {}, "b": {"url": "u", "headers": "x"}, "c": 1}}')).toMatchObject({
      ok: true,
      invalid: [{ name: 'a', reason: '缺少 url' }, { name: 'b', reason: 'headers 应为对象' }, { name: 'c', reason: '配置应为对象' }],
    })
    const many = Object.fromEntries(Array.from({ length: 21 }, (_, i) => [`s${i}`, { url: 'https://x' }]))
    expect(parseMcpServersJson(JSON.stringify({ mcpServers: many }))).toMatchObject({ ok: false, error: '一次最多导入 20 个服务' })
  })
})

describe('请求头编辑', () => {
  it('未修改（keep）时 PATCH 不带 headers 字段，保持原值', () => {
    expect(headersPatch({ mode: 'keep' })).toEqual({})
    const r = pluginUpdateBody(plugin, values, { mode: 'keep' })
    expect(r).toEqual({ body: {} })
    expect('headers' in (r as { body: object }).body).toBe(false)
  })
  it('只提交变化的字段；重新设置时整体替换，清空为 {}', () => {
    expect(pluginUpdateBody(plugin, { ...values, name: '新词典 ', url: 'https://example.com/v2' }, { mode: 'keep' })).toEqual({ body: { name: '新词典', url: 'https://example.com/v2' } })
    expect(pluginUpdateBody(plugin, values, { mode: 'edit', rows: [headerRow('Authorization', 'Bearer t'), headerRow('X-Key', 'k')] })).toEqual({ body: { headers: { Authorization: 'Bearer t', 'X-Key': 'k' } } })
    expect(pluginUpdateBody(plugin, values, { mode: 'edit', rows: [] })).toEqual({ body: { headers: {} } })
  })
  it('新建时没有请求头就不提交', () => {
    expect(pluginCreateBody({ ...values, slug: '' }, { mode: 'edit', rows: [headerRow()] })).toEqual({ body: { name: '词典', slug: null, transport: 'streamable_http', url: 'https://example.com/mcp' } })
    expect(pluginCreateBody(values, { mode: 'edit', rows: [headerRow('Authorization', 'x')] })).toMatchObject({ body: { headers: { Authorization: 'x' } } })
  })
  it('校验名称、保留头、重复与缺少取值', () => {
    expect(headersFromRows([headerRow('Bad Name', 'x')])).toEqual({ error: '请求头名称「Bad Name」不合法' })
    expect(headersFromRows([headerRow('Content-Type', 'x')])).toMatchObject({ error: expect.stringContaining('由系统管理') })
    expect(headersFromRows([headerRow('A', '1'), headerRow('a', '2')])).toMatchObject({ error: expect.stringContaining('重复') })
    expect(headersFromRows([headerRow('Authorization', '')])).toEqual({ error: '请填写请求头 Authorization 的值' })
    expect(headersFromRows([headerRow('', 'x')])).toEqual({ error: '请求头名称不能为空' })
    expect(pluginUpdateBody(plugin, values, { mode: 'edit', rows: [headerRow('Authorization', '')] })).toMatchObject({ error: expect.any(String) })
  })
})

describe('试用与状态', () => {
  it('参数文本解析、骨架与校验错误', () => {
    expect(parseToolArguments('  ')).toEqual({ args: {} })
    expect(parseToolArguments('{"q": "剑"}')).toEqual({ args: { q: '剑' } })
    expect(parseToolArguments('[1]')).toEqual({ error: '参数应为 JSON 对象' })
    expect(parseToolArguments('{')).toEqual({ error: '参数不是合法的 JSON' })
    expect(JSON.parse(argumentsTemplate({ type: 'object', properties: { q: { type: 'string' }, n: { type: 'integer' }, f: { type: 'boolean' } } }))).toEqual({ q: '', n: 0, f: false })
    expect(callErrors({ errors: [{ field: 'q', message: '必填' }] })).toEqual(['q：必填'])
    expect(callErrors(null)).toEqual([])
  })
  it('插件状态：失败 > 已连接 > 未测试', () => {
    expect(pluginStatus({ last_error: 'x', tools_refreshed_at: '2026-01-01' }).label).toBe('连接失败')
    expect(pluginStatus({ last_error: '', tools_refreshed_at: '2026-01-01' }).label).toBe('已连接')
    expect(pluginStatus({ last_error: '', tools_refreshed_at: null }).label).toBe('未测试')
  })
})
