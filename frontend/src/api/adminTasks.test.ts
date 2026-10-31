import { afterEach, describe, expect, it, vi } from 'vitest'
import { adminApi } from './admin'
import { adminTasksApi } from './adminTasks'
import { setUnauthorizedHandler } from './client'

const key = '00000000-0000-4000-8000-000000000001'
afterEach(() => { vi.unstubAllGlobals(); setUnauthorizedHandler(null) })
describe('管理员安全任务 API', () => {
  it('组合条件重复status query，沿用Cookie/AbortSignal；详情和取消只按UUID', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return new Response(JSON.stringify({ operation_id: 'operation-1', audit_log_id: 4 }), { status: init.method === 'POST' ? 202 : 200 })
    })
    const abort = new AbortController()
    await adminTasksApi.list({ q: '  %_作品  ', owner_user_id: 2, project_id: 8, task_id: 42, kind: 'agent.turn', status: ['failed', 'interrupted'], from_at: '2026-10-30T00:00:00Z', to_at: '2026-10-31T00:00:00Z', page: 2, page_size: 100, sort: 'running_longest' }, abort.signal)
    const query = new URL(calls[0].url, 'http://localhost').searchParams
    expect(query.getAll('status')).toEqual(['failed', 'interrupted'])
    expect(Object.fromEntries(query)).toMatchObject({ q: '%_作品', owner_user_id: '2', project_id: '8', task_id: '42', kind: 'agent.turn', page: '2', page_size: '100', sort: 'running_longest' })
    expect(query.get('from_at')).toBe('2026-10-30T00:00:00Z')
    await adminTasksApi.get(key, abort.signal)
    await expect(adminTasksApi.cancel(key, { reason: '人工排查', confirm: true }, abort.signal)).resolves.toEqual({ operation_id: 'operation-1', audit_log_id: 4 })
    expect(calls[1].url).toBe(`/api/v1/admin/tasks/${key}`)
    expect(calls[2].url).toBe(`/api/v1/admin/tasks/${key}/cancel`)
    expect(JSON.parse(String(calls[2].init.body))).toEqual({ reason: '人工排查', confirm: true })
    expect(calls.every(({ init }) => init.credentials === 'include' && init.signal === abort.signal)).toBe(true)
    expect(calls.map(({ init }) => init.method)).toEqual(['GET', 'GET', 'POST'])
    expect(calls.some(({ url }) => /\/output|\/stream|\/retry|\/resume|\/finish|\/ai-configs/.test(url))).toBe(false)
  })
  it('空搜索与空状态不发伪筛选，用户选择沿用同一会话与中止请求', async () => {
    const fetch = vi.fn(async () => new Response('{}'))
    vi.stubGlobal('fetch', fetch)
    const abort = new AbortController()
    await adminTasksApi.list({ q: '  ', status: [] }, abort.signal)
    await adminApi.listUsers({ q: '  作者甲 ', page_size: 20 }, abort.signal)
    expect(fetch.mock.calls[0]).toEqual(['/api/v1/admin/tasks', expect.objectContaining({ credentials: 'include', signal: abort.signal })])
    expect(fetch.mock.calls[1]).toEqual(['/api/v1/admin/users?q=%E4%BD%9C%E8%80%85%E7%94%B2&page_size=20', expect.objectContaining({ signal: abort.signal })])
  })
  it.each([401, 403, 404, 409])('HTTP %s保留结构化错误，不伪造成功响应', async (status) => {
    const unauthorized = vi.fn()
    setUnauthorizedHandler(unauthorized)
    vi.stubGlobal('fetch', async () => new Response(JSON.stringify({ detail: '固定安全说明', code: 'task_changed' }), { status }))
    await expect(adminTasksApi.cancel(key, { confirm: true, reason: '排查' })).rejects.toMatchObject({ status, detail: '固定安全说明', body: { code: 'task_changed' } })
    expect(unauthorized).toHaveBeenCalledTimes(status === 401 ? 1 : 0)
  })
  it('中止保持AbortError，网络故障不自动重发POST', async () => {
    const fetch = vi.fn(async () => { throw new TypeError('offline') })
    vi.stubGlobal('fetch', fetch)
    await expect(adminTasksApi.cancel(key, { confirm: true, reason: '排查' })).rejects.toMatchObject({ status: 0 })
    expect(fetch).toHaveBeenCalledTimes(1)
    vi.stubGlobal('fetch', async () => { throw new DOMException('aborted', 'AbortError') })
    await expect(adminTasksApi.get(key)).rejects.toMatchObject({ name: 'AbortError' })
  })
})
