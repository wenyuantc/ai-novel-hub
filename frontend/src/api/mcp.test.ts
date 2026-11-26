import { afterEach, describe, expect, it, vi } from 'vitest'
import { mcpApi } from './mcp'

interface Call {
  url: string
  init: RequestInit
}

function stubFetch(body: unknown): Call[] {
  const calls: Call[] = []
  vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Response(JSON.stringify(body), { status: 200 })
  })
  return calls
}

afterEach(() => vi.unstubAllGlobals())

describe('mcpApi', () => {
  it('本机目录只读，启用请求绑定目录项及部署版本', async () => {
    const calls = stubFetch([])
    await mcpApi.catalog()
    await mcpApi.enableCatalog('dict-local', '1.3')
    expect(calls.map(({ url, init }) => [url, init.method])).toEqual([
      ['/api/v1/mcp-plugins/catalog', 'GET'],
      ['/api/v1/mcp-plugins/catalog/dict-local/enable?catalog_version=1.3', 'POST'],
    ])
    expect(calls[1].init.body).toBeUndefined()
  })

  it('目录刷新绑定所见版本，远程刷新保留原请求路径', async () => {
    const calls = stubFetch({})
    await mcpApi.refresh(7, '2.0+dev')
    await mcpApi.refresh(8, null)
    expect(calls.map((call) => call.url)).toEqual([
      '/api/v1/mcp-plugins/7/refresh?catalog_version=2.0%2Bdev',
      '/api/v1/mcp-plugins/8/refresh',
    ])
  })

  it('工具偏好：工具名放在请求体里，含 / 也不进路径', async () => {
    const calls = stubFetch({})
    await mcpApi.updateTool(3, 'files/read', { auto_confirm: true })
    expect(calls[0].init.method).toBe('PATCH')
    expect(calls[0].url).toBe('/api/v1/mcp-plugins/3/tools')
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ tool: 'files/read', auto_confirm: true })
  })

  it('导入：mcpServers 原样提交，由后端逐项校验', async () => {
    const calls = stubFetch({ items: [] })
    await mcpApi.import({ a: { url: 'https://a.example/mcp' }, b: 'not-an-object' })
    expect(calls[0].url).toBe('/api/v1/mcp-plugins/import')
    expect(JSON.parse(String(calls[0].init.body))).toEqual({
      mcpServers: { a: { url: 'https://a.example/mcp' }, b: 'not-an-object' },
    })
  })
})
