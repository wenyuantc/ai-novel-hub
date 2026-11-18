import { afterEach, describe, expect, it, vi } from 'vitest'
import { aiUsageApi, type UsageModelPrice } from './aiUsage'
import { setUnauthorizedHandler } from './client'

const session = { id: 42, createdAt: '2026-11-18T02:10:00Z' }
const fetchJson = () => vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
  async () => new Response('null', { headers: { 'Content-Type': 'application/json' } }),
)

afterEach(() => { vi.unstubAllGlobals(); setUnauthorizedHandler(null) })

describe('AI 用量请求', () => {
  it('按个人、章节和全站作用域请求不同的路径，并保留筛选条件', async () => {
    const fetch = fetchJson()
    vi.stubGlobal('fetch', fetch)
    const options = { signal: new AbortController().signal, isCurrent: () => true }
    await aiUsageApi.list({ kind: 'account' }, { project_id: 8, model: '中转/model + one', page: 3 }, options)
    await aiUsageApi.list({ kind: 'project', projectId: 8 }, { project_id: 8, chapter_id: 15, kind: 'chapter.analyze' }, options)
    await aiUsageApi.list({ kind: 'admin' }, { actor_id: 9, payer_id: 5, status: 'failed' }, options)
    const paths = fetch.mock.calls.map(([url]) => new URL(url, 'http://local'))
    expect(paths.map((url) => url.pathname)).toEqual(['/api/v1/ai-usage', '/api/v1/projects/8/chapters/15/ai-usage', '/api/v1/admin/ai-usage'])
    expect(Object.fromEntries(paths[0].searchParams)).toEqual({ project_id: '8', model: '中转/model + one', page: '3' })
    expect(Object.fromEntries(paths[1].searchParams)).toEqual({ kind: 'chapter.analyze' })
    expect(Object.fromEntries(paths[2].searchParams)).toEqual({ actor_id: '9', payer_id: '5', status: 'failed' })
    expect(fetch.mock.calls[0][1]).toMatchObject({ credentials: 'include', signal: options.signal })
  })

  it('设置单价带精确金额、配置版本和真实账号身份，预算清除发送 null', async () => {
    const fetch = fetchJson()
    vi.stubGlobal('fetch', fetch)
    const price: UsageModelPrice = { currency: 'USD', input_per_million: '0.000000001', output_per_million: '2.40', per_image: null }
    await aiUsageApi.setPrice(3, 'v1', price, { editorActor: session })
    await aiUsageApi.setBudget({ kind: 'project', projectId: 8 }, 'CNY', null, { editorActor: session })
    const [[pricePath, priceRequest], [budgetPath, budgetRequest]] = fetch.mock.calls
    expect(pricePath).toBe('/api/v1/ai-configs/3/price')
    expect(JSON.parse(String(priceRequest?.body))).toEqual({ expected_version: 'v1', price })
    expect(budgetPath).toBe('/api/v1/projects/8/ai-usage/budgets/CNY')
    expect(budgetRequest?.body).toBe('null')
    for (const options of [priceRequest, budgetRequest]) expect(options).toMatchObject({ method: 'PUT', headers: {
      'X-Editor-Actor-Id': '42', 'X-Editor-Actor-Created-At': session.createdAt, 'Content-Type': 'application/json',
    } })
  })

  it('分时价保留追加时段、零元命中价和显式清除命中价', async () => {
    const fetch = fetchJson()
    vi.stubGlobal('fetch', fetch)
    const price: UsageModelPrice = {
      currency: 'CNY', input_per_million: '3', output_per_million: '7', per_image: null,
      cached_input_per_million: '0',
      off_peak: {
        timezone: 'Asia/Shanghai', start: '23:00', end: '07:00',
        additional_windows: [{ start: '12:00', end: '14:00' }],
        input_per_million: '1.5', output_per_million: '3.5', per_image: null, cached_input_per_million: null,
      },
    }
    await aiUsageApi.setPrice(3, 'v2', price, { editorActor: session })
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({ expected_version: 'v2', price })
  })

  it('已失效作用域的迟到 401 不清理当前账号', async () => {
    const unauthorized = vi.fn()
    setUnauthorizedHandler(unauthorized)
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 401 })))
    await expect(aiUsageApi.list({ kind: 'admin' }, {}, { isCurrent: () => false })).rejects.toMatchObject({ status: 401 })
    expect(unauthorized).not.toHaveBeenCalled()
  })
})
